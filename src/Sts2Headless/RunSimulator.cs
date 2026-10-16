using System.Reflection;
using MegaCrit.Sts2.Core.Combat;
using MegaCrit.Sts2.Core.Commands;
using MegaCrit.Sts2.Core.Context;
using MegaCrit.Sts2.Core.Events;
using MegaCrit.Sts2.Core.Entities.Ascension;
using MegaCrit.Sts2.Core.Entities.Cards;
using MegaCrit.Sts2.Core.Entities.Creatures;
using MegaCrit.Sts2.Core.Entities.Players;
using MegaCrit.Sts2.Core.GameActions;
using MegaCrit.Sts2.Core.Helpers;
using MegaCrit.Sts2.Core.Map;
using MegaCrit.Sts2.Core.Models;
using MegaCrit.Sts2.Core.Models.Characters;
using MegaCrit.Sts2.Core.Multiplayer;
using MegaCrit.Sts2.Core.CardSelection;
using MegaCrit.Sts2.Core.Entities.CardRewardAlternatives;
using MegaCrit.Sts2.Core.Entities.Merchant;
using MegaCrit.Sts2.Core.Entities.RestSite;
using MegaCrit.Sts2.Core.Rewards;
using MegaCrit.Sts2.Core.Rooms;
using MegaCrit.Sts2.Core.Models.Powers;
using MegaCrit.Sts2.Core.GameActions.Multiplayer;
using MegaCrit.Sts2.Core.Random;
using MegaCrit.Sts2.Core.Runs;
using MegaCrit.Sts2.Core.TestSupport;
using HarmonyLib;
using MegaCrit.Sts2.Core.Localization;
using MegaCrit.Sts2.Core.Multiplayer.Serialization;
using MegaCrit.Sts2.Core.Saves;
using MegaCrit.Sts2.Core.Saves.Validation;
using MegaCrit.Sts2.Core.Unlocks;

namespace Sts2Headless;

/// <summary>
/// Synchronization context that executes continuations inline immediately.
/// Task.Yield() posts to SynchronizationContext.Current — by executing inline,
/// the yield becomes a no-op and the entire async chain runs synchronously.
/// Uses a recursion guard to queue nested posts and drain them after.
/// </summary>
internal class InlineSynchronizationContext : SynchronizationContext
{
    // Post runs callbacks inline on the posting thread, so the recursion guard and the
    // nested-post queue belong to that thread. (Shared fields raced when a background
    // flow posted while the main thread was pumping.)
    [ThreadStatic] private static Queue<(SendOrPostCallback, object?)>? t_queue;
    [ThreadStatic] private static bool t_executing;
    private static Queue<(SendOrPostCallback, object?)> Queue => t_queue ??= new();

    public override void Post(SendOrPostCallback d, object? state)
    {
        if (t_executing)
        {
            Queue.Enqueue((d, state));
            return;
        }

        // Execute inline immediately, then drain any nested posts. The context is current
        // while the callback runs, so async methods it starts on a pool thread capture it too.
        var prev = Current;
        SetSynchronizationContext(this);
        t_executing = true;
        try
        {
            d(state);
            // Drain any callbacks that were queued during execution
            var q = Queue;
            while (q.Count > 0)
            {
                var (cb, st) = q.Dequeue();
                cb(st);
            }
        }
        finally
        {
            t_executing = false;
            SetSynchronizationContext(prev);
        }
    }

    public override void Send(SendOrPostCallback d, object? state)
    {
        d(state);
    }

    public void Pump()
    {
        // Drain any remaining queued callbacks (left behind if a callback threw)
        var q = Queue;
        while (q.Count > 0)
        {
            var (cb, st) = q.Dequeue();
            t_executing = true;
            try { cb(st); }
            finally { t_executing = false; }
        }
    }
}

/// <summary>
/// Bilingual localization lookup — loads eng/zhs JSON files for display names.
/// </summary>
internal class LocLookup
{
    private readonly Dictionary<string, Dictionary<string, string>> _eng = new();
    private readonly Dictionary<string, Dictionary<string, string>> _zhs = new();

    public LocLookup()
    {
        Load(DataDirs.Localization("localization_eng"), _eng);
        Load(DataDirs.Localization("localization_zhs"), _zhs);
    }

    private static void Load(string dir, Dictionary<string, Dictionary<string, string>> target)
    {
        if (!Directory.Exists(dir)) return;
        foreach (var file in Directory.GetFiles(dir, "*.json"))
        {
            try
            {
                var name = Path.GetFileNameWithoutExtension(file);
                var data = System.Text.Json.JsonSerializer.Deserialize<Dictionary<string, string>>(File.ReadAllText(file));
                if (data != null) target[name] = data;
            }
            catch { }
        }
    }

    /// <summary>Get bilingual name: "English / 中文" or just the key if not found.</summary>
    public string Name(string table, string key)
    {
        var en = _eng.GetValueOrDefault(table)?.GetValueOrDefault(key);
        var zh = _zhs.GetValueOrDefault(table)?.GetValueOrDefault(key);
        if (en != null && zh != null && en != zh) return $"{en} / {zh}";
        return en ?? zh ?? key;
    }

    public bool HasTable(string table) => _eng.ContainsKey(table) || _zhs.ContainsKey(table);

    public string? En(string table, string key) => _eng.GetValueOrDefault(table)?.GetValueOrDefault(key);

    /// <summary>Unformatted entry in the output language (falls back to English), or null.</summary>
    public string? Raw(string table, string key) =>
        (Lang == "zh" ? _zhs.GetValueOrDefault(table)?.GetValueOrDefault(key) : null)
        ?? _eng.GetValueOrDefault(table)?.GetValueOrDefault(key);

    public static string Clean(string text) => StripBBCode(ImagesToText(text));

    /// <summary>
    /// Inline icons the game renders as images ("[img]res://.../ironclad_energy_icon.png[/img]")
    /// become text: a run of N energy icons -> "N Energy" / "N点能量", star icons -> "★".
    /// EnergyIconsFormatter draws 1-3 energy as that many icons, but 4+ as "4[icon]", and
    /// templates use a single icon as a unit after a number or quantity word ("costs 0 [icon]",
    /// "2 additional [icon]", "per [icon]"). Then the icon is just the word "Energy"; adding its
    /// count would turn "4[icon]" into "41 Energy".
    /// </summary>
    private static readonly System.Text.RegularExpressions.Regex EnergyUnitBeforeEn = new(
        @"(?:\d|\(|\b(?:additional|less|more|enough|unspent|per|each))(?:\[/[a-z_]+\])*\s?$",
        System.Text.RegularExpressions.RegexOptions.Compiled);
    private static readonly System.Text.RegularExpressions.Regex EnergyUnitBeforeZh = new(
        @"(?:\d|点|的|个|多|（)(?:\[/[a-z_]+\])*\s?$", System.Text.RegularExpressions.RegexOptions.Compiled);

    private static string ImagesToText(string text)
    {
        if (!text.Contains("[img")) return text;
        text = System.Text.RegularExpressions.Regex.Replace(text,
            @"(?:\[img[^\]]*\][^\[]*energy_icon[^\[]*\[/img\]\s*)+", m =>
            {
                var n = System.Text.RegularExpressions.Regex.Matches(m.Value, @"\[/img\]").Count;
                var trail = m.Value.EndsWith(" ") ? " " : "";
                var before = text.Substring(Math.Max(0, m.Index - 40), Math.Min(40, m.Index));
                if (n == 1 && (_lang == "zh" ? EnergyUnitBeforeZh : EnergyUnitBeforeEn).IsMatch(before))
                {
                    if (_lang == "zh") return (char.IsDigit(StripBBCode(before).TrimEnd().LastOrDefault()) ? "点能量" : "能量") + trail;
                    return (before.Length == 0 || char.IsWhiteSpace(before[^1]) ? "Energy" : " Energy") + trail;
                }
                return (_lang == "zh" ? $"{n}点能量" : $"{n} Energy") + trail;
            });
        text = System.Text.RegularExpressions.Regex.Replace(text, @"\[img[^\]]*\][^\[]*star[^\[]*\[/img\]", "★");
        return System.Text.RegularExpressions.Regex.Replace(text, @"\[img[^\]]*\][^\[]*\[/img\]", "");
    }

    // Static mirror of Lang for the static helpers above.
    private static string _lang = "en";

    /// <summary>Replace icon placeholders the game draws as images with text.</summary>
    public string ReplaceIcons(string text)
    {
        if (!text.Contains('{')) return text;
        text = text.Replace("{singleStarIcon}", "★");
        if (!text.Contains("energyIcons(")) return text;
        // Same rendering as the game's formatter, then the same icon -> text rules.
        text = System.Text.RegularExpressions.Regex.Replace(text, @"\{energyPrefix:energyIcons\((\d+)\)\}", m =>
        {
            var n = int.Parse(m.Groups[1].Value);
            const string icon = "[img]res://images/packed/sprite_fonts/colorless_energy_icon.png[/img]";
            return n > 0 && n < 4 ? string.Concat(Enumerable.Repeat(icon, n)) : n + icon;
        });
        return ImagesToText(text);
    }

    private static readonly string[] TipTables =
        { "card_keywords", "static_hover_tips", "powers", "intents", "orbs", "enchantments", "afflictions" };

    /// <summary>Resolve a bare key like "EXHAUST.title" that came back unformatted.</summary>
    public string? ResolveKey(string text)
    {
        if (!System.Text.RegularExpressions.Regex.IsMatch(text, @"^[A-Z0-9_]+\.[a-zA-Z_.]+$")) return null;
        foreach (var t in TipTables)
            if (Raw(t, text) is string hit) return hit;
        return null;
    }
    /// <summary>The table's keys starting with <paramref name="prefix"/>, in key order (LocTable.GetLocStringsWithPrefix).</summary>
    public List<string> KeysWithPrefix(string table, string prefix) =>
        (_eng.GetValueOrDefault(table)?.Keys ?? Enumerable.Empty<string>())
            .Where(k => k.StartsWith(prefix, StringComparison.Ordinal)).OrderBy(k => k, StringComparer.Ordinal).ToList();
    public string? Zh(string table, string key) => _zhs.GetValueOrDefault(table)?.GetValueOrDefault(key);

    /// <summary>Strip BBCode tags like [gold], [/blue], [b], [sine], etc.</summary>
    private static string StripBBCode(string text)
    {
        return System.Text.RegularExpressions.Regex.Replace(text, @"\[/?[a-zA-Z_][a-zA-Z0-9_]*(?:[= ][^\]]*)?\]", "");
    }

    /// <summary>Language for JSON output: "en" or "zh". Default: "en".</summary>
    public string Lang
    {
        get => _lang;
        set
        {
            if (value != _lang) ClearGameTextCaches();
            _lang = value;
        }
    }

    /// <summary>
    /// The game caches some formatted hover tips process-wide (HoverTipFactory keyword and potion
    /// tips). A reused process that switches language would keep serving the old language's text.
    /// </summary>
    private static void ClearGameTextCaches()
    {
        try
        {
            foreach (var f in typeof(MegaCrit.Sts2.Core.HoverTips.HoverTipFactory).GetFields(
                         System.Reflection.BindingFlags.Static | System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Public))
                if (f.GetValue(null) is System.Collections.IDictionary d) d.Clear();
        }
        catch (Exception ex) { Console.Error.WriteLine($"[WARN] Clear hover tip caches: {ex.Message}"); }
    }

    /// <summary>Return localized string for JSON output based on Lang setting.</summary>
    public string Bilingual(string table, string key)
    {
        if (Lang == "zh")
        {
            var zh = _zhs.GetValueOrDefault(table)?.GetValueOrDefault(key);
            if (zh != null) return StripBBCode(zh);
        }
        var en = _eng.GetValueOrDefault(table)?.GetValueOrDefault(key) ?? key;
        return StripBBCode(en);
    }

    // Convenience helpers using ModelId
    public string Card(string entry) => Bilingual("cards", entry + ".title");
    public string Monster(string entry)
    {
        var result = MonsterRaw(entry);
        // Test Subject's title is "Test Subject #C{Count}" with Count = TestSubjectKills + 8.
        if (result.Contains("{Count}"))
        {
            int kills = 0;
            try { kills = MegaCrit.Sts2.Core.Saves.SaveManager.Instance.Progress.TestSubjectKills; } catch { }
            result = result.Replace("{Count}", (kills + 8).ToString());
        }
        return result;
    }

    private string MonsterRaw(string entry)
    {
        var key = entry + ".name";
        var result = Bilingual("monsters", key);
        // If no dedicated entry, fall back to the base segment key (e.g. DECIMILLIPEDE_SEGMENT_FRONT → DECIMILLIPEDE_SEGMENT)
        if (result == key)
        {
            var lastUnderscore = entry.LastIndexOf('_');
            if (lastUnderscore > 0)
            {
                var baseEntry = entry[..lastUnderscore];
                var baseKey = baseEntry + ".name";
                var baseResult = Bilingual("monsters", baseKey);
                if (baseResult != baseKey) return baseResult;
            }
        }
        return result;
    }
    public string Relic(string entry) => Bilingual("relics", entry + ".title");
    public string Potion(string entry) => Bilingual("potions", entry + ".title");
    public string Power(string entry) => Bilingual("powers", entry + ".title");
    public string Event(string entry) => Bilingual("events", entry + ".title");
    public string Act(string entry) => Bilingual("acts", entry + ".title");

    /// <summary>Resolve a full loc key like "TABLE.KEY.SUB" by searching all tables.</summary>
    public string BilingualFromKey(string locKey)
    {
        if (Lang == "zh")
        {
            foreach (var tableName in _zhs.Keys)
            {
                var zh = _zhs.GetValueOrDefault(tableName)?.GetValueOrDefault(locKey);
                if (zh != null) return zh;
            }
        }
        foreach (var tableName in _eng.Keys)
        {
            var en = _eng.GetValueOrDefault(tableName)?.GetValueOrDefault(locKey);
            if (en != null) return en;
        }
        return locKey;
    }

    public bool IsLoaded => _eng.Count > 0;
}

/// <summary>
/// Full run simulator — manages the game lifecycle from character selection
/// through map navigation, combat, events, rest sites, shops, and act transitions.
/// Drives the engine forward until it hits a "decision point" requiring external input.
/// </summary>
public partial class RunSimulator
{
    private static int? _expectedSaveSchemaVersion;
    private static bool _expectedSaveSchemaVersionReady;
    private static readonly object _expectedSaveSchemaVersionLock = new();

    private RunState? _runState;
    private static bool _modelDbInitialized;
    private static readonly InlineSynchronizationContext _syncCtx = new();
    // The thread that reads commands; only it can answer a prompt.
    private static int MainThreadId = -1;
    private readonly ManualResetEventSlim _turnStarted = new(false);
    private readonly ManualResetEventSlim _combatEnded = new(false);
    private static readonly LocLookup _loc = new();
    private bool _eventOptionChosen;
    private int _lastEventOptionCount;

    // Pending rewards for card selection (populated after combat, before proceeding)
    private List<Reward>? _pendingRewards;
    private bool _rewardsProcessed;
    // Proceed was pressed on the post-combat (terminal) rewards screen.
    private bool _rewardsProceeded;
    private int _goldBeforeCombat;
    private int _lastKnownHp;
    private readonly HeadlessCardSelector _cardSelector = new();
    // Pending bundle selection (Scroll Boxes: pick 1 of N packs)
    private IReadOnlyList<IReadOnlyList<CardModel>>? _pendingBundles;
    private TaskCompletionSource<IEnumerable<CardModel>>? _pendingBundleTcs;

    public const int MaxSeedLength = 64;
    private const int MaxEcho = 64;

    /// <summary>A client-supplied name as echoed in an error: at most 64 characters.</summary>
    internal static string Clip(string? s) =>
        s == null ? "" : s.Length <= MaxEcho ? s : s[..MaxEcho] + $"... ({s.Length} chars)";

    public Dictionary<string, object?> StartRun(string character, int ascension = 0, string? seed = null, string lang = "en",
        bool manualRewards = false, IReadOnlyList<string>? modifiers = null, string? gameMode = null,
        string? profilePath = null)
    {
        try
        {
            // Validate before tearing down the current run, so a bad request leaves it intact.
            if (!KnownCharacters.Contains(character.ToLowerInvariant()))
                return Error($"Unknown character: {Clip(character)} (Ironclad|Silent|Defect|Regent|Necrobinder)", "unknown_character");
            // The game's seeds are 12 characters (SeedHelper.seedDefaultLength); a seed is
            // echoed in every decision's context, so an absurd one is refused.
            if (seed != null && seed.Trim().Length > MaxSeedLength)
                return Error($"'seed' must be at most {MaxSeedLength} characters, got {seed.Trim().Length}", "bad_request");
            EnsureModelDbInitialized();
            var mode = (gameMode ?? (modifiers?.Count > 0 ? "custom" : "standard")).ToLowerInvariant() switch
            {
                "standard" => (GameMode?)GameMode.Standard,
                "custom" => GameMode.Custom,
                "daily" => GameMode.Daily,
                _ => null,
            };
            if (mode == null) return Error($"Unknown game_mode '{Clip(gameMode)}' (standard|custom|daily)", "bad_request");
            var modifierModels = new List<ModifierModel>();
            foreach (var name in modifiers ?? Array.Empty<string>())
            {
                var m = ResolveModifier(name);
                if (m == null)
                    return Error(name.Trim().ToUpperInvariant().StartsWith("CHARACTER_CARDS")
                        ? $"Modifier '{Clip(name)}' needs a character: CHARACTER_CARDS:<character>. Known: {string.Join(", ", KnownModifiers())}"
                        : $"Unknown modifier '{Clip(name)}'. Known: {string.Join(", ", KnownModifiers())}", "bad_request");
                modifierModels.Add(m);
            }
            ProgressState? progress = null;
            if (profilePath != null)
            {
                try { progress = LoadProgress(profilePath); }
                catch (Exception ex) { return Error($"Cannot read profile '{profilePath}': {ex.Message}", "io_error"); }
            }
            var unlocks = progress != null ? new UnlockState(progress) : UnlockState.all;
            if (ParityPatches.Failure != null) return Error(ParityPatches.Failure);
            if (RewardPatches.Failure != null) return Error(RewardPatches.Failure);
            if (!ResetForNewRun())
                return RestartRequired();
            // The game reads the profile's progress during the run (act discovery, first-run
            // rooms, ancient visits, ...); without a profile, one consistent with UnlockState.all.
            ParityPatches.SetProgress(progress);
            _loc.Lang = lang;
            _ = manualRewards;  // accepted but ignored: rewards are always a decision (NRewardsScreen)

            var player = CreatePlayer(character, unlocks);
            if (player == null)
                return Error($"Unknown character: {character}", "unknown_character");

            // Same seed handling as the game's lobby: typed seeds are canonicalized, and the acts
            // are drawn from the seed ("act_selection" stream), so a seed reproduces the real game's run.
            // A blank seed box is no seed (NCustomRunScreen.OnSeedInputSubmitted: "" -> SetSeed(null)),
            // and StartRunLobby then draws a random one; it is reported in context.seed.
            var seedStr = !string.IsNullOrWhiteSpace(seed) ? SeedHelper.CanonicalizeSeed(seed) : SeedHelper.GetRandomSeed();
            Log($"Creating RunState with seed={seedStr}");

            _runState = RunState.CreateForNewRun(
                new[] { player },
                // StartRunLobby.BeginRunLocally: the acts come from the seed's "act_selection"
                // stream; an unlocked, never-discovered act is forced (TestMode-off branch).
                ActModel.GetRandomList(new Rng(StringHelper.GetDeterministicHashCode(seedStr), "act_selection"), unlocks, isMultiplayer: false)
                    .Select(a => a.ToMutable()).ToList(),
                modifierModels,
                mode.Value,
                ascension,
                seedStr
            );

            // Set up RunManager with test mode
            var netService = new NetSingleplayerGameService();
            RunManager.Instance.SetUpTest(_runState, netService);
            LocalContext.NetId = netService.NetId;

            // StartedWithNeow comes from SetUpTest's SetStartedWithNeowFlag, as in the game: true
            // when the profile has revealed the Neow epoch (always with UnlockState.all).

            // Generate rooms for all acts
            RunManager.Instance.GenerateRooms();
            Log("Rooms generated");

            // Launch the run
            RunManager.Instance.Launch();
            Log("Run launched");

            // Register event handlers for combat turn transitions
            AttachCombatHandlers();

            // Finalize starting relics
            RunManager.Instance.FinalizeStartingRelics().GetAwaiter().GetResult();
            Log("Starting relics finalized");

            // Enter first act (generates map)
            RunManager.Instance.EnterAct(0, doTransition: false).GetAwaiter().GetResult();
            Log("Entered Act 0");

            // Register card selector for cards that need player choice
            AttachCardSelector();

            // Now we should be at the map — detect decision point
            return DetectDecisionPoint();
        }
        catch (Exception ex)
        {
            return ErrorWithTrace("StartRun failed", ex);
        }
    }

    // ─── Test/Debug commands ───

    private static readonly System.Reflection.BindingFlags NonPublic =
        System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.NonPublic;

    /// <summary>Get the backing List&lt;T&gt; behind an IReadOnlyList property via reflection.</summary>
    private static List<T>? GetBackingList<T>(object obj, string fieldName)
    {
        var field = obj.GetType().GetField(fieldName, NonPublic);
        return field?.GetValue(obj) as List<T>;
    }

    private static void SetField(object obj, string fieldName, object? value)
    {
        var field = obj.GetType().GetField(fieldName, NonPublic);
        field?.SetValue(obj, value);
    }

    public Dictionary<string, object?> SetPlayer(Dictionary<string, System.Text.Json.JsonElement> args)
    {
        try
        {
            if (_runState == null) return Error("No run in progress");
            var player = _runState.Players[0];

            if (args.TryGetValue("hp", out var hpEl) && player.Creature != null)
                SetField(player.Creature, "_currentHp", hpEl.GetInt32());
            if (args.TryGetValue("max_hp", out var mhpEl) && player.Creature != null)
                SetField(player.Creature, "_maxHp", mhpEl.GetInt32());
            if (args.TryGetValue("gold", out var goldEl))
                player.Gold = goldEl.GetInt32();

            if (args.TryGetValue("relics", out var relicsEl))
            {
                var list = GetBackingList<RelicModel>(player, "_relics");
                if (list != null)
                {
                    list.Clear();
                    foreach (var rEl in relicsEl.EnumerateArray())
                    {
                        var id = rEl.GetString();
                        if (id == null) continue;
                        var model = ModelDb.GetById<RelicModel>(new ModelId("RELIC", id));
                        // Owned like a loaded relic (AddRelicInternal sets the Owner), so owner-checked
                        // hooks (ShouldAllowFreeTravel, Miniature Tent) apply.
                        if (model != null) player.AddRelicInternal(model.ToMutable(), -1, silent: true);
                    }
                }
            }
            if (args.TryGetValue("deck", out var deckEl))
            {
                // Remove existing cards from RunState tracking
                foreach (var c in player.Deck.Cards.ToList())
                    _runState.RemoveCard(c);
                player.Deck.Clear(silent: true);
                // Add new cards via RunState.CreateCard (sets Owner + registers)
                foreach (var cEl in deckEl.EnumerateArray())
                {
                    var id = cEl.GetString();
                    if (id == null) continue;
                    // "BASH+" adds an upgraded copy.
                    var plus = id.EndsWith("+");
                    if (plus) id = id[..^1];
                    var canonical = ModelDb.GetById<CardModel>(new ModelId("CARD", id));
                    if (canonical != null)
                    {
                        var card = _runState.CreateCard(canonical, player);
                        if (plus && card.IsUpgradable) { card.UpgradeInternal(); card.FinalizeUpgradeInternal(); }
                        player.Deck.AddInternal(card, silent: true);
                    }
                }
            }
            if (args.TryGetValue("potions", out var potionsEl))
            {
                var slots = GetBackingList<PotionModel>(player, "_potionSlots")
                         ?? GetBackingList<PotionModel?>(player, "_potionSlots") as System.Collections.IList;
                if (slots != null)
                {
                    for (int i = 0; i < slots.Count; i++) slots[i] = null;
                    int idx = 0;
                    foreach (var pEl in potionsEl.EnumerateArray())
                    {
                        if (idx >= slots.Count) break;
                        var id = pEl.GetString();
                        if (id != null)
                        {
                            var model = ModelDb.GetById<PotionModel>(new ModelId("POTION", id));
                            // Inject a mutable instance (not the canonical model — that throws
                            // CanonicalModelException when the game reads potion.Owner) and set its
                            // Owner, or UsePotionAction fails with "without an owner!".
                            if (model != null)
                            {
                                var mutable = model.ToMutable();
                                mutable.Owner = player;
                                slots[idx] = mutable;
                            }
                        }
                        idx++;
                    }
                }
            }

            Log($"SetPlayer: hp={player.Creature?.CurrentHp} gold={player.Gold} relics={player.Relics.Count} deck={player.Deck?.Cards?.Count}");
            if (args.TryGetValue("obtain_relic", out var obtainEl))
            {
                // Obtain like a real pickup (RelicCmd.Obtain: AfterObtained runs, so Cauldron,
                // Calling Bell, ... offer their rewards). Returns the resulting decision.
                var id = obtainEl.GetString() ?? "";
                var model = ModelDb.GetById<RelicModel>(new ModelId("RELIC", id.ToUpperInvariant()));
                if (model == null) return Error($"Unknown relic: {id}");
                RunInBackground(() => RelicCmd.Obtain(model.ToMutable(), player));
                return DetectDecisionPoint();
            }
            return new Dictionary<string, object?>
            {
                ["type"] = "ok",
                ["player"] = PlayerSummary(player),
            };
        }
        catch (Exception ex) { return ErrorWithTrace("SetPlayer failed", ex); }
    }

    public Dictionary<string, object?> EnterRoom(string roomType, string? encounter, string? eventId, int? act = null)
    {
        try
        {
            if (_runState == null) return Error("No run in progress");
            var runState = _runState;
            // Debug "act" (1-based): move to that act first (RunManager.EnterAct), e.g. to fight
            // the last act's boss.
            if (act != null)
            {
                if (act < 1 || act > runState.Acts.Count) return Error($"act must be 1..{runState.Acts.Count}");
                if (act - 1 != runState.CurrentActIndex)
                {
                    RunManager.Instance.EnterAct(act.Value - 1, doTransition: false).GetAwaiter().GetResult();
                    _syncCtx.Pump();
                    WaitForActionExecutor();
                }
            }
            Log($"EnterRoom: type={roomType} encounter={encounter} event={eventId}");

            AbstractRoom room;
            switch (roomType.ToLowerInvariant())
            {
                case "combat":
                case "monster":
                case "elite":
                {
                    if (string.IsNullOrEmpty(encounter))
                        encounter = "SHRINKER_BEETLE_WEAK"; // default encounter
                    var encModel = ModelDb.GetById<EncounterModel>(new ModelId("ENCOUNTER", encounter));
                    if (encModel == null) return Error($"Unknown encounter: {encounter}");
                    room = new CombatRoom(encModel.ToMutable(), runState);
                    break;
                }
                case "shop":
                    room = new MerchantRoom();
                    break;
                case "rest":
                case "rest_site":
                    room = new RestSiteRoom();
                    break;
                case "event":
                {
                    if (string.IsNullOrEmpty(eventId))
                        return Error("event requires 'event' parameter (e.g. CHANGELING_GROVE)");
                    var evModel = ModelDb.GetById<EventModel>(new ModelId("EVENT", eventId));
                    if (evModel == null) return Error($"Unknown event: {eventId}");
                    room = new EventRoom(evModel);
                    break;
                }
                case "treasure":
                    room = new TreasureRoom(_runState.CurrentActIndex);
                    break;
                default:
                    return Error($"Unknown room type: {roomType}");
            }

            ResetPerRoomState(runState.Players[0]);
            RunManager.Instance.EnterRoom(room).GetAwaiter().GetResult();
            _syncCtx.Pump();
            WaitForActionExecutor();
            return DetectDecisionPoint();
        }
        catch (Exception ex) { return ErrorWithTrace("EnterRoom failed", ex); }
    }

    public Dictionary<string, object?> SetDrawOrder(List<string> cardIds)
    {
        try
        {
            if (_runState == null) return Error("No run in progress");
            var player = _runState.Players[0];
            var pcs = player.PlayerCombatState;
            if (pcs?.DrawPile == null) return Error("Not in combat");

            var drawList = GetBackingList<CardModel>(pcs.DrawPile, "_cards");
            if (drawList == null) return Error("Cannot access draw pile");

            var newOrder = new List<CardModel>();
            var available = new List<CardModel>(drawList);
            foreach (var cardId in cardIds)
            {
                var match = available.FirstOrDefault(c =>
                    c.Id.Entry.Equals(cardId, StringComparison.OrdinalIgnoreCase));
                if (match != null)
                {
                    newOrder.Add(match);
                    available.Remove(match);
                }
            }
            newOrder.AddRange(available);

            drawList.Clear();
            drawList.AddRange(newOrder);

            Log($"SetDrawOrder: {newOrder.Count} cards, top={newOrder.FirstOrDefault()?.Id.Entry}");
            return new Dictionary<string, object?>
            {
                ["type"] = "ok",
                ["draw_pile_count"] = drawList.Count,
                ["top_cards"] = newOrder.Take(5).Select(c => _loc.Card(c.Id.Entry)).ToList(),
            };
        }
        catch (Exception ex) { return ErrorWithTrace("SetDrawOrder failed", ex); }
    }

