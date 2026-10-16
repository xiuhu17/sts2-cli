using System.Reflection;
using System.Reflection.Emit;
using HarmonyLib;

namespace Sts2Headless;

/// <summary>
/// Null-guards UI calls inside game logic.
///
/// Game logic (events, powers, monsters, commands, ...) often calls presentation singletons
/// inline, e.g. <c>NGame.Instance.ScreenShakeTrauma(...)</c> or
/// <c>NEventRoom.Instance.Layout.RemoveNodesOnPortrait()</c>. Headless, those singletons are
/// null, so the call throws a NullReferenceException halfway through the logic — Amalgamator
/// removes two Strikes and never adds Ultimate Strike, The Trial's options never resolve, etc.
///
/// This rewrites every such call site: an instance call whose receiver type is a presentation
/// or Godot type becomes
/// <code>receiver == null ? default : receiver.Method(args)</code>
/// (Task-returning methods yield a completed task). Behaviour only changes where the original
/// would have thrown a NullReferenceException on a null UI receiver.
///
/// Set STS2_NO_UI_GUARD=1 to disable.
/// </summary>
internal static class HeadlessUiGuard
{
    private static readonly string[] UiNamespaces = UiGuardSpec.UiNamespaces;
    private static readonly string[] LogicNamespaces = UiGuardSpec.LogicNamespaces;

    private const BindingFlags AllDeclared = BindingFlags.Public | BindingFlags.NonPublic |
        BindingFlags.Instance | BindingFlags.Static | BindingFlags.DeclaredOnly;

    public static void Install(Assembly gameAssembly)
    {
        if (Environment.GetEnvironmentVariable("STS2_NO_UI_GUARD") == "1")
        {
            // The setup-time rewrite is part of the file and can't be switched off here.
            string? stamp = null;
            try { stamp = ReadStamp(gameAssembly.Location); } catch { }
            Console.Error.WriteLine(stamp != null
                ? "[WARN] STS2_NO_UI_GUARD=1 only skips the runtime rewrite; this sts2.dll was guarded by setup.sh (use lib/sts2.dll.original to run without it)"
                : "[INFO] UI guard disabled (STS2_NO_UI_GUARD=1)");
            return;
        }
        if (IsPreApplied(gameAssembly))
        {
            Console.Error.WriteLine("[INFO] UI guard: pre-applied to sts2.dll at setup");
            return;
        }
        var sw = System.Diagnostics.Stopwatch.StartNew();
        var harmony = new Harmony("sts2headless.uiguard");
        var transpiler = new HarmonyMethod(typeof(HeadlessUiGuard).GetMethod(nameof(Transpiler), BindingFlags.Static | BindingFlags.NonPublic));
        int patched = 0, failed = 0;
        var targets = new List<MethodBase>();

        Type[] types;
        try { types = gameAssembly.GetTypes(); }
        catch (ReflectionTypeLoadException e) { types = e.Types.Where(t => t != null).ToArray()!; }

        foreach (var type in types)
        {
            if (!InNamespaces(type, LogicNamespaces) || type.ContainsGenericParameters) continue;
            MethodBase[] methods;
            try { methods = type.GetMethods(AllDeclared).Cast<MethodBase>().Concat(type.GetConstructors(AllDeclared)).ToArray(); }
            catch { continue; }
            foreach (var m in methods)
            {
                if (m.IsAbstract || m.ContainsGenericParameters || !CallsUiInstanceMethod(m)) continue;
                targets.Add(m);
            }
        }
        var scanMs = sw.ElapsedMilliseconds;
        var dumpPath = Environment.GetEnvironmentVariable("STS2_UI_GUARD_DUMP");
        var dump = dumpPath != null ? new List<string>() : null;
        foreach (var m in targets)
        {
            s_sites = 0;
            try
            {
                harmony.Patch(m, transpiler: transpiler); patched++;
                if (dump != null && s_sites > 0) dump.Add($"{m.MetadataToken:X8}\t{s_sites}\t{m.DeclaringType?.FullName}::{m.Name}");
            }
            catch (Exception ex)
            {
                if (++failed <= 5) Console.Error.WriteLine($"[WARN] UI guard: {m.DeclaringType?.Name}.{m.Name}: {ex.Message}");
            }
        }
        if (dump != null) File.WriteAllLines(dumpPath!, dump.OrderBy(x => x, StringComparer.Ordinal));
        Console.Error.WriteLine($"[INFO] UI guard: {patched} methods patched, {failed} failed " +
                                $"(scan {scanMs}ms, patch {sw.ElapsedMilliseconds - scanMs}ms)");
    }

