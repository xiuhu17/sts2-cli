namespace Sts2Headless;

/// <summary>
/// Strict action validation: an action runs only if it is one of the current decision's
/// legal_actions (same verb, same arguments). Nothing is guessed for the player: an invalid
/// target is an error, never re-targeted.
///
/// Aliases name the same button with another verb and are rewritten to the legal entry first:
///   finished event page: leave_room / proceed  -> choose_option {option_index: Proceed}
///   rewards screen:      leave_room / skip_rewards -> proceed
///   treasure chest:      proceed / leave_room   -> skip_treasure_relic
///   rest site, shop:     proceed                -> leave_room
/// The only argument filled in is target_index when exactly one legal entry matches (a single
/// hittable enemy: the only place the card can be dropped).
/// Selections listed as {"choose": ...} / {"choose_cell": ...} are checked by their handlers
/// (indices range/count/duplicates, cell and tool).
/// </summary>
public partial class RunSimulator
{
    private static readonly string[] FillableArgs = { "target_index" };

    /// <summary>Rewrite an alias to the legal verb it stands for on this decision.</summary>
    private static (string action, Dictionary<string, object?>? args) Canonical(
        Dictionary<string, object?> decision, string action, Dictionary<string, object?>? args)
    {
        var name = decision.GetValueOrDefault("decision") as string ?? "";
        var legal = LegalOf(decision);
        bool Has(string a) => legal.Any(e => e.GetValueOrDefault("action") as string == a);
        if (Has(action)) return (action, args);
        switch (name)
        {
            case "event_choice" when action is "leave_room" or "proceed" && decision.GetValueOrDefault("finished") is true:
            {
                var proceed = (decision.GetValueOrDefault("options") as System.Collections.IEnumerable)?
                    .OfType<Dictionary<string, object?>>()
                    .FirstOrDefault(o => o.GetValueOrDefault("is_proceed") is true);
                if (proceed != null)
                    return ("choose_option", new Dictionary<string, object?> { ["option_index"] = proceed["index"] });
                break;
            }
            case "rewards" when action is "leave_room" or "skip_rewards" && Has("proceed"):
                return ("proceed", args);
            case "treasure_relic" when action is "proceed" or "leave_room" && Has("skip_treasure_relic"):
                return ("skip_treasure_relic", args);
            case "rest_site" or "shop" or "unknown" when action == "proceed" && Has("leave_room"):
                return ("leave_room", args);
            case "unknown" when action == "leave_room" && Has("proceed"):
                return ("proceed", args);
        }
        return (action, args);
    }

    private static bool SameValue(object? a, object? b)
    {
        if (a == null || b == null) return a == null && b == null;
        if (a is int ia && b is int ib) return ia == ib;
        return string.Equals(a.ToString(), b.ToString(), StringComparison.Ordinal);
    }

