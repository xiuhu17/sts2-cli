using System.Reflection;
using System.Reflection.Emit;
using HarmonyLib;
using MegaCrit.Sts2.Core.Combat;
using MegaCrit.Sts2.Core.Commands;
using MegaCrit.Sts2.Core.Entities.Creatures;
using MegaCrit.Sts2.Core.Entities.Merchant;
using MegaCrit.Sts2.Core.Entities.Players;
using MegaCrit.Sts2.Core.GameActions.Multiplayer;
using MegaCrit.Sts2.Core.Models;
using MegaCrit.Sts2.Core.Models.Powers;
using MegaCrit.Sts2.Core.Models.Relics;
using MegaCrit.Sts2.Core.Runs;
using MegaCrit.Sts2.Core.Saves;
using MegaCrit.Sts2.Core.TestSupport;
using MegaCrit.Sts2.Core.ValueProps;

namespace Sts2Headless;

/// <summary>
/// The headless engine runs with <c>TestMode.IsOn = true</c> (the TestMode-off branches drive the
/// game through Godot nodes and signals that do not exist headless). A handful of TestMode
/// branches are pure game logic, though, and change what the player gets: shop potion prices
/// and Shops-RNG draws, Cauldron's and Calling Bell's rewards (hardcoded test potions/relics),
/// act selection and the discovery-order (tutorial) room modifications. Those methods get their
/// TestMode checks rewritten at the call site to the real game's value (TestMode off), so the
/// game's own code runs unchanged. Branches whose off path runs UI get a hand-written stand-in
/// (Rolling Boulder) or are handled by the adapter (RewardsSet.testSelector, the bundle prefix).
///
/// Every whitelisted method must have at least one TestMode check replaced; otherwise the game
/// changed and the patch would silently do nothing, so <see cref="Failure"/> is set and every
/// start_run / load_save refuses to run.
///
/// STS2_TESTMODE_AUDIT=1 additionally scans every game-logic method for TestMode checks and
/// prints <c>[WARN] unaudited TestMode branch: Type.Method</c> for any not in
/// <see cref="AuditedSites"/> (drift guard for new game builds).
/// </summary>
internal static class ParityPatches
{
    /// <summary>Set when a parity patch could not be applied; runs refuse to start.</summary>
    public static string? Failure { get; private set; }

    private static readonly MethodInfo IsOnGetter = AccessTools.PropertyGetter(typeof(TestMode), nameof(TestMode.IsOn));
    private static readonly MethodInfo IsOffGetter = AccessTools.PropertyGetter(typeof(TestMode), nameof(TestMode.IsOff));

    /// <summary>Methods that must run their TestMode-off branch (all pure logic, no UI).</summary>
    private static IEnumerable<(string name, MethodBase? method)> ForceOffTargets() => new (string, MethodBase?)[]
    {
        // ±5% potion price jitter drawn from PlayerRng.Shops.
        ("MerchantPotionEntry.CalcCost", AccessTools.Method(typeof(MerchantPotionEntry), nameof(MerchantPotionEntry.CalcCost))),
        // 5 random potion rewards, not the fixed test list.
        ("Cauldron.GenerateRewards", AccessTools.Method(typeof(Cauldron), "GenerateRewards")),
        // Common/Uncommon/Rare relic rewards from the grab bag, not Anchor/Gremlin Horn/Mummified Hand.
        ("CallingBell.GenerateRewards", AccessTools.Method(typeof(CallingBell), "GenerateRewards")),
        // Discovery-order boss and first-run Overgrowth room order (keeps the State/GameMode checks).
        ("RunManager.ShouldApplyTutorialModifications", AccessTools.Method(typeof(RunManager), nameof(RunManager.ShouldApplyTutorialModifications))),
        // First-run '?' room override (reads SaveManager.Progress.NumberOfRuns).
        ("RunManager.TryGetRoomTypeForTutorial", AccessTools.Method(typeof(RunManager), "TryGetRoomTypeForTutorial")),
        // An unlocked but never-discovered act is forced (reads SaveManager.Progress.DiscoveredActs).
        ("ActModel.GetRandomList", AccessTools.Method(typeof(ActModel), nameof(ActModel.GetRandomList))),
    };