    // ─── Game actions ───
    public Dictionary<string, object?> LoadSave(string saveJson, string lang = "en", bool manualRewards = false,
        string? profilePath = null)
    {
        try
        {
            EnsureModelDbInitialized();
            if (ParityPatches.Failure != null) return Error(ParityPatches.Failure);
            if (RewardPatches.Failure != null) return Error(RewardPatches.Failure);
            ProgressState? progress = null;
            if (profilePath != null)
            {
                try { progress = LoadProgress(profilePath); }
                catch (Exception ex) { return Error($"Cannot read profile '{profilePath}': {ex.Message}", "io_error"); }
            }

            Log("Loading save file...");

            if (!ValidateSaveSchemaVersion(saveJson, out var schemaError))
                return Error($"Save schema mismatch: {schemaError}");

            var readResult = SaveManager.FromJson<SerializableRun>(saveJson);
            if (!readResult.Success || readResult.SaveData == null)
                return Error($"Failed to parse save file: {readResult.Status} {readResult.ErrorMessage}");

            var save = readResult.SaveData;
            Log($"Save loaded: seed={save.SerializableRng?.Seed}, act={save.CurrentActIndex}, ascension={save.Ascension}");

            // Only tear down the current run once the save is known to be usable: build a
            // throwaway RunState from a second parse first (FromSerializable shares the save's
            // lists with the run it builds), so a save the game can't load (unknown character
            // or card id, missing players, ...) is refused while the current run carries on.
            try
            {
                var probe = SaveManager.FromJson<SerializableRun>(saveJson).SaveData;
                if (probe == null || RunState.FromSerializable(probe) == null)
                    return Error("Save is not loadable: the game could not build a run from it", "bad_request");
            }
            catch (Exception ex)
            {
                return Error($"Save is not loadable: {ex.GetType().Name}: {Clip(ex.Message)}", "bad_request");
            }
            if (!ResetForNewRun())
                return RestartRequired();
            _loc.Lang = lang;
            _ = manualRewards;  // accepted but ignored: rewards are always a decision (NRewardsScreen)
            ParityPatches.SetProgress(progress);

            _runState = RunState.FromSerializable(save);
            if (_runState == null)
                return Error("Failed to create RunState from save");

            Log($"RunState created, players={_runState.Players?.Count}");

            var netService = new NetSingleplayerGameService();
            RunManager.Instance.SetUpSavedSingleplayer(_runState, save).GetAwaiter().GetResult();
            LocalContext.NetId = netService.NetId;

            AttachCombatHandlers();
            AttachCardSelector();

            var savedRoom = _runState.CurrentRoom;

            // Save visited coords before Launch (EnterAct will clear them)
            var savedVisitedCoords = _runState.VisitedMapCoords?.ToList() ?? new List<MapCoord>();
            var shouldResumeInitialNeow = IsInitialNeowSave(saveJson);
            Log($"Save has {savedVisitedCoords.Count} visited coords");

            RunManager.Instance.Launch();
            Log("Run launched");
            // RunState.FromSerializable shares the save's inner per-act history lists with the
            // live run, so re-entering the node would grow `save` too. Keep what is on disk, as
            // JSON (the game's Continue does not save again: saveGame=false).
            ContinueSave.Capture(saveJson);

            if (ContinueSave.ShouldReenterLatestCoord(save))
            {
                // The game's Continue (NGame.LoadRun): rebuild the saved map and re-enter the
                // last visited node, so the same room and encounter come back.
                Log("Re-entering the latest map coord (continue save)");
                RunManager.Instance.GenerateMap().GetAwaiter().GetResult();
                _syncCtx.Pump();
                var preFinished = ContinueSave.IsReloadableRoom(save.PreFinishedRoom)
                    ? AbstractRoom.FromSerializable(save.PreFinishedRoom, _runState) : null;
                _lastKnownHp = _runState.Players[0].Creature?.CurrentHp ?? 0;
                ContinueSave.SuppressRoomEndRewards = true;
                try
                {
                    RunManager.Instance.LoadIntoLatestMapCoord(preFinished).GetAwaiter().GetResult();
                    _syncCtx.Pump();
                    WaitForActionExecutor();
                }
                finally { ContinueSave.SuppressRoomEndRewards = false; }
                ContinueSave.Capture(saveJson);
            }
            else if (savedRoom is MapRoom || savedRoom == null)
            {
                // Preserve Neow for saves created before the first blessing choice.
                // Once the run has visited at least one map node, re-entering Act 1
                // should not send the player back through the Ancient start node.
                if (_runState.CurrentActIndex == 0 && savedVisitedCoords.Count > 0)
                    _runState.ExtraFields.StartedWithNeow = false;
                RunManager.Instance.EnterAct(_runState.CurrentActIndex, doTransition: false).GetAwaiter().GetResult();
                _syncCtx.Pump();
                Log($"Entered Act {_runState.CurrentActIndex}");

                if (shouldResumeInitialNeow && _runState.Map?.StartingMapPoint != null)
                {
                    Log("Restoring initial Neow event");
                    RunManager.Instance.EnterMapCoord(_runState.Map.StartingMapPoint.coord).GetAwaiter().GetResult();
                    _syncCtx.Pump();
                }

                // EnterAct clears visited coords and ActFloor — restore them from save
                if (savedVisitedCoords.Count > 0)
                {
                    if (_runState.VisitedMapCoords == null || _runState.VisitedMapCoords.Count == 0)
                    {
                        foreach (var coord in savedVisitedCoords)
                            _runState.AddVisitedMapCoord(coord);
                    }
                    _runState.ActFloor = savedVisitedCoords.Count;
                    var last = savedVisitedCoords[^1];
                    Log($"Restored map position: floor={_runState.ActFloor}, coord=({last.col},{last.row})");
                }
            }
            else
            {
                Log($"Preserving saved room: {savedRoom.GetType().Name}");
            }

            return DetectDecisionPoint();
        }
        catch (Exception ex)
        {
            return ErrorWithTrace("LoadSave failed", ex);
        }
    }

    /// <summary>
    /// Expected run save <c>schema_version</c> (lazy: first load_save only, so StartRun never fails on reflection).
    /// Order: <c>STS2_SAVE_SCHEMA_VERSION</c> env → reflect sts2.dll → unknown, defer to SaveManager.
    /// </summary>
    private static int? GetExpectedSaveSchemaVersion()
    {
        if (_expectedSaveSchemaVersionReady)
            return _expectedSaveSchemaVersion;
        lock (_expectedSaveSchemaVersionLock)
        {
            if (_expectedSaveSchemaVersionReady)
                return _expectedSaveSchemaVersion;
            _expectedSaveSchemaVersion = ResolveExpectedSaveSchemaVersion();
            _expectedSaveSchemaVersionReady = true;
            return _expectedSaveSchemaVersion;
        }
    }

    private static int? ResolveExpectedSaveSchemaVersion()
    {
        var env = Environment.GetEnvironmentVariable("STS2_SAVE_SCHEMA_VERSION");
        if (!string.IsNullOrWhiteSpace(env) && int.TryParse(env.Trim(), out var envVer))
            return envVer;

        var reflected = TryReflectLatestSaveSchemaVersion();
        if (reflected.HasValue)
            return reflected.Value;

        Console.Error.WriteLine(
            "[Sts2Headless] Could not read save schema from sts2.dll; deferring schema compatibility " +
            "to SaveManager.FromJson. Set STS2_SAVE_SCHEMA_VERSION to enforce a specific version.");
        return null;
    }

    /// <summary>Find static parameterless GetLatestSchemaVersion (or close) on sts2; supports int/uint/long.</summary>
    private static int? TryReflectLatestSaveSchemaVersion()
    {
        var asm = typeof(SerializableRun).Assembly;
        Type[] types;
        try
        {
            types = asm.GetTypes();
        }
        catch (ReflectionTypeLoadException ex)
        {
            types = ex.Types.Where(t => t != null).Cast<Type>().ToArray();
        }

        var candidates = new List<(int score, string typeName, int value)>();
        foreach (var t in types)
        {
            MethodInfo? m;
            try
            {
                foreach (var name in new[] { "GetLatestSchemaVersion", "GetLatestVersion" })
                {
                    m = t.GetMethod(name, BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static,
                        null, Type.EmptyTypes, null);
                    if (m == null) continue;
                    var tn = t.FullName ?? "";
                    // Avoid unrelated static GetLatestVersion() elsewhere in the assembly.
                    if (name == "GetLatestVersion" && !tn.Contains("Saves", StringComparison.Ordinal))
                        continue;

                    var conv = TryConvertSchemaNumber(m.Invoke(null, null));
                    if (!conv.HasValue) continue;

                    var score = name == "GetLatestSchemaVersion" ? 100 : 0;
                    if (tn.Contains("Saves", StringComparison.Ordinal)) score += 50;
                    if (tn.Contains("Schema", StringComparison.Ordinal) || tn.Contains("Migration", StringComparison.Ordinal))
                        score += 25;
                    candidates.Add((score, tn, conv.Value));
                }
            }
            catch
            {
                // type may not support full reflection on this runtime
            }
        }

        if (candidates.Count == 0)
            return null;

        var best = candidates.OrderByDescending(c => c.score).ThenBy(c => c.typeName).First();
        return best.value;
    }

    private static int? TryConvertSchemaNumber(object? value) => value switch
    {
        int i => i,
        uint u => u <= int.MaxValue ? (int)u : null,
        long l => l >= int.MinValue && l <= int.MaxValue ? (int)l : null,
        short s => s,
        ushort us => us,
        byte b => b,
        _ => null,
    };

    private static bool ValidateSaveSchemaVersion(string saveJson, out string error)
    {
        error = "";
        try
        {
            using var doc = System.Text.Json.JsonDocument.Parse(saveJson);
            var root = doc.RootElement;

            if (!root.TryGetProperty("schema_version", out var versionElem))
            {
                error = "missing schema_version";
                return false;
            }

            if (versionElem.ValueKind != System.Text.Json.JsonValueKind.Number ||
                !versionElem.TryGetInt32(out var schemaVersion))
            {
                error = "schema_version is not a valid integer";
                return false;
            }

            var expected = GetExpectedSaveSchemaVersion();
            if (expected.HasValue && schemaVersion != expected.Value)
            {
                error = $"expected v{expected.Value}, got v{schemaVersion}";
                return false;
            }

            return true;
        }
        catch (Exception ex)
        {
            error = $"could not inspect save: {ex.Message}";
            return false;
        }
    }

    private static bool TrySetPropertyValue(object target, string propertyName, object? value)
    {
        var prop = target.GetType().GetProperty(propertyName);
        if (prop?.CanWrite != true)
            return false;
        prop.SetValue(target, value);
        return true;
    }

    private static bool IsInitialNeowSave(string saveJson)
    {
        try
        {
            using var doc = System.Text.Json.JsonDocument.Parse(saveJson);
            var root = doc.RootElement;

            if (!root.TryGetProperty("current_act_index", out var actIndexElem) || actIndexElem.GetInt32() != 0)
                return false;

            var hasVisitedCoords = root.TryGetProperty("visited_map_coords", out var visitedElem)
                                && visitedElem.ValueKind == System.Text.Json.JsonValueKind.Array
                                && visitedElem.GetArrayLength() > 0;
            if (hasVisitedCoords)
                return false;

            return root.TryGetProperty("extra_fields", out var extraFieldsElem)
                && extraFieldsElem.ValueKind == System.Text.Json.JsonValueKind.Object
                && extraFieldsElem.TryGetProperty("started_with_neow", out var startedElem)
                && startedElem.ValueKind == System.Text.Json.JsonValueKind.True;
        }
        catch
        {
            return false;
        }
    }

    private static bool TryRollbackSerializedSaveToPreRoom(SerializableRun serializableRun, out string error)
    {
        error = "";

        var saveType = serializableRun.GetType();
        var visitedProp = saveType.GetProperty("VisitedMapCoords");
        if (visitedProp == null)
        {
            error = "Save data is missing VisitedMapCoords";
            return false;
        }

        var visitedValue = visitedProp.GetValue(serializableRun);
        var visitedItems = new List<object?>();
        if (visitedValue is System.Collections.IEnumerable visitedEnumerable)
        {
            foreach (var item in visitedEnumerable)
                visitedItems.Add(item);
        }

        if (visitedItems.Count == 0)
        {
            error = "Cannot roll back save before the first room";
            return false;
        }

        visitedItems.RemoveAt(visitedItems.Count - 1);

        var visitedType = visitedProp.PropertyType;
        if (visitedType.IsArray)
        {
            var elementType = visitedType.GetElementType()!;
            var array = Array.CreateInstance(elementType, visitedItems.Count);
            for (int i = 0; i < visitedItems.Count; i++)
                array.SetValue(visitedItems[i], i);
            visitedProp.SetValue(serializableRun, array);
        }
        else if (visitedType.IsGenericType)
        {
            var elementType = visitedType.GetGenericArguments()[0];
            var listType = typeof(List<>).MakeGenericType(elementType);
            var list = (System.Collections.IList)Activator.CreateInstance(listType)!;
            foreach (var item in visitedItems)
                list.Add(item);
            visitedProp.SetValue(serializableRun, list);
        }
        else
        {
            error = $"Unsupported VisitedMapCoords type: {visitedType.Name}";
            return false;
        }

        TrySetPropertyValue(serializableRun, "ActFloor", visitedItems.Count);
        TrySetPropertyValue(serializableRun, "CurrentMapCoord", visitedItems.Count > 0 ? visitedItems[^1] : null);
        TrySetPropertyValue(serializableRun, "PreFinishedRoom", null);
        TrySetPropertyValue(serializableRun, "CurrentRoom", null);
        return true;
    }

    public Dictionary<string, object?> SaveCheckpoint(string? outputPath)
    {
        try
        {
            if (_runState == null)
                return Error("No active run to save", "no_run");

            if (string.IsNullOrEmpty(outputPath))
                return Error("write_continue_save requires 'path' (the file to write)", "bad_request");

            var currentRoom = _runState.CurrentRoom;
            SerializableRun serializableRun;

            if (currentRoom is MapRoom || currentRoom == null)
            {
                Log($"Saving map checkpoint (room={currentRoom?.GetType().Name ?? "null"}, outputPath={outputPath})...");
                serializableRun = RunManager.Instance.ToSave(currentRoom);
            }
            else if (ContinueSave.Last is { } gameSave && ContinueSave.MatchesPosition(gameSave, _runState))
            {
                // Mid-room: write what the game itself last saved (on entering this node, or
                // after winning its fight). Loading it re-enters the same room, like Continue.
                Log($"Saving the game's continue save from {currentRoom.GetType().Name} (outputPath={outputPath})...");
                serializableRun = gameSave;
            }
            else
            {
                Log($"Saving pre-room checkpoint from {currentRoom.GetType().Name} (outputPath={outputPath})...");
                serializableRun = RunManager.Instance.ToSave(new MapRoom());
                if (!TryRollbackSerializedSaveToPreRoom(serializableRun, out var rollbackError))
                    return Error($"Cannot save checkpoint: {rollbackError}");
            }

            var saveJson = SaveManager.ToJson(serializableRun);
            Log($"Serialized save: {saveJson.Length} chars");

            try
            {
                var dir = System.IO.Path.GetDirectoryName(outputPath);
                if (!string.IsNullOrEmpty(dir))
                    System.IO.Directory.CreateDirectory(dir);
                System.IO.File.WriteAllText(outputPath, saveJson);
            }
            catch (Exception ex) when (ex is System.IO.IOException or UnauthorizedAccessException or NotSupportedException or ArgumentException)
            {
                return Error($"Cannot write save to '{outputPath}': {ex.GetType().Name}: {ex.Message}", "io_error");
            }
            Log($"Save written to: {outputPath}");

            return new Dictionary<string, object?>
            {
                ["type"] = "save_result",
                ["success"] = true,
                ["path"] = outputPath,
                ["size"] = saveJson.Length,
                ["room_type"] = currentRoom?.GetType().Name,
            };
        }
        catch (Exception ex)
        {
            return ErrorWithTrace("SaveCheckpoint failed", ex);
        }
    }
    public Dictionary<string, object?> ExecuteAction(string action, Dictionary<string, object?>? args)
    {
        try
        {
            if (_poisoned)
                return RestartRequired();
            if (_runState == null)
                return Error("No run in progress");
            if (_stuckReason != null)
                return StuckError();

            var player = _runState.Players[0];

            var routed = RouteRewardScreenAction(player, action, args);
            if (routed != null)
                return routed;

            switch (action)
            {
                case "select_map_node":
                    return DoMapSelect(player, args);
                case "play_card":
                    return DoPlayCard(player, args);
                case "end_turn":
                    return DoEndTurn(player);
                case "choose_option":
                    return DoChooseOption(player, args);
                case "select_card_reward":
                    return DoSelectCardReward(player, args);
                case "skip_card_reward":
                    return DoSkipCardReward(player);
                case "select_card_alternative":
                    return DoSelectCardAlternative(args);
                case "buy_card":
                    return DoBuyCard(player, args);
                case "buy_relic":
                    return DoBuyRelic(player, args);
                case "buy_potion":
                    return DoBuyPotion(player, args);
                case "remove_card":
                    return DoRemoveCard(player);
                case "select_bundle":
                    return DoSelectBundle(player, args);
                case "crystal_sphere_click":
                    return DoCrystalSphereClick(args);
                case "select_cards":
                    return DoSelectCards(player, args);
                case "skip_select":
                    return DoSkipSelect(player);
                case "use_potion":
                    return DoUsePotion(player, args);
                case "discard_potion":
                    return DoDiscardPotion(player, args);
                case "leave_room":
                    return DoLeaveRoom(player);
                case "proceed":
                    return DoProceed(player);
                default:
                    return Error($"Unknown action: {action}");
            }
        }
        catch (Exception ex)
        {
            return ErrorWithTrace($"Action '{action}' failed", ex);
        }
    }

    #region Actions

    /// <summary>
    /// Forget the previous room's reward / treasure / event tracking before entering a room
    /// (map travel and debug enter_room alike), so the next room's rewards are generated.
    /// </summary>
    private void ResetPerRoomState(Player player)
    {
        _treasureFinishPending = null;
        _rewardsProcessed = false;
        _rewardsProceeded = false;
        _eventOptionChosen = false;
        _lastEventOptionCount = 0;
        _pendingRewards = null;
        _rewardDone.Clear();
        _lastKnownHp = player.Creature?.CurrentHp ?? 0;
    }

    private Dictionary<string, object?> DoMapSelect(Player player, Dictionary<string, object?>? args)
    {
        if (args == null || !args.ContainsKey("col") || !args.ContainsKey("row"))
            return Error("select_map_node requires 'col' and 'row'");

        // Travel is only possible from the map (the game disables it while a room is open:
        // RunManager.EnterRoom -> SetTravelEnabled(false)), and only to the nodes map_select
        // offers; EnterMapCoord would happily teleport anywhere on the map.
        if (!(_runState?.CurrentRoom is MapRoom || _runState?.CurrentRoom == null) || HasPendingPrompt)
            return Error("select_map_node is only available at map_select");
        int reqCol, reqRow;
        try { reqCol = Convert.ToInt32(args["col"]); reqRow = Convert.ToInt32(args["row"]); }
        catch { return Error("select_map_node: 'col' and 'row' must be integers"); }
        if (_runState?.Map == null)
            return Error("No map available");
        var offered = TravelablePoints(_runState.Map);
        if (!offered.Any(p => p.coord.col == reqCol && p.coord.row == reqRow))
        {
            var list = string.Join(", ", offered.Select(p => $"({p.coord.col},{p.coord.row})"));
            return Error($"Map node ({reqCol},{reqRow}) is not reachable; choices: {list}");
        }

        ResetPerRoomState(player);

        var col = reqCol;
        var row = reqRow;
        var coord = new MapCoord((byte)col, (byte)row);

        Log($"Moving to map coord ({col},{row})");

        // BUG-013: Wait for any pending actions (relic sessions, etc.) to complete before entering new room
        WaitForActionExecutor();
        _syncCtx.Pump();

        // Call EnterMapCoord directly (same as what MoveToMapCoordAction does in TestMode)
        // This avoids the action executor which can swallow errors silently.
        RunManager.Instance.EnterMapCoord(coord).GetAwaiter().GetResult();
        _syncCtx.Pump();
        WaitForActionExecutor();

        return DetectDecisionPoint();
    }

    private Dictionary<string, object?> DoPlayCard(Player player, Dictionary<string, object?>? args)
    {
        if (args == null || !args.ContainsKey("card_index"))
            return Error("play_card requires 'card_index'");

        var cardIndex = Convert.ToInt32(args["card_index"]);
        var pcs = player.PlayerCombatState;
        if (pcs == null)
            return Error("Not in combat");
        // Never index the hand while a turn change is still drawing/discarding cards.
        if (!IsPlayPhase())
        {
            SpinWaitForCombatStable();
            if (!IsPlayPhase())
                return Error($"Cannot play cards now: player phase is {pcs.Phase}, not Play");
        }

        // Index the hand in the order the game shows it (HandOrder), not the pile order.
        var hand = HandOrder.Cards(pcs.Hand);
        if (cardIndex < 0 || cardIndex >= hand.Count)
            return Error($"Invalid card index {cardIndex}, hand has {hand.Count} cards");

        var card = hand[cardIndex];

        // Determine target based on card's TargetType first
        // Self/None/All cards: target = null (game handles internally)
        // AnyEnemy cards: use target_index or auto-pick first alive enemy
        Creature? target = null;
        var cardTargetType = card.TargetType;
        if (cardTargetType == TargetType.AnyEnemy)
        {
            // Caller's target_id (stable CombatId) or target_index (position among targetable
            // enemies). A given but invalid target is an error, never silently retargeted.
            var (resolved, targetError) = ResolveEnemyTarget(args);
            if (targetError != null) return Error(targetError);
            target = resolved;
            // No target given: only auto-target when the choice is unambiguous
            // (a single targetable enemy). With multiple enemies, picking one is a real game
            // decision — return an error instead of silently targeting enemy 0 (#79).
            if (target == null)
            {
                var alive = TargetableEnemies(CombatManager.Instance.DebugOnlyGetState());
                if (alive.Count == 1)
                    target = alive[0];
                else if (alive.Count > 1)
                    return Error($"Card {card.Id.Entry} targets a single enemy (AnyEnemy); " +
                                 $"'target_index' is required when multiple enemies are alive ({alive.Count}).");
            }
        }
        // All other target types (None, All, etc.) → leave target as null

        // Check if card can be played
        if (!card.CanPlay(out var reason, out var preventer))
        {
            return Error($"Cannot play card {card.GetType().Name}: {UnplayableText(reason, preventer)}");
        }

        Log($"Playing card {card.GetType().Name} (index {cardIndex}) targeting {(target != null ? target.Monster?.GetType().Name ?? "creature" : "none")}");

        var handCountBefore = hand.Count;
        // Count card plays recorded by the combat history: a card can legitimately end up back
        // in the same hand slot after being played (Particle Wall: "Return this card to your Hand"),
        // so "still in hand" alone is not proof that the play failed (BUG-006).
        var history = CombatManager.Instance.History;
        int CardPlays() { try { return history?.CardPlaysStarted.ToList().Count ?? 0; } catch { return 0; } }
        var playsBefore = CardPlays();

        var playAction = new PlayCardAction(card, target);
        RunManager.Instance.ActionQueueSet.EnqueueWithoutSynchronizing(playAction);
        WaitForActionExecutor();

        var cardWasPlayed = false;
        try
        {
            cardWasPlayed = history != null &&
                history.CardPlaysStarted.ToList().Skip(playsBefore).Any(e => e.CardPlay.Card == card);
        }
        catch (InvalidOperationException) { }  // history mutated mid-read; fall back to the hand check

        // Check if card play had no effect (hand unchanged, same card still at same index)
        var handAfter = HandOrder.Cards(pcs.Hand);
        if (!cardWasPlayed && handAfter.Count == handCountBefore && cardIndex < handAfter.Count && handAfter[cardIndex] == card)
        {
            return Error($"Card could not be played (still in hand after action): {card.GetType().Name} [{card.Id}]");
        }

        return DetectDecisionPoint();
    }

    // STS2 build 23372702 removed CombatManager.IsPlayPhase (global) in favor of a
    // per-player PlayerCombatState.Phase. Headless is single-player, so the local
    // player (Players[0]) being in the Play phase is the equivalent signal.
    private bool IsPlayPhase()
    {
        var p = (_runState != null && _runState.Players.Count > 0) ? _runState.Players[0] : null;
        return p?.PlayerCombatState?.Phase == MegaCrit.Sts2.Core.Combat.PlayerTurnPhase.Play;
    }

    private Dictionary<string, object?> DoEndTurn(Player player)
    {
        // A pending card / card-reward / bundle selection is an unresolved prompt; ending
        // the turn here would silently mutate combat instead. Surface the prompt unchanged
        // and let the caller resolve it first (#61).
        if (_cardSelector.HasPending || _cardSelector.HasPendingReward || _pendingBundles != null)
        {
            Log("end_turn ignored: a card selection is pending");
            return DetectDecisionPoint();
        }

        if (!IsPlayPhase())
        {
            // Might be between phases — pump and check
            _syncCtx.Pump();
            if (!IsPlayPhase())
            {
                if (!CombatManager.Instance.IsInProgress || player.Creature.IsDead)
                    return DetectDecisionPoint();
                // Brief wait for ThreadPool if sync context didn't catch it
                Thread.Sleep(100);
                _syncCtx.Pump();
                if (!IsPlayPhase())
                    return DetectDecisionPoint();
            }
        }

        // Ensure no actions are still running before ending turn
        WaitForActionExecutor();

        Log($"Ending turn (round={CombatManager.Instance.DebugOnlyGetState()?.RoundNumber ?? 0})");
        _turnStarted.Reset();
        _combatEnded.Reset();

        // Enable SuppressYield so Task.Yield() runs inline during enemy turn processing.
        // This prevents deadlocks during boss fights (e.g., Vantom) where continuations
        // would otherwise be posted to ThreadPool and never complete.
        // Keep SuppressYield=true through the initial fallback wait loop — multi-hit
        // attacks (e.g., 10x2) have continuations between hits that also need suppression.
        YieldPatches.SuppressYield = true;
        try
        {
            PlayerCmd.EndTurn(player, canBackOut: false);
            _syncCtx.Pump();

            // Fallback: if turn didn't complete synchronously, keep pumping with SuppressYield on
            if (CombatManager.Instance.IsInProgress && !IsPlayPhase() && !player.Creature.IsDead)
            {
                for (int i = 0; i < 400; i++)
                {
                    _syncCtx.Pump();
                    if (HasPendingPrompt) break;
                    if (_turnStarted.IsSet || _combatEnded.IsSet) break;
                    if (!CombatManager.Instance.IsInProgress || player.Creature.IsDead) break;
                    if (IsPlayPhase()) break;
                    Thread.Sleep(5);
                }
            }
        }
        finally
        {
            YieldPatches.SuppressYield = false;
        }

        // The turn transition is not stuck when it is awaiting a player choice: an
        // end-of-turn effect (Stampede auto-playing Headbutt) or an enemy move (Knowledge
        // Demon's Curse of Knowledge) opened a selection prompt. Surface it; select_cards /
        // skip_select resumes the transition. Cancelling + re-ending the turn here would
        // abandon the awaiting flow and wedge combat (engine_stuck).
        if (HasPendingPrompt)
        {
            Log("EndTurn paused on a selection prompt");
            return DetectDecisionPoint();
        }

        // Second fallback: if still stuck after SuppressYield window, cancel and retry.
        // The WaitUntilQueue TCS is likely deadlocked.
        if (CombatManager.Instance.IsInProgress && !IsPlayPhase() && !player.Creature.IsDead)
        {
            Log("EndTurn stuck, cancelling and retrying with SuppressYield...");
            try
            {
                RunManager.Instance.ActionExecutor.Cancel();
                _syncCtx.Pump();
                Thread.Sleep(50);
                _syncCtx.Pump();

                // Reset the player ready state and try again with SuppressYield
                CombatManager.Instance.UndoReadyToEndTurn(player);
                _syncCtx.Pump();

                YieldPatches.SuppressYield = true;
                try
                {
                    PlayerCmd.EndTurn(player, canBackOut: false);
                    _syncCtx.Pump();
                }
                finally
                {
                    YieldPatches.SuppressYield = false;
                }

                for (int i = 0; i < 100; i++)
                {
                    _syncCtx.Pump();
                    if (HasPendingPrompt) break;
                    if (_turnStarted.IsSet || _combatEnded.IsSet) break;
                    if (!CombatManager.Instance.IsInProgress || player.Creature.IsDead) break;
                    if (IsPlayPhase()) break;
                    Thread.Sleep(10);
                }
            }
            catch (Exception ex) { Log($"Cancel retry: {ex.Message}"); }

            if (HasPendingPrompt)
            {
                Log("EndTurn retry paused on a selection prompt");
                return DetectDecisionPoint();
            }

            // NUCLEAR OPTION: If STILL stuck after 2 attempts, use ThreadPool to force
            // the enemy turn processing to complete with SuppressYield permanently on.
            if (CombatManager.Instance.IsInProgress && !IsPlayPhase() && !player.Creature.IsDead)
            {
                var stuckState = CombatManager.Instance.DebugOnlyGetState();
                var stuckEnemies = stuckState?.Enemies?.Where(e => e != null && e.IsAlive)
                    .Select(e => $"{e.Monster?.GetType().Name}(hp={e.CurrentHp})").ToList();
                Log($"EndTurn STILL stuck after retry — nuclear fallback. Round={stuckState?.RoundNumber}, " +
                    $"Enemies=[{string.Join(",", stuckEnemies ?? new())}], " +
                    $"IsPlayPhase={IsPlayPhase()}, " +
                    $"IsInProgress={CombatManager.Instance.IsInProgress}, " +
                    $"ActionExecutor.IsRunning={RunManager.Instance.ActionExecutor.IsRunning}");
                try
                {
                    // Cancel again and undo
                    RunManager.Instance.ActionExecutor.Cancel();
                    _syncCtx.Pump();
                    CombatManager.Instance.UndoReadyToEndTurn(player);
                    _syncCtx.Pump();
                    Thread.Sleep(50);

                    // Run EndTurn on ThreadPool with SuppressYield permanently on
                    YieldPatches.SuppressYield = true;
                    var endTurnTask = Task.Run(() =>
                    {
                        PlayerCmd.EndTurn(player, canBackOut: false);
                    });

                    // Aggressively pump sync context while waiting (up to 5 seconds)
                    for (int i = 0; i < 500; i++)
                    {
                        _syncCtx.Pump();
                        if (endTurnTask.IsCompleted) break;
                        if (HasPendingPrompt) break;
                        if (_turnStarted.IsSet || _combatEnded.IsSet) break;
                        if (!CombatManager.Instance.IsInProgress || player.Creature.IsDead) break;
                        if (IsPlayPhase()) break;
                        Thread.Sleep(10);
                    }
                    YieldPatches.SuppressYield = false;

                    // If still not play phase, try just waiting a bit more
                    if (CombatManager.Instance.IsInProgress && !IsPlayPhase() && !player.Creature.IsDead)
                    {
                        for (int i = 0; i < 200; i++)
                        {
                            _syncCtx.Pump();
                            Thread.Sleep(10);
                            if (IsPlayPhase() || !CombatManager.Instance.IsInProgress || player.Creature.IsDead)
                                break;
                        }
                    }

                    if (HasPendingPrompt)
                    {
                        Log("Nuclear fallback paused on a selection prompt");
                        return DetectDecisionPoint();
                    }
                    if (IsPlayPhase())
                        Log("Nuclear fallback SUCCEEDED — play phase resumed");
                    else if (!CombatManager.Instance.IsInProgress || player.Creature.IsDead)
                        Log("Nuclear fallback: combat ended — reporting real outcome");
                    else
                    {
                        // Don't disguise a deadlock as a defeat: report it so callers can
                        // tell engine failures apart from real losses.
                        Log("Nuclear fallback FAILED — run is stuck");
                        return MarkStuck("end_turn could not advance combat (engine deadlock)");
                    }
                }
                catch (Exception ex)
                {
                    Log($"Nuclear fallback error: {ex.Message}");
                    YieldPatches.SuppressYield = false;
                }
            }
        }

        return DetectDecisionPoint();
    }

    private Dictionary<string, object?> DoSelectCardReward(Player player, Dictionary<string, object?>? args)
    {
        // Handle event-triggered card reward (blocking GetSelectedCardReward)
        if (_cardSelector.HasPendingReward)
        {
            if (args == null || !args.ContainsKey("card_index"))
                return Error("select_card_reward requires 'card_index'");
            var idx = Convert.ToInt32(args["card_index"]);
            var offered = _cardSelector.PendingRewardCards?.Count ?? 0;
            if (idx < 0 || idx >= offered)
                return Error($"Invalid card index {idx}, {offered} cards available");
            Log($"Resolving card reward: index {idx}");
            _cardSelector.ResolveReward(idx);
            AwaitBackground();
            WaitForActionExecutor();
            return DetectDecisionPoint();
        }

        return Error("No pending card reward");
    }

