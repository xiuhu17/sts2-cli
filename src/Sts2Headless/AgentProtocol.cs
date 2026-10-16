using MegaCrit.Sts2.Core.CardSelection;
using MegaCrit.Sts2.Core.Combat;
using MegaCrit.Sts2.Core.Combat.History;
using MegaCrit.Sts2.Core.Combat.History.Entries;
using MegaCrit.Sts2.Core.Commands;
using MegaCrit.Sts2.Core.Entities.Cards;
using MegaCrit.Sts2.Core.Entities.Creatures;
using MegaCrit.Sts2.Core.Entities.Players;
using MegaCrit.Sts2.Core.GameActions.Multiplayer;
using MegaCrit.Sts2.Core.Localization;
using MegaCrit.Sts2.Core.Models;
using MegaCrit.Sts2.Core.Rooms;
using MegaCrit.Sts2.Core.Runs;
using HarmonyLib;

namespace Sts2Headless;

/// <summary>
/// Agent-facing protocol layer: what a card selection is for, strict action validation,
/// read-only queries (get_state, get_deck, ...), the per-decision "events" log and the
/// lean (omit_deck) view. Kept apart from the game-driving code in RunSimulator.cs.
/// </summary>
public partial class RunSimulator
{
    // ───────────────────────── options ─────────────────────────

    /// <summary>Cheat/debug commands (set_player, enter_room, set_draw_order, ...) break
    /// human parity; they are only accepted with STS2_DEBUG=1 (or STS2_DEBUG_COMMANDS=1,
    /// or the engine started with --debug).</summary>
    public static bool DebugCommands { get; set; } =
        Environment.GetEnvironmentVariable("STS2_DEBUG") == "1"
        || Environment.GetEnvironmentVariable("STS2_DEBUG_COMMANDS") == "1";

    /// <summary>Include .NET stack traces in error responses (developers only).</summary>
    public static bool DebugTraces =>
        Environment.GetEnvironmentVariable("STS2_DEBUG") == "1"
        || Environment.GetEnvironmentVariable("STS2_DEBUG_TRACES") == "1";

    /// <summary>start_run/load_save "omit_deck": leave player.deck out of every decision
    /// (player.deck_size and player.deck_version stay; get_deck returns the cards).</summary>
    public bool OmitDeck { get; set; }

    // The last decision sent to the client (with legal_actions). Used to validate actions
    // and to answer get_state without touching the game.
    private Dictionary<string, object?>? _lastDecision;

    // ───────────────────────── card selection context ─────────────────────────

    /// <summary>What the game's card-selection screen shows besides the cards.</summary>
    internal sealed class SelectContext
    {
        public string Purpose = "choose";     // upgrade|remove|transform|enchant|discard|exhaust|choose
        public string Screen = "grid";        // hand|deck|grid|draw|discard|exhaust|choose_a_card|reward_grid
        public string? Prompt;                // the localized prompt text the screen shows
        public bool Cancelable;               // the screen has a cancel / skip button
        public bool RequireConfirm;
        public int ForceMinSelect;            // choose-a-card without skip: a pick is required
        public AbstractModel? Source;         // the card/relic/power/potion that asked
        public EnchantmentModel? Enchantment; // enchant screens: what gets applied
        public int EnchantAmount;
        public Func<CardModel, CardTransformation>? Transform;
    }

    /// <summary>
    /// Harmony prefixes on CardSelectCmd's entry points. ICardSelector.GetSelectedCards only
    /// receives (options, min, max); the prompt, the cancel flag and the kind of screen live
    /// in CardSelectorPrefs and the method called. Each prefix stages them; the selector
    /// takes the staged context when the game asks it for cards. Every caller of
    /// GetSelectedCards goes through one of these methods, so a stage is never stale.
    /// </summary>
    internal static class SelectPromptPatches
    {
        private static readonly object Lock = new();
        private static SelectContext? _staged;
        // >0 while a wrapper (FromDeckForRemoval, FromHandForDiscard) is calling the generic
        // method it is built on: the wrapper's context is the more specific one.
        private static int _outer;

        public static SelectContext? Take()
        {
            lock (Lock) { var c = _staged; _staged = null; return c; }
        }

        private static void Stage(SelectContext c, bool inner)
        {
            lock (Lock)
            {
                if (inner && _outer > 0) return;
                _staged = c;
            }
        }

        private static string? Format(LocString? ls)
        {
            if (ls == null) return null;
            var text = TryFormat(() => ls.GetFormattedText());
            if (text != null) return text;
            var raw = _loc.Bilingual(ls.LocTable, ls.LocEntryKey);
            return raw == ls.LocEntryKey ? null : LocLookup.Clean(raw);
        }

        private static SelectContext FromPrefs(CardSelectorPrefs prefs, string purpose, string screen, AbstractModel? source)
        {
            var ctx = new SelectContext
            {
                Purpose = purpose,
                Screen = screen,
                Cancelable = prefs.Cancelable,
                RequireConfirm = prefs.RequireManualConfirmation,
                Source = source,
            };
            try
            {
                var p = prefs.Prompt;
                if (p != null)
                {
                    ctx.Prompt = Format(p);
                    // The generic prompts say what happens to the chosen cards.
                    if (purpose == "choose" && p.LocTable == "card_selection" && p.LocEntryKey.StartsWith("TO_"))
                        ctx.Purpose = p.LocEntryKey[3..].ToLowerInvariant();
                }
            }
            catch { }
            return ctx;
        }

        private static AbstractModel? Involved(PlayerChoiceContext? context)
        {
            try { return context?.LastInvolvedModel; } catch { return null; }
        }

        public static void Upgrade(CardSelectorPrefs prefs) => Stage(FromPrefs(prefs, "upgrade", "deck", null), false);

        public static void Transform(CardSelectorPrefs prefs, Func<CardModel, CardTransformation>? cardToTransformation)
        {
            var c = FromPrefs(prefs, "transform", "deck", null);
            c.Transform = cardToTransformation;
            Stage(c, false);
        }

        public static void Enchant(EnchantmentModel enchantment, int amount, CardSelectorPrefs prefs)
        {
            var c = FromPrefs(prefs, "enchant", "deck", null);
            c.Enchantment = enchantment;
            c.EnchantAmount = amount;
            Stage(c, false);
        }

        public static void Removal(CardSelectorPrefs prefs)
        {
            Stage(FromPrefs(prefs, "remove", "deck", null), false);
            lock (Lock) _outer++;
        }

        public static void Discard(PlayerChoiceContext context, CardSelectorPrefs prefs, AbstractModel source)
        {
            Stage(FromPrefs(prefs, "discard", "hand", source ?? Involved(context)), false);
            lock (Lock) _outer++;
        }

        public static void OuterDone()
        {
            lock (Lock) if (_outer > 0) _outer--;
        }

        public static void DeckGeneric(CardSelectorPrefs prefs) => Stage(FromPrefs(prefs, "choose", "deck", null), true);

        public static void Hand(PlayerChoiceContext context, CardSelectorPrefs prefs, AbstractModel source) =>
            Stage(FromPrefs(prefs, "choose", "hand", source ?? Involved(context)), true);

