using System.Reflection;
using MegaCrit.Sts2.Core.Combat;
using MegaCrit.Sts2.Core.GameActions.Multiplayer;
using MegaCrit.Sts2.Core.Multiplayer.Game;
using MegaCrit.Sts2.Core.Runs;
using MegaCrit.Sts2.Core.Models;
using MegaCrit.Sts2.Core.Saves;

namespace Sts2Headless;

/// <summary>
/// Housekeeping that keeps one engine process healthy over thousands of runs. None of it
/// changes game behaviour: it only drops state the game itself never reads again.
/// </summary>
public partial class RunSimulator
{
    // ─── Memory: state the game leaves behind when a run is torn down ───

    private static readonly FieldInfo? s_saveStoreField =
        typeof(SaveManager).GetField("_saveStore", BindingFlags.Instance | BindingFlags.NonPublic);

    /// <summary>
    /// TestMode makes SaveManager use MockGodotFileIo, a test double that appends every call,
    /// including the full text of each progress.save write, to its Calls list forever.
    /// Nothing reads Calls; clear it so it can't grow without bound.
    /// </summary>
    internal static void ClearMockSaveStoreLog()
    {
        try
        {
            if (s_saveStoreField?.GetValue(SaveManager.Instance) is MegaCrit.Sts2.Core.Saves.Test.MockGodotFileIo mock
                && mock.Calls.Count > 0)
                mock.Calls.Clear();
        }
        catch { }
    }

    private static readonly MethodInfo? s_netDbOnCombatEnded =
        typeof(NetCombatCardDb).GetMethod("OnCombatEnded", BindingFlags.Instance | BindingFlags.NonPublic);

    /// <summary>
    /// NetCombatCardDb.StartCombat subscribes to every pile and to CombatManager.CombatEnded;
    /// only its OnCombatEnded undoes that, and CombatManager.Reset never raises CombatEnded.
    /// A run abandoned mid-combat would otherwise stay reachable (and add one more handler).
    /// OnCombatEnded is idempotent, so this is safe to call whether or not a combat is live.
    /// </summary>
    private static void ReleaseNetCombatCardDb()
    {
        try { s_netDbOnCombatEnded?.Invoke(NetCombatCardDb.Instance, new object?[] { null }); }
        catch (Exception ex) { Console.Error.WriteLine($"[WARN] NetCombatCardDb release: {ex.Message}"); }
    }

    private static readonly FieldInfo? s_loadCountsField =
        typeof(NetLoadingHandle).GetField("_loadCounts", BindingFlags.Static | BindingFlags.NonPublic);

    /// <summary>
    /// NetLoadingHandle keeps every net service it ever saw as a key of a static dictionary
    /// (the game never calls Release). Drop the idle ones: a missing key and a zero count
    /// behave identically, so this can't change what the next handle does.
    /// </summary>
    private static void ReleaseIdleNetLoadingHandles()
    {
        try
        {
            if (s_loadCountsField?.GetValue(null) is not Dictionary<INetGameService, int> counts || counts.Count == 0) return;
            List<INetGameService>? idle = null;
            foreach (var (svc, n) in counts)
                if (n == 0) (idle ??= new()).Add(svc);
            if (idle != null)
                foreach (var svc in idle) NetLoadingHandle.Release(svc);
        }
        catch (Exception ex) { Console.Error.WriteLine($"[WARN] NetLoadingHandle release: {ex.Message}"); }
    }

    /// <summary>Called at the start of CleanUp, before RunManager tears the run down.</summary>
    private static void ReleaseRunLeftovers()
    {
        ReleaseNetCombatCardDb();
        ClearMockSaveStoreLog();
    }

    /// <summary>Called at the end of CleanUp, once no loading handle of the old run is open.</summary>
    private static void ReleaseRunLeftoversAfterCleanUp()
    {
        ReleaseIdleNetLoadingHandles();
        ClearMockSaveStoreLog();
    }

    /// <summary>One-time engine initialisation, run right after 'ready' so the first start_run doesn't pay for it.</summary>
    internal static void Warmup()
    {
        // Test hook: simulate an initialisation failure (tests/test_perf.py).
        if (Environment.GetEnvironmentVariable("STS2_DEBUG_COMMANDS") == "1"
            && Environment.GetEnvironmentVariable("STS2_DEBUG_FAIL_WARMUP") == "1")
            throw new InvalidOperationException("simulated warm-up failure");
        var sw = System.Diagnostics.Stopwatch.StartNew();
        // The save serializer is independent of the engine set-up: warm it on another core meanwhile.
        double serializerMs = 0;
        var serializer = new Thread(() =>
        {
            var t = System.Diagnostics.Stopwatch.StartNew();
            WarmSaveSerializer();
            serializerMs = t.Elapsed.TotalMilliseconds;
        }) { IsBackground = true, Name = "sts2-warmup-json" };
        serializer.Start();
        EnsureModelDbInitialized();
        var init = sw.Elapsed.TotalMilliseconds;
        serializer.Join();
        Console.Error.WriteLine($"[INFO] Warmup: engine init {init:F0} ms, save serializer {serializerMs:F0} ms (in parallel), total {sw.Elapsed.TotalMilliseconds:F0} ms");
    }