    private Dictionary<string, object?> DoSkipCardReward(Player player)
    {
        if (_cardSelector.HasPendingReward)
        {
            // Skip is one of the reward's alternatives; it is missing when the reward can't be
            // skipped (Draft). Choosing it closes the screen without completing the reward.
            var alts = _cardSelector.PendingRewardAlternatives;
            var skipIdx = alts?.FindIndex(a => a.OptionId == "Skip") ?? -1;
            if (alts != null && skipIdx < 0)
                return Error("This card reward can't be skipped");
            Log("Skipping card reward");
            if (skipIdx >= 0) _cardSelector.ResolveRewardAlternative(skipIdx);
            else _cardSelector.SkipReward();
            AwaitBackground();
            WaitForActionExecutor();
            return DetectDecisionPoint();
        }
        return Error("No pending card reward");
    }

    private Dictionary<string, object?> DoBuyCard(Player player, Dictionary<string, object?>? args)
    {
        if (_runState?.CurrentRoom is not MerchantRoom merchantRoom)
            return Error("Not in a shop");
        if (args == null || !args.ContainsKey("card_index"))
            return Error("buy_card requires 'card_index'");

        var idx = Convert.ToInt32(args["card_index"]);
        var allEntries = merchantRoom.GetLocalInventory().CharacterCardEntries
            .Concat(merchantRoom.GetLocalInventory().ColorlessCardEntries).ToList();
        if (idx < 0 || idx >= allEntries.Count)
            return Error($"Invalid card index {idx}");

        var entry = allEntries[idx];
        if (!entry.IsStocked) return Error("Card already purchased");
        if (player.Gold < entry.Cost) return Error("Not enough gold");

        // A purchase clears the slot (and may restock it), so read what we buy beforehand.
        var cardName = entry.CreationResult?.Card?.GetType().Name ?? "?";
        var cardCost = entry.Cost;
        try
        {
            entry.OnTryPurchaseWrapper(merchantRoom.GetLocalInventory()).GetAwaiter().GetResult();
            _syncCtx.Pump();
            Log($"Bought card: {cardName} for {cardCost}g");
        }
        catch (Exception ex) { return Error($"Buy card failed: {ex.Message}"); }

        return DetectDecisionPoint();
    }

    private Dictionary<string, object?> DoBuyRelic(Player player, Dictionary<string, object?>? args)
    {
        var shopInv = CurrentMerchantInventory();  // a shop, or the Fake Merchant event
        if (shopInv == null)
            return Error("Not in a shop");
        if (args == null || !args.ContainsKey("relic_index"))
            return Error("buy_relic requires 'relic_index'");

        var idx = Convert.ToInt32(args["relic_index"]);
        var entries = shopInv.RelicEntries;
        if (idx < 0 || idx >= entries.Count) return Error($"Invalid relic index {idx}");

        var entry = entries[idx];
        if (!entry.IsStocked) return Error("Relic already purchased");
        if (player.Gold < entry.Cost) return Error("Not enough gold");

        // OnTryPurchase -> ClearAfterPurchase nulls entry.Model (and may restock the slot with
        // a different relic), so capture what we are buying before the purchase runs. Reading
        // entry.Model afterwards used to throw and report a successful purchase as an error.
        var relicName = entry.Model?.GetType().Name ?? "?";
        var relicCost = entry.Cost;
        try
        {
            // The pickup effect can open a card_select (e.g. KIFUDA → enchant up to 3 with
            // Adroit, #80). Run the purchase on a background task and yield as soon as a
            // pending selection appears so the caller can resolve it; the background task
            // continues once the selector's TCS is fed by select_cards.
            var inv = shopInv;
            if (RunInBackground(() => entry.OnTryPurchaseWrapper(inv)))
            {
                Log($"Buy relic {relicName}: yielded for pending selection");
                return DetectDecisionPoint();
            }
            Log($"Bought relic: {relicName} for {relicCost}g");
        }
        catch (Exception ex) { return Error($"Buy relic failed: {ex.Message}"); }

        return DetectDecisionPoint();
    }

    private Dictionary<string, object?> DoBuyPotion(Player player, Dictionary<string, object?>? args)
    {
        if (_runState?.CurrentRoom is not MerchantRoom merchantRoom)
            return Error("Not in a shop");
        if (args == null || !args.ContainsKey("potion_index"))
            return Error("buy_potion requires 'potion_index'");

        var idx = Convert.ToInt32(args["potion_index"]);
        var entries = merchantRoom.GetLocalInventory().PotionEntries;
        if (idx < 0 || idx >= entries.Count) return Error($"Invalid potion index {idx}");

        var entry = entries[idx];
        if (!entry.IsStocked) return Error("Potion already purchased");
        if (player.Gold < entry.Cost) return Error("Not enough gold");
        if (!player.HasOpenPotionSlots) return Error("Potion slots are full: discard_potion first");

        // Same as relics: the purchase clears entry.Model, so read it first.
        var potionName = entry.Model?.GetType().Name ?? "?";
        var potionCost = entry.Cost;
        try
        {
            entry.OnTryPurchaseWrapper(merchantRoom.GetLocalInventory()).GetAwaiter().GetResult();
            _syncCtx.Pump();
            Log($"Bought potion: {potionName} for {potionCost}g");
        }
        catch (Exception ex)
        {
            Log($"Buy potion failed: {ex.Message}");
        }

        return DetectDecisionPoint();
    }

    private Dictionary<string, object?> DoRemoveCard(Player player)
    {
        if (_runState?.CurrentRoom is not MerchantRoom merchantRoom)
            return Error("Not in a shop");

        var removal = merchantRoom.GetLocalInventory().CardRemovalEntry;
        if (removal == null) return Error("No card removal available");
        ApplyCardRemovalRules(removal, player);
        if (!removal.IsStocked) return Error("Card removal was already used at this shop");
        if (player.Gold < removal.Cost) return Error("Not enough gold");

        try
        {
            // Run on background thread so card selection can pause (same pattern as event options).
            // A successful removal marks the service used (RewardPatches, standing in for
            // NMerchantCardRemoval.OnCardRemovalUsed), for this and Lord's Parasol's free one.
            var inv = merchantRoom.GetLocalInventory();
            if (RunInBackground(() => removal.OnTryPurchaseWrapper(inv)))
            {
                WaitForActionExecutor();
                return DetectDecisionPoint();
            }
            Log($"Removed card for {removal.Cost}g");
        }
        catch (Exception ex) { return Error($"Remove card failed: {ex.Message}"); }

        return DetectDecisionPoint();
    }

    private Dictionary<string, object?> DoSelectBundle(Player player, Dictionary<string, object?>? args)
    {
        if (_pendingBundleTcs == null || _pendingBundles == null)
            return Error("No pending bundle selection");
        if (args == null || !args.ContainsKey("bundle_index"))
            return Error("select_bundle requires 'bundle_index'");

        var idx = Convert.ToInt32(args["bundle_index"]);
        if (idx < 0 || idx >= _pendingBundles.Count)
            return Error($"Invalid bundle index {idx}, {_pendingBundles.Count} bundles available");
        Log($"Bundle selection: pack {idx}");
        var bundles = _pendingBundles;
        var tcs = _pendingBundleTcs;
        _pendingBundles = null;
        _pendingBundleTcs = null;

        // Set result directly (no ContinueWith/ThreadPool)
        var selected = (idx >= 0 && idx < bundles.Count) ? bundles[idx] : bundles[0];
        ResumeFlow(() => tcs.TrySetResult(selected));

        AwaitBackground();
        WaitForActionExecutor();
        return DetectDecisionPoint();
    }

    /// <summary>True while combat is between player play phases (end of turn / enemy turn).</summary>
    private bool InTurnTransition(Player player) =>
        CombatManager.Instance.IsInProgress && !IsPlayPhase() && !player.Creature.IsDead;

    /// <summary>
    /// Resolve a prompt that was opened mid turn-transition and keep driving the transition
    /// (with SuppressYield, like DoEndTurn) until the next play phase, combat end, or prompt.
    /// </summary>
    /// <returns>false if the transition neither resumed nor paused on another prompt.</returns>
    private bool ResolveMidTurnPrompt(Player player, Action resolve)
    {
        YieldPatches.SuppressYield = true;
        try
        {
            resolve();
            for (int i = 0; i < 400 && InTurnTransition(player); i++)
            {
                _syncCtx.Pump();
                if (HasPendingPrompt || !InTurnTransition(player)) break;
                Thread.Sleep(5);
            }
        }
        finally
        {
            YieldPatches.SuppressYield = false;
        }
        if (!InTurnTransition(player) && !HasPendingPrompt)
            WaitForActionExecutor();
        else if (!HasPendingPrompt)
        {
            Log("Turn transition did not resume after selection");
            return false;
        }
        return true;
    }

    private Dictionary<string, object?> DoSelectCards(Player player, Dictionary<string, object?>? args)
    {
        if (!_cardSelector.HasPending)
            return Error("No pending card selection");
        if (args == null || !args.ContainsKey("indices"))
            return Error("select_cards requires 'indices' (comma-separated card indices)", "bad_request");

        // Accept "0,2" or [0, 2] (Program flattens arrays to "0,2"). Invalid entries used to be
        // dropped silently, which could resolve the prompt with nothing selected.
        var indicesStr = args["indices"]?.ToString() ?? "";
        var tokens = indicesStr.Split(',', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries);
        var optionCount = _cardSelector.PendingOptions?.Count ?? 0;
        var indices = new List<int>();
        foreach (var tok in tokens)
        {
            if (!int.TryParse(tok, out var v) || v < 0 || v >= optionCount)
                return Error($"Invalid card index '{tok}' (valid: 0-{optionCount - 1})",
                    int.TryParse(tok, out _) ? "illegal_action" : "bad_request");
            if (indices.Contains(v))
                return Error($"Card index {v} given twice");
            indices.Add(v);
        }
        var minSelect = Math.Min(_cardSelector.PendingMinSelect, optionCount);
        var maxSelect = _cardSelector.PendingMaxSelect;
        if (indices.Count < minSelect || indices.Count > maxSelect)
            return Error($"Select between {minSelect} and {maxSelect} cards (got {indices.Count})");

        Log($"Card selection: indices [{string.Join(",", indices)}]");
        if (_backgroundTask == null && InTurnTransition(player))
        {
            if (!ResolveMidTurnPrompt(player, () => _cardSelector.ResolvePendingByIndices(indices.ToArray())))
                return MarkStuck("turn transition did not resume after a mid-turn card selection");
            return DetectDecisionPoint();
        }
        ResumeFlow(_cardSelector.DetachByIndices(indices.ToArray()));
        // The flow that asked for the selection (event option, rest site SMITH, shop
        // removal, relic pickup) continues on its background task; let it finish so the
        // returned state is current and the next command can't race it.
        var prompted = AwaitBackground();
        WaitForActionExecutor();

        return DetectDecisionPoint();
    }

    private Dictionary<string, object?> DoSkipSelect(Player player)
    {
        if (!_cardSelector.HasPending)
            return Error("No pending card selection");
        if (_cardSelector.PendingMinSelect > 0 && _cardSelector.PendingContext?.Cancelable != true
            && (_cardSelector.PendingOptions?.Count ?? 0) > 0)
            return Error($"This selection can't be skipped: choose at least {_cardSelector.PendingMinSelect} card(s) with select_cards");
        if (_cardSelector.HasPending)
        {
            Log("Skipping card selection");
            if (_backgroundTask == null && InTurnTransition(player))
            {
                if (!ResolveMidTurnPrompt(player, () => _cardSelector.CancelPending()))
                    return MarkStuck("turn transition did not resume after a mid-turn card selection");
                return DetectDecisionPoint();
            }
            ResumeFlow(_cardSelector.DetachCancel());
            AwaitBackground();
            WaitForActionExecutor();
        }
        return DetectDecisionPoint();
    }

    private Dictionary<string, object?> DoUsePotion(Player player, Dictionary<string, object?>? args)
    {
        if (args == null || !args.ContainsKey("potion_index"))
            return Error("use_potion requires 'potion_index'");

        var idx = Convert.ToInt32(args["potion_index"]);
        var potionsList = player.Potions?.ToList() ?? new();
        if (idx < 0 || idx >= potionsList.Count) return Error($"Invalid potion index {idx}");
        var potion = potionsList[idx];
        if (potion == null) return Error($"No potion at index {idx}");
        if (TryThrowFoulPotionAtFakeMerchant(player, potion, out var thrown))
            return thrown!;
        // The game never lets the player use these by hand; UsePotionAction doesn't check.
        if (potion.Usage == MegaCrit.Sts2.Core.Entities.Potions.PotionUsage.Automatic
            || potion.Usage == MegaCrit.Sts2.Core.Entities.Potions.PotionUsage.None)
            return Error($"Potion {potion.Id.Entry} can't be used manually (usage: {potion.Usage})");
        // Same rules as the potion popup's Use button (NPotionPopup).
        if (!PotionUsable(player, potion, out var unusableReason))
            return Error($"Potion {potion.Id.Entry} can't be used now: {unusableReason}");

        // Determine target based on potion's TargetType first, then fall back to target_index
        Creature? target = null;
        var potionTargetType = potion.TargetType;

        // Self-targeting potions (Flex, Fortifier, etc.) ALWAYS target the player
        // regardless of any target_index the caller provides
        if (potionTargetType == TargetType.Self)
        {
            target = player.Creature;
        }
        else if (potionTargetType == TargetType.TargetedNoCreature)
        {
            // No creature target (e.g. Foul Potion thrown at the merchant): IsValidTarget only
            // accepts null for this target type.
            target = null;
        }
        else if (potionTargetType == TargetType.AnyEnemy)
        {
            if (CombatManager.Instance.IsInProgress)
            {
                var (resolved, targetError) = ResolveEnemyTarget(args);
                if (targetError != null) return Error(targetError);
                target = resolved;
            }
            // Same single-enemy rule as play_card: only auto-target when unambiguous (#79).
            if (target == null && CombatManager.Instance.IsInProgress)
            {
                var alive = TargetableEnemies(CombatManager.Instance.DebugOnlyGetState());
                if (alive.Count == 1)
                    target = alive[0];
                else if (alive.Count > 1)
                    return Error($"Potion {potion.Id.Entry} targets a single enemy (AnyEnemy); " +
                                 $"'target_index' is required when multiple enemies are alive ({alive.Count}).");
            }
        }
        // All other target types (None, All, etc.) → leave target as null

        // PotionModel.EnqueueManualUse: no explicit target -> the user, if that's a valid target
        // (AnyPlayer potions in single player).
        if (target == null && potionTargetType != TargetType.TargetedNoCreature)
        {
            try { if (potion.IsValidTarget(player.Creature)) target = player.Creature; } catch { }
        }
        // The popup's checks ran above (PotionUsable); this is the one UsePotionAction makes
        // before cancelling.
        if (!potion.IsValidTarget(target))
            return Error($"Invalid target for potion {potion.Id.Entry} ({potionTargetType})");

        Log($"Using potion: {potion.GetType().Name} at slot {idx} target={target?.GetType().Name ?? "none"}");
        try
        {
            // PotionModel.EnqueueManualUse: marks the potion IsQueued (the popup then disables
            // Use and Discard) and enqueues its UsePotionAction; a cancelled use clears the flag
            // (AfterUsageCanceled).
            potion.EnqueueManualUse(target);
            WaitForActionExecutor();
            _syncCtx.Pump();

            // Used while a card's selection is still running (True Grit's exhaust choice, ...):
            // the action waits in the queue behind that card, as in the game. Say so.
            if (potion.IsQueued && (player.Potions?.Contains(potion)) == true)
            {
                lock (_evLock)
                    AddEventLocked(new Dictionary<string, object?>
                    {
                        ["type"] = "potion_queued",
                        ["potion"] = _loc.Potion(potion.Id.Entry),
                    });
                return DetectDecisionPoint();
            }

            // Effect may open a prompt (card_select, bundle, ...) before the potion slot clears.
            if (HasPendingPrompt)
                return DetectDecisionPoint();

            // A cancelled use (invalid target, failed check) leaves the potion in its slot, as
            // in the game. Never discard it on the player's behalf.
            var afterPotions = player.Potions?.ToList() ?? new();
            if (afterPotions.Contains(potion))
            {
                Log("Potion not consumed by action (use cancelled); keeping it");
                return Error($"Potion {potion.Id.Entry} use was cancelled; the potion was kept");
            }
        }
        catch (Exception ex)
        {
            Log($"Use potion failed: {ex.Message}");
            if ((player.Potions?.Contains(potion)) == true)
                return Error($"Potion {potion.Id.Entry} use failed ({ex.Message}); the potion was kept");
        }

        return DetectDecisionPoint();
    }

    /// <summary>
    /// Whether the potion popup would enable "Use" (NPotionPopup: usage, CanUseOrRemovePotions,
    /// PassesCustomUsabilityCheck, and for CombatOnly potions RefreshButtons' turn checks).
    /// AnyTime potions stay usable while a selection screen is open, as in the game; CombatOnly
    /// ones don't (InACardSelectScreen). Without these checks a CombatOnly potion used on the
    /// map was queued and then silently lost.
    /// </summary>
    private bool PotionUsable(Player player, PotionModel potion, out string reason)
    {
        reason = "";
        if (potion.IsQueued) { reason = "already being used"; return false; }
        if (player.Creature == null || player.Creature.IsDead) { reason = "player is dead"; return false; }
        if (!player.CanUseOrRemovePotions) { reason = "potions are locked right now"; return false; }
        switch (potion.Usage)
        {
            case MegaCrit.Sts2.Core.Entities.Potions.PotionUsage.AnyTime:
                break;
            case MegaCrit.Sts2.Core.Entities.Potions.PotionUsage.CombatOnly:
            {
                if (!CombatManager.Instance.IsInProgress) { reason = "it can only be used in combat"; return false; }
                var creature = player.Creature;
                if (creature.CombatState?.CurrentSide != creature.Side || CombatManager.Instance.PlayerActionsDisabled)
                { reason = "it can only be used during your turn"; return false; }
                if (HasPendingPrompt)  // InACardSelectScreen
                { reason = "a card selection is open (only AnyTime potions can be used then)"; return false; }
                break;
            }
            default:
                reason = $"usage {potion.Usage}"; return false;
        }
        bool custom;
        // Foul Potion is thrown at the merchant: the game checks for the merchant's button node,
        // which headless shops don't have. Its OnUse (gain gold) works without it.
        if (potion is MegaCrit.Sts2.Core.Models.Potions.FoulPotion && !CombatManager.Instance.IsInProgress
            && (_runState?.CurrentRoom is MerchantRoom
                || (_runState?.CurrentRoom is EventRoom
                    && RunManager.Instance.EventSynchronizer?.GetLocalEvent() is MegaCrit.Sts2.Core.Models.Events.FakeMerchant fm
                    && FakeMerchantOpen(fm))))
            custom = true;  // ... and at the Fake Merchant (NFakeMerchant), where it starts the fight
        else
        {
            try { custom = potion.PassesCustomUsabilityCheck; } catch { custom = false; }
        }
        if (!custom) { reason = "it can't be used here"; return false; }
        return true;
    }

    private Dictionary<string, object?> DoDiscardPotion(Player player, Dictionary<string, object?>? args)
    {
        if (args == null || !args.ContainsKey("potion_index"))
            return Error("discard_potion requires 'potion_index'");

        var idx = Convert.ToInt32(args["potion_index"]);
        var potionsList = player.Potions?.ToList() ?? new();
        if (idx < 0 || idx >= potionsList.Count) return Error($"Invalid potion index {idx}");
        var potion = potionsList[idx];
        if (potion == null) return Error($"No potion at index {idx}");
        // Events that reference held potions (The Future of Potions, Ranwid, Stone of All Time)
        // lock them: the popup disables Discard too.
        if (!player.CanUseOrRemovePotions)
            return Error("Potions can't be used or discarded right now");
        if (potion.IsQueued)
            return Error($"Potion {potion.Id.Entry} is being used");

        MegaCrit.Sts2.Core.Commands.PotionCmd.Discard(potion).GetAwaiter().GetResult();
        _syncCtx.Pump();
        return DetectDecisionPoint();
    }

    private Dictionary<string, object?> DoChooseOption(Player player, Dictionary<string, object?>? args)
    {
        if (args == null || !args.ContainsKey("option_index"))
            return Error("choose_option requires 'option_index'");

        var optionIndex = Convert.ToInt32(args["option_index"]);
        Log($"Choosing option {optionIndex}");

        // Dispatch based on ROOM TYPE (not event state) to avoid cross-contamination
        if (_runState?.CurrentRoom is RestSiteRoom restSiteRoom)
        {
            // An out-of-range index used to make ChooseLocalOption fail silently and the
            // whole rest site get skipped.
            var restOptions = restSiteRoom.Options;
            if (restOptions == null || optionIndex < 0 || optionIndex >= restOptions.Count)
                return Error($"Invalid option index {optionIndex}, {restOptions?.Count ?? 0} options available");
            if (!restOptions[optionIndex].IsEnabled)
                return Error($"Rest site option {optionIndex} is not available");
            Log($"Rest site: choosing option {optionIndex}");
            try
            {
                // Run on background thread so Smith card selection can pause
                if (RunInBackground(async () =>
                    {
                        if (await RunManager.Instance.RestSiteSynchronizer.ChooseLocalOption(optionIndex))
                            _restChoiceRoom = restSiteRoom;
                    }))
                {
                    WaitForActionExecutor();
                    return DetectDecisionPoint();
                }
            }
            catch (Exception ex)
            {
                Log($"Rest site ChooseLocalOption failed: {ex.Message}");
            }

            // Let the action complete (heal HP, dig for relic, etc.). The synchronizer removes
            // the used option, or all of them unless a relic like Miniature Tent allows more;
            // RestSiteState leaves for the map once none are left.
            if (!_cardSelector.HasPending)
            {
                WaitForActionExecutor();
                AwaitBackground();
                WaitForActionExecutor();
                return DetectDecisionPoint();
            }
        }
        // For events — use EventSynchronizer
        // Run Chosen() on a background thread so card selections can pause
        else if (_runState?.CurrentRoom is EventRoom)
        {
            var eventSync = RunManager.Instance.EventSynchronizer;
            var localEvent = eventSync?.GetLocalEvent();
            if (localEvent is MegaCrit.Sts2.Core.Models.Events.FakeMerchant fakeMerchant && FakeMerchantOpen(fakeMerchant))
                return Error("The Fake Merchant has no options: use buy_relic, use_potion or leave_room");
            // The Trial's abandon-run popup is up: its Yes / No are the only buttons.
            if (AbandonPopupOpen(localEvent))
                return DoAbandonPopup(optionIndex);
            if (localEvent != null && localEvent.IsFinished)
            {
                // The outcome page's only option is Proceed (NEventRoom.SetOptions).
                if (optionIndex != 0)
                    return Error($"Invalid option index {optionIndex}, 1 option available (Proceed)");
                _finishedEventAcked = localEvent;
                return DetectDecisionPoint();
            }
            if (localEvent != null && !localEvent.IsFinished)
            {
                var options = localEvent.CurrentOptions;
                var optCountBefore = options?.Count ?? 0;
                if (options == null || optionIndex < 0 || optionIndex >= options.Count)
                    return Error($"Invalid option index {optionIndex}, {optCountBefore} options available");
                if (options[optionIndex].IsLocked)
                    return Error($"Option {optionIndex} is locked");
                {
                    try
                    {
                        _eventOptionChosen = true;
                        _lastEventOptionCount = options.Count;
                        MarkEventOptionChosen();
                        SaveEventOptionToHistory(player, options[optionIndex]);
                        // Run on thread pool so GetSelectedCards/GetSelectedCardReward can block
                        if (RunInBackground(() => options[optionIndex].Chosen()))
                        {
                            WaitForActionExecutor();
                            return DetectDecisionPoint();
                        }
                    }
                    catch (Exception ex) { Log($"Event choose: {ex.Message}"); }
                }

                // Note: do NOT force-finish on `optCountAfter == optCountBefore`. Events can
                // legitimately stay interactive with the same option count (Slippery Bridge
                // Hold On loops until Overcome is chosen, #59). Trust IsFinished and let the
                // next DetectDecisionPoint return the current options for the next choice.
            }
        }

        WaitForActionExecutor();
        return DetectDecisionPoint();
    }

    private Dictionary<string, object?> DoLeaveRoom(Player player)
    {
        Log("Leaving room");
        if (_runState?.CurrentRoom is EventRoom)
        {
            var ev = RunManager.Instance.EventSynchronizer?.GetLocalEvent();
            if (ev is MegaCrit.Sts2.Core.Models.Events.FakeMerchant)
                _finishedEventAcked = ev;
            else if (ev != null && !ev.IsFinished)
                return Error(ev.CurrentOptions?.Any(o => !o.IsLocked) == true
                    ? "The event isn't over: choose one of its options"
                    : "The event isn't over and the game shows no way out (softlock); see force_leave_room");
            else
                _finishedEventAcked = ev;
        }
        // NRestSiteRoom enables Proceed only once an option has been used (or none is left).
        if (_runState?.CurrentRoom is RestSiteRoom restRoom && !ReferenceEquals(_restChoiceRoom, restRoom)
            && restRoom.Options?.Count > 0)
            return Error(restRoom.Options.Any(o => o.IsEnabled)
                ? "Choose a rest site option first: the rest site can be left only after one is used"
                : "Every rest site option is disabled and the game shows no Proceed (softlock); see force_leave_room");
        try { RunManager.Instance.ProceedFromTerminalRewardsScreen().GetAwaiter().GetResult(); }
        catch { }
        _syncCtx.Pump();
        WaitForActionExecutor();

        // If still in a non-combat room, force to map
        var room = _runState?.CurrentRoom;
        if (room is RestSiteRoom || room is MerchantRoom || room is EventRoom || room is TreasureRoom)
        {
            Log("Force leaving non-combat room to map");
            try
            {
                RunManager.Instance.EnterRoom(new MapRoom()).GetAwaiter().GetResult();
                _syncCtx.Pump();
                WaitForActionExecutor();
            }
            catch (Exception ex) { Log($"Force leave: {ex.Message}"); }
        }
        return DetectDecisionPoint();
    }

    private bool IsLastAct() =>
        _runState != null && _runState.CurrentActIndex >= _runState.Acts.Count - 1;

    /// <summary>Standing on the first boss of a double-boss act (NRewardsScreen.OnProceedButtonPressed).</summary>
    private bool AtFirstOfDoubleBoss()
    {
        var map = _runState?.Map;
        return map?.SecondBossMapPoint != null && _runState!.CurrentMapCoord == map.BossMapPoint.coord;
    }

    private Dictionary<string, object?> DoProceed(Player player)
    {
        Log("Proceeding");

        // Check if we need to move to next act (boss defeated)
        var room = _runState?.CurrentRoom;
        if (room is CombatRoom combatRoom && combatRoom.RoomType == RoomType.Boss)
        {
            if (combatRoom.IsPreFinished || !CombatManager.Instance.IsInProgress)
            {
                // Rewards left on the boss screen don't follow the player onwards.
                _pendingRewards = null;
                // Ascension 10 first boss: the map opens for the second boss (NRewardsScreen).
                if (AtFirstOfDoubleBoss())
                {
                    _rewardsProcessed = true;
                    ForceToMap();
                    return MapSelectState();
                }
                // No heal here: the next act's Ancient start node heals (AncientEventModel).
                // After the last act's boss EnterNextAct enters The Architect's event room
                // (RunManager.EnterNextAct); its Proceed wins the run (TheArchitect.WinRun).
                if (IsLastAct()) Log($"Final boss defeated via Proceed (Act {_runState!.CurrentActIndex + 1}), entering The Architect");
                RunManager.Instance.EnterNextAct().GetAwaiter().GetResult();
                _syncCtx.Pump();
                WaitForActionExecutor();
                return DetectDecisionPoint();
            }
        }

        RunManager.Instance.ProceedFromTerminalRewardsScreen().GetAwaiter().GetResult();
        WaitForActionExecutor();
        return DetectDecisionPoint();
    }

    #endregion

    #region Decision Point Detection

