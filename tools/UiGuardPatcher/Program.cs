// UiGuardPatcher <sts2.dll> [--search <dir>]... [--dump <file>]
//
// The same rewrite HeadlessUiGuard does with Harmony at runtime, applied to the file: in every
// game-logic method (UiGuardSpec.LogicNamespaces), an instance call on a presentation type
// (UiGuardSpec.UiNamespaces) becomes `receiver == null ? default : receiver.Method(args)`.
// The assembly is stamped with [AssemblyMetadata(UiGuardSpec.MetadataKey, UiGuardSpec.Stamp)],
// which tells the engine the guard is already in place.
using Mono.Cecil;
using Mono.Cecil.Cil;
using Mono.Cecil.Rocks;
using Sts2Headless;

var dllPath = Path.GetFullPath(args[0]);
string? dumpPath = null;
var resolver = new DefaultAssemblyResolver();
var searchDirs = new List<string> { Path.GetDirectoryName(dllPath)! };
for (int i = 1; i < args.Length; i++)
{
    if (args[i] == "--search" && i + 1 < args.Length) searchDirs.Add(Path.GetFullPath(args[++i]));
    else if (args[i] == "--dump" && i + 1 < args.Length) dumpPath = args[++i];
}
foreach (var d in searchDirs) resolver.AddSearchDirectory(d);
// The runtime's own framework directory (System.Runtime and friends).
resolver.AddSearchDirectory(Path.GetDirectoryName(typeof(object).Assembly.Location)!);

ModuleDefinition module;
try { module = ModuleDefinition.ReadModule(dllPath, new ReaderParameters { AssemblyResolver = resolver, ReadingMode = ReadingMode.Immediate }); }
catch (AssemblyResolutionException ex)
{
    Console.WriteLine($"UI guard: cannot resolve {ex.AssemblyReference.Name}; pass its folder with --search (GodotSharp: src/GodotStubs/bin/<config>/net9.0)");
    return 4;
}
var existing = module.Assembly.CustomAttributes.FirstOrDefault(a =>
    a.AttributeType.FullName == "System.Reflection.AssemblyMetadataAttribute"
    && a.ConstructorArguments.Count == 2 && a.ConstructorArguments[0].Value as string == UiGuardSpec.MetadataKey);
if (existing != null)
{
    var same = existing.ConstructorArguments[1].Value as string == UiGuardSpec.Stamp;
    Console.WriteLine(same ? "UI guard: already applied" : "UI guard: sts2.dll was guarded with a different spec; start from the game's sts2.dll");
    return same ? 0 : 2;
}

var guard = new Guard(module);
var dump = new List<string>();
int methods = 0, sites = 0, skipped = 0;
foreach (var type in AllTypes(module))
{
    if (!UiGuardSpec.InNamespaces(Outermost(type).Namespace, UiGuardSpec.LogicNamespaces) || type.HasGenericParameters) continue;
    foreach (var m in type.Methods)
    {
        if (m.IsAbstract || !m.HasBody || m.HasGenericParameters) continue;
        var token = m.MetadataToken.ToUInt32();
        var n = guard.Rewrite(m, out var unresolvable);
        if (unresolvable) { skipped++; continue; }
        if (n == 0) continue;
        methods++; sites += n;
        dump.Add($"{token:X8}\t{n}\t{type.FullName.Replace('/', '+')}::{m.Name}");
    }
}

// Stamp: [assembly: AssemblyMetadata(key, stamp)]
var attrType = module.ImportReference(typeof(System.Reflection.AssemblyMetadataAttribute)).Resolve();
var ctor = module.ImportReference(attrType.Methods.First(c => c.IsConstructor && c.Parameters.Count == 2));
ctor = Guard.Retarget(module, ctor, "System.Reflection.AssemblyMetadataAttribute");
var stamp = new CustomAttribute(ctor);
stamp.ConstructorArguments.Add(new CustomAttributeArgument(module.TypeSystem.String, UiGuardSpec.MetadataKey));
stamp.ConstructorArguments.Add(new CustomAttributeArgument(module.TypeSystem.String, UiGuardSpec.Stamp));
module.Assembly.CustomAttributes.Add(stamp);

if (dumpPath != null) File.WriteAllLines(dumpPath, dump.OrderBy(x => x, StringComparer.Ordinal));
var outPath = dllPath + ".guarded";
module.Write(outPath);
module.Dispose();