        public static void HandUpgrade(PlayerChoiceContext context, AbstractModel source)
        {
            // The game builds these prefs inside the method (CHOOSE_CARD_UPGRADE_HEADER, 1 card).
            Stage(new SelectContext
            {
                Purpose = "upgrade",
                Screen = "hand",
                Prompt = Format(new LocString("gameplay_ui", "CHOOSE_CARD_UPGRADE_HEADER")),
                Source = source ?? Involved(context),
            }, false);
        }

        public static void CombatPile(PlayerChoiceContext context, CardPile pile, CardSelectorPrefs prefs)
        {
            string screen;
            try { screen = pile.Type.ToString().ToLowerInvariant(); } catch { screen = "pile"; }
            Stage(FromPrefs(prefs, "choose", screen, Involved(context)), false);
        }

        public static void SimpleGrid(PlayerChoiceContext context, CardSelectorPrefs prefs) =>
            Stage(FromPrefs(prefs, "choose", "grid", Involved(context)), false);

        public static void GridForRewards(PlayerChoiceContext context, CardSelectorPrefs prefs) =>
            Stage(FromPrefs(prefs, "choose", "reward_grid", Involved(context)), false);

        public static void ChooseACard(PlayerChoiceContext context, bool canSkip)
        {
            Stage(new SelectContext
            {
                Purpose = "choose",
                Screen = "choose_a_card",
                Prompt = Format(new LocString("gameplay_ui", "CHOOSE_CARD_HEADER")),
                Cancelable = canSkip,
                ForceMinSelect = canSkip ? 0 : 1,
                Source = Involved(context),
            }, false);
        }

        public static void Install()
        {
            var harmony = new Harmony("sts2headless.selectprompt");
            var t = typeof(CardSelectCmd);
            var done = HarmonyMethodFor(nameof(OuterDone));
            int patched = 0;
            void Patch(string method, string prefix, bool outer = false)
            {
                foreach (var m in t.GetMethods(System.Reflection.BindingFlags.Public | System.Reflection.BindingFlags.Static)
                             .Where(m => m.Name == method))
                {
                    try
                    {
                        harmony.Patch(m, prefix: HarmonyMethodFor(prefix), finalizer: outer ? done : null);
                        patched++;
                    }
                    catch (Exception ex) { Console.Error.WriteLine($"[WARN] select prompt patch {method}: {ex.Message}"); }
                }
            }
            Patch("FromDeckForUpgrade", nameof(Upgrade));
            Patch("FromDeckForTransformation", nameof(Transform));
            Patch("FromDeckForEnchantment", nameof(Enchant));
            Patch("FromDeckForRemoval", nameof(Removal), outer: true);
            Patch("FromDeckGeneric", nameof(DeckGeneric));
            Patch("FromHandForDiscard", nameof(Discard), outer: true);
            Patch("FromHand", nameof(Hand));
            Patch("FromHandForUpgrade", nameof(HandUpgrade));
            Patch("FromCombatPile", nameof(CombatPile));
            Patch("FromSimpleGrid", nameof(SimpleGrid));
            Patch("FromSimpleGridForRewards", nameof(GridForRewards));
            Patch("FromChooseACardScreen", nameof(ChooseACard));
            Console.Error.WriteLine($"[INFO] Card selection prompt patches: {patched}");
        }

        private static HarmonyMethod HarmonyMethodFor(string name) =>
            new(typeof(SelectPromptPatches).GetMethod(name, System.Reflection.BindingFlags.Public | System.Reflection.BindingFlags.Static));
    }


    private static string EventTitle(string entry)
    {
        var name = _loc.Bilingual("ancients", entry + ".title");
        return name == entry + ".title" ? _loc.Event(entry) : name;
    }

    private static string ModelKind(AbstractModel m) => m switch
    {
        CardModel => "card",
        RelicModel => "relic",
        PotionModel => "potion",
        PowerModel => "power",
        OrbModel => "orb",
        EventModel => "event",
        _ => "other",
    };

    /// <summary>Add what the selection screen tells a human: purpose, prompt, cancel button,
    /// the source, enchant/transform previews, and (in combat) the battlefield.</summary>
    private void AugmentCardSelect(Dictionary<string, object?> sel, Player player)
    {
        var ctx = _cardSelector.PendingContext;
        var options = _cardSelector.PendingOptions;
        var cards = sel.GetValueOrDefault("cards") as List<Dictionary<string, object?>>;
        sel["purpose"] = ctx?.Purpose ?? "choose";
        if (ctx?.Prompt != null) sel["prompt"] = ctx.Prompt;
        sel["cancelable"] = ctx?.Cancelable ?? false;
        if (ctx != null) sel["screen"] = ctx.Screen;

        var source = ctx?.Source;
        if (source != null)
            sel["source"] = new Dictionary<string, object?> { ["name"] = ModelName(source), ["kind"] = ModelKind(source) };
        else if (_runState?.CurrentRoom is EventRoom)
        {
            try
            {
                var ev = RunManager.Instance.EventSynchronizer?.GetLocalEvent();
                if (ev != null) sel["source"] = new Dictionary<string, object?> { ["name"] = EventTitle(ev.Id.Entry), ["kind"] = "event" };
            }
            catch { }
        }

        if (ctx?.Enchantment != null && cards != null && options != null)
        {
            try
            {
                var ench = ctx.Enchantment.ToMutable();
                ench.Amount = ctx.EnchantAmount;
                ench.RecalculateValues();
                sel["enchantment"] = new Dictionary<string, object?>
                {
                    ["id"] = ctx.Enchantment.Id.Entry,
                    ["name"] = _loc.Bilingual("enchantments", ctx.Enchantment.Id.Entry + ".title"),
                    ["amount"] = ctx.EnchantAmount,
                    ["description"] = TryFormat(() => ench.DynamicDescription.GetFormattedText())
                                      ?? _loc.Bilingual("enchantments", ctx.Enchantment.Id.Entry + ".description"),
                };
            }
            catch (Exception ex) { Log($"enchant preview: {ex.Message}"); }
            for (int i = 0; i < cards.Count && i < options.Count; i++)
            {
                var after = EnchantPreview(options[i], ctx.Enchantment, ctx.EnchantAmount);
                if (after != null) cards[i]["after_enchant"] = after;
            }
        }
        if (ctx?.Transform != null && cards != null && options != null)
        {
            for (int i = 0; i < cards.Count && i < options.Count; i++)
            {
                try
                {
                    var tr = ctx.Transform(options[i]);
                    if (tr.Replacement != null)
                        cards[i]["transforms_to"] = new Dictionary<string, object?>
                        {
                            ["name"] = _loc.Card(tr.Replacement.Id.Entry) + (tr.Replacement.IsUpgraded ? "+" : ""),
                            ["description"] = tr.Replacement.IsMutable ? PreviewedCard(tr.Replacement, PileType.None).text : CardText(tr.Replacement),
                        };
                    else if (tr.ReplacementOptions != null)
                        cards[i]["transform_options"] = tr.ReplacementOptions
                            .Select(o => _loc.Card(o.Id.Entry) + (o.IsUpgraded ? "+" : "")).Distinct().ToList();
                }
                catch (Exception ex) { Log($"transform preview: {ex.Message}"); }
            }
        }

        if (CombatManager.Instance.IsInProgress)
        {
            try
            {
                var cs = CombatPlayState(player);
                var combat = new Dictionary<string, object?>();
                foreach (var k in new[] { "round", "energy", "max_energy", "stars", "hand", "enemies", "player_powers",
                                          "orbs", "orb_slots", "osty", "draw_pile_count", "discard_pile_count", "exhaust_pile_count" })
                    if (cs.TryGetValue(k, out var v) && v != null) combat[k] = v;
                sel["combat"] = combat;
            }
            catch (Exception ex) { Log($"card_select combat snapshot: {ex.Message}"); }
        }
    }