    private Dictionary<string, object?> DetectDecisionPoint()
    {
        if (_runState == null)
            return Error("No run in progress");

        var player = _runState.Players[0];

        // Check game over (death). RunManager.WinRun also kills the player, in The Architect's
        // room: that is the victory.
        if (player.Creature != null && player.Creature.IsDead)
        {
            return GameOverState(WonAtArchitect());
        }

        // Crystal Sphere minigame waiting for cell clicks
        if (_crystalSphere != null)
            return CrystalSphereState();

        // Check if there's a pending bundle selection (Scroll Boxes: pick 1 of N packs)
        if (_pendingBundles != null && _pendingBundleTcs != null && !_pendingBundleTcs.Task.IsCompleted)
        {
            var bundles = _pendingBundles.Select((bundle, i) => new Dictionary<string, object?>
            {
                ["index"] = i,
                // NChooseABundleSelectionScreen: UpdateVisuals(PileType.None, Normal).
                ["cards"] = bundle.Select(card => ListCardInfo(card, PileType.None)).ToList(),
            }).ToList();

            return new Dictionary<string, object?>
            {
                ["type"] = "decision",
                ["decision"] = "bundle_select",
                ["context"] = RunContext(),
                ["bundles"] = bundles,
                ["player"] = PlayerSummary(player),
            };
        }

        // Check if there's a pending card reward from event (GetSelectedCardReward blocking)
        if (_cardSelector.HasPendingReward)
        {
            var rewardCards = _cardSelector.PendingRewardCards!;
            // NCardRewardSelectionScreen: UpdateVisuals(PileType.None, Normal).
            var cards = rewardCards.Select((cr, i) => IndexedCardInfo(cr.Card, i, PileType.None)).ToList();

            // Alternatives the game shows next to the cards (CardRewardAlternative.Generate):
            // Skip (absent under Draft), Reroll (Driftwood), Sacrifice (Pael's Wing).
            var alts = AlternativesInfo(_cardSelector.PendingRewardAlternatives);
            var postCombat = _pendingRewards != null && _runState.CurrentRoom is CombatRoom && !CombatManager.Instance.IsInProgress;
            var crState = new Dictionary<string, object?>
            {
                ["type"] = "decision",
                ["decision"] = "card_reward",
                ["context"] = RunContext(),
                ["cards"] = cards,
                ["can_skip"] = alts.Any(a => a["id"] as string == "Skip"),
                ["alternatives"] = alts,
                ["from_event"] = !postCombat,
                ["player"] = PlayerSummary(_runState!.Players[0]),
            };
            if (postCombat) crState["gold_earned"] = player.Gold - _goldBeforeCombat;
            return crState;
        }

        // Check if there's a pending card selection (upgrade, remove, transform, start-of-turn powers)
        checkCardSelect:
        if (_cardSelector.HasPending && _cardSelector.PendingOptions != null)
        {
            // Grid selections (NDeckCardSelectScreen, smith, ...) draw each card through
            // UpdateVisuals, so enchantment-modified values show.
            var opts = _cardSelector.PendingOptions.Select((card, i) => IndexedCardInfo(card, i)).ToList();

            var selectState = new Dictionary<string, object?>
            {
                ["type"] = "decision",
                ["decision"] = "card_select",
                ["context"] = RunContext(),
                ["cards"] = opts,
                ["min_select"] = _cardSelector.PendingMinSelect,
                ["max_select"] = _cardSelector.PendingMaxSelect,
                ["player"] = PlayerSummary(player),
            };
            AugmentCardSelect(selectState, player);
            return selectState;
        }

        // Rewards offered by an event or a relic pickup (RewardsSet.Offer)
        if (EventRewardsOpen)
            return EventRewardsState(player);

        // Check if RunManager reports game over (victory)
        if (RunManager.Instance.IsGameOver)
        {
            return GameOverState(true);
        }

        var room = _runState.CurrentRoom;

        // Map room — need to select a node
        if (room is MapRoom || room == null)
        {
            return MapSelectState();
        }

        // Combat room
        if (room is CombatRoom combatRoom)
        {
            // With Task.Yield() patched, combat init should be synchronous
            _syncCtx.Pump();
            WaitForActionExecutor();

            // Re-check for pending card selections AFTER pump (BUG-024: start-of-turn effects
            // like Tools of Trade create card selections during Pump, AFTER the initial HasPending check)
            if (_cardSelector.HasPending && _cardSelector.PendingOptions != null)
            {
                goto checkCardSelect;  // Jump back to card_select handling
            }

            if (CombatManager.Instance.IsInProgress && IsPlayPhase())
            {
                return CombatPlayState(player);
            }
            if (!CombatManager.Instance.IsInProgress || (player.Creature != null && player.Creature.IsDead))
            {
                return DetectPostCombatState(player, combatRoom);
            }
            // Fallback: brief wait
            for (int i = 0; i < 20; i++)
            {
                _syncCtx.Pump();
                Thread.Sleep(5);
                if (IsPlayPhase()) return CombatPlayState(player);
                if (!CombatManager.Instance.IsInProgress) return DetectPostCombatState(player, combatRoom);
            }
            return CombatPlayState(player);
        }

        // Event room
        if (room is EventRoom eventRoom)
        {
            return EventChoiceState(eventRoom);
        }

        // Rest site
        if (room is RestSiteRoom restRoom)
        {
            return RestSiteState(restRoom);
        }

        // Merchant/Shop
        if (room is MerchantRoom merchantRoom)
        {
            return ShopState(merchantRoom, player);
        }

        // Treasure room
        if (room is TreasureRoom treasureRoom)
        {
            return TreasureState(treasureRoom);
        }

        // Fallback
        return new Dictionary<string, object?>
        {
            ["type"] = "decision",
            ["decision"] = "unknown",
            ["context"] = RunContext(),
            ["room_type"] = room?.GetType().Name,
            ["message"] = "Unknown room type or state",
        };
    }

    private Dictionary<string, object?> MapSelectState()
    {
        var map = _runState?.Map;
        if (map == null)
        {
            Log("Map is null, generating...");
            try
            {
                RunManager.Instance.GenerateMap().GetAwaiter().GetResult();
                _syncCtx.Pump();
                map = _runState?.Map;
            }
            catch (Exception ex)
            {
                Log($"GenerateMap failed: {ex.Message}");
            }
            if (map == null)
                return Error("No map available");
        }
        // Same travelability rules as the game's map screen (NMapScreen.RecalculateTravelability):
        // the start/Ancient node first, then the current node's children — or the whole next
        // row while free travel (Winged Boots, Flight) is on — then the boss, then the second boss.
        var currentPoint = _runState!.CurrentMapPoint;
        var choices = TravelablePoints(map)
            .Select(p =>
            {
                var c = new Dictionary<string, object?>
                {
                    ["col"] = (int)p.coord.col,
                    ["row"] = (int)p.coord.row,
                    ["type"] = p.PointType.ToString(),
                };
                // Off-path move that spends a Winged Boots charge (WingedBoots.AfterRoomEntered).
                if (currentPoint?.Children != null && !currentPoint.Children.Contains(p))
                    c["free_travel"] = true;
                return c;
            })
            .ToList();

        var mapState = new Dictionary<string, object?>
        {
            ["type"] = "decision",
            ["decision"] = "map_select",
            ["context"] = RunContext(),
            ["choices"] = choices,
            ["player"] = PlayerSummary(_runState!.Players[0]),
            ["act"] = _runState.CurrentActIndex + 1,
            ["act_name"] = _loc.Act(_runState.Act?.Id.Entry ?? "OVERGROWTH"),
            ["floor"] = _runState.ActFloor,
        };
        try { if (MegaCrit.Sts2.Core.Hooks.Hook.ShouldAllowFreeTravel(_runState)) mapState["free_travel_available"] = true; }
        catch { }
        return mapState;
    }

    /// <summary>
    /// Map points the player may travel to now, computed exactly like
    /// NMapScreen.RecalculateTravelability (keyed on the last visited coord).
    /// </summary>
    private List<MapPoint> TravelablePoints(ActMap map)
    {
        var visited = _runState!.VisitedMapCoords;
        if (visited == null || visited.Count == 0)
            return new List<MapPoint> { map.StartingMapPoint };
        var last = visited[^1];
        if (map.SecondBossMapPoint != null && last == map.BossMapPoint.coord)
            return new List<MapPoint> { map.SecondBossMapPoint };
        if (map.SecondBossMapPoint != null && last == map.SecondBossMapPoint.coord)
            return new List<MapPoint>();
        if (last == map.BossMapPoint.coord)
            return new List<MapPoint>();
        if (last.row == map.GetRowCount() - 1)
            return new List<MapPoint> { map.BossMapPoint };
        var point = map.GetPoint(last);
        if (point == null)
        {
            Log($"Last visited coord ({last.col},{last.row}) not on the map, falling back to the starting point");
            return new List<MapPoint> { map.StartingMapPoint };
        }
        return MegaCrit.Sts2.Core.Map.MapTravel.GetTravelablePointsFrom(_runState, point)
            .Where(p => p != null).ToList();
    }

    /// <summary>
    /// Contents of a combat pile as the game's pile screen shows it (NCardPileScreen).
    /// Draw pile: grouped into {name, count, ...} and sorted by (rarity, id) like the game, so
    /// the draw order stays hidden. Discard/exhaust piles: one entry per card in pile order
    /// (count 1), since the game shows those in their real order.
    /// Upgraded cards are named "Strike+" so the information survives clients (like the
    /// HTTP bridge's --compact mode) that strip the "upgraded" field.
    /// </summary>
    private List<Dictionary<string, object?>> PileSummary(CardPile? pile)
    {
        var result = new List<Dictionary<string, object?>>();
        CardModel[] cards;
        try { cards = pile?.Cards?.ToArray() ?? Array.Empty<CardModel>(); }
        catch (Exception ex)
        {
            Log($"PileSummary snapshot failed: {ex.Message}");
            return result;
        }

        var pileType = pile?.Type ?? PileType.Draw;
        var grouped = pileType == PileType.Draw;
        if (grouped)
            cards = cards.Where(c => c != null)
                .OrderBy(c => c.Rarity).ThenBy(c => c.Id.Entry, StringComparer.Ordinal).ToArray();
        var groups = new Dictionary<string, Dictionary<string, object?>>();
        foreach (var c in cards)
        {
            if (c == null) continue;
            try
            {
                // Energy cost hooks are evaluated once per card (they're the expensive part).
                var ec = c.EnergyCost;
                int rawCost = ec == null || ec.CostsX ? 0 : ec.GetWithModifiers(CostModifiers.All);
                var cost = Math.Max(0, rawCost);
                var key = $"{c.Id}|{c.CurrentUpgradeLevel}|{cost}|{c.Enchantment?.Id}|{c.Affliction?.Id}";
                if (grouped && groups.TryGetValue(key, out var g))
                {
                    g["count"] = (int)g["count"]! + 1;
                    continue;
                }
                var kwSet = c.Keywords;
                var kws = kwSet?.Where(k => k != CardKeyword.None).Select(k => k.ToString()).ToList();
                g = new Dictionary<string, object?>
                {
                    ["name"] = CardName(c),
                    ["count"] = 1,
                    ["id"] = c.Id.ToString(),
                    ["cost"] = cost,
                    ["type"] = c.Type.ToString(),
                    ["upgraded"] = c.IsUpgraded,
                    ["description"] = PileCardText(c, pileType, kwSet, cost),
                };
                if (kws?.Count > 0) g["keywords"] = kws;
                AddCardMeta(g, c, kwSet, rawCost);
                if (c.Enchantment != null)
                    g["enchantment"] = _loc.Bilingual("enchantments", c.Enchantment.Id.Entry + ".title");
                if (c.Affliction != null)
                    g["affliction"] = _loc.Bilingual("afflictions", c.Affliction.Id.Entry + ".title");
                if (grouped) groups[key] = g;
                result.Add(g);
            }
            catch (Exception ex) { Log($"PileSummary card failed: {ex.Message}"); }
        }
        return result;
    }

    private Dictionary<string, object?> CombatPlayState(Player player)
    {
        var pcs = player.PlayerCombatState;
        var combatState = CombatManager.Instance.DebugOnlyGetState();

        // Track last known HP for accurate game_over reporting (BUG-005)
        if (player.Creature != null && player.Creature.CurrentHp > 0)
            _lastKnownHp = player.Creature.CurrentHp;

        // Targetable enemies in the same order play_card's AnyEnemy targeting uses, so
        // damage_by_target[i].target_index aligns with the target_index clients pass.
        var aliveEnemiesForTargeting = TargetableEnemies(combatState);

        var hand = (pcs?.Hand is { } handPile ? HandOrder.Cards(handPile) : null)?.Select((c, i) =>
        {
            // Export the *currently resolved* stat values, not the card base: refresh the
            // DynamicVar previews (mirrors NCard.UpdateVisuals) so damage reflects Strength/Weak,
            // block reflects Frail, calculateddamage reflects current Block, etc. ClearPreview
            // resets PreviewValue to BaseValue, and only damage/block/calculated vars override it,
            // so reading PreviewValue uniformly is safe. Issues #65 #69 #70 #71 #74 #75.
            var stats = new Dictionary<string, object?>();
            string? handText = null;
            IReadOnlySet<CardKeyword>? handKw = null;
            try { handKw = c.Keywords; } catch { }
            try
            {
                c.DynamicVars.ClearPreview();
                c.UpdateDynamicVarPreview(
                    MegaCrit.Sts2.Core.Entities.Cards.CardPreviewMode.Normal,
                    c.CurrentTarget, c.DynamicVars);
                foreach (var dv in c.DynamicVars.Values)
                {
                    stats[dv.Name.ToLowerInvariant()] = (int)dv.PreviewValue;
                }
                // NCard.UpdateVisuals previews the enchantment's vars too: its extra card text
                // (Adroit's "Gain X Block") shows Dexterity/Frail in hand.
                if (c.Enchantment != null)
                {
                    c.Enchantment.DynamicVars.ClearPreview();
                    c.UpdateDynamicVarPreview(MegaCrit.Sts2.Core.Entities.Cards.CardPreviewMode.Normal,
                        c.CurrentTarget, c.Enchantment.DynamicVars);
                }
                // Description with the same live values (Strength, Weak, ...), like the hand UI.
                handText = CardText(c, PileType.Hand, c.CurrentTarget, handKw);
                // Restore the live card to base state. UpdateDynamicVarPreview mutates the
                // card's preview (and for self-cost cards like Momentum Strike, leaving it in
                // preview state corrupts the subsequent PlayCardAction — card stays in hand).
                c.DynamicVars.ClearPreview();
                c.Enchantment?.DynamicVars.ClearPreview();
            }
            catch { try { c.DynamicVars.ClearPreview(); c.Enchantment?.DynamicVars.ClearPreview(); } catch { } }

            // Per-target resolved damage for attack cards: the scalar `stats` above use the
            // card's CurrentTarget, but a single value can't capture target-specific modifiers
            // (Vulnerable #60, Slow #77) or conditional hit counts (Dismantle #78, X-cost
            // Whirlwind #82). Re-run the preview per enemy via MultiCreatureTargeting, the same
            // path the game uses to draw multi-target previews, and read the resolved vars.
            List<Dictionary<string, object?>>? damageByTarget = null;
            var isOstyAttack = IsOstyAttack(c);
            if (c.Type == CardType.Attack && aliveEnemiesForTargeting.Count > 0
                && (c.TargetType == TargetType.AnyEnemy || c.TargetType == TargetType.AllEnemies
                    || c.TargetType == TargetType.RandomEnemy))
            {
                damageByTarget = new List<Dictionary<string, object?>>();
                for (int ti = 0; ti < aliveEnemiesForTargeting.Count; ti++)
                {
                    var tgt = aliveEnemiesForTargeting[ti];
                    try
                    {
                        c.DynamicVars.ClearPreview();
                        c.UpdateDynamicVarPreview(
                            MegaCrit.Sts2.Core.Entities.Cards.CardPreviewMode.MultiCreatureTargeting,
                            tgt, c.DynamicVars);
                        var tstats = new Dictionary<string, object?>();
                        foreach (var dv in c.DynamicVars.Values)
                            tstats[dv.Name.ToLowerInvariant()] = (int)dv.PreviewValue;

                        // Per-hit damage = calculateddamage (override cards), damage, or
                        // ostydamage (Osty attacks; its preview already uses Osty as dealer).
                        int? perHit = PerHitDamage(tstats, c);
                        // Hit count: CalculatedHits (Tear Asunder, Finisher, Flechettes...) if
                        // present, else an explicit `repeat` var, else for X-cost attacks the
                        // current X (= available energy / stars), e.g. Whirlwind (#82).
                        bool hasHitCount = false;
                        int repeat;
                        if (tstats.TryGetValue("calculatedhits", out var chv) && chv is int chi) { repeat = chi; hasHitCount = true; }
                        else repeat = tstats.TryGetValue("repeat", out var rv) && rv is int ri && ri > 0 ? ri : 1;
                        // X goes through Hook.ModifyXValue (Chemical X), as ResolveEnergyXValue does.
                        if (!hasHitCount && repeat == 1 && (c.EnergyCost?.CostsX == true || c.HasStarCostX) && pcs != null)
                            repeat = XValue(c, pcs) ?? repeat;
                        // Dismantle hits twice when the target is Vulnerable (#78). The doubled
                        // hit count lives in Dismantle.OnPlay, not in any DynamicVar preview or
                        // the Hook.ModifyAttackHitCount path (which needs an AttackCommand we
                        // don't have at preview time), so it's special-cased by card entry.
                        if (!hasHitCount && repeat == 1 && c.Id.Entry == "DISMANTLE" && tgt.Powers != null
                            && tgt.Powers.Any(p => p?.Id.Entry == "VULNERABLE_POWER"))
                            repeat = 2;

                        var row = new Dictionary<string, object?>
                        {
                            ["target_index"] = ti,
                            ["target_id"] = tgt.CombatId,
                            ["name"] = MonsterName(tgt.Monster),
                        };
                        if (perHit != null) row["damage"] = perHit;
                        if (isOstyAttack)
                        {
                            row["dealer"] = "osty";
                            if (player.IsOstyMissing) row["osty_missing"] = true;
                        }
                        // Replay / Echo Form / Burst ...: the whole card runs again on this target.
                        int plays = PlayCount(c, combatState, c.TargetType == TargetType.AnyEnemy ? tgt : null);
                        if (repeat > 1 || hasHitCount)
                            row["repeat"] = repeat;
                        if (plays > 1) row["plays"] = plays;
                        if ((repeat > 1 || hasHitCount || plays > 1) && perHit != null)
                            row["total_damage"] = perHit * repeat * plays;
                        damageByTarget.Add(row);
                    }
                    catch { }
                    finally { c.DynamicVars.ClearPreview(); }
                }
            }

            var cardInfo = new Dictionary<string, object?>
            {
                ["index"] = i,
                ["id"] = c.Id.ToString(),
                ["name"] = CardName(c),
                ["cost"] = c.EnergyCost?.GetResolved() ?? 0,
                ["type"] = c.Type.ToString(),
                ["rarity"] = c.Rarity.ToString(),
                ["can_play"] = false,
                ["target_type"] = c.TargetType.ToString(),
                ["stats"] = stats.Count > 0 ? stats : null,
                ["description"] = handText ?? CardText(c, PileType.Hand, c.CurrentTarget),
                ["tips"] = Tips(() => c.HoverTips, _loc.Card(c.Id.Entry)),
            };
            // star_cost is GetStarCostWithModifiers (Void Form / Brilliant Scarf make it 0), and
            // can_play is the game's CanPlay, which checks stars via HasEnoughResourcesFor.
            AddCardMeta(cardInfo, c, handKw);
            AddHandPlayability(cardInfo, c, pcs);
            var playCount = PlayCount(c, combatState, null);
            if (playCount > 1) cardInfo["play_count"] = playCount;
            var kws = (handKw ?? c.Keywords)?.Where(k => k != CardKeyword.None).Select(k => k.ToString()).ToList();
            if (kws?.Count > 0) cardInfo["keywords"] = kws;
            if (c.Enchantment != null)
            {
                cardInfo["enchantment"] = _loc.Bilingual("enchantments", c.Enchantment.Id.Entry + ".title");
                try { if (c.Enchantment.Amount != 0) cardInfo["enchantment_amount"] = c.Enchantment.Amount; } catch { }
            }
            if (c.Affliction != null)
            {
                cardInfo["affliction"] = _loc.Bilingual("afflictions", c.Affliction.Id.Entry + ".title");
                try { if (c.Affliction.Amount != 0) cardInfo["affliction_amount"] = c.Affliction.Amount; } catch { }
            }
            if (damageByTarget != null && damageByTarget.Count > 0)
                cardInfo["damage_by_target"] = damageByTarget;
            return cardInfo;
        }).ToList() ?? new();

        var playerCreatures = combatState?.PlayerCreatures?.ToList();

        // Kaiser Crab: which way the player faces decides which crab is behind (x1.5 damage).
        SurroundedPower? surrounded = null;
        try { surrounded = player.Creature?.GetPower<SurroundedPower>(); } catch { }

        Dictionary<string, object?> EnemyInfo(Creature e, int? i)
        {
                // Extract detailed intent info
                var intents = new List<Dictionary<string, object?>>();
                try
                {
                    if (e.Monster?.NextMove?.Intents != null)
                    {
                        foreach (var intent in e.Monster.NextMove.Intents)
                        {
                            var intentInfo = new Dictionary<string, object?>
                            {
                                ["type"] = intent.IntentType.ToString(),
                            };
                            // Get damage for attack intents
                            if (intent is MegaCrit.Sts2.Core.MonsterMoves.Intents.AttackIntent atk && playerCreatures != null)
                            {
                                try
                                {
                                    // For multi-hit attacks, expose per-hit `damage` (matching the
                                    // game's intent description, which pairs GetSingleDamage with
                                    // Repeat) plus an explicit `total_damage`. Reporting the total in
                                    // `damage` while also reporting `hits` let clients compute
                                    // damage*hits and double-count incoming damage (#67).
                                    var hits = atk.Repeats;
                                    if (hits > 1)
                                    {
                                        intentInfo["damage"] = atk.GetSingleDamage(playerCreatures, e);
                                        intentInfo["hits"] = hits;
                                        intentInfo["total_damage"] = atk.GetTotalDamage(playerCreatures, e);
                                    }
                                    else
                                    {
                                        intentInfo["damage"] = atk.GetTotalDamage(playerCreatures, e);
                                    }
                                }
                                catch { }
                            }
                            if (playerCreatures != null) AddIntentDetails(intentInfo, intent, playerCreatures, e);
                            intents.Add(intentInfo);
                        }
                    }
                }
                catch { }

                var info = new Dictionary<string, object?>
                {
                    ["index"] = i,
                    ["combat_id"] = e.CombatId,
                    ["monster_id"] = e.Monster?.Id.Entry,
                    ["name"] = MonsterName(e.Monster),
                    ["hp"] = e.CurrentHp,
                    ["max_hp"] = e.MaxHp,
                    ["block"] = e.Block,
                    ["intents"] = intents.Count > 0 ? intents : null,
                    ["intends_attack"] = e.Monster?.IntendsToAttack ?? false,
                    ["powers"] = SerializePowers(e),
                };
                if (BehindPlayer(surrounded, e) is bool behind) info["behind_player"] = behind;
                return info;
        }

        // `enemies` = targetable enemies (index = target_index). Enemies still in combat but not
        // targetable (a Decimillipede segment waiting to reattach) go to `inactive_enemies` with
        // their pending move, like the bodies the game keeps on screen.
        var enemies = aliveEnemiesForTargeting.Select((e, i) => EnemyInfo(e, i)).ToList();
        var inactiveEnemies = combatState?.Enemies?
            .Where(e => e != null && !aliveEnemiesForTargeting.Contains(e))
            .Select(e =>
            {
                var d = EnemyInfo(e, null);
                d.Remove("index");
                d["alive"] = e.IsAlive;
                d["reviving"] = e.IsDead;
                return d;
            }).ToList();

        // Player powers/buffs
        var playerPowers = SerializePowers(player.Creature);

        var result = new Dictionary<string, object?>
        {
            ["type"] = "decision",
            ["decision"] = "combat_play",
            ["context"] = RunContext(),
            ["round"] = combatState?.RoundNumber ?? 0,
            ["energy"] = pcs?.Energy ?? 0,
            ["max_energy"] = pcs?.MaxEnergy ?? 0,
            ["hand"] = hand,
            ["enemies"] = enemies,
            ["player"] = PlayerSummary(player),
            ["player_powers"] = playerPowers,
            ["draw_pile_count"] = pcs?.DrawPile?.Cards?.Count ?? 0,
            ["discard_pile_count"] = pcs?.DiscardPile?.Cards?.Count ?? 0,
            ["exhaust_pile_count"] = pcs?.ExhaustPile?.Cards?.Count ?? 0,
            ["draw_pile"] = PileSummary(pcs?.DrawPile),
            ["discard_pile"] = PileSummary(pcs?.DiscardPile),
            ["exhaust_pile"] = PileSummary(pcs?.ExhaustPile),
        };
        if (inactiveEnemies?.Count > 0) result["inactive_enemies"] = inactiveEnemies;
        if (surrounded != null) result["player_facing"] = surrounded.Facing.ToString();

        // Character-specific mechanics
        try
        {
            // Defect: Orbs. Shown whenever the player has orb slots (the game draws empty
            // slots too). Index 0 is the rightmost orb, evoked first (OrbCmd.EvokeNext);
            // the last index is the newest.
            var orbQueue = pcs?.OrbQueue;
            if (orbQueue != null && (orbQueue.Capacity > 0 || orbQueue.Orbs?.Count > 0))
            {
                result["orbs"] = (orbQueue.Orbs ?? new List<OrbModel>()).Select((orb, i) => new Dictionary<string, object?>
                {
                    ["index"] = i,
                    ["name"] = _loc.Bilingual("orbs", orb.Id.Entry + ".title"),
                    ["type"] = orb.GetType().Name.Replace("Orb", ""),
                    ["passive"] = (int)orb.PassiveVal,
                    ["evoke"] = (int)orb.EvokeVal,
                    ["description"] = OrbText(orb, player),
                    ["evokes_next"] = i == 0,
                }).ToList();
                result["orb_slots"] = orbQueue.Capacity;
            }

            // Regent: Stars
            if (pcs != null && pcs.Stars >= 0 && player.Character?.Id.Entry == "REGENT")
            {
                result["stars"] = pcs.Stars;
            }

            // Necrobinder: Osty (minion)
            var osty = player.Osty;
            if (osty != null)
            {
                result["osty"] = new Dictionary<string, object?>
                {
                    ["name"] = MonsterName(osty.Monster, "OSTY"),
                    ["hp"] = osty.CurrentHp,
                    ["max_hp"] = osty.MaxHp,
                    ["block"] = osty.Block,
                    ["alive"] = osty.IsAlive,
                    ["powers"] = SerializePowers(osty),
                };
            }
            else if (player.Character?.Id.Entry == "NECROBINDER")
            {
                result["osty"] = new Dictionary<string, object?> { ["alive"] = false };
            }
        }
        catch (Exception ex)
        {
            Log($"Character-specific data: {ex.Message}");
        }

        return result;
    }

    private Dictionary<string, object?> DetectPostCombatState(Player player, CombatRoom combatRoom)
    {
        Log($"Post-combat: RoomType={combatRoom.RoomType}, IsPreFinished={combatRoom.IsPreFinished}");
        _syncCtx.Pump();

        // Generate rewards manually instead of using TestMode auto-accept
        if (_pendingRewards == null && !_rewardsProcessed)
        {
            _goldBeforeCombat = player.Gold;
            _rewardDone.Clear();
            _rewardsProceeded = false;
            try
            {
                var rewardsSet = new RewardsSet(player).WithRewardsFromRoom(combatRoom);
                // build 23372702: GenerateWithoutOffering() now returns Task (void);
                // generated rewards live on rewardsSet.Rewards afterwards.
                rewardsSet.GenerateWithoutOffering().GetAwaiter().GetResult();
                _pendingRewards = rewardsSet.Rewards.ToList();
                _syncCtx.Pump();
            }
            catch (Exception ex)
            {
                // Never leave the room as if there had been no rewards: the player would lose
                // them without being asked.
                Log($"Generate rewards: {ex}");
                _pendingRewards = null;
                return MarkStuck($"combat rewards could not be generated: {ex.Message}");
            }
        }

        // The post-combat reward screen (NRewardsScreen, terminal) stays up until Proceed: every
        // reward is the player's to take or leave, and once all are taken the empty screen
        // keeps its Proceed button (potions can still be used or discarded there).
        if (_pendingRewards != null && !_rewardsProceeded)
            return RewardsState(player);

        // Proceed pressed — leave the room
        _pendingRewards = null;
        _rewardsProcessed = true;

        // Boss → next act; after the last act's boss EnterNextAct enters The Architect's
        // event room (RunManager.EnterNextAct), whose Proceed wins the run (TheArchitect.WinRun).
        if (combatRoom.RoomType == RoomType.Boss)
        {
            // Ascension 10: the first boss of the last act is followed by a second boss
            // (StandardActMap.SecondBossMapPoint); the run is only won after that one
            // (CombatManager sets WinTime only on the second boss point).
            if (AtFirstOfDoubleBoss())
            {
                Log("First of two final bosses defeated, opening the map for the second boss");
                ForceToMap();
                return MapSelectState();
            }
            Log(IsLastAct() ? "Final boss defeated, entering The Architect" : "Boss defeated, entering next act");
            try
            {
                // No heal here: the next act's Ancient start node heals (AncientEventModel).
                RunManager.Instance.EnterNextAct().GetAwaiter().GetResult();
                _syncCtx.Pump();
                WaitForActionExecutor();
            }
            catch (Exception ex) { Log($"EnterNextAct: {ex.Message}"); }
            return DetectDecisionPoint();
        }

        // An event fight that resumes its event (Battleworn Dummy): leaving the reward screen
        // goes back to the event (RunManager.ProceedFromTerminalRewardsScreen ->
        // ResumePreviousRoom), whose Resume shows the outcome and can offer rewards.
        if (combatRoom.ShouldResumeParentEventAfterCombat && _runState?.CurrentRoomCount > 1)
        {
            Log("Event combat over, resuming the event");
            if (!RunInBackground(() => RunManager.Instance.ProceedFromTerminalRewardsScreen()))
                WaitForActionExecutor();
            if (_runState?.CurrentRoom is EventRoom || HasPendingPrompt)
                return DetectDecisionPoint();
        }

        // Normal → go to map
        ForceToMap();
        return MapSelectState();
    }

    // ─── Event / relic reward sets ───
    // Events hand out rewards through RewardsSet.testSelector on their background flow. The
    // selector parks the set here and waits; claims run the game's own
    // RewardsSetSynchronizer.SelectLocalReward on a pool thread (a card reward opens the
    // usual card_reward prompt); proceed skips the rest and resumes the event.

    // Reward sets nest: claiming a relic whose pickup offers its own rewards (Small Capsule,
    // Lost Coffer, ...) opens a second screen on top of the first, as the game's
    // RewardsSetSynchronizer.rewardsStack does. Only the top set is shown and acted on.
    private sealed class EventRewardFrame
    {
        public required RewardsSet Set;
        public required List<Reward> Rewards;
        public readonly HashSet<int> Done = new();
        public Task<bool>? Claim;
        public int ClaimIndex = -1;
        public required TaskCompletionSource Tcs;
    }
    private readonly List<EventRewardFrame> _eventFrames = new();

    private EventRewardFrame? TopEventFrame
    {
        get { lock (_eventFrames) return _eventFrames.Count > 0 ? _eventFrames[^1] : null; }
    }
    private TaskCompletionSource? _eventRewardsTcs => TopEventFrame?.Tcs;
    private List<Reward>? _eventRewards => TopEventFrame?.Rewards;

    internal Task OfferEventRewards(RewardsSet set)
    {
        var frame = new EventRewardFrame { Set = set, Rewards = set.Rewards.ToList(), Tcs = new TaskCompletionSource() };
        int depth;
        lock (_eventFrames) { _eventFrames.Add(frame); depth = _eventFrames.Count; }
        Console.Error.WriteLine($"[SIM] Event rewards pending: {frame.Rewards.Count} (depth {depth})");
        return frame.Tcs.Task;
    }

    /// <summary>The event reward screen is showing (no claim prompt in front of it).</summary>
    private bool EventRewardsOpen =>
        TopEventFrame != null
        && !_cardSelector.HasPending && !_cardSelector.HasPendingReward && _pendingBundles == null
        && _crystalSphere == null;

    private void AwaitEventClaim()
    {
        var frame = TopEventFrame;
        var t = frame?.Claim;
        var sw = System.Diagnostics.Stopwatch.StartNew();
        // Also stop when the claim opened a nested reward set (a new frame on top).
        while (t != null && !t.IsCompleted && !_cardSelector.HasPending && !_cardSelector.HasPendingReward
               && _pendingBundles == null && ReferenceEquals(TopEventFrame, frame) && sw.ElapsedMilliseconds < 3000)
        {
            _syncCtx.Pump();
            WaitFlow(t);
        }
        if (t != null && t.IsFaulted)
        {
            Log($"Event reward claim failed: {t.Exception?.InnerException}");
            NoteFlowFault(t.Exception);
        }
        if (frame != null && t != null && t.IsCompleted && ReferenceEquals(frame.Claim, t))
        {
            // A reward the player backed out of (skipped card reward, cancelled removal) stays
            // on the screen, as NRewardButton only removes it when the selection succeeded.
            if (t.IsCompletedSuccessfully && !t.Result && frame.ClaimIndex >= 0)
                frame.Done.Remove(frame.ClaimIndex);
            frame.Claim = null;
            frame.ClaimIndex = -1;
        }
    }