// Have the JIT compile every rewritten method before replacing the file. An invalid body would
// otherwise only surface (as InvalidProgramException) when the game first calls it.
var failures = Verify.JitRewritten(outPath, searchDirs, dump.Select(d => d.Split('\t')[2]).ToList());
if (failures.Count > 0)
{
    foreach (var f in failures.Take(10)) Console.WriteLine($"  JIT check failed: {f}");
    File.Delete(outPath);
    Console.WriteLine($"UI guard: {failures.Count} rewritten methods don't compile; sts2.dll left unchanged (the engine applies the guard at runtime)");
    return 3;
}
File.Move(outPath, dllPath, overwrite: true);
Console.WriteLine($"UI guard: {methods} methods, {sites} call sites guarded ({skipped} methods skipped: they reference members missing from GodotSharp)");
return 0;

static IEnumerable<TypeDefinition> AllTypes(ModuleDefinition module)
{
    var stack = new Stack<TypeDefinition>(module.Types);
    while (stack.Count > 0)
    {
        var t = stack.Pop();
        yield return t;
        foreach (var n in t.NestedTypes) stack.Push(n);
    }
}

static TypeReference Outermost(TypeReference t)
{
    while (t.DeclaringType != null) t = t.DeclaringType;
    return t;
}

sealed class Guard
{
    private readonly ModuleDefinition _module;
    private readonly TypeReference _task;
    private readonly MethodReference _completedTask;
    private readonly MethodReference _fromResult;

    public Guard(ModuleDefinition module)
    {
        _module = module;
        // Reference Task through the scope sts2.dll already uses (System.Runtime), not the
        // patcher's System.Private.CoreLib.
        _task = module.GetTypeReferences().FirstOrDefault(t => t.FullName == "System.Threading.Tasks.Task")
                ?? module.ImportReference(typeof(Task));
        _completedTask = new MethodReference("get_CompletedTask", _task, _task) { HasThis = false };
        _fromResult = new MethodReference("FromResult", module.TypeSystem.Void, _task) { HasThis = false };
        var t = new GenericParameter("TResult", _fromResult);
        _fromResult.GenericParameters.Add(t);
        var taskOfT = _module.GetTypeReferences().FirstOrDefault(r => r.FullName == "System.Threading.Tasks.Task`1")
                      ?? module.ImportReference(typeof(Task<>));
        var ret = new GenericInstanceType(taskOfT);
        ret.GenericArguments.Add(t);
        _fromResult.ReturnType = ret;
        _fromResult.Parameters.Add(new ParameterDefinition(t));
    }

    /// <summary>Re-point a reference imported from the patcher's runtime at the scope sts2.dll uses for that type.</summary>
    public static MethodReference Retarget(ModuleDefinition module, MethodReference m, string typeName)
    {
        var scoped = module.GetTypeReferences().FirstOrDefault(t => t.FullName == typeName);
        if (scoped == null)
        {
            var runtime = module.AssemblyReferences.FirstOrDefault(a => a.Name == "System.Runtime");
            if (runtime == null) return m;
            scoped = new TypeReference("System.Reflection", "AssemblyMetadataAttribute", module, runtime);
        }
        var r = new MethodReference(m.Name, module.TypeSystem.Void, scoped) { HasThis = true };
        r.Parameters.Add(new ParameterDefinition(module.TypeSystem.String));
        r.Parameters.Add(new ParameterDefinition(module.TypeSystem.String));
        return r;
    }

    private sealed record Callee(MethodReference Ref, TypeReference Receiver, TypeReference[] Params, TypeReference Return);