    /// <summary>
    /// setup.sh rewrites lib/sts2.dll with the same guard (tools/UiGuardPatcher) and stamps it;
    /// then there is nothing left to do at runtime. A stale or missing stamp (lib copied from an
    /// unpatched game, or the spec changed since setup) falls back to the Harmony rewrite.
    /// STS2_UI_GUARD_RUNTIME=1 forces the Harmony path (used to compare the two).
    /// </summary>
    private static bool IsPreApplied(Assembly gameAssembly)
    {
        if (Environment.GetEnvironmentVariable("STS2_UI_GUARD_RUNTIME") == "1")
        {
            try
            {
                if (ReadStamp(gameAssembly.Location) != null)
                    Console.Error.WriteLine("[WARN] STS2_UI_GUARD_RUNTIME=1 on an sts2.dll already guarded by setup.sh: call sites get guarded twice (harmless, but dumps/timings aren't comparable)");
            }
            catch { }
            return false;
        }
        string? stamp;
        try { stamp = ReadStamp(gameAssembly.Location); }
        catch (Exception ex)
        {
            Console.Error.WriteLine($"[WARN] UI guard: cannot read the sts2.dll stamp: {ex.Message}");
            return false;
        }
        if (stamp == null) return false;
        if (stamp == UiGuardSpec.Stamp) return true;
        Console.Error.WriteLine("[INFO] UI guard: sts2.dll was guarded with another spec; rerun setup.sh. Using the runtime rewrite");
        return false;
    }

    /// <summary>
    /// The value of [assembly: AssemblyMetadata(UiGuardSpec.MetadataKey, value)], read from the
    /// file's metadata (reflection's GetCustomAttributesData would instantiate every attribute
    /// type, and some of sts2.dll's Godot attributes don't exist in the stub).
    /// </summary>
    internal static string? ReadStamp(string path)
    {
        if (string.IsNullOrEmpty(path) || !File.Exists(path)) return null;
        using var fs = File.OpenRead(path);
        using var pe = new System.Reflection.PortableExecutable.PEReader(fs);
        var md = System.Reflection.Metadata.PEReaderExtensions.GetMetadataReader(pe);
        foreach (var h in md.GetAssemblyDefinition().GetCustomAttributes())
        {
            var ca = md.GetCustomAttribute(h);
            if (ca.Constructor.Kind != System.Reflection.Metadata.HandleKind.MemberReference) continue;
            var ctor = md.GetMemberReference((System.Reflection.Metadata.MemberReferenceHandle)ca.Constructor);
            if (ctor.Parent.Kind != System.Reflection.Metadata.HandleKind.TypeReference) continue;
            var type = md.GetTypeReference((System.Reflection.Metadata.TypeReferenceHandle)ctor.Parent);
            if (md.GetString(type.Name) != "AssemblyMetadataAttribute" || md.GetString(type.Namespace) != "System.Reflection") continue;
            var blob = md.GetBlobReader(ca.Value);
            if (blob.ReadUInt16() != 1) continue;  // prolog
            var key = blob.ReadSerializedString();
            if (key == UiGuardSpec.MetadataKey) return blob.ReadSerializedString();
        }
        return null;
    }

    private static bool InNamespaces(Type t, string[] namespaces)
    {
        // Nested types (async state machines, lambdas) inherit the declaring type's namespace.
        while (t.DeclaringType != null) t = t.DeclaringType;
        return UiGuardSpec.InNamespaces(t.Namespace, namespaces);
    }

    private static bool IsGuardable(MethodBase callee)
    {
        if (callee.IsStatic || callee.IsConstructor || callee.DeclaringType == null) return false;
        if (callee.DeclaringType.IsValueType || callee.ContainsGenericParameters) return false;
        if (!InNamespaces(callee.DeclaringType, UiNamespaces)) return false;
        var mi = callee as MethodInfo;
        if (mi != null && (mi.ReturnType.IsByRef || mi.ReturnType.ContainsGenericParameters)) return false;
        return callee.GetParameters().All(p => !p.ParameterType.IsByRef && !p.ParameterType.ContainsGenericParameters);
    }