    private Dictionary<string, object?> EventRewardsState(Player player)
    {
        AwaitEventClaim();
        var frame = TopEventFrame;
        if (frame == null) return DetectDecisionPoint();
        if (frame.Claim != null && !frame.Claim.IsCompleted && !HasPendingPrompt)
            Log("Event reward claim still running");
        MarkRewardsSeen(frame.Rewards);
        var rewards = new List<Dictionary<string, object?>>();
        for (int i = 0; i < frame.Rewards.Count; i++)
            if (!frame.Done.Contains(i)) rewards.Add(RewardInfo(frame.Rewards[i], i));
        if (rewards.Count == 0)
            return DoFinishEventRewards();  // everything claimed: leave the screen like the game does
        return new Dictionary<string, object?>
        {
            ["type"] = "decision",
            ["decision"] = "rewards",
            ["source"] = "event",
            ["context"] = RunContext(),
            ["rewards"] = rewards,
            // RewardsSet.WithSkippingDisallowed (Neow's Bones): Proceed stays disabled until
            // every reward is taken.
            ["can_skip"] = !frame.Set.DisallowSkipping,
            ["potion_slots_full"] = !player.HasOpenPotionSlots,
            ["player"] = PlayerSummary(player),
        };
    }

    private Dictionary<string, object?> DoClaimEventReward(Player player, Dictionary<string, object?>? args)
    {
        if (args == null || !args.ContainsKey("reward_index"))
            return Error("claim_reward requires 'reward_index'");
        var frame = TopEventFrame!;
        var idx = Convert.ToInt32(args["reward_index"]);
        if (idx < 0 || idx >= frame.Rewards.Count || frame.Done.Contains(idx))
            return Error($"Invalid reward index {idx}");
        var reward = frame.Rewards[idx];
        if (reward is MegaCrit.Sts2.Core.Rewards.PotionReward && !player.HasOpenPotionSlots)
            return Error("Potion slots are full: discard_potion first, or proceed to leave this potion");

        frame.Done.Add(idx);
        Log($"Claiming event reward {idx}: {reward.GetType().Name}");
        var synchronizer = RunManager.Instance.RewardsSetSynchronizer;
        frame.ClaimIndex = idx;
        frame.Claim = StartFlow(() => synchronizer.SelectLocalReward(reward));
        AwaitEventClaim();
        WaitForActionExecutor();
        return DetectDecisionPoint();
    }

    private Dictionary<string, object?> DoFinishEventRewards()
    {
        AwaitEventClaim();
        EventRewardFrame? frame;
        lock (_eventFrames)
        {
            frame = _eventFrames.Count > 0 ? _eventFrames[^1] : null;
            if (frame != null) _eventFrames.RemoveAt(_eventFrames.Count - 1);
        }
        try
        {
            var synchronizer = RunManager.Instance.RewardsSetSynchronizer;
            if (frame != null && !synchronizer.IsRewardsSetCompleted(frame.Set))
                synchronizer.SkipLocalRewardsSet();
        }
        catch (Exception ex) { Log($"Skip event rewards: {ex.Message}"); }
        Log("Left event rewards");
        var tcs = frame?.Tcs;
        if (TopEventFrame is { } outer)
        {
            // A nested set closed: its offer returns inside the outer claim's flow, which then
            // finishes; the outer screen comes back with what is left on it.
            ResumeFlow(tcs == null ? null : () => tcs.TrySetResult());
            AwaitEventClaim();
            WaitForActionExecutor();
            return DetectDecisionPoint();
        }
        ResumeFlow(tcs == null ? null : () => tcs.TrySetResult());
        AwaitBackground();
        WaitForActionExecutor();
        return DetectDecisionPoint();
    }

    // ─── Crystal Sphere minigame ───
    // The event's minigame logic lives in CrystalSphereMinigame; only its screen is UI. The
    // screen is replaced (LocPatches.CrystalSphereScreenPrefix) by capturing the minigame here
    // and offering a "crystal_sphere" decision; clicks go through the game's CellClicked.

    internal MegaCrit.Sts2.Core.Events.Custom.CrystalSphereEvent.CrystalSphereMinigame? _crystalSphere;

    private static string CrystalItemType(object? item) => item?.GetType().Name switch
    {
        "CrystalSphereRelic" => "relic",
        "CrystalSpherePotion" => "potion",
        "CrystalSphereCardReward" => "card",
        "CrystalSphereCurse" => "curse",
        "CrystalSphereGold" => "gold",
        var n => n?.Replace("CrystalSphere", "").ToLowerInvariant() ?? "unknown",
    };

    private Dictionary<string, object?> CrystalSphereState()
    {
        var grid = _crystalSphere!;
        int w = grid.cells.GetLength(0), h = grid.cells.GetLength(1);
        // One string per row: '?' hidden, '.' cleared and empty, otherwise the first letter of
        // the item type seen in that cleared cell (r/p/c/x/g) — what the real screen shows.
        var rows = new List<string>();
        var hidden = new List<List<int>>();
        var seen = new Dictionary<object, Dictionary<string, object?>>();
        for (int y = 0; y < h; y++)
        {
            var sb = new System.Text.StringBuilder();
            for (int x = 0; x < w; x++)
            {
                var cell = grid.cells[x, y];
                if (cell.IsHidden) { sb.Append('?'); hidden.Add(new List<int> { x, y }); continue; }
                if (cell.Item == null) { sb.Append('.'); continue; }
                var type = CrystalItemType(cell.Item);
                sb.Append(type == "curse" ? 'x' : type[0]);
                if (!seen.TryGetValue(cell.Item, out var seenItem))
                {
                    var item = cell.Item;
                    var covered = 0;
                    for (int i = 0; i < item.Size.X; i++)
                        for (int j = 0; j < item.Size.Y; j++)
                            if (grid.cells[item.Position.X + i, item.Position.Y + j].IsHidden) covered++;
                    // Under the fog the player sees only the uncovered tiles of an item, not its
                    // bounding box (NCrystalSphereMask), so the extent is given once it is all uncovered.
                    seenItem = new Dictionary<string, object?>
                    {
                        ["id"] = seen.Count,
                        ["type"] = type,
                        ["revealed_cells"] = new List<List<int>>(),
                        ["fully_revealed"] = covered == 0,
                    };
                    if (covered == 0)
                    {
                        seenItem["x"] = item.Position.X; seenItem["y"] = item.Position.Y;
                        seenItem["width"] = item.Size.X; seenItem["height"] = item.Size.Y;
                    }
                    seen[item] = seenItem;
                }
                ((List<List<int>>)seenItem["revealed_cells"]!).Add(new List<int> { x, y });
            }
            rows.Add(sb.ToString());
        }
        return new Dictionary<string, object?>
        {
            ["type"] = "decision",
            ["decision"] = "crystal_sphere",
            ["context"] = RunContext(),
            ["divinations_left"] = grid.DivinationCount,
            ["width"] = w,
            ["height"] = h,
            ["rows"] = rows,
            ["hidden_cells"] = hidden,
            ["items_seen"] = seen.Values.ToList(),
            ["tools"] = new List<string> { "big", "small" },
            ["current_tool"] = grid.CrystalSphereTool == MegaCrit.Sts2.Core.Events.Custom.CrystalSphereEvent.CrystalSphereMinigame.CrystalSphereToolType.Small ? "small" : "big",
            ["player"] = PlayerSummary(_runState!.Players[0]),
        };
    }

    private Dictionary<string, object?> DoCrystalSphereClick(Dictionary<string, object?>? args)
    {
        var grid = _crystalSphere;
        if (grid == null) return Error("No Crystal Sphere minigame in progress");
        if (args == null || !args.ContainsKey("x") || !args.ContainsKey("y"))
            return Error("crystal_sphere_click requires 'x', 'y' and optionally 'tool' (big|small)");
        int x = Convert.ToInt32(args["x"]), y = Convert.ToInt32(args["y"]);
        // No tool given: the one currently selected in the minigame (the game starts on Big and
        // keeps the last tool picked), as a click without touching the tool buttons would.
        var toolName = args.GetValueOrDefault("tool")?.ToString()?.ToLowerInvariant()
                       ?? (grid.CrystalSphereTool == MegaCrit.Sts2.Core.Events.Custom.CrystalSphereEvent.CrystalSphereMinigame.CrystalSphereToolType.Small ? "small" : "big");
        if (toolName != "big" && toolName != "small") return Error($"Unknown tool '{toolName}' (big|small)");
        if (x < 0 || y < 0 || x >= grid.cells.GetLength(0) || y >= grid.cells.GetLength(1))
            return Error($"Cell ({x},{y}) is outside the {grid.cells.GetLength(0)}x{grid.cells.GetLength(1)} grid");
        if (!grid.cells[x, y].IsHidden) return Error($"Cell ({x},{y}) is already cleared");

        grid.SetTool(toolName == "small"
            ? MegaCrit.Sts2.Core.Events.Custom.CrystalSphereEvent.CrystalSphereMinigame.CrystalSphereToolType.Small
            : MegaCrit.Sts2.Core.Events.Custom.CrystalSphereEvent.CrystalSphereMinigame.CrystalSphereToolType.Big);
        Log($"Crystal Sphere: {toolName} at ({x},{y}), {grid.DivinationCount} divinations left");

        // The last click completes the minigame, which resumes the event flow (rewards) on this
        // task; run it off the main thread so a reward prompt there can't block us.
        var cell = grid.cells[x, y];
        var click = StartFlow(() => grid.CellClicked(cell));
        var sw = System.Diagnostics.Stopwatch.StartNew();
        while (!click.IsCompleted && sw.ElapsedMilliseconds < 3000)
        {
            _syncCtx.Pump();
            if (_cardSelector.HasPending || _cardSelector.HasPendingReward || _pendingBundles != null) break;
            WaitFlow(click);
        }
        if (click.IsFaulted)
            Log($"Crystal Sphere click failed: {click.Exception?.InnerException}");
        if (grid.DivinationCount <= 0)
        {
            _crystalSphere = null;
            AwaitBackground();  // the event flow: rewards, then the event's final page
        }
        WaitForActionExecutor();
        return DetectDecisionPoint();
    }

    // ─── Reward screens ───
    // Post-combat rewards, treasure relics and event/relic reward sets are always offered as
    // decisions, as NRewardsScreen / NTreasureRoom do. (start_run/load_save still accept the
    // old "manual_rewards" flag, which no longer changes anything.)

    // Indices into _pendingRewards already claimed, skipped, or handed to the card reward screen.
    private readonly HashSet<int> _rewardDone = new();

    /// <summary>The post-combat reward screen is open (combat over, Proceed not pressed yet).</summary>
    private bool RewardsScreenOpen =>
        _pendingRewards != null && !_rewardsProceeded
        && _runState?.CurrentRoom is CombatRoom && !CombatManager.Instance.IsInProgress
        && !HasPendingPrompt;

    private Dictionary<string, object?> RelicInfo(RelicModel r, int? index = null)
    {
        var vars = new Dictionary<string, object?>();
        try { foreach (var dv in r.DynamicVars.Values) vars[dv.Name] = (int)dv.BaseValue; } catch { }
        var info = new Dictionary<string, object?>();
        if (index != null) info["index"] = index;
        info["id"] = r.Id.Entry;
        info["name"] = RelicName(r);
        info["rarity"] = r.Rarity.ToString();
        info["description"] = RelicText(r);
        var relicTips = Tips(() => RelicExtraTips?.GetValue(r) as IEnumerable<MegaCrit.Sts2.Core.HoverTips.IHoverTip>);
        if (relicTips != null) info["tips"] = relicTips;
        info["vars"] = vars.Count > 0 ? vars : null;
        AddRelicState(info, r);
        return info;
    }

    /// <summary>
    /// What the relic icon shows besides its art (NRelicInventoryHolder / NRelic): the counter
    /// (ShowCounter/DisplayAmount, e.g. Pen Nib's attack count), the glow/greyed status, and
    /// the used-up / melted / wax markers. Only non-default values are emitted.
    /// </summary>
    private static void AddRelicState(Dictionary<string, object?> d, RelicModel r)
    {
        try { if (r.ShowCounter) d["counter"] = r.DisplayAmount; } catch { }
        try { if (r.Status != MegaCrit.Sts2.Core.Entities.Relics.RelicStatus.Normal) d["status"] = r.Status.ToString(); } catch { }
        try { if (r.IsUsedUp) d["used_up"] = true; } catch { }
        try { if (r.IsMelted) d["melted"] = true; } catch { }
        try { if (r.IsWax) d["wax"] = true; } catch { }
        try { if (r.StackCount > 1) d["stack_count"] = r.StackCount; } catch { }
    }

    private Dictionary<string, object?> RewardInfo(Reward reward, int index)
    {
        var info = new Dictionary<string, object?> { ["index"] = index };
        switch (reward)
        {
            case GoldReward g:
                info["type"] = "gold";
                info["amount"] = g.Amount;
                break;
            case MegaCrit.Sts2.Core.Rewards.RelicReward rr:
                info["type"] = "relic";
                if (rr.Relic != null)
                    foreach (var kv in RelicInfo(rr.Relic)) info[kv.Key] = kv.Value;
                break;
            case MegaCrit.Sts2.Core.Rewards.PotionReward pr:
                info["type"] = "potion";
                if (pr.Potion != null)
                    foreach (var kv in PotionInfo(pr.Potion)) info[kv.Key] = kv.Value;
                break;
            case CardReward cr:
                info["type"] = "card";
                info["card_count"] = cr.Cards?.Count() ?? 0;
                info["can_skip"] = cr.CanSkip;
                break;
            default:
                info["type"] = reward.GetType().Name;
                break;
        }
        AddSpecialRewardInfo(reward, info);
        return info;
    }

    /// <summary>NRewardsScreen marks each reward it shows as seen (Reward.MarkContentAsSeen):
    /// relics and potions shown there count as discoveries on the game-over screen.</summary>
    private static void MarkRewardsSeen(IEnumerable<MegaCrit.Sts2.Core.Rewards.Reward> rewards)
    {
        foreach (var r in rewards)
        {
            try { r.MarkContentAsSeen(); } catch (Exception ex) { Console.Error.WriteLine($"[WARN] MarkContentAsSeen: {ex.Message}"); }
        }
    }

    private Dictionary<string, object?> RewardsState(Player player)
    {
        MarkRewardsSeen(_pendingRewards!);
        var rewards = new List<Dictionary<string, object?>>();
        for (int i = 0; i < _pendingRewards!.Count; i++)
            if (!_rewardDone.Contains(i)) rewards.Add(RewardInfo(_pendingRewards[i], i));
        return new Dictionary<string, object?>
        {
            ["type"] = "decision",
            ["decision"] = "rewards",
            ["context"] = RunContext(),
            ["rewards"] = rewards,
            ["potion_slots_full"] = !player.HasOpenPotionSlots,
            ["gold_earned"] = player.Gold - _goldBeforeCombat,
            ["player"] = PlayerSummary(player),
        };
    }

    private Dictionary<string, object?> DoClaimReward(Player player, Dictionary<string, object?>? args)
    {
        if (!RewardsScreenOpen)
            return Error("No rewards to claim");
        if (args == null || !args.ContainsKey("reward_index"))
            return Error("claim_reward requires 'reward_index'");
        var idx = Convert.ToInt32(args["reward_index"]);
        if (idx < 0 || idx >= _pendingRewards!.Count || _rewardDone.Contains(idx))
            return Error($"Invalid reward index {idx}");

        var reward = _pendingRewards[idx];
        if (reward is MegaCrit.Sts2.Core.Rewards.PotionReward && !player.HasOpenPotionSlots)
            return Error("Potion slots are full: discard_potion first, or proceed to leave this potion");

        _rewardDone.Add(idx);
        Log($"Claiming reward {idx}: {reward.GetType().Name}");
        return SelectRewardInBackground(reward, idx);
    }

    /// <summary>
    /// Take a reward through the game's own selection logic (Reward.SelectUnsynchronized), on a
    /// background flow so a card reward or a pickup prompt can pause it. On the reward screen a
    /// reward the player backs out of (Skip on a card reward, cancelled removal) stays claimable,
    /// as NRewardButton keeps it unless the selection succeeded.
    /// </summary>
    private Dictionary<string, object?> SelectRewardInBackground(Reward reward, int idx)
    {
        if (RunInBackground(async () =>
            {
                bool ok = false;
                try { ok = await reward.SelectUnsynchronized(); }
                finally { if (!ok) lock (_rewardDone) _rewardDone.Remove(idx); }
            }))
            return DetectDecisionPoint();
        WaitForActionExecutor();
        return DetectDecisionPoint();
    }

    private Dictionary<string, object?> DoSkipRemainingRewards()
    {
        for (int i = 0; i < _pendingRewards!.Count; i++)
        {
            if (!_rewardDone.Add(i)) continue;
            try { _pendingRewards[i].OnSkipped(); }
            catch (Exception ex) { Log($"Skip reward {i}: {ex.Message}"); }
        }
        _rewardsProceeded = true;
        Log("Proceeding from the rewards screen");
        return DetectDecisionPoint();
    }

    private Dictionary<string, object?> DoTreasureChoice(Dictionary<string, object?>? args, bool skip)
    {
        if (!TreasureChoicePending(out var offered) || _runState?.CurrentRoom is not TreasureRoom room)
            return Error("No treasure relic to choose");
        if (skip)
            return FinishTreasure(room, relicIndex: null);
        if (args == null || !args.ContainsKey("relic_index"))
            return Error("select_treasure_relic requires 'relic_index'");
        var idx = Convert.ToInt32(args["relic_index"]);
        if (idx < 0 || idx >= offered!.Count)
            return Error($"Invalid relic index {idx}, {offered!.Count} relics offered");
        return FinishTreasure(room, relicIndex: idx);
    }

    /// <summary>
    /// While a reward screen or treasure choice is open, only its own actions (and potion
    /// management) are valid; anything else would leave the screen half-handled.
    /// </summary>
    private Dictionary<string, object?>? RouteRewardScreenAction(Player player, string action, Dictionary<string, object?>? args)
    {
        if (EventRewardsOpen)
        {
            switch (action)
            {
                case "claim_reward": return DoClaimEventReward(player, args);
                case "proceed":
                case "leave_room":
                case "skip_rewards":
                    if (TopEventFrame?.Set.DisallowSkipping == true)
                        return Error("These rewards can't be skipped: claim every reward to continue");
                    return DoFinishEventRewards();
                case "use_potion":
                case "discard_potion": return null;
                default: return Error($"'{action}' is not available on the rewards screen: use claim_reward or proceed");
            }
        }

        // The Crystal Sphere minigame must be played out: leaving would strand the event flow
        // that is waiting on it.
        if (_crystalSphere != null)
            return action is "crystal_sphere_click" or "use_potion" or "discard_potion"
                ? null
                : Error($"'{action}' is not available during the Crystal Sphere minigame: use crystal_sphere_click");

        if (RewardsScreenOpen)
        {
            switch (action)
            {
                case "claim_reward": return DoClaimReward(player, args);
                case "proceed":
                case "leave_room":
                case "skip_rewards": return DoSkipRemainingRewards();
                case "use_potion":
                case "discard_potion": return null;
                default: return Error($"'{action}' is not available on the rewards screen: use claim_reward or proceed");
            }
        }
        if (TreasureChoicePending(out _))
        {
            switch (action)
            {
                case "select_treasure_relic": return DoTreasureChoice(args, skip: false);
                case "skip_treasure_relic":
                case "proceed":
                case "leave_room": return DoTreasureChoice(args, skip: true);
                case "use_potion":
                case "discard_potion": return null;
                default: return Error($"'{action}' is not available while choosing a treasure relic: use select_treasure_relic or skip_treasure_relic");
            }
        }
        // A card reward (or a prompt) opened from the rewards screen is showing: leaving now
        // would drop the remaining rewards, so resolve it first.
        if (_pendingRewards != null && _runState?.CurrentRoom is CombatRoom
            && !CombatManager.Instance.IsInProgress
            && HasPendingPrompt
            && action is "proceed" or "leave_room" or "select_map_node")
            return Error($"'{action}' is not available yet: finish the current reward choice first");

        return action switch
        {
            "claim_reward" or "skip_rewards" => Error("No rewards to claim"),
            "select_treasure_relic" or "skip_treasure_relic" => Error("No treasure relic to choose"),
            _ => null,
        };
    }

    private void ForceToMap()
    {
        try
        {
            RunManager.Instance.ProceedFromTerminalRewardsScreen().GetAwaiter().GetResult();
            _syncCtx.Pump();
        }
        catch { }

        if (_runState?.CurrentRoom is not MapRoom)
        {
            try { RunManager.Instance.EnterRoom(new MapRoom()).GetAwaiter().GetResult(); _syncCtx.Pump(); }
            catch (Exception ex) { Log($"ForceToMap: {ex.Message}"); }
        }
    }

    private Dictionary<string, object?> EventChoiceState(EventRoom eventRoom)
    {
        var localEvent = RunManager.Instance.EventSynchronizer?.GetLocalEvent();
        _syncCtx.Pump();

        // Reset the choice-tracking flag once we re-export the event state. Earlier this
        // block force-finished events whose option count was unchanged, but that incorrectly
        // killed legitimate loops like Slippery Bridge Hold On (#59). Rely on IsFinished
        // instead and let DetectDecisionPoint show the current options for the next choice.
        if (_eventOptionChosen) _eventOptionChosen = false;

        // Fake Merchant: a custom-layout event (no options, IsFinished from the start) that is
        // really a small shop plus a Foul Potion fight.
        if (localEvent is MegaCrit.Sts2.Core.Models.Events.FakeMerchant fm && FakeMerchantOpen(fm))
            return FakeMerchantState(fm);

        // The Trial's abandon-run popup over the event page.
        if (AbandonPopupOpen(localEvent))
            return AbandonPopupState(localEvent!);

        // A finished event shows its outcome page with a Proceed button first (NEventRoom.SetOptions).
        if (localEvent != null && localEvent.IsFinished && !ReferenceEquals(_finishedEventAcked, localEvent))
            return FinishedEventState(localEvent);

        // If event is finished, proceed to map
        if (localEvent == null || localEvent.IsFinished)
        {
            Log($"Event {localEvent?.GetType().Name ?? "null"} finished, proceeding");
            try
            {
                RunManager.Instance.ProceedFromTerminalRewardsScreen().GetAwaiter().GetResult();
                _syncCtx.Pump();
            }
            catch { }
            // Force to map if still in event room
            if (_runState?.CurrentRoom is EventRoom)
            {
                try { RunManager.Instance.EnterRoom(new MapRoom()).GetAwaiter().GetResult(); _syncCtx.Pump(); }
                catch { }
            }
            return _runState?.CurrentRoom is MapRoom ? MapSelectState() : DetectDecisionPoint();
        }

        // An unfinished event without options is shown as it is (never skipped for the player);
        // the game has no button then either, so it is reported as a softlock.
        var currentOptions = localEvent.CurrentOptions ?? (IReadOnlyList<EventOption>)Array.Empty<EventOption>();
        if (currentOptions.Count == 0)
            Log($"[WARN] Event {localEvent.GetType().Name} is not finished but has no options");

        var options = currentOptions
            .Select((opt, i) =>
            {
                // The event's vars go into the option's text first, as NEventOptionButton does.
                AddEventVars(localEvent, opt);
                // Try to resolve title via loc tables
                string? title = null;
                if (opt.Title != null)
                {
                    var t = _loc.Bilingual(opt.Title.LocTable, opt.Title.LocEntryKey);
                    // Check if we actually found a translation (not just the key echoed back)
                    if (t != opt.Title.LocEntryKey)
                        title = t;
                }
                // Fallback: try to extract option ID from the key and look up as relic/card/potion
                if (title == null && opt.TextKey != null)
                {
                    // TextKey like "NEOW.pages.INITIAL.options.STONE_HUMIDIFIER" → extract "STONE_HUMIDIFIER"
                    var parts = opt.TextKey.Split('.');
                    var optionId = parts.Length > 0 ? parts[^1] : opt.TextKey;
                    // Try relic, then card, then just use the optionId
                    var relic = _loc.Relic(optionId);
                    if (relic != optionId + ".title")
                        title = relic;
                    else
                    {
                        var card = _loc.Card(optionId);
                        if (card != optionId + ".title")
                            title = card;
                        else
                            title = optionId.Replace("_", " ");
                    }
                }
                title ??= $"option_{i}";

                // Description: try loc table first
                string? optDesc = null;
                if (opt.Description != null && !string.IsNullOrEmpty(opt.Description.LocEntryKey))
                {
                    var d = _loc.Bilingual(opt.Description.LocTable, opt.Description.LocEntryKey);
                    if (d != opt.Description.LocEntryKey)
                        optDesc = d;
                }
                // Fallback: try relic/card description
                if (optDesc == null && opt.TextKey != null)
                {
                    var parts = opt.TextKey.Split('.');
                    var optionId = parts.Length > 0 ? parts[^1] : opt.TextKey;
                    var rd = _loc.Bilingual("relics", optionId + ".description");
                    if (rd != optionId + ".description")
                        optDesc = rd;
                }

                // Extract vars: try event's own DynamicVars first, then relic
                Dictionary<string, object?>? optVars = null;
                try
                {
                    // Event's DynamicVars (covers Gold, HpLoss, Heal, etc.)
                    if (localEvent.DynamicVars?.Values != null)
                    {
                        optVars = new Dictionary<string, object?>();
                        foreach (var dv in localEvent.DynamicVars.Values)
                            optVars[dv.Name] = VarValue(dv);
                    }
                }
                catch { }
                // Also try relic vars (for Neow options)
                if (opt.TextKey != null)
                {
                    try
                    {
                        var parts = opt.TextKey.Split('.');
                        var optionId = parts.Length > 0 ? parts[^1] : opt.TextKey;
                        // The option's own relic carries its rolled state (e.g. Dusty Tome's card).
                        var relicModel = opt.Relic ?? ModelDb.GetByIdOrNull<RelicModel>(new ModelId("RELIC", optionId))?.ToMutable();
                        if (relicModel != null)
                        {
                            optVars ??= new Dictionary<string, object?>();
                            foreach (var dv in relicModel.DynamicVars.Values)
                                optVars[dv.Name] = VarValue(dv);
                        }
                    }
                    catch { }
                }

                // `RandomCard` (Slippery Bridge / Overcome) carries a *deck index*, but the
                // description template `{RandomCard}` should render that deck card's name. Resolve
                // it to the localized name so clients can substitute it (#58). Scoped to this var
                // name on purpose: other card vars (e.g. Wood Carvings' BirdCard/ToricCard) index
                // a transform-target pool, not the deck, so a generic rule would mis-resolve them.
                if (optVars != null && optVars.TryGetValue("RandomCard", out var rcVal) && rcVal is int rcIdx)
                {
                    try
                    {
                        var deck = _runState?.Players?[0]?.Deck?.Cards;
                        if (deck != null && rcIdx >= 0 && rcIdx < deck.Count && deck[rcIdx] != null)
                            optVars["RandomCard"] = _loc.Card(deck[rcIdx].Id.Entry);
                    }
                    catch { }
                }

                // Numbers filled in, as on the real option button.
                if (title != null && title.Contains('{'))
                    title = TryFormat(() => opt.Title?.GetFormattedText()) ?? FillTemplate(title, optVars);
                if (optDesc != null && optDesc.Contains('{'))
                    optDesc = TryFormat(() => opt.Description?.GetFormattedText()) ?? FillTemplate(optDesc, optVars);

                var optInfo = new Dictionary<string, object?>
                {
                    ["index"] = i,
                    ["title"] = title,
                    ["description"] = optDesc,
                    ["text_key"] = opt.TextKey,
                    ["is_locked"] = opt.IsLocked,
                    ["vars"] = optVars?.Count > 0 ? optVars : null,
                };
                foreach (var kv in EventOptionExtras(localEvent, opt)) optInfo[kv.Key] = kv.Value;
                return optInfo;
            }).ToList();

        var state = new Dictionary<string, object?>
        {
            ["type"] = "decision",
            ["decision"] = "event_choice",
            ["context"] = RunContext(),
            ["event_name"] = EventName(localEvent),
            ["event_id"] = localEvent.Id?.Entry,
            ["description"] = EventDescriptionText(localEvent),
            ["options"] = options,
            ["player"] = PlayerSummary(_runState!.Players[0]),
        };
        // An ancient's opening dialogue (e.g. Neow's speech), shown above the first options.
        if (AncientDialogue(localEvent) is { } dialogue)
        {
            state["dialogue"] = dialogue;
            // The game draws the dialogue from Rng.Chaotic (cosmetic, unseeded): every line set
            // it could have shown for this profile and character.
            if (_dialoguePool != null)
            {
                state["dialogue_pool"] = _dialoguePool;
                state["dialogue_random"] = _dialoguePool.Count > 1;
            }
        }
        // The Architect's lines so far (speech bubbles), one per Respond / Continue.
        if (localEvent is MegaCrit.Sts2.Core.Models.Events.TheArchitect arch && ArchitectDialogue(arch) is { } archLines)
            state["dialogue"] = archLines;
        if (currentOptions.Count == 0)
            MarkSoftlock(state, "this event is not finished but shows no options");
        else if (currentOptions.All(o => o.IsLocked))
            MarkSoftlock(state, "every option of this event is locked");
        return state;
    }

    private Dictionary<string, object?> RestSiteState(RestSiteRoom restRoom)
    {
        var options = restRoom.Options;
        var player = _runState!.Players[0];

        // No options left (the synchronizer cleared them after the choice): the game hides the
        // choices and enables Proceed (NRestSiteRoom.AfterSelectingOptionAsync /
        // OnActiveScreenUpdated); the room is left only when the player presses it (leave_room).
        var optionList = (options ?? (IReadOnlyList<RestSiteOption>)Array.Empty<RestSiteOption>())
            .Select((opt, i) => RestOptionInfo(opt, i, player)).ToList();

        var state = new Dictionary<string, object?>
        {
            ["type"] = "decision",
            ["decision"] = "rest_site",
            ["context"] = RunContext(),
            ["options"] = optionList,
            // After an option is used the game shows Proceed (NRestSiteRoom.ShowProceedButton).
            ["can_leave"] = ReferenceEquals(_restChoiceRoom, restRoom) || optionList.Count == 0,
            ["player"] = PlayerSummary(player),
        };
        // Disabled options can't be clicked (NRestSiteButton) and Proceed isn't shown yet.
        if (state["can_leave"] is false && options!.All(o => !o.IsEnabled))
            MarkSoftlock(state, "every rest site option is disabled and Proceed is not shown before one is used");
        return state;
    }