    /// <summary>Guard every UI instance call in <paramref name="m"/>; returns the number of call sites rewritten.</summary>
    public int Rewrite(MethodDefinition m, out bool unresolvable)
    {
        unresolvable = false;
        var body = m.Body;
        var sites = new List<(Instruction ins, Callee callee)>();
        Instruction? prev = null;
        foreach (var ins in body.Instructions)
        {
            // The runtime (Harmony) rewrite can't read a method that references a member the
            // GodotSharp stub lacks; such methods fail to JIT anyway. Leave them alone too.
            if (ins.Operand is MemberReference mr && !Resolves(mr)) { unresolvable = true; return 0; }
            if ((ins.OpCode == OpCodes.Call || ins.OpCode == OpCodes.Callvirt) && ins.Operand is MethodReference mref
                && prev?.OpCode != OpCodes.Constrained && Guardable(mref) is { } callee)
                sites.Add((ins, callee));
            prev = ins;
        }
        if (sites.Count == 0) return 0;

        body.SimplifyMacros();
        var il = body.GetILProcessor();
        foreach (var (call, callee) in sites)
        {
            var argLocals = callee.Params.Select(p => AddLocal(body, p)).ToArray();
            var self = AddLocal(body, callee.Receiver);
            var seq = new List<Instruction>();
            for (int a = argLocals.Length - 1; a >= 0; a--) seq.Add(il.Create(OpCodes.Stloc, argLocals[a]));
            seq.Add(il.Create(OpCodes.Stloc, self));
            var end = il.Create(OpCodes.Nop);
            var nullBranch = DefaultValue(body, callee.Return);
            if (nullBranch.Count == 0) nullBranch.Add(il.Create(OpCodes.Nop));
            seq.Add(il.Create(OpCodes.Ldloc, self));
            seq.Add(il.Create(OpCodes.Brfalse, nullBranch[0]));
            seq.Add(il.Create(OpCodes.Ldloc, self));
            foreach (var l in argLocals) seq.Add(il.Create(OpCodes.Ldloc, l));
            seq.Add(il.Create(call.OpCode, callee.Ref));
            seq.Add(il.Create(OpCodes.Br, end));
            seq.AddRange(nullBranch);
            seq.Add(end);

            // Turn the call instruction itself into the first store, so every branch and
            // exception-handler boundary that pointed at the call now points at the sequence.
            call.OpCode = seq[0].OpCode;
            call.Operand = seq[0].Operand;
            var after = call;
            for (int i = 1; i < seq.Count; i++)
            {
                il.InsertAfter(after, seq[i]);
                after = seq[i];
            }
        }
        body.InitLocals = true;
        body.OptimizeMacros();
        return sites.Count;
    }

    private static VariableDefinition AddLocal(MethodBody body, TypeReference type)
    {
        var v = new VariableDefinition(type);
        body.Variables.Add(v);
        return v;
    }

    private List<Instruction> DefaultValue(MethodBody body, TypeReference type)
    {
        var list = new List<Instruction>();
        if (type.MetadataType == MetadataType.Void) return list;
        if (type.FullName == "System.Threading.Tasks.Task")
        {
            list.Add(Instruction.Create(OpCodes.Call, _completedTask));
            return list;
        }
        if (type is GenericInstanceType git && git.ElementType.FullName == "System.Threading.Tasks.Task`1")
        {
            var arg = git.GenericArguments[0];
            list.AddRange(DefaultValue(body, arg));
            var fr = new GenericInstanceMethod(_fromResult);
            fr.GenericArguments.Add(arg);
            list.Add(Instruction.Create(OpCodes.Call, fr));
            return list;
        }
        if (!IsValueType(type))
        {
            list.Add(Instruction.Create(OpCodes.Ldnull));
            return list;
        }
        var tmp = AddLocal(body, type);
        list.Add(Instruction.Create(OpCodes.Ldloca, tmp));
        list.Add(Instruction.Create(OpCodes.Initobj, type));
        list.Add(Instruction.Create(OpCodes.Ldloc, tmp));
        return list;
    }

    private static bool IsValueType(TypeReference t)
    {
        if (t.IsByReference || t.IsPointer || t.IsArray) return false;
        if (t.IsPrimitive) return true;
        var d = t.Resolve();
        return d?.IsValueType ?? t.IsValueType;
    }

    private static bool Resolves(MemberReference mr)
    {
        try
        {
            return mr switch
            {
                MethodReference m => m.Resolve() != null,
                FieldReference f => f.Resolve() != null,
                TypeReference t => t is GenericParameter || t.Resolve() != null,
                _ => true,
            };
        }
        catch { return false; }
    }