    /// <summary>
    /// Check (action, args) against the decision's legal_actions. Returns null when legal
    /// (args may be completed with a unique target_index), else a reason for the error.
    /// </summary>
    internal static string? CheckLegal(Dictionary<string, object?> decision, string action, ref Dictionary<string, object?>? args)
    {
        var name = decision.GetValueOrDefault("decision") as string ?? "";
        if (name == "game_over") return "the run is over";
        var entries = LegalOf(decision).Where(e => e.GetValueOrDefault("action") as string == action).ToList();
        if (entries.Count == 0)
        {
            return name switch
            {
                "card_select" when action == "skip_select" => "this selection requires a choice (it is not cancelable)",
                _ when action == "leave_room" && name is "event_choice" or "rest_site" => "a choice is still required here",
                _ => ReasonFor(decision, action, args ?? new Dictionary<string, object?>()) is { } r
                    && action is "use_potion" or "discard_potion" or "claim_reward"
                    ? r : $"it is not available on {Where(name)}",
            };
        }
        // Selections over a set: the handler validates the chosen indices / cell.
        if (entries.Any(e => e.ContainsKey("choose") || e.ContainsKey("choose_cell"))) return null;

        var given = args ?? new Dictionary<string, object?>();
        // target_id (an enemy's stable combat_id) names the same target as its target_index.
        if (given.TryGetValue("target_id", out var tid) && tid != null)
        {
            var enemy = Items(decision, "enemies").FirstOrDefault(e => SameValue(e.GetValueOrDefault("combat_id"), tid));
            if (enemy == null)
            {
                var ids = string.Join(",", Items(decision, "enemies").Select(e => e.GetValueOrDefault("combat_id")));
                return $"target_id {tid} is not a targetable enemy (targetable ids: {ids})";
            }
            if (given.TryGetValue("target_index", out var ti) && ti != null && !SameValue(ti, enemy["index"]))
                return $"target_id {tid} and target_index {ti} name different enemies";
            given = new Dictionary<string, object?>(given) { ["target_index"] = enemy["index"] };
            given.Remove("target_id");
        }
        var keys = new HashSet<string>(entries.SelectMany(e =>
            (e.GetValueOrDefault("args") as Dictionary<string, object?>)?.Keys ?? Enumerable.Empty<string>()));
        foreach (var k in given.Keys)
            if (IntArgs.Contains(k) && !keys.Contains(k) && given[k] != null)
                return $"'{k}' does not apply to {action} here";

        var matches = entries.Where(e =>
        {
            var ea = e.GetValueOrDefault("args") as Dictionary<string, object?> ?? new();
            foreach (var (k, g) in given)
                if (IntArgs.Contains(k) && g != null && !ea.ContainsKey(k)) return false;
            foreach (var (k, v) in ea)
            {
                if (given.TryGetValue(k, out var g) && g != null)
                {
                    if (!SameValue(g, v)) return false;
                }
                else if (!FillableArgs.Contains(k)) return false;
            }
            return true;
        }).ToList();

        if (matches.Count == 0)
        {
            var shown = given.Count == 0 ? "" : $"({string.Join(", ", given.Select(kv => $"{kv.Key}={kv.Value}"))})";
            var reason = ReasonFor(decision, action, given);
            return $"{action}{shown} is not one of the legal actions" + (reason != null ? $" ({reason})" : "");
        }
        if (matches.Count > 1)
        {
            var missing = keys.Where(k => !given.ContainsKey(k) || given[k] == null).ToList();
            if (missing.Count > 0)
                return $"{action} needs {string.Join(" and ", missing)} (several are legal)";
            return null;
        }
        // Exactly one legal entry: complete a missing target_index from it.
        if (matches[0].GetValueOrDefault("args") is Dictionary<string, object?> only)
            foreach (var k in FillableArgs)
                if (only.TryGetValue(k, out var v) && (!given.ContainsKey(k) || given[k] == null))
                {
                    args ??= new Dictionary<string, object?>();
                    args[k] = v;
                }
        return null;
    }

    private static IEnumerable<Dictionary<string, object?>> Items(Dictionary<string, object?>? d, string key) =>
        (d?.GetValueOrDefault(key) as System.Collections.IEnumerable)?.OfType<Dictionary<string, object?>>()
        ?? Enumerable.Empty<Dictionary<string, object?>>();

    private static Dictionary<string, object?>? ByIndex(IEnumerable<Dictionary<string, object?>> items, object? index) =>
        index == null ? null : items.FirstOrDefault(i => SameValue(i.GetValueOrDefault("index"), index));