    private Dictionary<string, object?> ShopState(MerchantRoom merchantRoom, Player player)
    {
        var inv = merchantRoom.GetLocalInventory();
        if (inv == null) { ForceToMap(); return MapSelectState(); }

        var cards = inv.CharacterCardEntries.Concat(inv.ColorlessCardEntries)
            .Select((e, i) =>
            {
                var card = e.CreationResult?.Card;
                var entry = card?.Id.Entry ?? "?";
                var stats = new Dictionary<string, object?>();
                int cardCost = 0;
                string? shopText = null;
                try
                {
                    if (card != null)
                    {
                        cardCost = card.EnergyCost?.GetResolved() ?? 0;
                        // NMerchantCard draws the card through UpdateVisuals (enchantment-modified values).
                        (shopText, stats) = PreviewedCard(card);
                    }
                    if (card != null && stats.Count == 0)
                    {
                        // The shop entry's card can have uninitialized DynamicVars (stats: null
                        // while after_upgrade is populated, #68). Read base stats from a fresh
                        // ModelDb clone at the card's current upgrade level, like GetUpgradedInfo.
                        var fresh = ModelDb.GetById<CardModel>(card.Id).ToMutable();
                        for (int u = 0; u < card.CurrentUpgradeLevel; u++)
                        {
                            fresh.UpgradeInternal();
                            fresh.FinalizeUpgradeInternal();
                        }
                        foreach (var dv in fresh.DynamicVars.Values)
                            stats[dv.Name.ToLowerInvariant()] = (int)dv.BaseValue;
                    }
                }
                catch { }
                var shopkws = card?.Keywords?.Where(k => k != CardKeyword.None).Select(k => k.ToString()).ToList();
                var shopCard = new Dictionary<string, object?>
                {
                    ["index"] = i,
                    ["id"] = card?.Id.ToString(),
                    ["name"] = card != null ? CardName(card) : null,
                    ["type"] = card?.Type.ToString(),
                    ["rarity"] = card?.Rarity.ToString(),
                    // The merchant's second row (MerchantInventory.ColorlessCardEntries).
                    ["colorless"] = i >= inv.CharacterCardEntries.Count,
                    ["card_cost"] = cardCost,
                    ["description"] = shopText ?? (card != null ? CardText(card) : null),
                    ["tips"] = card != null ? Tips(() => card.HoverTips, _loc.Card(card.Id.Entry)) : null,
                    ["stats"] = stats.Count > 0 ? stats : null,
                    ["keywords"] = shopkws?.Count > 0 ? shopkws : null,
                    ["after_upgrade"] = card != null ? GetUpgradedInfo(card) : null,
                    ["cost"] = e.Cost,
                    ["is_stocked"] = e.IsStocked,
                    ["on_sale"] = e.IsOnSale,
                };
                // The shop's "cost" is the gold price; the energy cost is "card_cost".
                if (card != null) { AddCardMeta(shopCard, card, energyCostKey: "card_cost"); AddCardMods(shopCard, card); }
                return shopCard;
            }).ToList();

        // A bought slot is empty in the game: sold-out entries keep their index with null fields.
        var relics = inv.RelicEntries.Select((e, i) =>
        {
            var d = e.Model != null ? RelicInfo(e.Model, i)
                : new Dictionary<string, object?> { ["index"] = i, ["name"] = null, ["description"] = null };
            d["cost"] = e.Cost;
            d["is_stocked"] = e.IsStocked;
            return d;
        }).ToList();

        var potions = inv.PotionEntries.Select((e, i) =>
        {
            var d = e.Model != null ? PotionInfo(e.Model, i)
                : new Dictionary<string, object?> { ["index"] = i, ["name"] = null, ["description"] = null };
            d["cost"] = e.Cost;
            d["is_stocked"] = e.IsStocked;
            return d;
        }).ToList();

        var removal = merchantRoom.GetLocalInventory().CardRemovalEntry;
        ApplyCardRemovalRules(removal, player);

        return new Dictionary<string, object?>
        {
            ["type"] = "decision",
            ["decision"] = "shop",
            ["context"] = RunContext(),
            ["cards"] = cards,
            ["relics"] = relics,
            ["potions"] = potions,
            ["card_removal_cost"] = removal?.Cost,
            // Offered once per visit (MerchantCardRemovalEntry.Used); the next shop costs more.
            ["card_removal_available"] = removal?.IsStocked ?? false,
            ["player"] = PlayerSummary(player),
        };
    }

    private Dictionary<string, object?> TreasureState(TreasureRoom treasureRoom)
    {
        if (ReferenceEquals(_treasureFinishPending, treasureRoom))
        {
            if (AwaitBackground())
                return DetectDecisionPoint();
            WaitForActionExecutor();
            return CompleteTreasureRoom(treasureRoom);
        }
        if (!ReferenceEquals(_treasureChestOpened, treasureRoom))
        {
            // NTreasureRoom.OpenChest: the chest's gold, then any extra rewards screen (relics
            // that add treasure rewards), then the relic choice. Opening is the room's only
            // action (travel stays disabled until the relic is resolved), so it is not a choice.
            _treasureChestOpened = treasureRoom;
            if (RunInBackground(async () =>
                {
                    await treasureRoom.DoNormalRewards();
                    await treasureRoom.DoExtraRewardsIfNeeded();
                }))
                return DetectDecisionPoint();
            WaitForActionExecutor();
        }
        if (TreasureChoicePending(out var offered))
        {
            return new Dictionary<string, object?>
            {
                ["type"] = "decision",
                ["decision"] = "treasure_relic",
                ["context"] = RunContext(),
                ["relics"] = offered!.Select((r, i) => RelicInfo(r, i)).ToList(),
                ["can_skip"] = true,
                ["player"] = PlayerSummary(_runState!.Players[0]),
            };
        }
        // Nothing to choose from: close the picking session (CompleteWithNoRelics) and leave.
        // Relics still on offer here (a prompt in front of them) must never be skipped for the
        // player.
        IReadOnlyList<RelicModel>? left = null;
        try { left = RunManager.Instance.TreasureRoomRelicSynchronizer?.CurrentRelics; } catch { }
        if (left is { Count: > 0 })
            return MarkStuck("treasure relics are on offer but no treasure_relic decision could be shown");
        return FinishTreasure(treasureRoom, relicIndex: null);
    }

    // The treasure room whose chest has been opened (gold and extra rewards handed out).
    private TreasureRoom? _treasureChestOpened;

    /// <summary>True when a treasure chest's relics await the player's choice.</summary>
    private bool TreasureChoicePending(out IReadOnlyList<RelicModel>? offered)
    {
        offered = null;
        if (_runState?.CurrentRoom is not TreasureRoom) return false;
        if (_treasureFinishPending != null || HasPendingPrompt) return false;  // already chosen
        try { offered = RunManager.Instance.TreasureRoomRelicSynchronizer?.CurrentRelics; }
        catch { return false; }
        return offered != null && offered.Count > 0;
    }

    /// <summary>Award the chosen chest relic (null = skip it), the room's other rewards, then go to the map.</summary>
    private Dictionary<string, object?> FinishTreasure(TreasureRoom treasureRoom, int? relicIndex)
    {
        // Treasure rooms give relics via TreasureRoomRelicSynchronizer
        Log("Treasure room — collecting rewards");

        WaitForActionExecutor();
        _syncCtx.Pump();

        // TreasureRoom.EnterInternal opens a relic-picking session (BeginRelicPicking) that the
        // headless must drive, otherwise (a) no relic is ever awarded and (b) the session stays
        // open so the NEXT treasure room's BeginRelicPicking throws "relic picking session while
        // one was already occurring!" (issue #56). PickRelicLocally (the player's choice) enqueues a
        // PickRelicAction whose execution awards the relic and ends the session (OnPicked ->
        // EndRelicVoting); SkipRelicLocally is the Skip button. An empty offer is closed with
        // CompleteWithNoRelics.
        try
        {
            var relicSync = RunManager.Instance.TreasureRoomRelicSynchronizer;
            if (relicSync?.CurrentRelics != null)
            {
                // The actual relic grant normally lives in the UI node's RelicsAwarded handler
                // (RelicCmd.Obtain per result), which is absent in headless. Capture the awarded
                // results, then grant them ourselves after the pick resolves (granting inside the
                // event — mid action-execution — risks re-entrancy).
                List<MegaCrit.Sts2.Core.Entities.TreasureRelicPicking.RelicPickingResult>? awarded = null;
                Action<List<MegaCrit.Sts2.Core.Entities.TreasureRelicPicking.RelicPickingResult>> capture = r => awarded = r;
                relicSync.RelicsAwarded += capture;
                try
                {
                    if (relicSync.CurrentRelics.Count > 0 && relicIndex == null)
                    {
                        Log("Skipping treasure relic");
                        relicSync.SkipRelicLocally();
                    }
                    else if (relicSync.CurrentRelics.Count > 0)
                    {
                        Log($"Picking treasure relic {relicIndex} of {relicSync.CurrentRelics.Count}");
                        relicSync.PickRelicLocally(relicIndex);
                    }
                    else
                    {
                        relicSync.CompleteWithNoRelics();
                    }
                    _syncCtx.Pump();
                    WaitForActionExecutor();
                    _syncCtx.Pump();
                }
                finally { relicSync.RelicsAwarded -= capture; }

                if (awarded != null)
                {
                    // On a background flow: a relic whose pickup opens a prompt (e.g. choose
                    // cards) would otherwise block the only thread that can answer it.
                    var grants = awarded.Where(res => res.relic != null && res.player != null).ToList();
                    if (RunInBackground(async () =>
                        {
                            foreach (var res in grants)
                                await RelicCmd.Obtain(res.relic!.ToMutable(), res.player!);
                        }))
                    {
                        _treasureFinishPending = treasureRoom;
                        return DetectDecisionPoint();
                    }
                    WaitForActionExecutor();
                    _syncCtx.Pump();
                }
            }
        }
        catch (Exception ex)
        {
            // Don't move on as if the pick had worked: the relic would be lost silently.
            Log($"Treasure relic pick: {ex}");
            return MarkStuck($"treasure relic could not be resolved: {ex.Message}");
        }

        return CompleteTreasureRoom(treasureRoom);
    }

    // Treasure room whose relic grant paused on a prompt; finished once the prompt resolves.
    private TreasureRoom? _treasureFinishPending;

    private Dictionary<string, object?> CompleteTreasureRoom(TreasureRoom treasureRoom)
    {
        // The chest's gold and extra rewards came when it was opened (TreasureState); with the
        // relic resolved, Proceed is the room's only button.
        _treasureFinishPending = null;
        ForceToMap();
        return MapSelectState();
    }

    private Dictionary<string, object?> GameOverState(bool isVictory)
    {
        var player = _runState!.Players[0];
        var summary = PlayerSummary(player);
        // BUG-005: When player died, the engine resets HP to max. Use last known HP instead.
        if (!isVictory)
            summary["hp"] = _lastKnownHp > 0 ? 0 : (player.Creature?.CurrentHp ?? 0);
        var state = new Dictionary<string, object?>
        {
            ["type"] = "decision",
            ["decision"] = "game_over",
            ["context"] = RunContext(),
            ["victory"] = isVictory,
            // Abandoned runs (The Trial's Double Down) end as a loss (RunManager.AbandonInternal).
            ["abandoned"] = RunManager.Instance.IsAbandoned,
            ["player"] = summary,
            ["act"] = _runState.CurrentActIndex + 1,
            ["floor"] = _runState.ActFloor,
        };
        // What NGameOverScreen shows: score (and its lines), floors climbed, what killed you.
        AddRunStats(state, isVictory);
        return state;
    }

    #endregion

    #region Helpers

    // Game flows that can open a selection prompt (event options, rest site, shop purchases)
    // run on a thread-pool task so they can block on the selector. The task is kept here
    // while the prompt is surfaced, so the follow-up command can wait for it to finish
    // instead of sleeping a fixed amount of time.
    private Task? _backgroundTask;
    // A background flow that outlived AwaitBackground's timeout (no prompt, still running).
    private Task? _detachedTask;

    private bool HasPendingPrompt =>
        _cardSelector.HasPending || _cardSelector.HasPendingReward || _pendingBundles != null
        || _crystalSphere != null || _eventRewardsTcs != null;

    /// <summary>
    /// Run <paramref name="work"/> on the thread pool and pump until it completes or opens a
    /// prompt. Returns true if a prompt is pending (the task keeps running in the background).
    /// </summary>
    private bool RunInBackground(Func<Task> work, int timeoutMs = 3000)
    {
        // Never run two game flows concurrently (game state isn't thread-safe).
        if (_backgroundTask != null && AwaitBackground(timeoutMs))
        {
            Log("Previous background flow is waiting on a prompt; not starting a new one");
            return true;
        }
        if (_detachedTask != null)
        {
            if (((IAsyncResult)_detachedTask).AsyncWaitHandle.WaitOne(timeoutMs))
                _detachedTask = null;
            else
                Log("A timed-out background flow is still running; starting a new one anyway");
        }
        _backgroundTask = StartFlow(work);
        return AwaitBackground(timeoutMs);
    }

    /// <summary>
    /// Resume a game flow that was waiting on a prompt. A flow started by RunInBackground
    /// captured the host context in its awaits, so completing its task on the main thread would
    /// run the rest of the flow right here (inline), unbounded by AwaitBackground's timeout and
    /// unable to block on a later card reward. Complete it from a pool thread instead and wait
    /// until it yields, finishes, or opens the next prompt. Flows that already ran on the main
    /// thread (combat actions, the turn loop) resume inline as before.
    /// </summary>
    private void ResumeFlow(Action? complete)
    {
        if (complete == null) return;
        if (_backgroundTask == null)
        {
            complete();
            _syncCtx.Pump();
            return;
        }
        var t = StartFlow(() => { complete(); return Task.CompletedTask; });
        var sw = System.Diagnostics.Stopwatch.StartNew();
        while (!t.IsCompleted && !HasPendingPrompt && sw.ElapsedMilliseconds < 3000)
            WaitFlow(t);
    }

    /// <summary>
    /// Start <paramref name="work"/> with the host sync context installed, so every async game
    /// method it starts (e.g. the turn loop of a combat entered from an event option) captures
    /// it. With a null context their later continuations (end turn, enemy turn, next draw) are
    /// queued to the thread pool and mutate combat state while the main thread serializes it
    /// ("Collection was modified", stale end_turn responses).
    /// </summary>
    private static Task RunWithHostContext(Func<Task> work)
    {
        var prev = SynchronizationContext.Current;
        SynchronizationContext.SetSynchronizationContext(_syncCtx);
        try { return work(); }
        finally { SynchronizationContext.SetSynchronizationContext(prev); }
    }

    /// <summary>
    /// Pump until the current background task completes or a prompt appears.
    /// Returns true if a prompt is pending.
    /// </summary>
    private bool AwaitBackground(int timeoutMs = 3000)
    {
        var task = _backgroundTask;
        var sw = System.Diagnostics.Stopwatch.StartNew();
        while (true)
        {
            _syncCtx.Pump();
            if (HasPendingPrompt) return true;
            if (task == null || task.IsCompleted) break;
            if (sw.ElapsedMilliseconds >= timeoutMs)
            {
                // Stop waiting on it in every command, but keep a reference so a new flow
                // or a run reset can still tell it is alive.
                Log($"Background task still running after {timeoutMs}ms; no longer waiting on it");
                if (ReferenceEquals(_backgroundTask, task))
                {
                    _detachedTask = task;
                    _backgroundTask = null;
                }
                return HasPendingPrompt;
            }
            // Wakes as soon as the task completes or parks; never throws on a faulted task.
            WaitFlow(task);
        }
        _syncCtx.Pump();
        if (task != null && task.IsCompleted)
        {
            if (task.IsFaulted)
            {
                Log($"Background task failed: {task.Exception?.InnerException}");
                NoteFlowFault(task.Exception);
            }
            if (ReferenceEquals(_backgroundTask, task)) _backgroundTask = null;
        }
        return HasPendingPrompt;
    }

    private void WaitForActionExecutor()
    {
        try
        {
            // Ensure sync context is set for this thread
            SynchronizationContext.SetSynchronizationContext(_syncCtx);

            // Pump the synchronization context to execute any pending continuations
            _syncCtx.Pump();

            // Executor may stay "running" while the game awaits headless card selection / reward (e.g. Attack Potion).
            // Spinning here would time out and downstream code could mis-handle an in-flight potion use (BUG-026).
            if (_cardSelector.HasPending || _cardSelector.HasPendingReward)
                return;

            var executor = RunManager.Instance.ActionExecutor;
            if (executor.IsRunning)
            {
                // Pump while waiting for executor
                int maxPumps = 1000;
                for (int i = 0; i < maxPumps; i++)
                {
                    _syncCtx.Pump();
                    if (!executor.IsRunning) break;
                    Thread.Sleep(1);
                }
            }
        }
        catch (Exception ex)
        {
            Log($"WaitForActionExecutor exception: {ex.Message}");
        }
    }

    private void SpinWaitForCombatStable()
    {
        int maxIterations = 200;
        for (int i = 0; i < maxIterations; i++)
        {
            _syncCtx.Pump();
            if (!CombatManager.Instance.IsInProgress) return;
            if (IsPlayPhase()) return;
            WaitForActionExecutor();
            if (IsPlayPhase() || !CombatManager.Instance.IsInProgress) return;
            Thread.Sleep(5);
        }
    }

    /// <summary>Compute what a card would look like after upgrading (stats + cost + description).</summary>
    private static int _cardTextFailures;

    /// <summary>
    /// Text formatted by the game (numbers filled in), or the raw localized template if the
    /// game's formatting fails or still contains placeholders.
    /// </summary>
    private static string GameText(Func<string?> format, string table, string key, string? cacheKey = null)
    {
        if (cacheKey != null && _textCache.TryGetValue(cacheKey, out var cached)) return cached;
        var prevRealText = LocPatches.RealText;  // nested formatting keeps the caller's mode
        try
        {
            LocPatches.RealText = true;
            var text = format();
            if (!string.IsNullOrWhiteSpace(text))
            {
                text = _loc.ReplaceIcons(LocLookup.Clean(_loc.ResolveKey(text) ?? text).Trim());
                if (!text.Contains('{'))
                {
                    if (cacheKey != null && _textCache.Count < 50000) _textCache[cacheKey] = text;
                    return text;
                }
            }
        }
        catch (Exception ex)
        {
            if (_cardTextFailures++ < 5) Console.Error.WriteLine($"[WARN] Text for {key}: {ex.Message}");
        }
        finally { LocPatches.RealText = prevRealText; }
        return _loc.ReplaceIcons(_loc.Bilingual(table, key));
    }

    /// <summary>Format a game LocString with its own variables; null if that doesn't fully work.</summary>
    private static string? TryFormat(Func<string?> format)
    {
        var prevRealText = LocPatches.RealText;  // nested formatting keeps the caller's mode
        try
        {
            LocPatches.RealText = true;
            var text = format();
            if (string.IsNullOrWhiteSpace(text)) return null;
            text = _loc.ReplaceIcons(LocLookup.Clean(_loc.ResolveKey(text) ?? text).Trim());
            // An unresolved key ("X_POWER.remoteDescription") is a failure too.
            if (text.Contains('{') || System.Text.RegularExpressions.Regex.IsMatch(text, @"^[A-Z0-9_]+\.[a-zA-Z_.]+$"))
                return null;
            return text;
        }
        catch { return null; }
        finally { LocPatches.RealText = prevRealText; }
    }

    /// <summary>
    /// Fill a localized template from known variables: {X}, {X:diff()}, {X:plural:one|many},
    /// {X:energyIcons()}. Placeholders it can't resolve are left as they are.
    /// </summary>
    private static string FillTemplate(string text, Dictionary<string, object?>? vars)
    {
        text = _loc.ReplaceIcons(text);
        if (vars == null || vars.Count == 0 || !text.Contains('{')) return text;
        return System.Text.RegularExpressions.Regex.Replace(text, @"\{(\w+)(?::(\w+)(?:\(\))?(?::([^{}]*))?)?\}", m =>
        {
            if (!vars.TryGetValue(m.Groups[1].Value, out var v) || v == null) return m.Value;
            var fmt = m.Groups[2].Value;
            if (fmt == "plural" && m.Groups[3].Success)
            {
                var forms = m.Groups[3].Value.Split('|');
                var n = Convert.ToDecimal(v);
                return (n == 1 ? forms[0] : forms[^1]).Replace("{}", n.ToString());
            }
            if (fmt == "energyIcons") return _loc.Lang == "zh" ? $"{v}点能量" : $"{v} Energy";
            return fmt is "" or "diff" ? v.ToString()! : m.Value;
        });
    }

    private static string RelicText(RelicModel r) =>
        GameText(() => r.HoverTip.Description, "relics", r.Id.Entry + ".description",
                 CacheKey(r, RelicTextState(r), r.DynamicVars.Values.Select(v => v.BaseValue)));

    // HoverTip wraps the description once the relic is used up or melted, so they key the text too.
    private static string RelicTextState(RelicModel r)
    {
        try { return $"relic|{r.IsUsedUp}|{r.IsMelted}"; } catch { return "relic"; }
    }

    private static string PotionText(PotionModel p) =>
        GameText(() => p.HoverTip.Description, "potions", p.Id.Entry + ".description",
                 CacheKey(p, "potion", p.DynamicVars.Values.Select(v => v.BaseValue)));

    private static readonly System.Reflection.MethodInfo? AddPowerVars = typeof(PowerModel).GetMethod(
        "AddDumbVariablesToDescription", System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.Public | System.Reflection.BindingFlags.NonPublic);

    private static string PowerText(PowerModel pw)
    {
        string owner;
        // Text can name the owner ("if Vine Shambler has 13 or less HP"), so key on it.
        try { owner = pw.Owner?.IsPlayer == true ? "player" : pw.Owner?.Monster?.Id.ToString() ?? "other"; } catch { owner = "?"; }
        var cacheKey = CacheKey(pw, $"power|{pw.Amount}|{owner}", pw.DynamicVars.Values.Select(v => v.BaseValue));
        if (_textCache.TryGetValue(cacheKey, out var cached)) return cached;

        // Format the power's description deterministically from its own variables (the hover
        // tips the game builds lazily depend on when they were first created).
        string? text = null;
        try
        {
            var ls = pw.HasSmartDescription ? pw.SmartDescription : pw.Description;
            AddPowerVars?.Invoke(pw, new object?[] { ls, null });
            text = TryFormat(() => ls.GetFormattedText());
            if (text == null)
            {
                var vars = new Dictionary<string, object>
                {
                    ["Amount"] = pw.Amount,
                    ["DisplayAmount"] = pw.Amount,
                    ["OnPlayer"] = pw.Owner?.IsPlayer == true,
                    ["IsMultiplayer"] = false,
                    ["OwnerName"] = pw.Owner?.Monster != null ? MonsterName(pw.Owner.Monster)
                                    : _loc.Bilingual("characters", (pw.Owner?.Player?.Character?.Id.Entry ?? "IRONCLAD") + ".title"),
                };
                foreach (var v in pw.DynamicVars.Values) vars[v.Name] = v;
                text = TryFormat(() => LocManager.Instance.SmartFormat(ls, vars));
            }
        }
        catch (Exception ex)
        {
            if (_cardTextFailures++ < 5) Console.Error.WriteLine($"[WARN] Power text {pw.Id.Entry}: {ex.Message}");
        }
        if (text != null)
        {
            if (_textCache.Count < 50000) _textCache[cacheKey] = text;
            return text;
        }
        // Fall back to the static text: no numbers, but never raw template syntax.
        var raw = _loc.Raw("powers", pw.Id.Entry + ".description");
        return raw != null ? FillTemplate(LocLookup.Clean(raw), new() { ["Amount"] = pw.Amount }) : _loc.Bilingual("powers", pw.Id.Entry + ".description");
    }
    private static bool IsBareKey(string text) =>
        System.Text.RegularExpressions.Regex.IsMatch(text, @"^[A-Z0-9_]+\.[a-zA-Z_.]+$");

    private static readonly System.Reflection.PropertyInfo? RelicExtraTips = typeof(RelicModel).GetProperty(
        "ExtraHoverTips", System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.Public | System.Reflection.BindingFlags.NonPublic);

    /// <summary>
    /// The secondary tooltips the game shows next to an item (keyword and power explanations
    /// such as Vulnerable or Exhaust), as [{title, description}], or null if there are none.
    /// </summary>
    private static List<Dictionary<string, object?>>? Tips(Func<IEnumerable<MegaCrit.Sts2.Core.HoverTips.IHoverTip>?> source, string? skipTitle = null)
    {
        var prevRealText = LocPatches.RealText;  // nested formatting keeps the caller's mode
        try
        {
            LocPatches.RealText = true;
            var list = new List<Dictionary<string, object?>>();
            var seen = new HashSet<string>();
            foreach (var tip in source() ?? Enumerable.Empty<MegaCrit.Sts2.Core.HoverTips.IHoverTip>())
            {
                if (tip is MegaCrit.Sts2.Core.HoverTips.CardHoverTip cht)
                {
                    // Card previews (the card an option/relic/card grants): name + its text.
                    var cardTip = CardTipInfo(cht);
                    if (cardTip != null && cardTip["title"] is string ct && ct != skipTitle && seen.Add(ct))
                        list.Add(cardTip);
                    continue;
                }
                if (tip is not MegaCrit.Sts2.Core.HoverTips.HoverTip ht) continue;
                var title = LocLookup.Clean(_loc.ResolveKey(ht.Title ?? "") ?? ht.Title ?? "").Trim();
                var desc = LocLookup.Clean(_loc.ResolveKey(ht.Description ?? "") ?? ht.Description ?? "").Trim();
                if (title.Length == 0 || desc.Length == 0 || title == skipTitle || desc.Contains('{')
                    || IsBareKey(title) || IsBareKey(desc) || !seen.Add(title))
                    continue;
                list.Add(new Dictionary<string, object?> { ["title"] = title, ["description"] = desc });
            }
            return list.Count > 0 ? list : null;
        }
        catch (Exception ex)
        {
            if (_cardTextFailures++ < 5) Console.Error.WriteLine($"[WARN] Tooltips: {ex.Message}");
            return null;
        }
        finally { LocPatches.RealText = prevRealText; }
    }

    /// <summary>
    /// Card description with numbers filled in, as the game renders it ("Deal 9 damage."
    /// rather than "Deal {Damage:diff()} damage."), using the card's live values (Strength,
    /// Weak, upgrades, ...). Falls back to the raw template if formatting fails.
    /// </summary>
    // Formatted text keyed by everything it depends on (model, upgrade, live values, language).
    private static readonly Dictionary<string, string> _textCache = new();

    private static string CacheKey(AbstractModel model, string extra, IEnumerable<decimal> values) =>
        $"{_loc.Lang}|{model.Id}|{extra}|{string.Join(",", values)}";

    private static string CardText(CardModel card, PileType pile = PileType.Deck, Creature? target = null,
        IReadOnlySet<CardKeyword>? keywords = null, int? resolvedCost = null)
    {
        string? key = null;
        try
        {
            // Everything GetDescriptionForPile reads: afflictions (Bound) and keywords added in
            // combat (Hex's Ethereal) change the text, as do replays. (Sly/Retain lines only
            // show for cards whose keyword set has them, so the set covers those too.)
            // Keywords in combat run hooks, so callers that already have them pass them in.
            long kws = 0;
            foreach (var k in keywords ?? card.Keywords) kws |= 1L << ((int)k & 63);
            key = CacheKey(card, $"{card.CurrentUpgradeLevel}|{pile}|{card.Enchantment?.Id}|{card.Enchantment?.Amount}|{resolvedCost ?? card.EnergyCost?.GetResolved()}" +
                $"|{card.Affliction?.Id}|{card.Affliction?.Amount}|{kws}|{card.GetEnchantedReplayCount()}" +
                $"|{CombatManager.Instance.IsInProgress}|{target != null}",
                card.Enchantment == null ? card.DynamicVars.Values.Select(v => v.PreviewValue)
                    : card.DynamicVars.Values.Select(v => v.PreviewValue).Append(-1m)
                        .Concat(card.Enchantment.DynamicVars.Values.Select(v => v.PreviewValue)));
            if (_textCache.TryGetValue(key, out var cached)) return cached;
        }
        catch { key = null; }
        var prevRealText = LocPatches.RealText;  // nested formatting keeps the caller's mode
        try
        {
            LocPatches.RealText = true;
            var text = card.GetDescriptionForPile(pile, target);
            if (!string.IsNullOrWhiteSpace(text))
            {
                text = _loc.ReplaceIcons(LocLookup.Clean(text).Trim());
                if (!text.Contains('{'))
                {
                    if (key != null && _textCache.Count < 50000) _textCache[key] = text;
                    return text;
                }
            }
        }
        catch (Exception ex)
        {
            if (_cardTextFailures++ < 5) Console.Error.WriteLine($"[WARN] Card text for {card.Id.Entry}: {ex.Message}");
        }
        finally { LocPatches.RealText = prevRealText; }
        return _loc.ReplaceIcons(_loc.Bilingual("cards", card.Id.Entry + ".description"));
    }

    private Dictionary<string, object?>? GetUpgradedInfo(CardModel card)
    {
        if (!card.IsUpgradable) return null;
        return CachedUpgradedInfo(card, ComputeUpgradedInfo);
    }

    private Dictionary<string, object?>? ComputeUpgradedInfo(CardModel card)
    {
        try
        {
            CardModel clone;
            var modded = card.Enchantment != null || card.Affliction != null;
            if (modded)
            {
                // The upgrade preview (NUpgradePreview / NDeckUpgradeSelectScreen) upgrades a
                // clone of the card itself, enchantment included. The clone is not added to the run.
                clone = (CardModel)card.ClonePreservingMutability();
            }
            else
            {
                clone = ModelDb.GetById<CardModel>(card.Id).ToMutable();
                // Apply existing upgrades first
                for (int i = 0; i < card.CurrentUpgradeLevel; i++)
                {
                    clone.UpgradeInternal();
                    clone.FinalizeUpgradeInternal();
                }
            }
            // Apply one more upgrade
            clone.UpgradeInternal();
            clone.FinalizeUpgradeInternal();

            var stats = new Dictionary<string, object?>();
            string? modText = null;
            if (modded)
                (modText, stats) = PreviewedCard(clone);
            else
                try { foreach (var dv in clone.DynamicVars.Values) stats[dv.Name.ToLowerInvariant()] = (int)dv.BaseValue; } catch { }

            // Compare keywords before/after upgrade
            var oldKws = card.Keywords?.Where(k => k != CardKeyword.None).Select(k => k.ToString()).ToHashSet() ?? new();
            var newKws = clone.Keywords?.Where(k => k != CardKeyword.None).Select(k => k.ToString()).ToHashSet() ?? new();
            var addedKws = newKws.Except(oldKws).ToList();
            var removedKws = oldKws.Except(newKws).ToList();

            var upInfo = new Dictionary<string, object?>
            {
                ["cost"] = clone.EnergyCost?.GetResolved() ?? 0,
                ["stats"] = stats.Count > 0 ? stats : null,
                ["description"] = modText ?? CardText(clone),
                ["added_keywords"] = addedKws.Count > 0 ? addedKws : null,
                ["removed_keywords"] = removedKws.Count > 0 ? removedKws : null,
            };
            try
            {
                if (clone.EnergyCost?.CostsX == true) { upInfo["costs_x"] = true; upInfo["cost"] = 0; }
                if (clone.HasStarCostX) upInfo["star_cost_x"] = true;
                else if (clone.CurrentStarCost >= 0) upInfo["star_cost"] = clone.CurrentStarCost;
            }
            catch { }
            return upInfo;
        }
        catch { return null; }
    }