    /// <summary>The card as the enchant screen previews it (NEnchantPreview.Init): a clone
    /// with the enchantment applied. The clone is not added to the run.</summary>
    private Dictionary<string, object?>? EnchantPreview(CardModel card, EnchantmentModel enchantment, int amount)
    {
        try
        {
            var clone = (CardModel)card.ClonePreservingMutability();
            var e = enchantment.ToMutable();
            clone.EnchantInternal(e, amount);
            clone.IsEnchantmentPreview = true;
            e.ModifyCard();
            // NCard.UpdateVisuals(PileType.None, Normal): the card's and the new enchantment's
            // vars are previewed, so its hooks (Sharp's extra damage, ...) and its own extra
            // text values show up.
            var (text, stats) = PreviewedCard(clone, PileType.None);
            return new Dictionary<string, object?>
            {
                ["cost"] = clone.EnergyCost?.GetResolved() ?? 0,
                ["stats"] = stats.Count > 0 ? stats : null,
                ["description"] = text,
            };
        }
        catch (Exception ex)
        {
            Log($"EnchantPreview {card.Id.Entry}: {ex.Message}");
            return null;
        }
    }

    // ───────────────────────── background flow faults ─────────────────────────

    private Exception? _flowFault;

    private void NoteFlowFault(AggregateException? ex)
    {
        if (ex != null) _flowFault = ex.GetBaseException();
    }

    // ───────────────────────── action validation ─────────────────────────

    internal static readonly string[] AllActions =
    {
        "select_map_node", "play_card", "end_turn", "choose_option", "select_card_reward", "skip_card_reward",
        "buy_card", "buy_relic", "buy_potion", "remove_card", "select_bundle", "crystal_sphere_click",
        "select_cards", "skip_select", "use_potion", "discard_potion", "leave_room", "proceed",
        "claim_reward", "skip_rewards", "select_treasure_relic", "skip_treasure_relic",
        "select_card_alternative",
    };

    private static readonly string[] IntArgs =
        { "card_index", "target_index", "potion_index", "option_index", "reward_index", "relic_index", "bundle_index", "alternative_index", "col", "row", "x", "y" };

    private static List<Dictionary<string, object?>> LegalOf(Dictionary<string, object?> decision)
    {
        if (decision.GetValueOrDefault("legal_actions") is List<Dictionary<string, object?>> l) return l;
        try { return LegalActions.For(decision); } catch { return new(); }
    }

    /// <summary>"choose_option(option_index=0|1|2), leave_room" — a short list for error messages.</summary>
    internal static string SummarizeLegal(List<Dictionary<string, object?>> legal)
    {
        var parts = new List<string>();
        foreach (var g in legal.GroupBy(a => a.GetValueOrDefault("action") as string ?? "?"))
        {
            var argVals = new SortedDictionary<string, SortedSet<string>>();
            foreach (var a in g)
            {
                if (a.GetValueOrDefault("args") is Dictionary<string, object?> args)
                    foreach (var (k, v) in args)
                    {
                        if (!argVals.TryGetValue(k, out var set)) argVals[k] = set = new SortedSet<string>(StringComparer.Ordinal);
                        set.Add(v?.ToString() ?? "null");
                    }
                if (a.GetValueOrDefault("choose") is Dictionary<string, object?> ch)
                    argVals["indices"] = new SortedSet<string> { $"{ch.GetValueOrDefault("min")}-{ch.GetValueOrDefault("max")} of [{string.Join(",", (ch.GetValueOrDefault("from") as System.Collections.IEnumerable)?.Cast<object>() ?? Array.Empty<object>())}]" };
                if (a.ContainsKey("choose_cell"))
                    argVals["x,y,tool"] = new SortedSet<string> { "a hidden cell" };
            }
            parts.Add(argVals.Count == 0 ? g.Key
                : $"{g.Key}({string.Join(", ", argVals.Select(kv => $"{kv.Key}={string.Join("|", kv.Value)}"))})");
        }
        return parts.Count == 0 ? "none (start_run or load_save to play again)" : string.Join("; ", parts);
    }

    private Dictionary<string, object?> CurrentDecision()
    {
        var d = DetectDecisionPoint();
        LegalActions.Attach(d);
        return d;
    }

    private static string Where(string decision) => decision switch
    {
        "map_select" => "the map",
        "combat_play" => "your combat turn",
        "card_reward" => "the card reward screen",
        "card_select" => "the card selection screen",
        "bundle_select" => "the pack selection screen",
        "event_choice" => "this event",
        "rest_site" => "the rest site",
        "shop" => "the shop",
        "rewards" => "the rewards screen",
        "treasure_relic" => "the treasure chest",
        "crystal_sphere" => "the Crystal Sphere minigame",
        _ => decision,
    };

    private static Dictionary<string, object?> IllegalAction(Dictionary<string, object?> decision, string action, string why)
    {
        var name = decision.GetValueOrDefault("decision") as string ?? "?";
        var legal = LegalOf(decision);
        return new Dictionary<string, object?>
        {
            ["type"] = "error",
            ["code"] = "illegal_action",
            ["message"] = $"'{action}' is not allowed now: {why}. Current decision: {name}. Legal: {SummarizeLegal(legal)}",
            ["decision"] = name,
            ["legal_actions"] = legal,
        };
    }

    private static string? Suggest(string action)
    {
        var a = action.Trim().ToLowerInvariant().Replace('-', '_').Replace(' ', '_');
        if (AllActions.Contains(a)) return a;
        return AllActions.FirstOrDefault(x => x.Contains(a) || a.Contains(x))
               ?? AllActions.OrderBy(x => Levenshtein(x, a)).FirstOrDefault(x => Levenshtein(x, a) <= 3);
    }

    private static int Levenshtein(string s, string t)
    {
        var d = new int[s.Length + 1, t.Length + 1];
        for (int i = 0; i <= s.Length; i++) d[i, 0] = i;
        for (int j = 0; j <= t.Length; j++) d[0, j] = j;
        for (int i = 1; i <= s.Length; i++)
            for (int j = 1; j <= t.Length; j++)
                d[i, j] = Math.Min(Math.Min(d[i - 1, j] + 1, d[i, j - 1] + 1), d[i - 1, j - 1] + (s[i - 1] == t[j - 1] ? 0 : 1));
        return d[s.Length, t.Length];
    }