    /// <summary>Mirror of HeadlessUiGuard.IsGuardable on the reflection view of the callee.</summary>
    private Callee? Guardable(MethodReference mref)
    {
        MethodDefinition? def;
        try { def = mref.Resolve(); } catch { return null; }
        if (def == null || def.IsStatic || def.IsConstructor || def.DeclaringType == null) return null;
        if (def.DeclaringType.IsValueType) return null;
        if (!UiGuardSpec.InNamespaces(Outermost(def.DeclaringType).Namespace, UiGuardSpec.UiNamespaces)) return null;

        var ret = Substitute(mref.ReturnType, mref);
        if (ret.IsByReference || ContainsGenericParameter(ret)) return null;
        var ps = mref.Parameters.Select(p => Substitute(p.ParameterType, mref)).ToArray();
        if (ps.Any(p => p.IsByReference || ContainsGenericParameter(p))) return null;
        var receiver = mref.DeclaringType;
        if (ContainsGenericParameter(receiver)) return null;
        return new Callee(mref, receiver, ps, ret);
    }

    private static TypeReference Outermost(TypeReference t)
    {
        while (t.DeclaringType != null) t = t.DeclaringType;
        return t;
    }

    /// <summary>Close a signature type over the generic arguments of the call's declaring type and method.</summary>
    private static TypeReference Substitute(TypeReference t, MethodReference context)
    {
        switch (t)
        {
            // Reflection's ParameterType/ReturnType drop custom modifiers (e.g. the
            // modreq(IsExternalInit) on an init accessor's void return); so do we.
            case IModifierType mod:
                return Substitute(mod.ElementType, context);
            case GenericParameter gp when gp.Type == GenericParameterType.Type
                                          && context.DeclaringType is GenericInstanceType git && gp.Position < git.GenericArguments.Count:
                return git.GenericArguments[gp.Position];
            case GenericParameter gp when gp.Type == GenericParameterType.Method
                                          && context is GenericInstanceMethod gim && gp.Position < gim.GenericArguments.Count:
                return gim.GenericArguments[gp.Position];
            case ByReferenceType br:
                return new ByReferenceType(Substitute(br.ElementType, context));
            case ArrayType at:
                return new ArrayType(Substitute(at.ElementType, context), at.Rank);
            case GenericInstanceType gi:
            {
                var r = new GenericInstanceType(gi.ElementType);
                foreach (var a in gi.GenericArguments) r.GenericArguments.Add(Substitute(a, context));
                return r;
            }
            default:
                return t;
        }
    }

    private static bool ContainsGenericParameter(TypeReference t) => t switch
    {
        GenericParameter => true,
        TypeSpecification ts when ts is GenericInstanceType gi => gi.GenericArguments.Any(ContainsGenericParameter),
        TypeSpecification ts => ContainsGenericParameter(ts.ElementType),
        _ => t.HasGenericParameters,  // an open generic definition used as a type
    };
}

static class Verify
{
    public static List<string> JitRewritten(string dll, List<string> searchDirs, List<string> methods)
    {
        var failures = new List<string>();
        var alc = new System.Runtime.Loader.AssemblyLoadContext("uiguard-verify", isCollectible: true);
        alc.Resolving += (ctx, name) =>
        {
            foreach (var d in searchDirs)
            {
                var p = Path.Combine(d, name.Name + ".dll");
                if (File.Exists(p)) return ctx.LoadFromStream(new MemoryStream(File.ReadAllBytes(p)));
            }
            return null;
        };
        try
        {
            var asm = alc.LoadFromStream(new MemoryStream(File.ReadAllBytes(dll)));
            const System.Reflection.BindingFlags all = System.Reflection.BindingFlags.Public | System.Reflection.BindingFlags.NonPublic
                | System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.Static | System.Reflection.BindingFlags.DeclaredOnly;
            foreach (var full in methods.Distinct())
            {
                var sep = full.LastIndexOf("::", StringComparison.Ordinal);
                var type = asm.GetType(full[..sep]);
                var name = full[(sep + 2)..];
                if (type == null) { failures.Add($"{full}: type not found"); continue; }
                var ms = type.GetMethods(all).Cast<System.Reflection.MethodBase>().Concat(type.GetConstructors(all)).Where(m => m.Name == name);
                foreach (var m in ms)
                {
                    try { System.Runtime.CompilerServices.RuntimeHelpers.PrepareMethod(m.MethodHandle); }
                    catch (Exception ex) { failures.Add($"{full}: {ex.GetType().Name}: {ex.Message}"); }
                }
            }
        }
        catch (Exception ex) { failures.Add($"load: {ex.Message}"); }
        finally { alc.Unload(); }
        return failures;
    }
}