    private Dictionary<string, object?> PlayerSummary(Player player)
    {
        return new Dictionary<string, object?>
        {
            ["name"] = _loc.Bilingual("characters", (player.Character?.Id.Entry ?? "IRONCLAD") + ".title"),
            ["hp"] = player.Creature?.CurrentHp ?? 0,
            ["max_hp"] = player.Creature?.MaxHp ?? 0,
            ["block"] = player.Creature?.Block ?? 0,
            ["gold"] = player.Gold,
            ["potion_slots"] = player.MaxPotionCount,
            // False while an event locks potions (no use, no discard).
            ["can_use_or_remove_potions"] = player.CanUseOrRemovePotions,
            ["relics"] = player.Relics?.Select(r =>
            {
                var vars = new Dictionary<string, object?>();
                try { foreach (var dv in r.DynamicVars.Values) vars[dv.Name] = (int)dv.BaseValue; } catch { }
                var rd = new Dictionary<string, object?>
                {
                    ["id"] = r.Id.Entry,
                    ["name"] = RelicName(r),
                    ["description"] = RelicText(r),
                    ["vars"] = vars.Count > 0 ? vars : null,
                };
                AddRelicState(rd, r);
                return rd;
            }).ToList(),
            ["potions"] = player.Potions?.Select((p, i) =>
            {
                if (p == null) return null;
                var pvars = new Dictionary<string, object?>();
                try { foreach (var dv in p.DynamicVars.Values) pvars[dv.Name] = (int)dv.BaseValue; } catch { }
                return new Dictionary<string, object?>
                {
                    ["index"] = i,
                    ["name"] = _loc.Potion(p.Id.Entry),
                    ["description"] = PotionText(p),
                    ["vars"] = pvars.Count > 0 ? pvars : null,
                    ["target_type"] = p.TargetType.ToString(),
                    ["tips"] = Tips(() => p.ExtraHoverTips),
                    // CombatOnly / AnyTime can be used; Automatic (e.g. Fairy in a Bottle) triggers by itself.
                    ["usage"] = p.Usage.ToString(),
                    // Whether "Use" is enabled right now (in or out of combat), like the potion popup.
                    ["can_use"] = PotionUsable(player, p, out _),
                    // Used, waiting in the action queue (EnqueueManualUse): the popup disables
                    // Use and Discard until it runs.
                    ["queued"] = p.IsQueued ? true : null,
                };
            }).Where(x => x != null).ToList(),
            ["deck_size"] = player.Deck?.Cards?.Count(c => c != null) ?? 0,
            // The deck view (NDeckViewScreen) draws each card through UpdateVisuals: enchantment-
            // modified values, plus enchantment/affliction (#76).
            ["deck"] = OmitDeck ? null : player.Deck?.Cards?.Where(c => c != null).Select(c =>
            {
                var dcard = ListCardInfo(c, PileType.Deck, withTips: false);
                dcard["after_upgrade"] = GetUpgradedInfo(c);
                return dcard;
            }).ToList(),
        };
    }

    /// <summary>Common context added to every decision point.</summary>
    private Dictionary<string, object?> RunContext()
    {
        if (_runState == null) return new();
        var ctx = new Dictionary<string, object?>
        {
            ["act"] = _runState.CurrentActIndex + 1,
            ["act_name"] = _loc.Act(_runState.Act?.Id.Entry ?? "OVERGROWTH"),
            ["floor"] = _runState.ActFloor,
            ["room_type"] = _runState.CurrentRoom?.RoomType.ToString(),
        };

        // What the top bar shows: ascension level, modifiers, cumulative floor; plus the seed.
        try { ctx["total_floor"] = _runState.TotalFloor; } catch { }
        ctx["ascension"] = _runState.AscensionLevel;
        try { ctx["seed"] = _runState.Rng.StringSeed; } catch { }
        // NRunTimer (the top bar's run timer): RunManager.RunTime, wall-clock seconds.
        try { ctx["run_time_seconds"] = RunManager.Instance.RunTime; } catch { }
        try
        {
            if (_runState.Modifiers.Count > 0)
                ctx["modifiers"] = _runState.Modifiers.Select(m => ModifierInfo(m, false)).ToList();
        }
        catch { }

        // Boss icon(s) of the top bar (NTopBarBossIcon): the encounter titles.
        var boss = BossInfo(_runState.Act?.BossEncounter);
        if (boss != null) ctx["boss"] = boss;
        var secondBoss = BossInfo(_runState.Act?.SecondBossEncounter);
        if (secondBoss != null) ctx["second_boss"] = secondBoss;

        return ctx;
    }

    /// <summary>{id, name} of a boss encounter, named by its title like the top bar's boss icon.</summary>
    private static Dictionary<string, object?>? BossInfo(EncounterModel? encounter)
    {
        try
        {
            var id = encounter?.Id?.Entry;
            if (string.IsNullOrEmpty(id)) return null;
            return new Dictionary<string, object?>
            {
                ["id"] = id,
                ["name"] = _loc.Bilingual("encounters", id + ".title"),
            };
        }
        catch { return null; }
    }

    private static void EnsureModelDbInitialized()
    {
        if (_modelDbInitialized) return;
        _modelDbInitialized = true;

        TestMode.IsOn = true;

        // The current engine requires mod discovery and assembly metadata even
        // for unmodded runs. TestMode skips filesystem/workshop mod loading.
        MegaCrit.Sts2.Core.Modding.ModManager.Initialize(
            new MegaCrit.Sts2.Core.Modding.ModManagerFileIo(), null, null).GetAwaiter().GetResult();
        MegaCrit.Sts2.Core.Modding.AssemblyInfo.Init();

        // Install inline sync context on main thread
        SynchronizationContext.SetSynchronizationContext(_syncCtx);
        MainThreadId = Environment.CurrentManagedThreadId;

        // Initialize PlatformServices before anything touches PlatformUtil
        try
        {
            // Try to access PlatformUtil to trigger its static init
            // If it fails, it won't be available but most code checks SteamInitializer.Initialized
            var _ = MegaCrit.Sts2.Core.Platform.PlatformUtil.PrimaryPlatform;
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine($"[WARN] PlatformUtil init: {ex.Message}");
        }

        // Initialize SaveManager with a dummy profile for save/load support
        try { SaveManager.Instance.InitProfileId(0); }
        catch (Exception ex) { Console.Error.WriteLine($"[WARN] SaveManager.InitProfileId: {ex.Message}"); }

        // Initialize PrefsSave (FastMode etc.). build 23372702 reads PrefsSave.FastMode
        // from many gameplay paths (e.g. Slice.OnPlay anim delay); without this the
        // PrefsSave getter returns null and those paths NRE.
        try { SaveManager.Instance.InitPrefsDataForTest(); }
        catch (Exception ex) { Console.Error.WriteLine($"[WARN] SaveManager.InitPrefsDataForTest: {ex.Message}"); }

        // Install the Task.Yield patch but keep SuppressYield=false by default.
        // SuppressYield is toggled to true only during EndTurn to prevent boss fight deadlocks.
        PatchTaskYield();

        // Patch Cmd.Wait to be a no-op in headless mode.
        // Cmd.Wait(duration) is used for UI animations (e.g., PreviewCardPileAdd during
        // Vantom's Dismember move adding Wounds). In headless mode, these never complete
        // because there's no Godot scene tree, causing the ActionExecutor to deadlock.
        PatchCmdWait();

        // Patch TalkCmd.Play to a no-op (issue #64). Monster speech-bubble VFX during
        // moves (e.g. BygoneEffigy.WakeMove) NRE in headless and break the enemy turn.
        PatchTalkCmd();
        PatchHeadlessPresentation();
        HeadlessUiGuard.Install(typeof(CombatManager).Assembly);
        // Real-game (TestMode off) behaviour for the TestMode branches that are game logic.
        ParityPatches.Install(new Harmony("sts2headless.parity"));
        RewardPatches.Install(new Harmony("sts2headless.rewards"));
        // Trial's abandon popup, the Architect's speaker (EventFlow.cs).
        InstallEventFlowPatches();
        MapPruningPatch.Install(new Harmony("sts2headless.mappruning"));
        AnimationDelayPatch.Install(new Harmony("sts2headless.animationdelay"));

        // Capture the game's own run saves instead of writing them (write_continue_save).
        ContinueSave.Install();
        InstallAgentPatches();

        // Initialize localization system (needed for events, cards, etc.)
        InitLocManager();

        var subtypes = MegaCrit.Sts2.Core.Models.AbstractModelSubtypes.All;
        int registered = 0, failed = 0;
        for (int i = 0; i < subtypes.Count; i++)
        {
            try
            {
                ModelDb.Inject(subtypes[i]);
                registered++;
            }
            catch (Exception ex)
            {
                failed++;
                // Only log first few failures to reduce noise
                if (failed <= 5)
                    Console.Error.WriteLine($"[WARN] Failed to register {subtypes[i].Name}: {ex.GetType().Name}: {ex.Message}");
            }
        }
        Console.Error.WriteLine($"[INFO] ModelDb: {registered} registered, {failed} failed out of {subtypes.Count}");

        // Progress now resolves character models, so register ModelDb first.
        SaveManager.Instance.InitProgressData();

        // RewardsSet.Offer runs this instead of NRewardsScreen.ShowScreen (TestMode). Every set
        // (event, relic pickup, treasure extras) is parked as a "rewards" decision until the
        // player claims or leaves its rewards; nothing is ever taken on the player's behalf.
        RewardsSet.testSelector = async set =>
        {
            var sim = LocPatches._bundleSimRef
                ?? throw new InvalidOperationException("A reward set was offered with no run in progress");
            if (Environment.CurrentManagedThreadId == MainThreadId)
                Console.Error.WriteLine($"[WARN] Reward set offered on the main thread:\n{Environment.StackTrace}");
            await sim.OfferEventRewards(set);
        };

        // Initialize net ID serialization cache (needed for combat actions)
        try
        {
            ModelIdSerializationCache.Init();
            Console.Error.WriteLine("[INFO] ModelIdSerializationCache initialized");
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine($"[WARN] ModelIdSerializationCache.Init: {ex.Message}");
        }
    }

    /// <summary>Modifier ids as used by custom mode, e.g. "MIDAS", "SEALED_DECK".</summary>
    // The custom-run screen's list (NCustomRunModifiersList.GetAllModifiers): the good and bad
    // modifiers, with Character Cards once per character ("CHARACTER_CARDS:DEFECT").
    private static IEnumerable<string> KnownModifiers() =>
        ModelDb.GoodModifiers.Concat(ModelDb.BadModifiers).SelectMany(m =>
            m is MegaCrit.Sts2.Core.Models.Modifiers.CharacterCards
                ? ModelDb.AllCharacters.Select(c => $"{m.Id.Entry}:{c.Id.Entry}")
                : new[] { m.Id.Entry });

    private static ModifierModel? ResolveModifier(string name)
    {
        var parts = name.Trim().ToUpperInvariant().Replace(' ', '_').Split(':', 2);
        var canonical = ModelDb.GoodModifiers.Concat(ModelDb.BadModifiers).FirstOrDefault(m => m.Id.Entry == parts[0]);
        if (canonical == null) return null;
        var mutable = (ModifierModel)canonical.ToMutable();
        if (mutable is MegaCrit.Sts2.Core.Models.Modifiers.CharacterCards cc)
        {
            // Needs the character whose cards join the pools; the getter throws without one.
            var ch = parts.Length > 1 ? ModelDb.AllCharacters.FirstOrDefault(c => c.Id.Entry == parts[1]) : null;
            if (ch == null) return null;
            cc.CharacterModel = ch.Id;
        }
        else if (parts.Length > 1) return null;
        return mutable;
    }

    private static readonly HashSet<string> KnownCharacters =
        new() { "ironclad", "silent", "defect", "regent", "necrobinder" };