    /// <summary>
    /// The "action" command: validate the verb against the current decision and the argument
    /// types, run it, then report background-flow failures. Errors name the current decision
    /// and its legal actions.
    /// </summary>
    public Dictionary<string, object?> Act(string action, Dictionary<string, object?>? args)
    {
        if (_poisoned || _runState == null || _stuckReason != null)
            return ExecuteAction(action, args);

        if (!AllActions.Contains(action))
        {
            var s = Suggest(action);
            var cur0 = _lastDecision ?? SafeCurrentDecision();
            var err = Error($"Unknown action '{action}'.{(s != null ? $" Did you mean '{s}'?" : "")} " +
                            $"Legal now: {(cur0 != null ? SummarizeLegal(LegalOf(cur0)) : "?")}");
            err["code"] = "unknown_action";
            if (cur0 != null) err["decision"] = cur0.GetValueOrDefault("decision");
            return err;
        }

        // Argument types: indices are integers (numeric strings are accepted).
        if (args != null)
        {
            foreach (var key in IntArgs)
            {
                if (!args.TryGetValue(key, out var v) || v == null || v is int) continue;
                if (v is string str && int.TryParse(str.Trim(), out var parsed)) { args[key] = parsed; continue; }
                var bad = Error($"args.{key} must be an integer, got {(v is string ? $"\"{v}\"" : v.ToString())}");
                bad["code"] = "bad_request";
                return bad;
            }
        }

        // Validate against the decision the client was shown; recompute before rejecting in
        // case the game moved on by itself (a detached background flow).
        // Strict: the action (after aliases) must be one of legal_actions, arguments included.
        var cur = _lastDecision;
        string? why = null;
        if (cur != null)
        {
            var (a0, args0) = Canonical(cur, action, args);
            why = CheckLegal(cur, a0, ref args0);
            if (why == null) { action = a0; args = args0; }
        }
        if (cur == null || why != null)
        {
            cur = SafeCurrentDecision();
            // No decision to check the action against: never run it unvalidated.
            if (cur == null) return Error($"'{action}' rejected: the current decision could not be determined");
            if (cur.GetValueOrDefault("type") as string != "decision")
                return cur.GetValueOrDefault("type") as string == "error" ? cur
                    : Error($"'{action}' rejected: no decision is waiting ({cur.GetValueOrDefault("type")})");
            _lastDecision = cur = Viewed(cur);
            var (a1, args1) = Canonical(cur, action, args);
            why = CheckLegal(cur, a1, ref args1);
            if (why != null) return IllegalAction(cur, action, why);
            action = a1; args = args1;
        }

