using System.Reflection;
using System.Reflection.Emit;
using System.Runtime.CompilerServices;
using HarmonyLib;

namespace Sts2Headless;

/// <summary>
/// Skips real-time animation pauses the game takes with Task.Delay instead of Cmd.Wait (which is
/// already a no-op headless). The only one in run code is Amalgamator's Combine Strikes / Combine
/// Defends, a 300 ms pause between removing the two cards and adding the new one, with nothing
/// but sound and screen shake around it. The await then completes synchronously; the steps
/// after it run in the same order, so the outcome is unchanged and select_cards returns ~300 ms
/// sooner. STS2_KEEP_ANIMATION_DELAYS=1 leaves the delays in place.
/// </summary>
internal static class AnimationDelayPatch
{
    private static readonly MethodInfo? DelayInt =
        typeof(Task).GetMethod(nameof(Task.Delay), new[] { typeof(int) });

    /// <summary>Types whose async methods only use Task.Delay as a visual pause.</summary>
    private static IEnumerable<Type> Targets()
    {
        yield return typeof(MegaCrit.Sts2.Core.Models.Events.Amalgamator);
    }

    public static int Patched;

    public static void Install(Harmony harmony)
    {
        if (Environment.GetEnvironmentVariable("STS2_KEEP_ANIMATION_DELAYS") == "1" || DelayInt == null) return;
        var transpiler = new HarmonyMethod(typeof(AnimationDelayPatch), nameof(Transpiler));
        foreach (var type in Targets())
        {
            foreach (var nested in type.GetNestedTypes(BindingFlags.NonPublic | BindingFlags.Public))
            {
                if (!typeof(IAsyncStateMachine).IsAssignableFrom(nested)) continue;
                var moveNext = nested.GetMethod("MoveNext", BindingFlags.Instance | BindingFlags.NonPublic | BindingFlags.Public);
                if (moveNext == null || !CallsDelay(moveNext)) continue;
                try { harmony.Patch(moveNext, transpiler: transpiler); Patched++; }
                catch (Exception ex) { Console.Error.WriteLine($"[WARN] Animation delay patch {nested.Name}: {ex.Message}"); }
            }
        }
    }

    private static bool CallsDelay(MethodInfo m)
    {
        try
        {
            return PatchProcessor.GetOriginalInstructions(m)
                .Any(i => (i.opcode == OpCodes.Call || i.opcode == OpCodes.Callvirt) && Equals(i.operand, DelayInt));
        }
        catch { return false; }
    }

    private static IEnumerable<CodeInstruction> Transpiler(IEnumerable<CodeInstruction> instructions)
    {
        var skip = AccessTools.Method(typeof(AnimationDelayPatch), nameof(SkipDelay));
        foreach (var ins in instructions)
        {
            if ((ins.opcode == OpCodes.Call || ins.opcode == OpCodes.Callvirt) && Equals(ins.operand, DelayInt))
            {
                ins.opcode = OpCodes.Call;
                ins.operand = skip;
            }
            yield return ins;
        }
    }

    /// <summary>Stands in for Task.Delay(ms): already completed.</summary>
    public static Task SkipDelay(int ms) => Task.CompletedTask;
}