    /// <summary>Reads a game profile's progress.save (unlocked epochs, seen encounters, discovered acts).</summary>
    private static ProgressState LoadProgress(string path)
    {
        var json = File.ReadAllText(Environment.ExpandEnvironmentVariables(
            path.StartsWith("~/") ? Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), path[2..]) : path));
        var result = SaveManager.FromJson<SerializableProgress>(json);
        if (result.SaveData == null)
            throw new InvalidDataException($"unreadable progress save ({result.Status})");
        return ProgressState.FromSerializable(result.SaveData, new DeserializationContext());
    }

    private Player? CreatePlayer(string characterName, UnlockState unlocks)
    {
        return characterName.ToLowerInvariant() switch
        {
            "ironclad" => Player.CreateForNewRun<Ironclad>(unlocks, 1uL),
            "silent" => Player.CreateForNewRun<Silent>(unlocks, 1uL),
            "defect" => Player.CreateForNewRun<Defect>(unlocks, 1uL),
            "regent" => Player.CreateForNewRun<Regent>(unlocks, 1uL),
            "necrobinder" => Player.CreateForNewRun<Necrobinder>(unlocks, 1uL),
            _ => null
        };
    }

    private static void PatchCmdWait()
    {
        try
        {
            var harmony = new Harmony("sts2headless.cmdwait");
            // Find Cmd.Wait(float) — it's in MegaCrit.Sts2.Core.Commands namespace
            // Find Cmd type via CardPileCmd's assembly (both are in same namespace)
            var cmdPileType = typeof(MegaCrit.Sts2.Core.Commands.CardPileCmd);
            var cmdAsm = cmdPileType.Assembly;
            Type? cmdType = cmdAsm.GetType("MegaCrit.Sts2.Core.Commands.Cmd");
            // If not found by exact name, search by namespace + "Wait" method
            if (cmdType == null)
            {
                foreach (var t in cmdAsm.GetTypes())
                {
                    if (t.Namespace == "MegaCrit.Sts2.Core.Commands")
                    {
                        var waitM = t.GetMethods(System.Reflection.BindingFlags.Public | System.Reflection.BindingFlags.Static | System.Reflection.BindingFlags.DeclaredOnly)
                            .Where(m => m.Name == "Wait").ToList();
                        if (waitM.Count > 0)
                        {
                            cmdType = t;
                            Console.Error.WriteLine($"[INFO] Found Wait() in {t.FullName}");
                            break;
                        }
                    }
                }
            }
            if (cmdType != null)
            {
                var waitMethod = cmdType.GetMethod("Wait",
                    System.Reflection.BindingFlags.Public | System.Reflection.BindingFlags.Static,
                    null, new[] { typeof(float) }, null);
                if (waitMethod != null)
                {
                    var prefix = typeof(YieldPatches).GetMethod(nameof(YieldPatches.CmdWaitPrefix),
                        System.Reflection.BindingFlags.Static | System.Reflection.BindingFlags.Public);
                    if (prefix != null)
                    {
                        harmony.Patch(waitMethod, new HarmonyMethod(prefix));
                        Console.Error.WriteLine("[INFO] Patched Cmd.Wait() to no-op (prevents boss fight deadlocks)");
                    }
                }
                else
                {
                    // Try to find any Wait method
                    var methods = cmdType.GetMethods(System.Reflection.BindingFlags.Public | System.Reflection.BindingFlags.Static)
                        .Where(m => m.Name == "Wait").ToList();
                    foreach (var m in methods)
                    {
                        Console.Error.WriteLine($"[INFO] Found Cmd.Wait({string.Join(",", m.GetParameters().Select(p => p.ParameterType.Name))})");
                        var prefix = typeof(YieldPatches).GetMethod(nameof(YieldPatches.CmdWaitPrefix),
                            System.Reflection.BindingFlags.Static | System.Reflection.BindingFlags.Public);
                        if (prefix != null)
                        {
                            harmony.Patch(m, new HarmonyMethod(prefix));
                            Console.Error.WriteLine($"[INFO] Patched Cmd.Wait variant");
                        }
                    }
                }
            }
            else
            {
                Console.Error.WriteLine("[WARN] Could not find MegaCrit.Sts2.Core.Commands.Cmd type");
            }
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine($"[WARN] Failed to patch Cmd.Wait: {ex.Message}");
        }
    }

    /// <summary>
    /// Presentation-only game methods that crash headless (null NCombatRoom/NGame nodes) and
    /// abort the combat flow that called them (issue #64). Each target was checked in IL to
    /// only touch nodes/VFX:
    ///  - skip:    the whole body is presentation (VFX / texture / hit-stop).
    ///  - swallow: run the body but drop the NRE (SoulNexus.AfterDeath first unsubscribes its
    ///             Died handler, which must still happen, then resets a Spine animation).
    /// </summary>
    private static void PatchHeadlessPresentation()
    {
        var harmony = new Harmony("sts2headless.presentation");
        var asm = typeof(CombatManager).Assembly;
        var flags = System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.Static
            | System.Reflection.BindingFlags.Public | System.Reflection.BindingFlags.NonPublic
            | System.Reflection.BindingFlags.DeclaredOnly;
        var skip = new HarmonyMethod(typeof(YieldPatches).GetMethod(nameof(YieldPatches.SkipVoidPrefix)));
        var skipTask = new HarmonyMethod(typeof(YieldPatches).GetMethod(nameof(YieldPatches.SkipTaskPrefix)));
        var swallow = new HarmonyMethod(typeof(YieldPatches).GetMethod(nameof(YieldPatches.SwallowNullRefFinalizer)));
        (string type, string method, bool skipBody)[] targets =
        {
            ("MegaCrit.Sts2.Core.Nodes.NGame", "DoHitStop", true),                         // Vantom.DismemberMove
            ("MegaCrit.Sts2.Core.Models.Monsters.DecimillipedeSegment", "ChangePhobiaModeTexture", true),
            ("MegaCrit.Sts2.Core.Models.Powers.ReattachPower", "DoFadeOutOnAllSegments", true),
            ("MegaCrit.Sts2.Core.Models.Monsters.TestSubject", "SetColor", true),          // tints the creature node
            ("MegaCrit.Sts2.Core.Models.Monsters.Crusher", "BeforeDeath", true),          // Kaiser Crab: sfx + background anim + music
            ("MegaCrit.Sts2.Core.Models.Monsters.Rocket", "BeforeDeath", true),
            ("MegaCrit.Sts2.Core.Models.Monsters.SoulNexus", "AfterDeath", false),
            ("MegaCrit.Sts2.Core.Models.Monsters.SoulNexus", "BeforeRemovedFromRoom", false),
        };
        foreach (var (typeName, methodName, skipBody) in targets)
        {
            try
            {
                var type = asm.GetType(typeName) ?? asm.GetTypes().FirstOrDefault(t => t.Name == typeName.Split('.').Last());
                var methods = type?.GetMethods(flags).Where(m => m.Name == methodName && (m.ReturnType == typeof(void) || m.ReturnType == typeof(Task))).ToList();
                if (methods == null || methods.Count == 0)
                {
                    Console.Error.WriteLine($"[WARN] Presentation patch target not found: {typeName}.{methodName}");
                    continue;
                }
                foreach (var m in methods)
                {
                    if (skipBody) harmony.Patch(m, prefix: m.ReturnType == typeof(Task) ? skipTask : skip);
                    else harmony.Patch(m, finalizer: swallow);
                }
            }
            catch (Exception ex)
            {
                Console.Error.WriteLine($"[WARN] Failed to patch {typeName}.{methodName}: {ex.Message}");
            }
        }
    }

    private static void PatchTalkCmd()
    {
        try
        {
            var harmony = new Harmony("sts2headless.talkpatch");
            var talkType = typeof(CombatManager).Assembly.GetType("MegaCrit.Sts2.Core.Commands.TalkCmd");
            var playMethod = talkType?.GetMethods(System.Reflection.BindingFlags.Public | System.Reflection.BindingFlags.Static)
                .FirstOrDefault(m => m.Name == "Play");
            if (playMethod == null)
            {
                Console.Error.WriteLine("[WARN] Could not find TalkCmd.Play to patch");
                return;
            }
            var prefix = typeof(YieldPatches).GetMethod(nameof(YieldPatches.TalkCmdPlayPrefix),
                System.Reflection.BindingFlags.Static | System.Reflection.BindingFlags.Public);
            if (prefix != null)
            {
                harmony.Patch(playMethod, new HarmonyMethod(prefix));
                Console.Error.WriteLine("[INFO] Patched TalkCmd.Play() to no-op (prevents enemy-move VFX crash, issue #64)");
            }
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine($"[WARN] Failed to patch TalkCmd.Play: {ex.Message}");
        }
    }

    private static void PatchTaskYield()
    {
        try
        {
            var harmony = new Harmony("sts2headless.yieldpatch");

            // Patch YieldAwaitable.YieldAwaiter.IsCompleted to return true
            // This makes `await Task.Yield()` execute synchronously (continuation runs inline)
            var yieldAwaiterType = typeof(System.Runtime.CompilerServices.YieldAwaitable)
                .GetNestedType("YieldAwaiter");
            if (yieldAwaiterType != null)
            {
                var isCompletedProp = yieldAwaiterType.GetProperty("IsCompleted");
                if (isCompletedProp != null)
                {
                    var getter = isCompletedProp.GetGetMethod();
                    var prefix = typeof(YieldPatches).GetMethod(nameof(YieldPatches.IsCompletedPrefix),
                        System.Reflection.BindingFlags.Static | System.Reflection.BindingFlags.Public);
                    if (getter != null && prefix != null)
                    {
                        harmony.Patch(getter, new HarmonyMethod(prefix));
                        Console.Error.WriteLine("[INFO] Patched Task.Yield() to be synchronous");
                    }
                }
            }
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine($"[WARN] Failed to patch Task.Yield: {ex.Message}");
        }
    }

    /// <summary>
    /// Card selector for headless mode — picks first available card for any selection prompt.
    /// Used by cards like Headbutt, Armaments, etc. that need player to choose a card.
    /// </summary>
    /// <summary>
    /// Card selector that creates a pending selection decision point.
    /// When the game needs the player to choose cards (upgrade, remove, transform, bundle pick),
    /// this stores the options and waits for the main loop to provide the answer.
    /// </summary>
    internal class HeadlessCardSelector : MegaCrit.Sts2.Core.TestSupport.ICardSelector
    {
        // Pending card selection — set by game engine, read by main loop
        public List<CardModel>? PendingOptions { get; private set; }
        public int PendingMinSelect { get; private set; }
        public int PendingMaxSelect { get; private set; }
        public string PendingPrompt { get; private set; } = "";
        // What the game's selection screen says about this prompt (purpose, prompt text,
        // cancel button, ...), captured from CardSelectCmd by SelectPromptPatches.
        public SelectContext? PendingContext { get; private set; }
        private TaskCompletionSource<IEnumerable<CardModel>>? _pendingTcs;

        public bool HasPending => _pendingTcs != null && !_pendingTcs.Task.IsCompleted;

        public Task<IEnumerable<CardModel>> GetSelectedCards(
            IEnumerable<CardModel> options, int minSelect, int maxSelect)
        {
            var optList = options.ToList();
            var ctx = SelectPromptPatches.Take();
            if (optList.Count == 0)
                return Task.FromResult<IEnumerable<CardModel>>(Array.Empty<CardModel>());

            // No auto-pick here: CardSelectCmd already skips the screen itself when the game
            // does (count <= MinSelect && !RequireManualConfirmation, one upgradable card in
            // hand, ...). Every call that reaches the selector is a screen a human sees, even
            // with a single card (choose-a-card, manual-confirm and cancelable screens).
            // A choose-a-card screen without a skip button requires a pick even though the
            // game asks the selector for 0-1 cards.
            if (ctx != null && ctx.ForceMinSelect > minSelect)
                minSelect = Math.Min(ctx.ForceMinSelect, maxSelect);
            // A hand selection happens on the hand row: list the cards in the row's order.
            if (ctx?.Screen == "hand")
                optList = HandOrder.Sort(optList);

            // Store pending selection and wait
            PendingContext = ctx;
            PendingOptions = optList;
            PendingMinSelect = minSelect;
            PendingMaxSelect = maxSelect;
            _pendingTcs = new TaskCompletionSource<IEnumerable<CardModel>>();
            RunSimulator.SignalFlow();

            Console.Error.WriteLine($"[SIM] Card selection pending: {optList.Count} options, select {minSelect}-{maxSelect}");

            // Return the task — the main loop will complete it
            return _pendingTcs.Task;
        }

        public void ResolvePending(IEnumerable<CardModel> selected)
        {
            // Clear state before completing: the continuation may run synchronously and
            // request the next selection, which must not be wiped by this call.
            var tcs = _pendingTcs;
            var ctx = PendingContext;
            PendingOptions = null;
            PendingContext = null;
            _pendingTcs = null;
            if (tcs != null) Lift(ctx, selected);
            tcs?.TrySetResult(selected);
        }

        /// <summary>
        /// Clear the pending selection on the calling thread and return the action that hands
        /// <paramref name="indices"/> to the waiting game flow (null if nothing is pending).
        /// Lets the caller decide which thread the flow resumes on.
        /// </summary>
        public Action? DetachByIndices(int[] indices)
        {
            var options = PendingOptions;
            var tcs = _pendingTcs;
            if (options == null || tcs == null) return null;
            var selected = indices.Where(i => i >= 0 && i < options.Count).Select(i => options[i]).ToList();
            var ctx = PendingContext;
            PendingOptions = null;
            PendingContext = null;
            _pendingTcs = null;
            return () => { Lift(ctx, selected); tcs.TrySetResult(selected); };
        }

        /// <summary>Like DetachByIndices, resolving with an empty selection (skip).</summary>
        public Action? DetachCancel()
        {
            var tcs = _pendingTcs;
            PendingOptions = null;
            PendingContext = null;
            _pendingTcs = null;
            return tcs == null ? null : () => tcs.TrySetResult(Array.Empty<CardModel>());
        }

        // NPlayerHand lifts the cards picked in a hand selection out of the row (HandOrder).
        private static void Lift(SelectContext? ctx, IEnumerable<CardModel> selected)
        {
            if (ctx?.Screen != "hand") return;
            try { HandOrder.Selected(selected, ctx.HandSource); } catch { }
        }

        public void ResolvePendingByIndices(int[] indices)
        {
            if (PendingOptions == null) return;
            var selected = indices
                .Where(i => i >= 0 && i < PendingOptions.Count)
                .Select(i => PendingOptions[i])
                .ToList();
            ResolvePending(selected);
        }

        public void CancelPending()
        {
            var tcs = _pendingTcs;
            PendingOptions = null;
            PendingContext = null;
            _pendingTcs = null;
            tcs?.TrySetResult(Array.Empty<CardModel>());
        }

        // Pending card reward from events (GetSelectedCardReward blocks until resolved)
        public List<MegaCrit.Sts2.Core.Entities.Cards.CardCreationResult>? PendingRewardCards { get; private set; }
        private ManualResetEventSlim? _rewardWait;
        private int _rewardChoice = -1;

        // NOTE: STS2 build 23372702 changed ICardSelector.GetSelectedCardReward to return
        // a CardRewardSelection struct { CardModel card; CardRewardAlternative alternative }.
        // CardReward.OnSelect interprets: alternative != null → pick that alternative
        // (Skip/Reroll/Sacrifice); else card != null → take that card; both null → skip.
        public MegaCrit.Sts2.Core.TestSupport.CardRewardSelection GetSelectedCardReward(
            IReadOnlyList<MegaCrit.Sts2.Core.Entities.Cards.CardCreationResult> options,
            IReadOnlyList<CardRewardAlternative> alternatives)
        {
            // OnSelect passes the list it generated when the screen opened on every pass, but the
            // screen shows the latest CardRewardAlternative.Generate(reward) (RefreshOptions after
            // a Driftwood Reroll, which drops REROLL). The screen's button i still resolves to
            // alternatives[i] (OnSelect indexes its own list by the screen's position).
            var shownFor = RewardPatches.ShownFor(options);
            var shown = shownFor?.Alternatives ?? alternatives ?? Array.Empty<CardRewardAlternative>();

            // Nothing on the screen at all (no cards, no Skip/Reroll/Sacrifice): nothing to decide.
            if (options.Count == 0 && shown.Count == 0) return default;

            // Blocking the main thread would wait for an answer only it can deliver. Every reward
            // flow runs on a background flow, so this is an adapter bug: fail loudly instead of
            // skipping the reward for the player.
            if (Environment.CurrentManagedThreadId == MainThreadId)
                throw new InvalidOperationException("Card reward requested on the main thread (adapter bug): it can't wait for the player here");

            // Store pending and block until main loop resolves
            var wait = new ManualResetEventSlim(false);
            int generation;
            lock (_rewardLock)
            {
                // A reward from a run that is being (or has been) torn down: fault that flow
                // instead of parking its screen in the next run or choosing anything for it.
                if (!_runOpen || (shownFor != null && _activeRun != null
                        && !ReferenceEquals(shownFor.Reward.Player.RunState, _activeRun)))
                    throw new OperationCanceledException("Card reward from a finished run");
                generation = _generation;
                _rewardChoice = -1;
                _rewardAltChoice = -1;
                PendingRewardCards = options.ToList();
                PendingRewardAlternatives = shown.ToList();
                _rewardWait = wait;
            }
            RunSimulator.SignalFlow();  // the waiter sees HasPendingReward without a 1 ms poll

            Console.Error.WriteLine($"[SIM] Card reward pending: {options.Count} cards (blocking)");
            // No timeout: the reward waits for the player's answer (a timeout used to skip it
            // for them). Reset() wakes it when the run is torn down.
            wait.Wait();

            int choice, altChoice;
            lock (_rewardLock)
            {
                choice = _rewardChoice;
                altChoice = _rewardAltChoice;
                // Normally already cleared by ResolveReward/SkipReward.
                if (ReferenceEquals(_rewardWait, wait))
                {
                    PendingRewardCards = null;
                    _rewardWait = null;
                }
                // Woken by Reset(): the run is gone, nothing was chosen.
                if (generation != _generation)
                    throw new OperationCanceledException("Card reward cancelled: the run was reset");
            }

            if (alternatives != null && altChoice >= 0 && altChoice < alternatives.Count)
                return new MegaCrit.Sts2.Core.TestSupport.CardRewardSelection { alternative = alternatives[altChoice] };
            if (choice >= 0 && choice < options.Count)
                return new MegaCrit.Sts2.Core.TestSupport.CardRewardSelection { card = options[choice].Card };
            return default;  // Skip (card=null, alternative=null)
        }

        public bool HasPendingReward => PendingRewardCards != null && _rewardWait != null;

        // Run-generation guard: Reset() closes the selector and bumps the generation, so a
        // reward flow of the torn-down run that publishes late, or is waiting, is cancelled.
        private int _generation;
        private bool _runOpen;
        private object? _activeRun;

        /// <summary>A new run owns the selector (called once its RunState exists).</summary>
        public void BeginRun(object runState)
        {
            lock (_rewardLock) { _runOpen = true; _activeRun = runState; }
        }

        /// <summary>Drop any pending prompt, waking the game thread that is waiting on it.</summary>
        public void Reset()
        {
            lock (_rewardLock) { _runOpen = false; _activeRun = null; _generation++; }
            CancelPending();
            CompleteReward(-1);
            // The alternatives' closures hold the old CardReward (and its Player / RunState).
            lock (_rewardLock) { if (_rewardWait == null) PendingRewardAlternatives = null; }
        }

        public void ResolveReward(int index) => CompleteReward(index);

        /// <summary>
        /// The Skip / Reroll / Sacrifice buttons the card reward screen shows now (index i
        /// resolves to the i-th alternative CardReward.OnSelect generated, as on the screen).
        /// </summary>
        public List<CardRewardAlternative>? PendingRewardAlternatives { get; private set; }
        private int _rewardAltChoice = -1;

        public void ResolveRewardAlternative(int index) => CompleteReward(-1, index);

        public void SkipReward() => CompleteReward(-1);

        // Clear the pending flag on the calling (main) thread before waking the blocked
        // game thread, so HasPendingReward is false as soon as this returns instead of
        // whenever the background thread gets scheduled.
        private void CompleteReward(int choice, int altChoice = -1)
        {
            // Locked with GetSelectedCardReward's publish so a reward published concurrently
            // (game thread) is never half-cleared here.
            ManualResetEventSlim? wait;
            lock (_rewardLock)
            {
                wait = _rewardWait;
                if (wait == null) return;
                _rewardChoice = choice;
                _rewardAltChoice = altChoice;
                PendingRewardCards = null;
                _rewardWait = null;
            }
            wait.Set();
        }

        private readonly object _rewardLock = new();
    }

    internal static class YieldPatches
    {
        // Only suppress Task.Yield() when this flag is set (during end_turn processing)
        public static volatile bool SuppressYield;

        public static bool IsCompletedPrefix(ref bool __result)
        {
            if (SuppressYield)
            {
                __result = true;
                return false;
            }
            return true; // Let normal Yield behavior run
        }

        /// <summary>Harmony prefix: make Cmd.Wait() return completed task immediately (no-op in headless).</summary>
        public static bool CmdWaitPrefix(ref Task __result)
        {
            __result = Task.CompletedTask;
            return false; // Skip original method
        }

        /// <summary>Harmony prefix: skip a presentation-only void method in headless.</summary>
        public static bool SkipVoidPrefix() => false;

        /// <summary>Harmony prefix: skip a presentation-only Task method, returning a completed task.</summary>
        public static bool SkipTaskPrefix(ref Task __result)
        {
            __result = Task.CompletedTask;
            return false;
        }

        /// <summary>Harmony finalizer: drop NREs from presentation code that dereferences absent nodes.</summary>
        public static Exception? SwallowNullRefFinalizer(Exception? __exception) =>
            __exception is NullReferenceException ? null : __exception;

        /// <summary>
        /// Harmony prefix: no-op TalkCmd.Play (issue #64). The speech-bubble VFX
        /// (NSpeechBubbleVfx.Create + GetVfxContainer().AddChildSafely) NREs in headless,
        /// which derails enemy moves like BygoneEffigy.WakeMove mid enemy turn and forces
        /// the EndTurn nuclear fallback / false game_over. The bubble is purely cosmetic and
        /// its return value is ignored by callers, so returning null is safe.
        /// </summary>
        public static bool TalkCmdPlayPrefix(LocString line, Creature speaker, ref MegaCrit.Sts2.Core.Nodes.Vfx.NSpeechBubbleVfx? __result)
        {
            __result = null;
            // The player still sees the line: record it (TalkCmd.Play says nothing for the dead).
            try { if (!speaker.IsDead) RecordBubble("speech", line, speaker); } catch { }
            return false; // Skip original method
        }
    }

    private static void InitLocManager()
    {
        // Create a LocManager instance with stub tables via reflection.
        // LocManager.Initialize() fails because PlatformUtil isn't available,
        // and Harmony can't patch some LocString methods due to JIT issues.
        // Solution: create an uninitialized LocManager, set its _tables, and
        // use Harmony only for the simple LocTable.GetRawText fallback.
        try
        {
            // Create uninitialized LocManager and set Instance
            var instanceProp = typeof(LocManager).GetProperty("Instance",
                System.Reflection.BindingFlags.Static | System.Reflection.BindingFlags.Public);
            var instance = System.Runtime.CompilerServices.RuntimeHelpers.GetUninitializedObject(typeof(LocManager));
            instanceProp!.SetValue(null, instance);

            // Load REAL localization data from localization_eng/ JSON files
            var tablesField = typeof(LocManager).GetField("_tables",
                System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.NonPublic);
            var tables = new Dictionary<string, LocTable>();

            var locDir = DataDirs.Localization("localization_eng");
            if (Directory.Exists(locDir))
            {
                foreach (var file in Directory.GetFiles(locDir, "*.json"))
                {
                    try
                    {
                        var name = Path.GetFileNameWithoutExtension(file);
                        var data = System.Text.Json.JsonSerializer.Deserialize<Dictionary<string, string>>(
                            File.ReadAllText(file));
                        if (data != null)
                            tables[name] = new LocTable(name, data);
                    }
                    catch { }
                }
                Console.Error.WriteLine($"[INFO] Loaded {tables.Count} localization tables from {locDir}");
            }
            else
            {
                Console.Error.WriteLine($"[WARN] Localization dir not found: {locDir}");
                // Fallback: empty tables
                var tableNames = new[] {
                    "achievements","acts","afflictions","ancients","ascension",
                    "bestiary","card_keywords","card_library","card_reward_ui",
                    "card_selection","cards","characters","combat_messages",
                    "credits","enchantments","encounters","epochs","eras",
                    "events","ftues","game_over_screen","gameplay_ui",
                    "inspect_relic_screen","intents","main_menu_ui","map",
                    "merchant_room","modifiers","monsters","orbs","potion_lab",
                    "potions","powers","relic_collection","relics","rest_site_ui",
                    "run_history","settings_ui","static_hover_tips","stats_screen",
                    "timeline","vfx"
                };
                foreach (var name in tableNames)
                    tables[name] = new LocTable(name, new Dictionary<string, string>());
            }
            tablesField!.SetValue(instance, tables);

            // Set Language
            var langProp = typeof(LocManager).GetProperty("Language",
                System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.Public);
            try { langProp?.SetValue(instance, "eng"); } catch { }

            // Set CultureInfo
            var cultureProp = typeof(LocManager).GetProperty("CultureInfo",
                System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.Public);
            try { cultureProp?.SetValue(instance, System.Globalization.CultureInfo.InvariantCulture); } catch { }

            // Initialize _smartFormatter — the game uses `new SmartFormatter()`
            try
            {
                var sfField = typeof(LocManager).GetField("_smartFormatter",
                    System.Reflection.BindingFlags.Static | System.Reflection.BindingFlags.NonPublic);
                // Dump ALL fields (instance + static)
                foreach (var f in typeof(LocManager).GetFields(System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.Static | System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Public))
                    Console.Error.WriteLine($"[DEBUG] LocManager {(f.IsStatic?"static":"inst")} field: {f.Name} ({f.FieldType.Name})");
                Console.Error.WriteLine($"[DEBUG] sfField: {sfField?.Name ?? "null"} type: {sfField?.FieldType?.Name ?? "null"}");
                if (sfField != null)
                {
                    try
                    {
                        // List constructors to find the right one
                        var ctors = sfField.FieldType.GetConstructors(
                            System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.Public | System.Reflection.BindingFlags.NonPublic);
                        Console.Error.WriteLine($"[DEBUG] SmartFormatter has {ctors.Length} constructors:");
                        foreach (var ctor in ctors)
                        {
                            var ps = ctor.GetParameters();
                            Console.Error.WriteLine($"  ({string.Join(", ", ps.Select(p => $"{p.ParameterType.Name} {p.Name}"))})");
                        }
                        // Try the one with fewest params
                        var bestCtor = ctors.OrderBy(c => c.GetParameters().Length).First();
                        var args2 = bestCtor.GetParameters().Select(p =>
                            p.HasDefaultValue ? p.DefaultValue :
                            p.ParameterType.IsValueType ? Activator.CreateInstance(p.ParameterType) : null
                        ).ToArray();
                        var sf = bestCtor.Invoke(args2);
                        // Register extensions using the game's own LoadLocFormatters logic
                        // Call it via reflection on LocManager instance
                        try
                        {
                            var loadMethod = typeof(LocManager).GetMethod("LoadLocFormatters",
                                System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.NonPublic);
                            if (loadMethod != null)
                            {
                                loadMethod.Invoke(instance, null);
                                Console.Error.WriteLine("[INFO] SmartFormatter initialized via LoadLocFormatters");
                            }
                            else
                            {
                                sfField.SetValue(null, sf);
                                Console.Error.WriteLine("[INFO] SmartFormatter set (no LoadLocFormatters found)");
                            }
                        }
                        catch (Exception lfEx)
                        {
                            sfField.SetValue(null, sf);
                            Console.Error.WriteLine($"[WARN] LoadLocFormatters failed: {lfEx.InnerException?.Message ?? lfEx.Message}");
                        }
                    }
                    catch (Exception sfEx)
                    {
                        Console.Error.WriteLine($"[WARN] SmartFormatter create failed: {sfEx.GetType().Name}: {sfEx.Message}");
                        if (sfEx.InnerException != null)
                            Console.Error.WriteLine($"  Inner: {sfEx.InnerException.GetType().Name}: {sfEx.InnerException.Message}");
                    }
                }
                else
                {
                    Console.Error.WriteLine("[WARN] _smartFormatter field not found in LocManager");
                }
            }
            catch (Exception ex) { Console.Error.WriteLine($"[WARN] _smartFormatter init: {ex.GetType().Name}: {ex.Message}\n{ex.InnerException?.Message}"); }

            // Initialize _engTables to point to _tables (avoid null ref in fallback)
            try
            {
                var engTablesField = typeof(LocManager).GetField("_engTables",
                    System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.NonPublic);
                engTablesField?.SetValue(instance, tables);
            }
            catch { }

            Console.Error.WriteLine("[INFO] LocManager initialized with stub tables");

            // Use Harmony to patch methods that need fallback behavior
            var harmony = new Harmony("sts2headless.locpatch");

            // With real loc data loaded, we only need fallback patches for:
            // 1. LocTable.GetRawText — return key for missing entries instead of throwing
            // 2. LocManager.SmartFormat — _smartFormatter is null, return raw text instead
            // We do NOT patch GetFormattedText/GetRawText on LocString anymore
            // so the real localization pipeline works (needed for Neow event etc.)

            var getRawText = typeof(LocTable).GetMethod("GetRawText",
                System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.Public,
                null, new[] { typeof(string) }, null);
            var prefix = typeof(LocPatches).GetMethod(nameof(LocPatches.GetRawTextPrefix),
                System.Reflection.BindingFlags.Static | System.Reflection.BindingFlags.Public);
            if (getRawText != null && prefix != null)
            {
                harmony.Patch(getRawText, new HarmonyMethod(prefix));
                Console.Error.WriteLine("[INFO] Patched LocTable.GetRawText");
            }

            // Patch GetLocString to not throw
            var getLocString = typeof(LocTable).GetMethod("GetLocString");
            var glsPrefix = typeof(LocPatches).GetMethod(nameof(LocPatches.GetLocStringPrefix),
                System.Reflection.BindingFlags.Static | System.Reflection.BindingFlags.Public);
            if (getLocString != null && glsPrefix != null)
            {
                try { harmony.Patch(getLocString, new HarmonyMethod(glsPrefix)); }
                catch (Exception ex4) { Console.Error.WriteLine($"[WARN] Failed to patch GetLocString: {ex4.Message}"); }
            }

            // Patch FromChooseABundleScreen to use our card selector
            try
            {
                var bundleMethod = typeof(MegaCrit.Sts2.Core.Commands.CardSelectCmd).GetMethod("FromChooseABundleScreen",
                    System.Reflection.BindingFlags.Static | System.Reflection.BindingFlags.Public);
                var bundlePrefix = typeof(LocPatches).GetMethod(nameof(LocPatches.BundleScreenPrefix),
                    System.Reflection.BindingFlags.Static | System.Reflection.BindingFlags.Public);
                if (bundleMethod != null && bundlePrefix != null)
                {
                    harmony.Patch(bundleMethod, new HarmonyMethod(bundlePrefix));
                    Console.Error.WriteLine("[INFO] Patched FromChooseABundleScreen");
                }
            }
            catch (Exception ex) { Console.Error.WriteLine($"[WARN] Bundle patch: {ex.Message}"); }

            // Replace the Crystal Sphere screen with a crystal_sphere decision
            try
            {
                var showScreen = typeof(MegaCrit.Sts2.Core.Nodes.Events.Custom.CrystalSphere.NCrystalSphereScreen)
                    .GetMethod("ShowScreen", System.Reflection.BindingFlags.Static | System.Reflection.BindingFlags.Public);
                var spherePrefix = typeof(LocPatches).GetMethod(nameof(LocPatches.CrystalSphereScreenPrefix),
                    System.Reflection.BindingFlags.Static | System.Reflection.BindingFlags.Public);
                if (showScreen != null && spherePrefix != null)
                {
                    harmony.Patch(showScreen, new HarmonyMethod(spherePrefix));
                    Console.Error.WriteLine("[INFO] Patched NCrystalSphereScreen.ShowScreen");
                }
            }
            catch (Exception ex) { Console.Error.WriteLine($"[WARN] Crystal Sphere patch: {ex.Message}"); }


            // Patch HasEntry to always return true
            PatchMethod(harmony, typeof(LocTable), "HasEntry", nameof(LocPatches.TableHasEntryPrefix));

            // IsLocalKey: same truthful answer
            PatchMethod(harmony, typeof(LocTable), "IsLocalKey", nameof(LocPatches.TableHasEntryPrefix));

            // Patch LocString.Exists (static) to always return true
            var locStringExists = typeof(LocString).GetMethod("Exists",
                System.Reflection.BindingFlags.Static | System.Reflection.BindingFlags.Public);
            if (locStringExists != null)
            {
                PatchMethod(harmony, locStringExists, nameof(LocPatches.LocStringExistsPrefix));
            }

            // Patch LocTable.GetLocStringsWithPrefix to return empty list
            PatchMethod(harmony, typeof(LocTable), "GetLocStringsWithPrefix", nameof(LocPatches.GetLocStringsWithPrefixPrefix));
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine($"[WARN] InitLocManager failed: {ex.Message}");
        }
    }

    private static void PatchMethod(Harmony harmony, Type type, string methodName, string patchName)
    {
        try
        {
            var method = type.GetMethod(methodName, System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.Public | System.Reflection.BindingFlags.Static);
            PatchMethod(harmony, method, patchName);
        }
        catch (Exception ex) { Console.Error.WriteLine($"[WARN] Failed to patch {type.Name}.{methodName}: {ex.Message}"); }
    }

    private static void PatchMethod(Harmony harmony, System.Reflection.MethodInfo? method, string patchName)
    {
        if (method == null) return;
        try
        {
            var prefix = typeof(LocPatches).GetMethod(patchName, System.Reflection.BindingFlags.Static | System.Reflection.BindingFlags.Public);
            if (prefix != null) harmony.Patch(method, new HarmonyMethod(prefix));
        }
        catch (Exception ex) { Console.Error.WriteLine($"[WARN] Failed to patch {method.Name}: {ex.Message}"); }
    }

    internal static class LocPatches
    {
        // Entries resolve to real text (in the output language) so every string the game builds —
        // descriptions, tooltips, event text — has its numbers filled in by the game's own
        // SmartFormat. Unknown entries still resolve to their key. RealText is kept as a no-op
        // marker around the host's own formatting calls.
        [ThreadStatic] internal static bool RealText;
        private static readonly System.Reflection.FieldInfo? LocTableName =
            typeof(LocTable).GetField("_name", System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.NonPublic);

        public static bool GetRawTextPrefix(LocTable __instance, string key, ref string __result)
        {
            if (LocTableName?.GetValue(__instance) is string table && _loc.Raw(table, key) is string text)
            {
                __result = text;
                return false;
            }
            // Return key as fallback "translation"
            __result = key;
            return false;
        }

        public static bool GetFormattedTextPrefix(LocString __instance, ref string __result)
        {
            __result = __instance?.LocEntryKey ?? "";
            return false;
        }

        public static bool GetRawTextInstancePrefix(LocString __instance, ref string __result)
        {
            __result = __instance?.LocEntryKey ?? "";
            return false;
        }



        public static bool HasEntryPrefix(ref bool __result)
        {
            __result = true;
            return false;
        }

        // Answer key-existence checks truthfully (game code picks fallbacks with them, e.g.
        // EventOption.FromRelic -> relic.DynamicEventDescription, RelicModel.EventDescription,
        // ancient dialogue keys). Tables the CLI doesn't ship keep the old always-true answer.
        public static bool TableHasEntryPrefix(LocTable __instance, string key, ref bool __result)
        {
            __result = LocTableName?.GetValue(__instance) is not string table || !_loc.HasTable(table)
                       || _loc.Raw(table, key) != null;
            return false;
        }

        public static bool LocStringExistsPrefix(string table, string key, ref bool __result)
        {
            __result = !_loc.HasTable(table) || _loc.Raw(table, key) != null;
            return false;
        }

        public static bool GetLocStringPrefix(LocTable __instance, string key, ref LocString __result)
        {
            var nameField = typeof(LocTable).GetField("_name",
                System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.NonPublic);
            var tableName = nameField?.GetValue(__instance) as string ?? "_unknown";
            __result = new LocString(tableName, key);
            return false;
        }

        /// <summary>
        /// Intercept bundle selection — store bundles and wait for player to pick a pack index.
        /// </summary>
        public static bool BundleScreenPrefix(
            MegaCrit.Sts2.Core.Entities.Players.Player player,
            IReadOnlyList<IReadOnlyList<CardModel>> bundles,
            ref Task<IEnumerable<CardModel>> __result)
        {
            // Same early outs as the game's FromChooseABundleScreen.
            if (CombatManager.Instance.IsEnding || bundles.Count == 0)
            {
                __result = Task.FromResult<IEnumerable<CardModel>>(Array.Empty<CardModel>());
                return false;
            }
            // The game reserves a choice id before asking (keeps later choice ids aligned).
            RunManager.Instance.PlayerChoiceSynchronizer.ReserveChoiceId(player);

            // Store pending bundles for the main loop to present
            var sim = _bundleSimRef;
            if (sim != null)
            {
                sim._pendingBundles = bundles;
                sim._pendingBundleTcs = new TaskCompletionSource<IEnumerable<CardModel>>();
                SignalFlow();
                Console.Error.WriteLine($"[SIM] Bundle selection pending: {bundles.Count} packs");

                __result = sim._pendingBundleTcs.Task;
                return false;
            }

            // No simulator to ask (the run is being torn down): never pick a pack for the player.
            Console.Error.WriteLine("[WARN] Bundle selection requested with no active run; not choosing one");
            __result = Task.FromException<IEnumerable<CardModel>>(
                new InvalidOperationException("bundle selection without an active run"));
            return false;
        }

        // Static reference so Harmony patch can access the simulator instance
        internal static RunSimulator? _bundleSimRef;

        /// <summary>Harmony prefix: instead of opening the Crystal Sphere screen, hand the
        /// minigame to the simulator, which offers it as a crystal_sphere decision.</summary>
        public static bool CrystalSphereScreenPrefix(
            MegaCrit.Sts2.Core.Events.Custom.CrystalSphereEvent.CrystalSphereMinigame grid,
            ref MegaCrit.Sts2.Core.Nodes.Events.Custom.CrystalSphere.NCrystalSphereScreen __result)
        {
            var sim = _bundleSimRef;
            if (sim != null)
            {
                sim._crystalSphere = grid;
                SignalFlow();
                Console.Error.WriteLine($"[SIM] Crystal Sphere minigame pending: {grid.DivinationCount} divinations");
            }
            __result = null!;
            return false;
        }

        public static bool GetLocStringsWithPrefixPrefix(ref IReadOnlyList<LocString> __result)
        {
            __result = new List<LocString>();
            return false;
        }
    }

    private static void Log(string message)
    {
        Console.Error.WriteLine($"[SIM] {message}");
    }

    private static Dictionary<string, object?> Error(string message) =>
        new() { ["type"] = "error", ["message"] = message };

    private static Dictionary<string, object?> Error(string message, string code) =>
        new() { ["type"] = "error", ["code"] = code, ["message"] = message };

    // Set when the engine can no longer advance the current run. Every later action gets
    // the same error until a new run is started or loaded.
    private string? _stuckReason;

    private Dictionary<string, object?> MarkStuck(string reason)
    {
        _stuckReason = reason;
        return StuckError();
    }

    private Dictionary<string, object?> StuckError()
    {
        var ctx = _runState == null ? null : new Dictionary<string, object?>
        {
            ["act"] = _runState.CurrentActIndex + 1,
            ["floor"] = _runState.ActFloor,
            ["room_type"] = _runState.CurrentRoom?.GetType().Name,
        };
        return new()
        {
            ["type"] = "error",
            ["code"] = "engine_stuck",
            ["message"] = $"Run is stuck: {_stuckReason}. Start or load a new run.",
            ["context"] = ctx,
        };
    }

    private static Dictionary<string, object?> ErrorWithTrace(string context, Exception ex)
    {
        var inner = ex;
        while (inner.InnerException != null) inner = inner.InnerException;
        var err = new Dictionary<string, object?>
        {
            ["type"] = "error",
            ["code"] = "exception",
            ["message"] = $"{context}: {inner.GetType().Name}: {inner.Message}",
        };
        // Stack traces are for engine developers, not agents (STS2_DEBUG=1 to include them).
        if (DebugTraces) err["stack_trace"] = inner.StackTrace;
        return err;
    }

    public Dictionary<string, object?> GetFullMap()
    {
        if (_runState?.Map == null)
            return Error("No map available");

        var map = _runState.Map;
        var rows = new List<List<Dictionary<string, object?>>>();
        var currentCoord = _runState.CurrentMapCoord;
        var visited = _runState.VisitedMapCoords ?? (IReadOnlyList<MapCoord>)Array.Empty<MapCoord>();
        // Same Traveled/Travelable states the map screen draws. Travel is disabled while a
        // room is open, so nothing is travelable then.
        var canTravel = _runState.CurrentRoom is MapRoom || _runState.CurrentRoom == null;
        var travelable = canTravel
            ? TravelablePoints(map).Select(p => p.coord).ToHashSet()
            : new HashSet<MapCoord>();

        Dictionary<string, object?> NodeDict(MapPoint point)
        {
            var children = point.Children?.Select(ch => new Dictionary<string, object?>
            {
                ["col"] = (int)ch.coord.col,
                ["row"] = (int)ch.coord.row,
            }).ToList();
            var node = new Dictionary<string, object?>
            {
                ["col"] = (int)point.coord.col,
                ["row"] = (int)point.coord.row,
                ["type"] = point.PointType.ToString(),
                ["children"] = children,
                ["visited"] = visited.Contains(point.coord),
                ["current"] = currentCoord.HasValue && currentCoord.Value == point.coord,
                ["travelable"] = travelable.Contains(point.coord),
            };
            if (point.PointType == MapPointType.Unknown && visited.Contains(point.coord)
                && RevealedRoomType(point) is { } revealed)
                node["revealed_type"] = revealed;
            return node;
        }

        for (int row = 0; row < map.GetRowCount(); row++)
        {
            var rowNodes = new List<Dictionary<string, object?>>();
            foreach (var point in map.GetPointsInRow(row))
            {
                if (point == null) continue;
                rowNodes.Add(NodeDict(point));
            }
            if (rowNodes.Count > 0)
                rows.Add(rowNodes);
        }

        // Start (Neow / Ancient) node, the root of the graph; it lives outside the grid.
        var startNode = NodeDict(map.StartingMapPoint);

        var bossNode = NodeDict(map.BossMapPoint);
        if (BossInfo(_runState.Act?.BossEncounter) is { } boss)
        {
            bossNode["id"] = boss["id"];
            bossNode["name"] = boss["name"];
        }

        Dictionary<string, object?>? secondBossNode = null;
        if (map.SecondBossMapPoint != null)
        {
            secondBossNode = NodeDict(map.SecondBossMapPoint);
            if (BossInfo(_runState.Act?.SecondBossEncounter) is { } boss2)
            {
                secondBossNode["id"] = boss2["id"];
                secondBossNode["name"] = boss2["name"];
            }
        }

        var result = new Dictionary<string, object?>
        {
            ["type"] = "map",
            ["context"] = RunContext(),
            ["start"] = startNode,
            ["rows"] = rows,
            ["boss"] = bossNode,
            ["current_coord"] = currentCoord.HasValue ? new Dictionary<string, object?>
            {
                ["col"] = (int)currentCoord.Value.col,
                ["row"] = (int)currentCoord.Value.row,
            } : null,
            ["visited_path"] = visited.Select(v => new Dictionary<string, object?>
            {
                ["col"] = (int)v.col,
                ["row"] = (int)v.row,
            }).ToList(),
        };
        if (secondBossNode != null) result["second_boss"] = secondBossNode;
        try { if (MegaCrit.Sts2.Core.Hooks.Hook.ShouldAllowFreeTravel(_runState)) result["free_travel_available"] = true; }
        catch { }

        // Top-bar hover tips: the ascension levels in effect (AscensionHelper.GetHoverTip) and
        // the modifiers with their descriptions; the run timer (NRunTimer, opt-in in the game).
        var asc = _runState.AscensionLevel;
        result["ascension_effects"] = Enumerable.Range(1, Math.Max(0, Math.Min(asc, 10))).Select(i => new Dictionary<string, object?>
        {
            ["level"] = i,
            ["name"] = _loc.Bilingual("ascension", $"LEVEL_{i:00}.title"),
            ["description"] = _loc.Bilingual("ascension", $"LEVEL_{i:00}.description"),
        }).ToList();
        try
        {
            result["modifiers"] = _runState.Modifiers.Select(m => ModifierInfo(m, true)).ToList();
        }
        catch { }
        try { result["run_time_seconds"] = RunManager.Instance.RunTime; } catch { }
        return result;
    }

    public void CleanUp()
    {
        ReleaseRunLeftovers();
        try
        {
            if (RunManager.Instance.IsInProgress)
                RunManager.Instance.CleanUp(graceful: true);
            _runState = null;
        }
        catch (Exception ex)
        {
            Log($"CleanUp exception: {ex}");
        }
        // RunManager.CleanUp aborts at the first exception, and CombatManager.Reset(graceful),
        // which it calls, throws if a creature's power-removal hook throws (typical after a
        // stuck combat). The combat then survives, and the next SetUpCombat throws "Make sure
        // to reset the combat before setting up a new one." Drop it explicitly.
        ForceResetCombat();
        ReleaseRunLeftoversAfterCleanUp();
    }

    private void ForceResetCombat()
    {
        var combat = CombatManager.Instance;
        if (combat.CurrentCombatId == null) return;  // normal path: nothing left over
        Log($"Combat #{combat.CurrentCombatId} survived cleanup; dropping it");
        var state = combat.DebugOnlyGetState();
        if (state != null)
        {
            foreach (var creature in state.Creatures.ToList())
            {
                // Same steps as CombatManager.Reset(graceful: true), each one isolated.
                try { creature.Reset(); } catch (Exception ex) { Log($"  creature.Reset: {ex}"); }
                try { combat.RemoveCreature(creature); } catch (Exception ex) { Log($"  RemoveCreature: {ex.Message}"); }
                try { state.RemoveCreature(creature); } catch (Exception ex) { Log($"  state.RemoveCreature: {ex.Message}"); }
            }
        }
        // Non-graceful: clears the turn state, history and effect depth, cancels the dead loop.
        try { combat.Reset(graceful: false); }
        catch (Exception ex) { Log($"  CombatManager.Reset: {ex}"); }
    }

    // ─── Run lifecycle (reusing one process for many runs) ───

    private IDisposable? _selectorScope;
    private CombatManager? _handlersAttachedTo;

    private void AttachCombatHandlers()
    {
        // Don't stack a second pair of handlers on the same CombatManager across runs.
        var combat = CombatManager.Instance;
        if (ReferenceEquals(_handlersAttachedTo, combat)) return;
        combat.TurnStarted += _ => _turnStarted.Set();
        combat.CombatEnded += _ => _combatEnded.Set();
        _handlersAttachedTo = combat;
    }

    private void AttachCardSelector()
    {
        _selectorScope?.Dispose();
        _selectorScope = CardSelectCmd.UseSelector(_cardSelector);
        if (_runState != null) _cardSelector.BeginRun(_runState);
        LocPatches._bundleSimRef = this;
    }

    // Set when a previous run's game flow could not be stopped; the process can't be reused.
    private bool _poisoned;

    private Dictionary<string, object?> RestartRequired() => new()
    {
        ["type"] = "error",
        ["code"] = "restart_required",
        ["message"] = "A game flow from the previous run is still running; restart the engine process.",
    };

    private void CancelAllPrompts()
    {
        List<EventRewardFrame> frames;
        lock (_eventFrames) { frames = _eventFrames.AsEnumerable().Reverse().ToList(); _eventFrames.Clear(); }
        foreach (var f in frames) f.Tcs.TrySetResult();  // innermost first
        var sphere = _crystalSphere;
        _crystalSphere = null;
        try { sphere?.ForceMinigameEnd(); } catch (Exception ex) { Log($"Crystal Sphere end: {ex.Message}"); }
        var bundleTcs = _pendingBundleTcs;
        _pendingBundles = null;
        _pendingBundleTcs = null;
        _cardSelector.Reset();
        bundleTcs?.TrySetResult(Array.Empty<CardModel>());
    }

    /// <summary>
    /// Tear down the previous run, if any, so start_run / load_save can be called repeatedly
    /// in one process without paying the one-time engine initialisation again.
    /// Returns false if the previous run's background flow wouldn't stop (process unusable).
    /// </summary>
    private bool ResetForNewRun()
    {
        if (_poisoned) return false;
        _stuckReason = null;
        if (_runState == null && !RunManager.Instance.IsInProgress)
        {
            ResetEvents();
            return true;
        }

        Log("Resetting previous run");
        // Unblock the previous run's game flow and let it unwind completely. Each cancelled
        // prompt can make it open another one, so keep cancelling until the flow is done.
        LocPatches._bundleSimRef = null;
        var sw = System.Diagnostics.Stopwatch.StartNew();
        while (true)
        {
            CancelAllPrompts();
            _syncCtx.Pump();
            var task = _backgroundTask is { IsCompleted: false } ? _backgroundTask
                : _detachedTask is { IsCompleted: false } ? _detachedTask : null;
            if (task == null && !HasPendingPrompt) break;
            if (sw.ElapsedMilliseconds > 5000)
            {
                Log("Previous run's background flow did not stop; process must be restarted");
                _poisoned = true;
                return false;
            }
            if (task != null)
                ((IAsyncResult)task).AsyncWaitHandle.WaitOne(1);
        }
        _backgroundTask = null;
        _detachedTask = null;

        CleanUp();
        _syncCtx.Pump();

        _selectorScope?.Dispose();
        _selectorScope = null;
        CardSelectCmd.Reset();
        CancelAllPrompts();
        YieldPatches.SuppressYield = false;

        _eventOptionChosen = false;
        _lastEventOptionCount = 0;
        _pendingRewards = null;
        _rewardsProcessed = false;
        _rewardsProceeded = false;
        _rewardDone.Clear();
        ContinueSave.Capture(null);
        ResetRoomParityState();
        _treasureFinishPending = null;
        _treasureChestOpened = null;
        _goldBeforeCombat = 0;
        _lastKnownHp = 0;
        _turnStarted.Reset();
        _combatEnded.Reset();
        // Last: unwinding the old flow above still appends to its combat history and flashes
        // relics; none of that belongs to the run about to start.
        ResetEvents();
        return true;
    }

    #endregion
}