        _flowFault = null;
        var r = ExecuteAction(action, args);
        if (r.GetValueOrDefault("type") as string == "error")
        {
            // Errors say where the client is and what it can do.
            var now = SafeCurrentDecision();
            if (now != null && now.GetValueOrDefault("type") as string == "decision")
            {
                _lastDecision = now = Viewed(now);
                r.TryAdd("decision", now.GetValueOrDefault("decision"));
                r.TryAdd("legal_actions", LegalOf(now));
            }
        }
        else if (_flowFault != null && r.GetValueOrDefault("type") as string == "decision")
        {
            var f = _flowFault;
            _flowFault = null;
            LegalActions.Attach(r);
            return new Dictionary<string, object?>
            {
                ["type"] = "error",
                ["code"] = "flow_failed",
                ["message"] = $"The game flow started by '{action}' failed: {f.GetType().Name}: {f.Message}. " +
                              "The current state is in 'state'.",
                ["decision"] = r.GetValueOrDefault("decision"),
                ["state"] = r,
            };
        }
        return r;
    }

    private Dictionary<string, object?>? SafeCurrentDecision()
    {
        try { return _runState == null ? null : CurrentDecision(); }
        catch (Exception ex) { Log($"CurrentDecision: {ex.Message}"); return null; }
    }

    // ───────────────────────── post-processing of every response ─────────────────────────

    private static readonly HashSet<string> QueryCommands = new()
    {
        "get_state", "state", "get_deck", "get_relics", "get_potions", "get_piles", "get_card", "get_map", "help",
    };

    /// <summary>Called by Program after each command (legal_actions already attached).
    /// Remembers the decision, attaches the events since the previous decision, applies the
    /// lean view and orders the keys so a truncated line still shows what matters.</summary>
    public Dictionary<string, object?> AfterCommand(string cmd, Dictionary<string, object?> r)
    {
        var isQuery = QueryCommands.Contains(cmd);
        var inner = r.GetValueOrDefault("type") as string == "error" && r.GetValueOrDefault("state") is Dictionary<string, object?> st ? st : r;
        if (!isQuery && inner.GetValueOrDefault("type") as string == "decision")
        {
            var events = DrainEvents();
            if (events.Count > 0) inner["events"] = events;
            ApplyView(inner);
            var ordered = Ordered(inner);
            _lastDecision = ordered;
            if (ReferenceEquals(inner, r)) return ordered;
            r["state"] = ordered;
            return r;
        }
        // Anything else that may have changed the game (debug commands, a failed start) makes
        // the remembered decision stale; action errors already refreshed it.
        if (!isQuery && cmd != "action") _lastDecision = null;
        if (cmd is "get_state" or "state" && r.GetValueOrDefault("type") as string == "decision") _lastDecision = r;
        return r;
    }

    /// <summary>A decision as clients get it (lean view, key order), without draining events.</summary>
    private Dictionary<string, object?> Viewed(Dictionary<string, object?> d)
    {
        if (d.GetValueOrDefault("type") as string != "decision") return d;
        ApplyView(d);
        return Ordered(d);
    }

    private static readonly string[] LeadingKeys = { "type", "decision", "legal_actions", "events" };

    private static Dictionary<string, object?> Ordered(Dictionary<string, object?> r)
    {
        var o = new Dictionary<string, object?>(r.Count);
        foreach (var k in LeadingKeys) if (r.TryGetValue(k, out var v)) o[k] = v;
        foreach (var (k, v) in r) if (!o.ContainsKey(k)) o[k] = v;
        return o;
    }

    private void ApplyView(Dictionary<string, object?> r)
    {
        // Shop: "cost" is gold there but energy everywhere else; add unambiguous names.
        if (r.GetValueOrDefault("decision") as string == "shop")
        {
            foreach (var key in new[] { "cards", "relics", "potions" })
                if (r.GetValueOrDefault(key) is System.Collections.IEnumerable items)
                    foreach (var it in items.OfType<Dictionary<string, object?>>())
                    {
                        if (it.TryGetValue("cost", out var price)) it.TryAdd("price", price);
                        if (key == "cards" && it.TryGetValue("card_cost", out var ec)) it.TryAdd("energy_cost", ec);
                    }
        }
        if (r.GetValueOrDefault("player") is Dictionary<string, object?> p)
        {
            if (OmitDeck)
            {
                p.Remove("deck");
                p["deck_version"] = DeckVersion();
            }
        }
    }

    /// <summary>Changes whenever the deck's contents change (cards, upgrades, enchantments).</summary>
    private string DeckVersion()
    {
        var player = _runState?.Players[0];
        ulong h = 1469598103934665603UL;
        try
        {
            foreach (var c in player?.Deck?.Cards ?? Enumerable.Empty<CardModel>())
            {
                if (c == null) continue;
                var s = $"{c.Id.Entry}|{c.CurrentUpgradeLevel}|{c.Enchantment?.Id.Entry}|{c.Enchantment?.Amount}|{c.Affliction?.Id.Entry};";
                foreach (var ch in s) { h ^= ch; h *= 1099511628211UL; }
            }
        }
        catch { }
        return h.ToString("x16");
    }

    // ───────────────────────── read-only queries ─────────────────────────

    public Dictionary<string, object?> GetState()
    {
        if (_runState == null) return Error("No run in progress");
        if (_lastDecision != null) return _lastDecision;
        var d = SafeCurrentDecision();
        return d == null ? Error("No decision available") : Viewed(d);
    }

    private Player? LocalPlayer => _runState?.Players[0];

    public Dictionary<string, object?> GetDeck(bool upgrades)
    {
        var player = LocalPlayer;
        if (player == null) return Error("No run in progress");
        var cards = new List<Dictionary<string, object?>>();
        int i = 0;
        foreach (var c in player.Deck?.Cards?.Where(c => c != null) ?? Enumerable.Empty<CardModel>())
            cards.Add(CardDetail(c, i++, upgrades));
        return new Dictionary<string, object?>
        {
            ["type"] = "deck",
            ["deck_size"] = cards.Count,
            ["deck_version"] = DeckVersion(),
            ["cards"] = cards,
        };
    }

    private Dictionary<string, object?> CardDetail(CardModel c, int? index, bool upgrade, PileType pile = PileType.Deck)
    {
        var stats = new Dictionary<string, object?>();
        string? text = null;
        if (pile == PileType.Hand && CombatManager.Instance.IsInProgress)
        {
            // In hand the card shows its live values (Strength, Weak, ...), as in the combat
            // view. UpdateDynamicVarPreview mutates the live card: clear before and after.
            try
            {
                c.DynamicVars.ClearPreview();
                c.UpdateDynamicVarPreview(MegaCrit.Sts2.Core.Entities.Cards.CardPreviewMode.Normal, c.CurrentTarget, c.DynamicVars);
                foreach (var dv in c.DynamicVars.Values) stats[dv.Name.ToLowerInvariant()] = (int)dv.PreviewValue;
                if (c.Enchantment != null)
                {
                    c.Enchantment.DynamicVars.ClearPreview();
                    c.UpdateDynamicVarPreview(MegaCrit.Sts2.Core.Entities.Cards.CardPreviewMode.Normal, c.CurrentTarget, c.Enchantment.DynamicVars);
                }
                text = CardText(c, PileType.Hand, c.CurrentTarget);
            }
            catch { stats.Clear(); text = null; }
            finally { try { c.DynamicVars.ClearPreview(); c.Enchantment?.DynamicVars.ClearPreview(); } catch { } }
        }
        // Elsewhere (deck view, piles) the card shows as UpdateVisuals draws it: enchantment-
        // modified values (Sharp), no global hooks.
        if (text == null)
            (text, stats) = PreviewedCard(c, pile);
        var kws = c.Keywords?.Where(k => k != CardKeyword.None).Select(k => k.ToString()).ToList();
        var d = new Dictionary<string, object?>();
        if (index != null) d["index"] = index;
        d["id"] = c.Id.ToString();
        d["name"] = _loc.Card(c.Id.Entry) + (c.IsUpgraded ? "+" : "");
        d["cost"] = c.EnergyCost?.GetResolved() ?? 0;
        if (c.EnergyCost?.CostsX == true) d["cost_x"] = true;
        try { if (c.CurrentStarCost > 0) d["star_cost"] = c.CurrentStarCost; } catch { }
        d["type"] = c.Type.ToString();
        d["rarity"] = c.Rarity.ToString();
        d["upgraded"] = c.IsUpgraded;
        d["description"] = text ?? CardText(c, pile);
        if (stats.Count > 0) d["stats"] = stats;
        if (kws?.Count > 0) d["keywords"] = kws;
        var tips = Tips(() => c.HoverTips, _loc.Card(c.Id.Entry));
        if (tips != null) d["tips"] = tips;
        if (c.Enchantment != null)
        {
            d["enchantment"] = _loc.Bilingual("enchantments", c.Enchantment.Id.Entry + ".title");
            try { if (c.Enchantment.Amount != 0) d["enchantment_amount"] = c.Enchantment.Amount; } catch { }
        }
        if (c.Affliction != null)
        {
            d["affliction"] = _loc.Bilingual("afflictions", c.Affliction.Id.Entry + ".title");
            try { if (c.Affliction.Amount != 0) d["affliction_amount"] = c.Affliction.Amount; } catch { }
        }
        if (upgrade)
        {
            var up = GetUpgradedInfo(c);
            if (up != null) d["after_upgrade"] = up;
        }
        return d;
    }

    public Dictionary<string, object?> GetRelics()
    {
        var player = LocalPlayer;
        if (player == null) return Error("No run in progress");
        var relics = new List<Dictionary<string, object?>>();
        int i = 0;
        foreach (var r in player.Relics ?? Enumerable.Empty<RelicModel>())
        {
            var vars = new Dictionary<string, object?>();
            try { foreach (var dv in r.DynamicVars.Values) vars[dv.Name] = (int)dv.BaseValue; } catch { }
            var d = new Dictionary<string, object?>
            {
                ["index"] = i++,
                ["id"] = r.Id.Entry,
                ["name"] = _loc.Relic(r.Id.Entry),
                ["description"] = RelicText(r),
            };
            if (vars.Count > 0) d["vars"] = vars;
            try { if (r.ShowCounter) d["counter"] = r.DisplayAmount; } catch { }
            var tips = Tips(() => RelicHoverTips(r), _loc.Relic(r.Id.Entry));
            if (tips != null) d["tips"] = tips;
            relics.Add(d);
        }
        return new Dictionary<string, object?> { ["type"] = "relics", ["relics"] = relics };
    }

    private static IEnumerable<MegaCrit.Sts2.Core.HoverTips.IHoverTip>? RelicHoverTips(RelicModel r)
    {
        try { return r.HoverTipsExcludingRelic; } catch { return null; }
    }

    public Dictionary<string, object?> GetPotions()
    {
        var player = LocalPlayer;
        if (player == null) return Error("No run in progress");
        var ps = PlayerSummary(player);
        return new Dictionary<string, object?>
        {
            ["type"] = "potions",
            ["potion_slots"] = player.MaxPotionCount,
            ["potions"] = ps.GetValueOrDefault("potions"),
        };
    }

    /// <summary>Combat piles with full card text, grouped and sorted by name like the game's
    /// pile views (the draw order is never revealed).</summary>
    public Dictionary<string, object?> GetPiles(string? which)
    {
        var player = LocalPlayer;
        if (player == null) return Error("No run in progress");
        var pcs = player.PlayerCombatState;
        if (pcs == null || !CombatManager.Instance.IsInProgress)
            return Error("Not in combat: combat piles exist only during combat (use get_deck)");
        var result = new Dictionary<string, object?> { ["type"] = "piles" };
        foreach (var (name, pile, type) in new[] { ("draw", pcs.DrawPile, PileType.Draw), ("discard", pcs.DiscardPile, PileType.Discard),
                                                    ("exhaust", pcs.ExhaustPile, PileType.Exhaust), ("hand", pcs.Hand, PileType.Hand) })
        {
            if (which != null && which != "all" && which != name) continue;
            result[name] = DetailedPile(pile, type);
        }
        if (which != null && which != "all" && result.Count == 1)
            return Error($"Unknown pile '{which}' (draw, discard, exhaust, hand, all)");
        return result;
    }

    private List<Dictionary<string, object?>> DetailedPile(CardPile? pile, PileType type)
    {
        var groups = new Dictionary<string, Dictionary<string, object?>>();
        var list = new List<Dictionary<string, object?>>();
        CardModel[] cards;
        try { cards = pile?.Cards?.ToArray() ?? Array.Empty<CardModel>(); } catch { return list; }
        foreach (var c in cards)
        {
            if (c == null) continue;
            try
            {
                var key = $"{c.Id}|{c.CurrentUpgradeLevel}|{c.EnergyCost?.GetResolved()}|{c.Enchantment?.Id}|{c.Affliction?.Id}";
                if (type != PileType.Hand && groups.TryGetValue(key, out var g)) { g["count"] = (int)g["count"]! + 1; continue; }
                var d = CardDetail(c, null, false, type == PileType.Hand ? PileType.Hand : PileType.Deck);
                d["count"] = 1;
                groups[key] = d;
                list.Add(d);
            }
            catch { }
        }
        if (type != PileType.Hand)
            list.Sort((a, b) => string.CompareOrdinal(a["name"] as string, b["name"] as string));
        return list;
    }

    /// <summary>One card in detail: by deck_index, hand_index, or id (any card in the game).</summary>
    public Dictionary<string, object?> GetCard(string? where, int? index, string? id, bool upgraded)
    {
        var player = LocalPlayer;
        if (player == null && id == null) return Error("No run in progress");
        CardModel? card = null;
        var pile = PileType.Deck;
        if (id != null)
        {
            var entry = id.Trim().ToUpperInvariant().Replace(' ', '_');
            if (entry.StartsWith("CARD.")) entry = entry[5..];
            var canonical = ModelDb.AllCards.FirstOrDefault(c => c.Id.Entry == entry);
            if (canonical == null) return Error($"Unknown card id '{id}'");
            card = canonical.ToMutable();
            if (upgraded && card.IsUpgradable) { card.UpgradeInternal(); card.FinalizeUpgradeInternal(); }
        }
        else
        {
            IReadOnlyList<CardModel>? source = where switch
            {
                "hand" => player!.PlayerCombatState?.Hand?.Cards?.ToList(),
                "deck" or null => player!.Deck?.Cards?.Where(c => c != null).ToList(),
                _ => null,
            };
            if (source == null)
                return Error(where == "hand" ? "Not in combat: no hand" : $"Unknown card location '{where}' (deck, hand)");
            if (index == null || index < 0 || index >= source.Count)
                return Error($"{where ?? "deck"}_index must be 0-{source.Count - 1}");
            card = source[index.Value];
            if (where == "hand") pile = PileType.Hand;
        }
        var d = CardDetail(card, index, true, pile);
        return new Dictionary<string, object?> { ["type"] = "card", ["card"] = d };
    }

    // ───────────────────────── events since the previous decision ─────────────────────────

    private static RunSimulator? _eventSink;
    private static bool _eventHooksInstalled;
    private readonly object _evLock = new();
    private List<Dictionary<string, object?>> _events = new();
    private int _historySeen;
    private (int round, CombatSide side)? _lastTurn;
    private PlayerSnapshot? _snapshot;
    private const int MaxEvents = 80;

    private sealed class PlayerSnapshot
    {
        public int Hp, MaxHp, Gold;
        public bool InCombat;
        public List<string> Deck = new(), Relics = new(), Potions = new();
        public List<(Creature creature, string name)> Enemies = new();
    }

    /// <summary>Hook CombatHistory (everything the combat log shows) and relic flashes.</summary>
    internal static void InstallAgentPatches()
    {
        SelectPromptPatches.Install();
        try
        {
            var harmony = new Harmony("sts2headless.events");
            var flash = typeof(RelicModel).GetMethod("Flash", new[] { typeof(IEnumerable<Creature>) });
            if (flash != null)
                harmony.Patch(flash, postfix: new HarmonyMethod(typeof(RunSimulator).GetMethod(nameof(RelicFlashPostfix),
                    System.Reflection.BindingFlags.Static | System.Reflection.BindingFlags.NonPublic)));
        }
        catch (Exception ex) { Console.Error.WriteLine($"[WARN] relic flash hook: {ex.Message}"); }
    }

    private static void RelicFlashPostfix(RelicModel __instance)
    {
        var sink = _eventSink;
        if (sink == null) return;
        try
        {
            var name = _loc.Relic(__instance.Id.Entry);
            lock (sink._evLock)
            {
                // Some relics flash once per target; one line is enough.
                if (sink._events.Count > 0 && sink._events[^1].GetValueOrDefault("type") as string == "relic"
                    && sink._events[^1].GetValueOrDefault("relic") as string == name)
                    return;
                sink.AddEventLocked(new Dictionary<string, object?> { ["type"] = "relic", ["relic"] = name });
            }
        }
        catch { }
    }

    private void EnsureEventHooks()
    {
        _eventSink = this;
        if (_eventHooksInstalled) return;
        try
        {
            CombatManager.Instance.History.Changed += OnHistoryChanged;
            _eventHooksInstalled = true;
        }
        catch (Exception ex) { Log($"History hook: {ex.Message}"); }
    }

    private static void OnHistoryChanged()
    {
        var sink = _eventSink;
        if (sink == null) return;
        try
        {
            var history = CombatManager.Instance.History;
            if (history.Entries is not IReadOnlyList<CombatHistoryEntry> list) return;
            lock (sink._evLock)
            {
                if (list.Count < sink._historySeen) sink._historySeen = 0;  // cleared (combat ended)
                for (int i = sink._historySeen; i < list.Count; i++)
                {
                    try { sink.RecordEntry(list[i]); } catch { }
                }
                sink._historySeen = list.Count;
            }
        }
        catch { }
    }

    private void AddEventLocked(Dictionary<string, object?> e)
    {
        if (_events.Count < MaxEvents) _events.Add(e);
        else if (_events.Count == MaxEvents) _events.Add(new Dictionary<string, object?> { ["type"] = "truncated" });
    }

    private static string CreatureName(Creature? c)
    {
        if (c == null) return "?";
        try
        {
            if (c.IsPlayer) return _loc.Bilingual("characters", (c.Player?.Character?.Id.Entry ?? "IRONCLAD") + ".title");
            return _loc.Monster(c.Monster?.Id.Entry ?? "UNKNOWN");
        }
        catch { return "?"; }
    }

    private void RecordEntry(CombatHistoryEntry entry)
    {
        // Turn markers, like the game's "Your turn" / "Enemy turn" banners.
        try
        {
            var cs = CombatManager.Instance.DebugOnlyGetState();
            if (cs != null)
            {
                var turn = (cs.RoundNumber, cs.CurrentSide);
                if (_lastTurn != turn)
                {
                    _lastTurn = turn;
                    AddEventLocked(new Dictionary<string, object?>
                    {
                        ["type"] = "turn", ["round"] = turn.RoundNumber,
                        ["side"] = turn.CurrentSide == CombatSide.Enemy ? "enemy" : "player",
                    });
                }
            }
        }
        catch { }

        Dictionary<string, object?>? e = null;
        switch (entry)
        {
            case CardPlayStartedEntry cp:
                e = new() { ["type"] = "card_played", ["card"] = CardName(cp.CardPlay.Card) };
                if (cp.CardPlay.Target != null) e["target"] = CreatureName(cp.CardPlay.Target);
                if (cp.CardPlay.IsAutoPlay) e["auto"] = true;
                break;
            case DamageReceivedEntry dr:
                e = new() { ["type"] = "damage", ["target"] = CreatureName(dr.Receiver), ["amount"] = dr.Result.UnblockedDamage };
                if (dr.Result.BlockedDamage > 0) e["blocked"] = dr.Result.BlockedDamage;
                if (dr.Dealer != null) e["source"] = CreatureName(dr.Dealer);
                if (dr.CardSource != null) e["card"] = CardName(dr.CardSource);
                if (dr.Result.WasTargetKilled) e["killed"] = true;
                break;
            case BlockGainedEntry bg:
                e = new() { ["type"] = "block", ["target"] = CreatureName(bg.Receiver), ["amount"] = bg.Amount };
                break;
            case PowerReceivedEntry pr:
                e = new() { ["type"] = "power", ["target"] = CreatureName(pr.Actor), ["power"] = _loc.Power(pr.Power.Id.Entry), ["amount"] = (int)pr.Amount };
                if (pr.Applier != null && pr.Applier != pr.Actor) e["source"] = CreatureName(pr.Applier);
                break;
            case MonsterPerformedMoveEntry mv:
            {
                // The combat screen shows the move's intents (move names are Bestiary-only).
                e = new() { ["type"] = "enemy_move", ["enemy"] = _loc.Monster(mv.Monster?.Id.Entry ?? "UNKNOWN") };
                try
                {
                    var intents = mv.Move?.Intents?.Select(i => i.IntentType.ToString()).ToList();
                    if (intents?.Count > 0) e["intents"] = intents;
                }
                catch { }
                break;
            }
            case CardDrawnEntry cd:
            {
                var name = CardName(cd.Card);
                if (_events.Count > 0 && _events[^1].GetValueOrDefault("type") as string == "draw"
                    && _events[^1].GetValueOrDefault("cards") is List<string> drawn)
                {
                    drawn.Add(name);
                    return;
                }
                e = new() { ["type"] = "draw", ["cards"] = new List<string> { name } };
                break;
            }
            case CardDiscardedEntry dc:
                e = new() { ["type"] = "discard", ["card"] = CardName(dc.Card) };
                break;
            case CardExhaustedEntry ex:
                e = new() { ["type"] = "exhaust", ["card"] = CardName(ex.Card) };
                break;
            case CardGeneratedEntry cg:
                e = new() { ["type"] = "card_added", ["card"] = CardName(cg.Card) };
                try { if (cg.Card.Pile != null) e["pile"] = cg.Card.Pile.Type.ToString().ToLowerInvariant(); } catch { }
                break;
            case CardAfflictedEntry ca:
                e = new() { ["type"] = "afflicted", ["card"] = CardName(ca.Card) };
                try { e["affliction"] = _loc.Bilingual("afflictions", ca.Affliction.Id.Entry + ".title"); } catch { }
                break;
            case OrbChanneledEntry oc:
                e = new() { ["type"] = "orb_channeled", ["orb"] = _loc.Bilingual("orbs", oc.Orb.Id.Entry + ".title") };
                break;
            case PotionUsedEntry pu:
                e = new() { ["type"] = "potion_used", ["potion"] = _loc.Potion(pu.Potion.Id.Entry) };
                if (pu.Target != null) e["target"] = CreatureName(pu.Target);
                break;
            case StarsModifiedEntry sm:
                e = new() { ["type"] = "stars", ["amount"] = sm.Amount };
                break;
            case SummonedEntry su:
                e = new() { ["type"] = "summon", ["amount"] = su.Amount };
                break;
        }
        if (e != null) AddEventLocked(e);
    }

    private PlayerSnapshot? TakeSnapshot()
    {
        var p = LocalPlayer;
        if (p == null) return null;
        try
        {
            var s = new PlayerSnapshot
            {
                Hp = p.Creature?.CurrentHp ?? 0,
                MaxHp = p.Creature?.MaxHp ?? 0,
                Gold = p.Gold,
                InCombat = CombatManager.Instance.IsInProgress,
            };
            foreach (var c in p.Deck?.Cards ?? Enumerable.Empty<CardModel>())
                if (c != null) s.Deck.Add(DeckKey(c));
            foreach (var r in p.Relics ?? Enumerable.Empty<RelicModel>()) s.Relics.Add(_loc.Relic(r.Id.Entry));
            foreach (var po in p.Potions ?? Enumerable.Empty<PotionModel?>())
                if (po != null) s.Potions.Add(_loc.Potion(po.Id.Entry));
            if (s.InCombat)
                foreach (var e in CombatManager.Instance.DebugOnlyGetState()?.Enemies ?? Enumerable.Empty<Creature>())
                    if (e != null && e.IsAlive) s.Enemies.Add((e, CreatureName(e)));
            return s;
        }
        catch { return null; }
    }

    // "<id>\u0001<display name>": the id pairs a card with its changed self.
    private static string DeckKey(CardModel c)
    {
        var name = CardName(c);
        if (c.Enchantment != null)
            name += $" [{_loc.Bilingual("enchantments", c.Enchantment.Id.Entry + ".title")}{(c.Enchantment.Amount > 1 ? " " + c.Enchantment.Amount : "")}]";
        if (c.Affliction != null)
            name += $" [{_loc.Bilingual("afflictions", c.Affliction.Id.Entry + ".title")}]";
        return c.Id.Entry + "\u0001" + name;
    }

    private static string DeckLabel(string key)
    {
        var i = key.IndexOf('\u0001');
        return i < 0 ? key : key[(i + 1)..];
    }

    private static List<string> Minus(List<string> a, List<string> b)
    {
        var rest = new List<string>(b);
        var outp = new List<string>();
        foreach (var x in a) if (!rest.Remove(x)) outp.Add(x);
        return outp;
    }

    /// <summary>Events recorded since the previous decision, plus run-level changes (gold,
    /// max HP, deck, relics, potions) found by comparing with the previous decision.</summary>
    private List<Dictionary<string, object?>> DrainEvents()
    {
        EnsureEventHooks();
        List<Dictionary<string, object?>> evs;
        lock (_evLock)
        {
            // Pick up entries added while nothing was listening.
            OnHistoryChangedUnlocked();
            evs = _events;
            _events = new List<Dictionary<string, object?>>();
        }
        var now = TakeSnapshot();
        var prev = _snapshot;
        _snapshot = now;
        if (prev == null || now == null) return evs;
        // The blow that ends a combat is not in CombatHistory (it stops recording once the
        // combat is ending), but the player sees the enemy die.
        foreach (var (creature, name) in prev.Enemies)
        {
            bool dead;
            try { dead = creature.IsDead; } catch { continue; }
            if (dead && !evs.Any(e => e.GetValueOrDefault("killed") is true && e.GetValueOrDefault("target") as string == name))
                evs.Add(new() { ["type"] = "enemy_died", ["enemy"] = name });
        }
        if (now.Gold != prev.Gold)
            evs.Add(new() { ["type"] = "gold", ["change"] = now.Gold - prev.Gold, ["gold"] = now.Gold });
        if (now.MaxHp != prev.MaxHp)
            evs.Add(new() { ["type"] = "max_hp", ["change"] = now.MaxHp - prev.MaxHp, ["max_hp"] = now.MaxHp });
        // In combat the damage/heal lines already say it; outside (events, rest, relics) they don't.
        if (now.Hp != prev.Hp && !(now.InCombat && prev.InCombat))
            evs.Add(new() { ["type"] = "hp", ["change"] = now.Hp - prev.Hp, ["hp"] = now.Hp });
        var added = Minus(now.Deck, prev.Deck);
        var removed = Minus(prev.Deck, now.Deck);
        // Same card, new state (upgraded, enchanted): one "deck_changed" line.
        foreach (var a in added.ToList())
        {
            var baseA = a.Split('\u0001')[0];
            var r = removed.FirstOrDefault(x => x.Split('\u0001')[0] == baseA);
            if (r == null) continue;
            removed.Remove(r);
            added.Remove(a);
            evs.Add(new() { ["type"] = "deck_changed", ["from"] = DeckLabel(r), ["to"] = DeckLabel(a) });
        }
        added = added.Select(DeckLabel).ToList();
        removed = removed.Select(DeckLabel).ToList();
        foreach (var a in added) evs.Add(new() { ["type"] = "deck_added", ["card"] = a });
        foreach (var r in removed) evs.Add(new() { ["type"] = "deck_removed", ["card"] = r });
        foreach (var a in Minus(now.Relics, prev.Relics)) evs.Add(new() { ["type"] = "relic_obtained", ["relic"] = a });
        foreach (var a in Minus(prev.Relics, now.Relics)) evs.Add(new() { ["type"] = "relic_lost", ["relic"] = a });
        foreach (var a in Minus(now.Potions, prev.Potions)) evs.Add(new() { ["type"] = "potion_obtained", ["potion"] = a });
        // A drunk potion already has its potion_used line.
        var used = evs.Where(e => e.GetValueOrDefault("type") as string == "potion_used")
            .Select(e => e.GetValueOrDefault("potion") as string ?? "").ToList();
        foreach (var a in Minus(prev.Potions, now.Potions))
            if (!used.Remove(a)) evs.Add(new() { ["type"] = "potion_lost", ["potion"] = a });
        return evs;
    }

    private void OnHistoryChangedUnlocked()
    {
        try
        {
            if (CombatManager.Instance.History.Entries is not IReadOnlyList<CombatHistoryEntry> list) return;
            if (list.Count < _historySeen) _historySeen = 0;
            for (int i = _historySeen; i < list.Count; i++)
            {
                try { RecordEntry(list[i]); } catch { }
            }
            _historySeen = list.Count;
        }
        catch { }
    }

    /// <summary>A new run starts with an empty log.</summary>
    private void ResetEvents()
    {
        lock (_evLock)
        {
            _events = new List<Dictionary<string, object?>>();
            _lastTurn = null;
            try { _historySeen = CombatManager.Instance.History.Entries.Count(); } catch { _historySeen = 0; }
        }
        _snapshot = null;
    }

    public void BeforeCommand(string cmd)
    {
        EnsureEventHooks();
        if (cmd is "start_run" or "load_save")
            ResetEvents();
    }

    // ───────────────────────── help ─────────────────────────

    public static Dictionary<string, object?> Help() => new()
    {
        ["type"] = "help",
        ["commands"] = new Dictionary<string, object?>
        {
            ["start_run"] = "{cmd:start_run, character:Ironclad|Silent|Defect|Regent|Necrobinder, seed?, ascension?:0-10, lang?:en|zh, manual_rewards?:bool (ignored: rewards are always decisions), omit_deck?:bool, modifiers?:[..], game_mode?, profile?:path}",
            ["action"] = "{cmd:action, action:<verb>, args:{...}} - send one entry of legal_actions as-is",
            ["get_state"] = "re-send the current decision (no game change)",
            ["get_deck"] = "{cmd:get_deck, upgrades?:bool} the deck with full card text",
            ["get_relics"] = "relics with descriptions and counters",
            ["get_potions"] = "potions with descriptions",
            ["get_piles"] = "{cmd:get_piles, pile?:draw|discard|exhaust|hand|all} combat piles (draw pile sorted, order hidden)",
            ["get_card"] = "{cmd:get_card, deck_index:N | hand_index:N | id:CARD_ID, upgraded?:bool}",
            ["get_map"] = "the whole act map",
            ["load_save"] = "{cmd:load_save, path|json, lang?, manual_rewards? (ignored), omit_deck?, profile?}",
            ["write_continue_save"] = "{cmd:write_continue_save, path?}",
            ["quit"] = "{cmd:quit, path?}",
            ["help"] = "this text",
        },
        ["actions"] = new Dictionary<string, object?>
        {
            ["select_map_node"] = "{col,row}",
            ["play_card"] = "{card_index, target_index? | target_id?} (a target is required for AnyEnemy cards when 2+ enemies; target_id = enemy combat_id)",
            ["end_turn"] = "{}",
            ["use_potion"] = "{potion_index, target_index? | target_id?} (only when the potion's can_use is true)",
            ["discard_potion"] = "{potion_index} (not while the potion is queued)",
            ["choose_option"] = "{option_index} (events, rest site)",
            ["select_card_reward"] = "{card_index}",
            ["skip_card_reward"] = "{} (only when can_skip)",
            ["select_card_alternative"] = "{alternative_index} card_reward alternatives other than Skip (Driftwood reroll, Pael's Wing sacrifice)",
            ["select_cards"] = "{indices:\"0,2\"} card_select; legal_actions gives choose:{from,min,max} for multi-picks",
            ["skip_select"] = "{} only when card_select has min_select 0 or cancelable true",
            ["select_bundle"] = "{bundle_index}",
            ["buy_card"] = "{card_index}", ["buy_relic"] = "{relic_index}", ["buy_potion"] = "{potion_index}",
            ["remove_card"] = "{} shop card removal (opens a card_select)",
            ["claim_reward"] = "{reward_index}",
            ["proceed"] = "{} leave a rewards screen (not when its can_skip is false) / continue",
            ["leave_room"] = "{} leave a shop, a used rest site or a finished event page (alias of its Proceed option)",
            ["select_treasure_relic"] = "{relic_index}", ["skip_treasure_relic"] = "{}",
            ["crystal_sphere_click"] = "{x, y, tool?:big|small} from choose_cell (no tool = current_tool)",
        },
        ["notes"] = "Every decision lists legal_actions; an action (verb and args) not listed is rejected with code illegal_action - nothing is chosen for you; only a missing target_index is filled in when a single entry matches. Aliases of the same button: finished event page leave_room|proceed = choose_option Proceed; rewards leave_room|skip_rewards = proceed; treasure proceed|leave_room = skip_treasure_relic; rest site/shop proceed = leave_room. 'events' lists what happened since the previous decision.",
    };
}