    // Cheap pre-filter on raw IL so only methods that need it go through Harmony.
    private static bool CallsUiInstanceMethod(MethodBase m)
    {
        byte[]? il;
        try { il = m.GetMethodBody()?.GetILAsByteArray(); } catch { return false; }
        if (il == null) return false;
        for (int i = 0; i + 4 < il.Length; i++)
        {
            if (il[i] != 0x28 && il[i] != 0x6F) continue;  // call / callvirt
            var token = BitConverter.ToInt32(il, i + 1);
            if ((token >> 24) != 0x0A && (token >> 24) != 0x06 && (token >> 24) != 0x2B) continue;  // MemberRef/MethodDef/MethodSpec
            try
            {
                var callee = m.Module.ResolveMethod(token,
                    m.DeclaringType?.IsGenericType == true ? m.DeclaringType.GetGenericArguments() : null,
                    m.IsGenericMethod ? m.GetGenericArguments() : null);
                if (callee != null && IsGuardable(callee)) return true;
            }
            catch { }
        }
        return false;
    }

    [ThreadStatic] private static int s_sites;  // guarded call sites in the method being patched (dump only)

    private static IEnumerable<CodeInstruction> Transpiler(IEnumerable<CodeInstruction> instructions, ILGenerator il)
    {
        var list = instructions.ToList();
        for (int i = 0; i < list.Count; i++)
        {
            var ins = list[i];
            if ((ins.opcode != OpCodes.Call && ins.opcode != OpCodes.Callvirt) || ins.operand is not MethodBase callee
                || !IsGuardable(callee))
            {
                yield return ins;
                continue;
            }
            // A 'constrained.' prefix needs the original call shape; leave those alone.
            if (i > 0 && list[i - 1].opcode == OpCodes.Constrained)
            {
                yield return ins;
                continue;
            }

            s_sites++;
            var ps = callee.GetParameters();
            var argLocals = ps.Select(p => il.DeclareLocal(p.ParameterType)).ToArray();
            var self = il.DeclareLocal(callee.DeclaringType!);
            var isNull = il.DefineLabel();
            var end = il.DefineLabel();

            // Pop args (last first) and the receiver into locals, keeping the call's labels/blocks.
            var first = true;
            for (int a = argLocals.Length - 1; a >= -1; a--)
            {
                var st = new CodeInstruction(OpCodes.Stloc, a >= 0 ? argLocals[a] : self);
                if (first) { st.labels.AddRange(ins.labels); st.blocks.AddRange(ins.blocks); first = false; }
                yield return st;
            }
            yield return new CodeInstruction(OpCodes.Ldloc, self);
            yield return new CodeInstruction(OpCodes.Brfalse, isNull);
            yield return new CodeInstruction(OpCodes.Ldloc, self);
            foreach (var l in argLocals) yield return new CodeInstruction(OpCodes.Ldloc, l);
            yield return new CodeInstruction(ins.opcode, callee);
            yield return new CodeInstruction(OpCodes.Br, end);

            var nullBranch = DefaultValue((callee as MethodInfo)?.ReturnType ?? typeof(void), il).ToList();
            if (nullBranch.Count == 0) nullBranch.Add(new CodeInstruction(OpCodes.Nop));
            nullBranch[0].labels.Add(isNull);
            foreach (var n in nullBranch) yield return n;

            yield return new CodeInstruction(OpCodes.Nop) { labels = { end } };
        }
    }

    private static readonly MethodInfo CompletedTask =
        typeof(Task).GetProperty(nameof(Task.CompletedTask))!.GetGetMethod()!;
    private static readonly MethodInfo FromResult =
        typeof(Task).GetMethod(nameof(Task.FromResult))!;

    private static IEnumerable<CodeInstruction> DefaultValue(Type type, ILGenerator il)
    {
        if (type == typeof(void)) yield break;
        if (type == typeof(Task))
        {
            yield return new CodeInstruction(OpCodes.Call, CompletedTask);
            yield break;
        }
        if (type.IsGenericType && type.GetGenericTypeDefinition() == typeof(Task<>))
        {
            var arg = type.GetGenericArguments()[0];
            foreach (var c in DefaultValue(arg, il)) yield return c;
            yield return new CodeInstruction(OpCodes.Call, FromResult.MakeGenericMethod(arg));
            yield break;
        }
        if (!type.IsValueType)
        {
            yield return new CodeInstruction(OpCodes.Ldnull);
            yield break;
        }
        var tmp = il.DeclareLocal(type);
        yield return new CodeInstruction(OpCodes.Ldloca, tmp);
        yield return new CodeInstruction(OpCodes.Initobj, type);
        yield return new CodeInstruction(OpCodes.Ldloc, tmp);
    }
}