    /// <summary>Why a well-formed but unlisted action is not legal, from the decision's own fields.</summary>
    private static string? ReasonFor(Dictionary<string, object?> d, string action, Dictionary<string, object?> args)
    {
        var name = d.GetValueOrDefault("decision") as string ?? "";
        var player = d.GetValueOrDefault("player") as Dictionary<string, object?>;
        var enemies = Items(d, "enemies").ToList();
        string? Target(string? targetType)
        {
            if (!args.TryGetValue("target_index", out var t) || t == null) return null;
            if (targetType != "AnyEnemy") return "'target_index' does not apply: it does not target a single enemy";
            if (ByIndex(enemies, t) == null)
                return enemies.Count == 0 ? $"target_index {t}: no targetable enemies"
                    : $"target_index {t} out of range (0..{enemies.Count - 1})";
            return null;
        }
        switch (action)
        {
            case "play_card":
            {
                var hand = Items(d, "hand").ToList();
                var card = ByIndex(hand, args.GetValueOrDefault("card_index"));
                if (card == null) return $"invalid card index {args.GetValueOrDefault("card_index")}, hand has {hand.Count} cards";
                if (card.GetValueOrDefault("can_play") is not true)
                {
                    var reasons = (card.GetValueOrDefault("unplayable_reasons") as System.Collections.IEnumerable)?.Cast<object>().Select(o => o.ToString());
                    return $"cannot play {card.GetValueOrDefault("name")}: {(reasons != null ? string.Join(", ", reasons) : "not playable now")}";
                }
                return Target(card.GetValueOrDefault("target_type") as string);
            }
            case "use_potion":
            {
                var potion = ByIndex(Items(player, "potions"), args.GetValueOrDefault("potion_index"));
                if (potion == null) return $"no potion at index {args.GetValueOrDefault("potion_index")}";
                if (potion.GetValueOrDefault("usage") as string == "CombatOnly" && name != "combat_play")
                    return d.ContainsKey("combat")
                        ? "combat potions can't be used during a card selection"
                        : "this potion can only be used in combat";
                if (potion.GetValueOrDefault("usage") is string u && u != "CombatOnly" && u != "AnyTime")
                    return $"this potion is not used by hand (usage {u})";
                if (potion.GetValueOrDefault("can_use") is false) return "this potion can't be used now";
                if (player?.GetValueOrDefault("can_use_or_remove_potions") is false) return "potions are locked here";
                if (name != "combat_play" && potion.GetValueOrDefault("target_type") as string == "AnyEnemy")
                    return "this potion needs an enemy target: use it in combat";
                return Target(potion.GetValueOrDefault("target_type") as string);
            }
            case "discard_potion":
                if (player?.GetValueOrDefault("can_use_or_remove_potions") is false) return "potions are locked here";
                return $"no potion at index {args.GetValueOrDefault("potion_index")}";
            case "claim_reward":
            {
                var w = ByIndex(Items(d, "rewards"), args.GetValueOrDefault("reward_index"));
                if (w == null) return $"no reward at index {args.GetValueOrDefault("reward_index")}";
                if (w.GetValueOrDefault("type") as string == "potion" && d.GetValueOrDefault("potion_slots_full") is true)
                    return "Potion slots are full: discard a potion first";
                return null;
            }
            case "select_map_node":
                return $"node ({args.GetValueOrDefault("col")},{args.GetValueOrDefault("row")}) is not reachable from here";
            case "buy_card" or "buy_relic" or "buy_potion":
            {
                var key = action switch { "buy_card" => "cards", "buy_relic" => "relics", _ => "potions" };
                var idx = args.GetValueOrDefault(action switch { "buy_card" => "card_index", "buy_relic" => "relic_index", _ => "potion_index" });
                var item = ByIndex(Items(d, key), idx);
                if (item == null) return $"no item at index {idx}";
                if (item.GetValueOrDefault("is_stocked") is not true) return "sold out";
                if (action == "buy_potion" && player?.GetValueOrDefault("potion_slots") is int slots
                    && Items(player, "potions").Count(p => p.ContainsKey("index")) >= slots)
                    return "Potion slots are full";
                return "not enough gold";
            }
            case "choose_option":
            {
                var o = ByIndex(Items(d, "options"), args.GetValueOrDefault("option_index"));
                if (o == null) return $"no option at index {args.GetValueOrDefault("option_index")}";
                if (o.GetValueOrDefault("is_locked") is true || o.GetValueOrDefault("is_enabled") is false) return "this option is locked";
                return null;
            }
        }
        return null;
    }
}
