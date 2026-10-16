namespace Sts2Headless;

/// <summary>
/// Builds the "legal_actions" list for a decision response.
///
/// It is derived from the response dictionary itself (not re-computed from game state), so it
/// always agrees with the fields the client sees. Each entry is a ready-to-send action:
///   {"action": "play_card", "args": {"card_index": 0, "target_index": 1}}
/// except multi-card selections, where enumerating every combination would explode; those get
///   {"action": "select_cards", "choose": {"from": [0, 1, 2], "min": 1, "max": 2}}
/// and the client sends {"indices": "0,2"} built from that. The Crystal Sphere minigame gets
///   {"action": "crystal_sphere_click", "choose_cell": {"cells": [[x, y], ...], "tools": ["big", "small"]}}
/// </summary>
internal static class LegalActions
{
    public static void Attach(Dictionary<string, object?> response)
    {
        if (response.GetValueOrDefault("type") as string != "decision") return;
        try
        {
            response["legal_actions"] = For(response);
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine($"[WARN] legal_actions failed: {ex.Message}");
        }
    }

    internal static List<Dictionary<string, object?>> For(Dictionary<string, object?> r)
    {
        var acts = new List<Dictionary<string, object?>>();
        var decision = r.GetValueOrDefault("decision") as string;
        var player = r.GetValueOrDefault("player") as Dictionary<string, object?>;

        switch (decision)
        {
            case "map_select":
                foreach (var c in Items(r, "choices"))
                    acts.Add(Act("select_map_node", ("col", c["col"]), ("row", c["row"])));
                break;

            case "combat_play":
            {
                var enemies = Items(r, "enemies").Select(e => e["index"]).ToList();
                foreach (var card in Items(r, "hand"))
                {
                    // can_play is the game's CardModel.CanPlay (energy, stars, hooks, card logic).
                    if (card.GetValueOrDefault("can_play") is not true) continue;
                    if (card.GetValueOrDefault("target_type") as string == "AnyEnemy")
                        foreach (var e in enemies)
                            acts.Add(Act("play_card", ("card_index", card["index"]), ("target_index", e)));
                    else
                        acts.Add(Act("play_card", ("card_index", card["index"])));
                }
                acts.Add(Act("end_turn"));
                foreach (var p in Potions(player))
                {
                    if (p.GetValueOrDefault("usage") is string usage && usage != "CombatOnly" && usage != "AnyTime")
                        continue;  // Automatic potions trigger by themselves
                    if (p.GetValueOrDefault("can_use") is false)
                        continue;  // e.g. potions locked, or the custom usability check fails
                    if (p.GetValueOrDefault("target_type") as string == "AnyEnemy")
                        foreach (var e in enemies)
                            acts.Add(Act("use_potion", ("potion_index", p["index"]), ("target_index", e)));
                    else
                        acts.Add(Act("use_potion", ("potion_index", p["index"])));
                }
                break;
            }

            case "card_reward":
                foreach (var c in Items(r, "cards"))
                    acts.Add(Act("select_card_reward", ("card_index", c["index"])));
                if (r.GetValueOrDefault("can_skip") is not false)
                    acts.Add(Act("skip_card_reward"));
                // Reroll / Sacrifice (Skip is skip_card_reward).
                foreach (var a in Items(r, "alternatives"))
                    if (a.GetValueOrDefault("id") as string != "Skip")
                        acts.Add(Act("select_card_alternative", ("alternative_index", a["index"])));
                break;

            case "card_select":
            {
                var from = Items(r, "cards").Select(c => c["index"]).ToList();
                // Same clamping as DoSelectCards: a prompt can ask for more cards than it offers.
                var min = Math.Min(Int(r, "min_select"), from.Count);
                var max = Math.Min(Int(r, "max_select"), from.Count);
                if (max == 1)
                    foreach (var i in from)
                        acts.Add(Act("select_cards", ("indices", i?.ToString())));
                else if (max > 1)
                    acts.Add(new Dictionary<string, object?>
                    {
                        ["action"] = "select_cards",
                        ["choose"] = new Dictionary<string, object?> { ["from"] = from, ["min"] = Math.Min(Math.Max(min, 1), max), ["max"] = max },
                    });
                // Skipping is selecting nothing (min 0) or the screen's cancel button.
                if (min == 0 || r.GetValueOrDefault("cancelable") is true)
                    acts.Add(Act("skip_select"));
                break;
            }

            case "bundle_select":
                foreach (var b in Items(r, "bundles"))
                    acts.Add(Act("select_bundle", ("bundle_index", b["index"])));
                break;

            case "event_choice":
            {
                var options = Items(r, "options").ToList();
                foreach (var o in options)
                    if (o.GetValueOrDefault("is_locked") is not true)
                        acts.Add(Act("choose_option", ("option_index", o["index"])));
                if (acts.Count == 0)
                    acts.Add(Act("leave_room"));
                break;
            }

            case "rest_site":
                foreach (var o in Items(r, "options"))
                    if (o.GetValueOrDefault("is_enabled") is not false)
                        acts.Add(Act("choose_option", ("option_index", o["index"])));
                if (acts.Count == 0 || r.GetValueOrDefault("can_leave") is true)
                    acts.Add(Act("leave_room"));
                break;

            case "shop":
            {
                var gold = Int(player, "gold");
                var slotsFull = PotionSlotsFull(player);
                foreach (var c in Items(r, "cards"))
                    if (c.GetValueOrDefault("is_stocked") is true && Int(c, "cost") <= gold)
                        acts.Add(Act("buy_card", ("card_index", c["index"])));
                foreach (var c in Items(r, "relics"))
                    if (c.GetValueOrDefault("is_stocked") is true && Int(c, "cost") <= gold)
                        acts.Add(Act("buy_relic", ("relic_index", c["index"])));
                if (!slotsFull)
                    foreach (var c in Items(r, "potions"))
                        if (c.GetValueOrDefault("is_stocked") is true && Int(c, "cost") <= gold)
                            acts.Add(Act("buy_potion", ("potion_index", c["index"])));
                if (r.GetValueOrDefault("card_removal_cost") is int removal && removal <= gold
                    && r.GetValueOrDefault("card_removal_available") is not false)
                    acts.Add(Act("remove_card"));
                // Fake Merchant: a Foul Potion can be thrown at him (throwable_potions). It has
                // can_use there, so the generic potion loop below lists it.
                acts.Add(Act("leave_room"));
                break;
            }

            case "rewards":
            {
                var slotsFull = r.GetValueOrDefault("potion_slots_full") is true;
                foreach (var w in Items(r, "rewards"))
                    if (!(slotsFull && w.GetValueOrDefault("type") as string == "potion"))
                        acts.Add(Act("claim_reward", ("reward_index", w["index"])));
                if (r.GetValueOrDefault("can_skip") is not false)
                    acts.Add(Act("proceed"));
                break;
            }

            case "crystal_sphere":
                // One entry instead of cells x tools: send {"x":..,"y":..,"tool":"big"|"small"}.
                if (r.GetValueOrDefault("hidden_cells") is System.Collections.IList { Count: > 0 } hidden)
                    acts.Add(new Dictionary<string, object?>
                    {
                        ["action"] = "crystal_sphere_click",
                        ["choose_cell"] = new Dictionary<string, object?>
                        {
                            ["cells"] = hidden,
                            ["tools"] = r.GetValueOrDefault("tools"),
                        },
                    });
                break;

            case "treasure_relic":
                foreach (var t in Items(r, "relics"))
                    acts.Add(Act("select_treasure_relic", ("relic_index", t["index"])));
                acts.Add(Act("skip_treasure_relic"));
                break;

            case "game_over":
                return acts;  // start_run / load_save to continue

            default:
                acts.Add(Act("proceed"));
                break;
        }

        // Outside the combat turn the potion popup still enables "Use" for AnyTime potions (Blood
        // Potion, Fruit Juice, Foul Potion at the merchant, ...), even on a selection screen;
        // "can_use" carries the game's checks. Enemy-targeted ones would need a target here.
        if (decision != "combat_play")
            foreach (var p in Potions(player))
                if (p.GetValueOrDefault("can_use") is true
                    && p.GetValueOrDefault("target_type") as string != "AnyEnemy")
                    acts.Add(Act("use_potion", ("potion_index", p["index"])));

        // Discarding a potion is allowed at any decision point, unless an event locks potions.
        if (player?.GetValueOrDefault("can_use_or_remove_potions") is not false)
            foreach (var p in Potions(player))
                if (p.GetValueOrDefault("queued") is not true)  // NPotionPopup disables Discard while queued
                    acts.Add(Act("discard_potion", ("potion_index", p["index"])));
        return acts;
    }

    private static Dictionary<string, object?> Act(string action, params (string key, object? value)[] args)
    {
        var a = new Dictionary<string, object?> { ["action"] = action };
        if (args.Length > 0)
            a["args"] = args.ToDictionary(kv => kv.key, kv => kv.value);
        return a;
    }

    private static IEnumerable<Dictionary<string, object?>> Items(Dictionary<string, object?>? d, string key) =>
        (d?.GetValueOrDefault(key) as System.Collections.IEnumerable)?.OfType<Dictionary<string, object?>>()
        ?? Enumerable.Empty<Dictionary<string, object?>>();

    private static IEnumerable<Dictionary<string, object?>> Potions(Dictionary<string, object?>? player) =>
        Items(player, "potions").Where(p => p.ContainsKey("index"));

    private static bool PotionSlotsFull(Dictionary<string, object?>? player) =>
        player?.GetValueOrDefault("potion_slots") is int slots && Potions(player).Count() >= slots;

    private static int Int(Dictionary<string, object?>? d, string key) =>
        d?.GetValueOrDefault(key) is int i ? i : 0;
}