    /// <summary>
    /// First use of the game's save serializer (System.Text.Json source-generated metadata for
    /// SerializableRun) JIT-compiles over a thousand methods, ~30 ms. Every first run of a process
    /// paid it on its first map point: EnterMapPointInternal -> SaveRun, which ContinueSave
    /// captures as JSON. Serializing an empty save here, after 'ready', does it instead.
    /// Only serializer metadata is touched (no game state, no SaveManager). Building the metadata
    /// of every save type as well costs twice the CPU and gains nothing measurable.
    /// </summary>
    private static void WarmSaveSerializer()
    {
        try
        {
            // Writing only: reading a save (load_save, write_continue_save mid-room) is rarer.
            JsonSerializationUtility.ToJson(new SerializableRun());
        }
        catch (Exception ex) { Console.Error.WriteLine($"[INFO] save serializer warm-up: {ex.GetType().Name}: {ex.Message}"); }
    }

    /// <summary>Cheap per-command housekeeping (bounds very long runs and load_save loops).</summary>
    internal static void AfterEveryCommand() => ClearMockSaveStoreLog();
}

public partial class RunSimulator
{
    // ─── Waiting on background game flows ───

    // Set when a flow started by StartFlow has run its synchronous part: it either finished or
    // parked on an await (typically a selection prompt). Waiters re-check their conditions then
    // instead of sleeping out a full 1 ms timeout. Auto-reset: a stale signal only costs one
    // extra loop iteration.
    private static readonly AutoResetEvent s_flowSignal = new(false);

    /// <summary>
    /// Wake a WaitFlow waiter now: a game thread just published a prompt (card reward, card
    /// selection) and will block or park without returning, so StartFlow's own Set never runs.
    /// </summary>
    internal static void SignalFlow() => s_flowSignal.Set();

    /// <summary>Task.Run(() => RunWithHostContext(work)) that signals waiters once the pool thread is done with it.</summary>
    private static Task StartFlow(Func<Task> work) => Task.Run(() =>
    {
        try { return RunWithHostContext(work); }
        finally { s_flowSignal.Set(); }
    });

    /// <summary>StartFlow for a flow with a result (e.g. RewardsSetSynchronizer.SelectLocalReward).</summary>
    private static Task<T> StartFlow<T>(Func<Task<T>> work) => Task.Run(() =>
    {
        var prev = SynchronizationContext.Current;
        SynchronizationContext.SetSynchronizationContext(_syncCtx);
        try { return work(); }
        finally
        {
            SynchronizationContext.SetSynchronizationContext(prev);
            s_flowSignal.Set();
        }
    });

    /// <summary>
    /// One wait step of a "pump until done or prompt" loop: returns when <paramref name="t"/>
    /// completes, a flow parks, or after 1 ms (the previous fixed poll, kept as the fallback for
    /// prompts opened later from an asynchronous continuation).
    /// </summary>
    private static void WaitFlow(Task t) =>
        WaitHandle.WaitAny(new[] { ((IAsyncResult)t).AsyncWaitHandle, s_flowSignal }, 1);
}

public partial class RunSimulator
{
    /// <summary>Test hook (STS2_DEBUG_COMMANDS=1): sizes of the structures the leak fixes keep bounded.</summary>
    internal static Dictionary<string, object?> DebugStats()
    {
        int Count(object? o) => o is System.Collections.ICollection c ? c.Count : -1;
        var combatEnded = typeof(CombatManager).GetField("CombatEnded", BindingFlags.Instance | BindingFlags.NonPublic)
            ?.GetValue(CombatManager.Instance) as Delegate;
        var subs = typeof(NetCombatCardDb).GetField("_subscriptions", BindingFlags.Instance | BindingFlags.NonPublic)
            ?.GetValue(NetCombatCardDb.Instance);
        var store = s_saveStoreField?.GetValue(SaveManager.Instance) as MegaCrit.Sts2.Core.Saves.Test.MockGodotFileIo;
        return new Dictionary<string, object?>
        {
            ["type"] = "debug_stats",
            ["heap_bytes"] = GC.GetTotalMemory(forceFullCollection: true),
            ["combat_ended_handlers"] = combatEnded?.GetInvocationList().Length ?? 0,
            ["net_card_db_subscriptions"] = Count(subs),
            ["net_loading_handles"] = Count(s_loadCountsField?.GetValue(null)),
            ["save_store_calls"] = store?.Calls.Count ?? -1,
            ["map_pruning_verified"] = MapPruningPatch.VerifiedCalls,
        };
    }
}

public partial class RunSimulator
{
    // ─── after_upgrade preview cache ───

    // GetUpgradedInfo clones the canonical card, replays its upgrades plus one, and formats the
    // clone: the result depends only on (language, card id, upgrade level, the live card's
    // keyword sequence). Every decision response lists the whole deck with it, so memoize.
    // Callers only serialize the returned dictionary, so one shared instance is safe.
    private static readonly Dictionary<string, Dictionary<string, object?>> s_upgradeCache = new();

    private static Dictionary<string, object?>? CachedUpgradedInfo(CardModel card, Func<CardModel, Dictionary<string, object?>?> compute)
    {
        string key;
        try
        {
            var sb = new System.Text.StringBuilder(48);
            sb.Append(_loc.Lang).Append('|').Append(card.Id.ToString()).Append('|').Append(card.CurrentUpgradeLevel);
            if (card.Keywords != null)
                foreach (var k in card.Keywords)
                    if (k != MegaCrit.Sts2.Core.Entities.Cards.CardKeyword.None) sb.Append('|').Append((int)k);
            key = sb.ToString();
        }
        catch { return compute(card); }
        lock (s_upgradeCache)
            if (s_upgradeCache.TryGetValue(key, out var hit)) return hit;
        var info = compute(card);
        if (info != null)
            lock (s_upgradeCache)
                if (s_upgradeCache.Count < 20000) s_upgradeCache[key] = info;
        return info;
    }
}