    public static void Install(Harmony harmony)
    {
        var errors = new List<string>();
        foreach (var (name, method) in ForceOffTargets())
        {
            if (method == null) { errors.Add($"{name}: method not found"); continue; }
            try
            {
                s_replaced = 0;
                harmony.Patch(method, transpiler: new HarmonyMethod(typeof(ParityPatches), nameof(ForceTestModeOff)));
                if (s_replaced == 0) errors.Add($"{name}: no TestMode check found (game changed?)");
                else Console.Error.WriteLine($"[INFO] TestMode off in {name} ({s_replaced} check(s))");
            }
            catch (Exception ex) { errors.Add($"{name}: {ex.Message}"); }
        }

        try
        {
            var boulder = AccessTools.Method(typeof(RollingBoulderPower), nameof(RollingBoulderPower.AfterPlayerTurnStart));
            if (boulder == null || s_flash == null) errors.Add("RollingBoulderPower.AfterPlayerTurnStart/PowerModel.Flash: method not found");
            else harmony.Patch(boulder, prefix: new HarmonyMethod(typeof(ParityPatches), nameof(RollingBoulderPrefix)));
        }
        catch (Exception ex) { errors.Add($"RollingBoulderPower.AfterPlayerTurnStart: {ex.Message}"); }

        if (errors.Count > 0)
        {
            Failure = "TestMode parity patches failed: " + string.Join("; ", errors);
            Console.Error.WriteLine("[ERROR] " + Failure);
        }

        if (Environment.GetEnvironmentVariable("STS2_TESTMODE_AUDIT") == "1")
            AuditTestModeSites(typeof(RunManager).Assembly);
    }

    [ThreadStatic] private static int s_replaced;

    /// <summary>Call-site rewrite: TestMode.IsOn → false, TestMode.IsOff → true.</summary>
    public static IEnumerable<CodeInstruction> ForceTestModeOff(IEnumerable<CodeInstruction> instructions)
    {
        foreach (var ins in instructions)
        {
            if (ins.opcode == OpCodes.Call && ins.operand is MethodInfo m && (m == IsOnGetter || m == IsOffGetter))
            {
                s_replaced++;
                // Keep labels/blocks so branch targets stay valid.
                yield return new CodeInstruction(m == IsOnGetter ? OpCodes.Ldc_I4_0 : OpCodes.Ldc_I4_1)
                    { labels = ins.labels, blocks = ins.blocks };
                continue;
            }
            yield return ins;
        }
    }

    // ── Rolling Boulder ──

    /// <summary>
    /// RollingBoulderPower.AfterPlayerTurnStart, TestMode-off semantics: the boulder VFX emits
    /// HitCreature for each hittable enemy as it rolls past them, left to right, and each hit is
    /// its own single-target Damage. Enemy order (encounter slots) stands in for screen X. The
    /// TestMode branch instead hit every enemy in one multi-target Damage call.
    /// </summary>
    public static bool RollingBoulderPrefix(RollingBoulderPower __instance, PlayerChoiceContext choiceContext,
        Player player, ref Task __result)
    {
        __result = RollingBoulder(__instance, choiceContext, player);
        return false;
    }

    private static readonly MethodInfo? s_flash = AccessTools.Method(typeof(PowerModel), "Flash");

    private static async Task RollingBoulder(RollingBoulderPower power, PlayerChoiceContext ctx, Player player)
    {
        if (player != power.Owner.Player) return;
        s_flash?.Invoke(power, null);
        var amount = power.Amount;  // NRollingBoulderVfx.Create(HittableEnemies, Amount): fixed at launch
        foreach (var c in power.CombatState.HittableEnemies.ToList())
        {
            if (!c.IsHittable) continue;  // the VFX only hits creatures still on screen
            await CreatureCmd.Damage(ctx, new[] { c }, amount, ValueProp.Unpowered, power.Owner);
        }
        power.SetAmount(power.Amount + power.DynamicVars.Damage.IntValue);
    }

    // ── Progress (SaveManager.Instance.Progress) ──

    /// <summary>
    /// What the game reads as the player's progress (act discovery, run count, ancient visits,
    /// Architect dialogue, Wongo points, ...): the loaded profile, or with no profile a progress
    /// consistent with UnlockState.all (every act discovered, not a first run). Never saved.
    /// </summary>
    public static void SetProgress(ProgressState? profile)
    {
        SaveManager.Instance.Progress = profile ?? FullyDiscoveredProgress();
    }

