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
    // Receivers in these namespaces are presentation-only (nodes, VFX, audio, Godot types).
    private static readonly string[] UiNamespaces =
    {
        "MegaCrit.Sts2.Core.Nodes", "MegaCrit.Sts2.addons", "MegaCrit.Sts2.Core.Bindings",
        "MegaCrit.Sts2.Core.Animation", "MegaCrit.Sts2.Core.Audio", "Godot",
    };

    // Callers in these namespaces are game logic and get guarded.
    private static readonly string[] LogicNamespaces =
    {
        "MegaCrit.Sts2.Core.Models", "MegaCrit.Sts2.Core.Commands", "MegaCrit.Sts2.Core.Events",
        "MegaCrit.Sts2.Core.Entities", "MegaCrit.Sts2.Core.Combat", "MegaCrit.Sts2.Core.Rewards",
        "MegaCrit.Sts2.Core.Rooms", "MegaCrit.Sts2.Core.Runs", "MegaCrit.Sts2.Core.Hooks",
        "MegaCrit.Sts2.Core.GameActions", "MegaCrit.Sts2.Core.MonsterMoves", "MegaCrit.Sts2.Core.Helpers",
    };

    private const BindingFlags AllDeclared = BindingFlags.Public | BindingFlags.NonPublic |
        BindingFlags.Instance | BindingFlags.Static | BindingFlags.DeclaredOnly;

    public static void Install(Assembly gameAssembly)
    {
        if (Environment.GetEnvironmentVariable("STS2_NO_UI_GUARD") == "1")
        {
            Console.Error.WriteLine("[INFO] UI guard disabled (STS2_NO_UI_GUARD=1)");
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
        foreach (var m in targets)
        {
            try { harmony.Patch(m, transpiler: transpiler); patched++; }
            catch (Exception ex)
            {
                if (++failed <= 5) Console.Error.WriteLine($"[WARN] UI guard: {m.DeclaringType?.Name}.{m.Name}: {ex.Message}");
            }
        }
        Console.Error.WriteLine($"[INFO] UI guard: {patched} methods patched, {failed} failed " +
                                $"(scan {scanMs}ms, patch {sw.ElapsedMilliseconds - scanMs}ms)");
    }

    private static bool InNamespaces(Type t, string[] namespaces)
    {
        // Nested types (async state machines, lambdas) inherit the declaring type's namespace.
        while (t.DeclaringType != null) t = t.DeclaringType;
        var ns = t.Namespace ?? "";
        return namespaces.Any(n => ns == n || ns.StartsWith(n + "."));
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