    private static ProgressState FullyDiscoveredProgress()
    {
        var p = ProgressState.CreateDefault();
        foreach (var act in ModelDb.Acts) p.MarkActAsSeen(act.Id);
        // NumberOfRuns = Wins + Losses > 0: not the tutorial run (UnlockState.all has 999999999).
        p.GetOrCreateCharacterStats(ModelDb.Character<MegaCrit.Sts2.Core.Models.Characters.Ironclad>().Id).TotalLosses = 1;
        return p;
    }

    // ── Drift guard ──

    /// <summary>
    /// Methods with a TestMode check that were audited (gameplay ones are patched above or in
    /// RunSimulator; the rest only change presentation, saving, logging or test hooks).
    /// Key: declaring type full name (compiler-generated state machines folded into their
    /// owner) + "." + method name.
    /// </summary>
    private static HashSet<string> AuditedSites()
    {
        var path = Path.Combine(AppContext.BaseDirectory, "testmode-audited.txt");
        var set = new HashSet<string>();
        if (!File.Exists(path)) return set;
        foreach (var line in File.ReadAllLines(path))
        {
            var t = line.Trim();
            if (t.Length > 0 && !t.StartsWith("#")) set.Add(t.Split(' ', 2)[0]);
        }
        return set;
    }

    private static void AuditTestModeSites(Assembly asm)
    {
        var audited = AuditedSites();
        var found = new SortedSet<string>(StringComparer.Ordinal);
        foreach (var type in AccessTools.GetTypesFromAssembly(asm))
        {
            var ns = type.Namespace ?? "";
            if (!ns.StartsWith("MegaCrit.Sts2.Core") || ns.StartsWith("MegaCrit.Sts2.Core.Nodes")
                || ns.StartsWith("MegaCrit.Sts2.Core.TestSupport"))
                continue;
            foreach (var m in type.GetMethods(BindingFlags.Instance | BindingFlags.Static | BindingFlags.Public
                                              | BindingFlags.NonPublic | BindingFlags.DeclaredOnly)
                         .Cast<MethodBase>()
                         .Concat(type.GetConstructors(BindingFlags.Instance | BindingFlags.Static | BindingFlags.Public
                                                      | BindingFlags.NonPublic | BindingFlags.DeclaredOnly)))
            {
                List<KeyValuePair<OpCode, object>> il;
                try
                {
                    if (m.GetMethodBody() == null) continue;
                    il = PatchProcessor.ReadMethodBody(m).ToList();
                }
                catch { continue; }
                if (!il.Any(p => p.Value is MethodInfo mi && mi.DeclaringType == typeof(TestMode)
                                                         && mi.Name is "get_IsOn" or "get_IsOff" or "AssertOn" or "AssertOff"))
                    continue;
                found.Add(SiteKey(type, m));
            }
        }
        int unaudited = 0;
        foreach (var site in found)
            if (!audited.Contains(site)) { Console.Error.WriteLine($"[WARN] unaudited TestMode branch: {site}"); unaudited++; }
        Console.Error.WriteLine($"[INFO] TestMode audit: {found.Count} sites, {unaudited} unaudited");
        var dump = Environment.GetEnvironmentVariable("STS2_TESTMODE_AUDIT_DUMP");
        if (!string.IsNullOrEmpty(dump)) File.WriteAllLines(dump, found);
    }

    /// <summary>Owner type + method; async/iterator/lambda bodies map to the method that owns them.</summary>
    private static string SiteKey(Type type, MethodBase m)
    {
        var t = type;
        var name = m.Name;
        while (t.DeclaringType != null && t.Name.Contains('<'))
        {
            // <Method>d__12 (state machine) or <>c / <>c__DisplayClass (lambdas)
            var n = t.Name;
            var close = n.IndexOf('>');
            if (n.StartsWith("<") && close > 1) name = n.Substring(1, close - 1);
            else if (m.Name.StartsWith("<") && m.Name.IndexOf('>') > 1) name = m.Name.Substring(1, m.Name.IndexOf('>') - 1);
            t = t.DeclaringType;
        }
        if (name.StartsWith("<") && name.IndexOf('>') > 1) name = name.Substring(1, name.IndexOf('>') - 1);
        return $"{t.FullName}.{name}";
    }
}
