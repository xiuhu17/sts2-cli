using System.Reflection;
using MegaCrit.Sts2.Core.Combat;
using MegaCrit.Sts2.Core.Entities.Cards;
using MegaCrit.Sts2.Core.Entities.Creatures;
using MegaCrit.Sts2.Core.Entities.Players;
using MegaCrit.Sts2.Core.Localization;
using MegaCrit.Sts2.Core.Localization.DynamicVars;
using MegaCrit.Sts2.Core.Models;
using MegaCrit.Sts2.Core.Models.Powers;
using MegaCrit.Sts2.Core.MonsterMoves.Intents;

namespace Sts2Headless;

/// <summary>
/// Combat/card serialization helpers that mirror what the game's UI shows: card names with
/// "+", X / star costs, unplayable reasons, intent tooltips, power/monster/relic titles,
/// orbs and the targetable-enemy list.
/// </summary>
public partial class RunSimulator
{
    // ── Names (the models' Title overrides, falling back to the loc tables) ──

    /// <summary>CardModel.Title: "Strike+" or "Searing Blow+3" for multi-upgrade cards.</summary>
    private static string CardName(CardModel c) =>
        _loc.Card(c.Id.Entry) + (!c.IsUpgraded ? "" : c.MaxUpgradeLevel > 1 ? "+" + c.CurrentUpgradeLevel : "+");

    private static readonly Dictionary<Type, bool> _titleOverridden = new();

    private static bool OverridesTitle(object model, Type baseType)
    {
        var t = model.GetType();
        lock (_titleOverridden)
        {
            if (_titleOverridden.TryGetValue(t, out var v)) return v;
            try { v = t.GetMethod("get_Title", BindingFlags.Instance | BindingFlags.Public)?.DeclaringType != baseType; }
            catch { v = false; }
            _titleOverridden[t] = v;
            return v;
        }
    }

    /// <summary>
    /// PowerModel.Title. Temporary Strength/Dexterity/Focus powers (Flex Potion, Setup Strike,
    /// Piercing Wail, ...) take their title from the card/potion/relic that applied them and have
    /// no "&lt;ID&gt;.title" loc entry of their own.
    /// </summary>
    private static string PowerName(PowerModel pw)
    {
        var entry = pw.Id.Entry;
        if (!OverridesTitle(pw, typeof(PowerModel)) && _loc.Raw("powers", entry + ".title") != null)
            return _loc.Power(entry);
        return FormatTitle(() => pw.Title) ?? _loc.Power(entry);
    }

    private static readonly Dictionary<string, string?> _titleCache = new();

    /// <summary>Format a Title LocString, cached by its table/key/variables (titles rarely change).</summary>
    private static string? FormatTitle(Func<LocString?> get)
    {
        LocString? ls;
        try { ls = get(); } catch { return null; }
        if (ls == null) return null;
        string key;
        try
        {
            key = $"{_loc.Lang}|{ls.LocTable}|{ls.LocEntryKey}|" +
                  string.Join(",", ls.Variables.Select(kv => kv.Key + "=" + (kv.Value is LocString v ? v.LocEntryKey : kv.Value)));
        }
        catch { return TryFormat(() => ls.GetFormattedText()); }
        lock (_titleCache)
            if (_titleCache.TryGetValue(key, out var hit)) return hit;
        var text = TryFormat(() => ls.GetFormattedText());
        lock (_titleCache)
            if (_titleCache.Count < 5000) _titleCache[key] = text;
        return text;
    }

    /// <summary>MonsterModel.Title: Tough Egg -> Hatchling, Test Subject #C{Count}, ...</summary>
    private static string MonsterName(MonsterModel? m, string fallbackEntry = "UNKNOWN")
    {
        if (m == null) return _loc.Monster(fallbackEntry);
        if (OverridesTitle(m, typeof(MonsterModel)))
        {
            var t = FormatTitle(() => m.Title);
            if (t != null) return t;
        }
        return _loc.Monster(m.Id.Entry);
    }

    /// <summary>RelicModel.Title: "Wax X" for Toy Box relics, per-character Sea Glass, ...</summary>
    private static string RelicName(RelicModel r)
    {
        bool special;
        try { special = r.IsWax || OverridesTitle(r, typeof(RelicModel)); } catch { special = false; }
        if (special && FormatTitle(() => r.Title) is string t) return t;
        return _loc.Relic(r.Id.Entry);
    }

    private static string ModelName(AbstractModel m) => m switch
    {
        CardModel c => CardName(c),
        PowerModel p => PowerName(p),
        RelicModel r => RelicName(r),
        PotionModel po => _loc.Potion(po.Id.Entry),
        EnchantmentModel e => _loc.Bilingual("enchantments", e.Id.Entry + ".title"),
        AfflictionModel a => _loc.Bilingual("afflictions", a.Id.Entry + ".title"),
        MonsterModel mo => MonsterName(mo),
        _ => m.Id.Entry,
    };

    // ── Cards ──

    /// <summary>
    /// Cost / upgrade / rarity fields shared by every card serializer (hand, piles, deck,
    /// rewards, shop, card_select), mirroring what NCard draws: an "X" energy or star cost,
    /// a star cost when the card has one (CurrentStarCost >= 0), and the upgrade level.
    /// "cost" itself stays an int (0 for X cards) for existing clients.
    /// </summary>
    private static void AddCardMeta(Dictionary<string, object?> d, CardModel c, IReadOnlySet<CardKeyword>? keywords = null,
        int? rawEnergyCost = null, string energyCostKey = "cost")
    {
        try
        {
            if (c.EnergyCost?.CostsX == true)
            {
                d["costs_x"] = true;
                // NCard draws "X"; GetResolved would return the stale X of the last play.
                if (d.ContainsKey(energyCostKey)) d[energyCostKey] = 0;
            }
            else if (c.EnergyCost != null && (rawEnergyCost ?? c.EnergyCost.GetWithModifiers(CostModifiers.All)) < 0) d["unplayable"] = true;
            if (!d.ContainsKey("unplayable") && (keywords ?? c.Keywords)?.Contains(CardKeyword.Unplayable) == true)
                d["unplayable"] = true;
        }
        catch { }
        try
        {
            if (c.HasStarCostX)
            {
                d["star_cost"] = 0;
                d["star_cost_x"] = true;
            }
            else if (c.CurrentStarCost >= 0)
                d["star_cost"] = c.GetStarCostWithModifiers();
        }
        catch { }
        try
        {
            d.TryAdd("rarity", c.Rarity.ToString());
            d["upgraded"] = c.IsUpgraded;
            if (c.CurrentUpgradeLevel > 0 || c.MaxUpgradeLevel > 1) d["upgrade_level"] = c.CurrentUpgradeLevel;
            if (c.MaxUpgradeLevel > 1) d["max_upgrade_level"] = c.MaxUpgradeLevel;
        }
        catch { }
    }

    private static Dictionary<string, object?> WithCardMeta(Dictionary<string, object?> d, CardModel c)
    {
        AddCardMeta(d, c);
        return d;
    }

    /// <summary>
    /// Hand-only fields: why a card can't be played (the game's UnplayableReason and the model
    /// blocking it, e.g. Normality/Smoggy), its gold/red glow, and the current X value.
    /// </summary>
    private static void AddHandPlayability(Dictionary<string, object?> d, CardModel c, PlayerCombatState? pcs)
    {
        bool playable = false;
        try
        {
            playable = c.CanPlay(out var reason, out var preventer);
            d["can_play"] = playable;
            if (!playable && reason != UnplayableReason.None)
            {
                d["unplayable_reasons"] = Enum.GetValues<UnplayableReason>()
                    .Where(f => f != UnplayableReason.None && reason.HasFlag(f))
                    .Select(f => f.ToString()).ToList();
                if (preventer != null && preventer != c) d["blocked_by"] = ModelName(preventer);
            }
        }
        catch { d["can_play"] = false; }
        // NHandCardHolder: gold only while playable in the Play phase; red regardless.
        try
        {
            var inPlay = pcs?.Phase == PlayerTurnPhase.Play;
            if (inPlay && playable && c.ShouldGlowGold) d["glow"] = "gold";
            else if (inPlay && c.ShouldGlowRed) d["glow"] = "red";
        }
        catch { }
        try
        {
            if (c.EnergyCost?.CostsX == true) d["x_value"] = c.EnergyCost.GetAmountToSpend();
            else if (c.HasStarCostX) d["x_value"] = pcs?.Stars ?? 0;
        }
        catch { }
    }

    private static string UnplayableText(UnplayableReason reason, AbstractModel? preventer) =>
        preventer != null ? $"{reason} ({ModelName(preventer)})" : reason.ToString();

    // ── Powers ──

    private static List<Dictionary<string, object?>>? SerializePowers(Creature? c)
    {
        var list = c?.Powers?.Where(pw => pw != null).Select(pw => new Dictionary<string, object?>
        {
            ["id"] = pw.Id.Entry,
            ["name"] = PowerName(pw),
            ["description"] = PowerText(pw),
            ["amount"] = pw.Amount,
            ["type"] = pw.Type.ToString(),
        }).ToList();
        return list?.Count > 0 ? list : null;
    }

    // ── Enemies / targeting ──

    /// <summary>
    /// Enemies the player can target, in combat order: alive and hittable (CombatState.HittableEnemies,
    /// which is what NTargetManager allows). A Decimillipede segment waiting to reattach is dead
    /// and unhittable, so it isn't a target. play_card/use_potion's target_index, the "enemies"
    /// list and damage_by_target all index into this one list.
    /// </summary>
    private static List<Creature> TargetableEnemies(CombatState? state) =>
        state?.Enemies?.Where(e => e != null && e.IsHittable).ToList() ?? new();

    /// <summary>
    /// Resolve an AnyEnemy target from target_id (Creature.CombatId, stable for the whole combat)
    /// or target_index. Returns (target, error); both null means "not given".
    /// </summary>
    private static (Creature? target, string? error) ResolveEnemyTarget(Dictionary<string, object?> args)
    {
        var state = CombatManager.Instance.DebugOnlyGetState();
        var enemies = TargetableEnemies(state);
        if (args.TryGetValue("target_id", out var idObj) && idObj != null)
        {
            long id;
            try { id = Convert.ToInt64(idObj); } catch { return (null, $"Invalid target_id {idObj}"); }
            var t = enemies.FirstOrDefault(e => e.CombatId == id);
            if (t != null) return (t, null);
            var ids = string.Join(",", enemies.Select(e => e.CombatId));
            return (null, $"target_id {id} is not a targetable enemy (targetable ids: {ids})");
        }
        if (args.TryGetValue("target_index", out var tObj) && tObj != null)
        {
            int idx;
            try { idx = Convert.ToInt32(tObj); } catch { return (null, $"Invalid target_index {tObj}"); }
            if (idx >= 0 && idx < enemies.Count) return (enemies[idx], null);
            return (null, enemies.Count == 0
                ? $"target_index {idx}: no targetable enemies"
                : $"target_index {idx} out of range (0..{enemies.Count - 1})");
        }
        return (null, null);
    }

    // ── Intents ──

    private static readonly MethodInfo? IntentDescMethod = typeof(AbstractIntent).GetMethod(
        "GetIntentDescription", BindingFlags.Instance | BindingFlags.NonPublic | BindingFlags.Public);
    private static readonly PropertyInfo? IntentPrefixProp = typeof(AbstractIntent).GetProperty(
        "IntentPrefix", BindingFlags.Instance | BindingFlags.NonPublic | BindingFlags.Public);
    private static readonly Dictionary<string, (string? title, string? desc)> _intentTextCache = new();

    /// <summary>
    /// The intent's hover tip (AbstractIntent.GetHoverTip: IntentTitle + GetIntentDescription)
    /// and, for Status intents, the card count the game draws on the icon.
    /// </summary>
    private static void AddIntentDetails(Dictionary<string, object?> info, AbstractIntent intent,
        IEnumerable<Creature> targets, Creature owner)
    {
        if (intent is StatusIntent si) info["card_count"] = si.CardCount;
        try
        {
            if (!intent.HasIntentTip) return;
            var prefix = IntentPrefixProp?.GetValue(intent) as string;
            if (prefix == null) return;
            var key = $"{_loc.Lang}|{intent.GetType().Name}|{prefix}|{(intent is StatusIntent s ? s.CardCount : -1)}" +
                      $"|{info.GetValueOrDefault("damage")}|{info.GetValueOrDefault("hits")}";
            (string? title, string? desc) text;
            lock (_intentTextCache)
                if (!_intentTextCache.TryGetValue(key, out text))
                {
                    var title = _loc.Raw("intents", prefix + ".title") != null ? _loc.Bilingual("intents", prefix + ".title") : null;
                    string? desc = null;
                    if (IntentDescMethod != null)
                    {
                        var arr = targets.ToArray();
                        desc = TryFormat(() => (IntentDescMethod.Invoke(intent, new object[] { arr, owner }) as LocString)?.GetFormattedText());
                    }
                    text = (title, desc);
                    if (_intentTextCache.Count < 5000) _intentTextCache[key] = text;
                }
            if (text.title != null) info["title"] = text.title;
            if (text.desc != null) info["description"] = text.desc;
        }
        catch { }
    }

    // ── Kaiser Crab (Surrounded) ──

    /// <summary>True when this enemy is behind the player (its attacks get Surrounded's x1.5).</summary>
    private static bool? BehindPlayer(SurroundedPower? sur, Creature e)
    {
        if (sur == null) return null;
        try
        {
            return sur.Facing == SurroundedPower.Direction.Right
                ? e.HasPower<BackAttackLeftPower>()
                : e.HasPower<BackAttackRightPower>();
        }
        catch { return null; }
    }

    // ── Orbs ──

    private static string? OrbText(OrbModel orb, Player player)
    {
        var cacheKey = $"{_loc.Lang}|orb|{orb.Id}|{orb.PassiveVal}|{orb.EvokeVal}";
        lock (_textCache)
            if (_textCache.TryGetValue(cacheKey, out var cached)) return cached;
        var text = TryFormat(() =>
        {
            var ls = orb.SmartDescription;
            try { ls.Add("energyPrefix", player.Character.CardPool.Title); } catch { }
            ls.Add("Passive", orb.PassiveVal);
            ls.Add("Evoke", orb.EvokeVal);
            return ls.GetFormattedText();
        });
        if (text == null)
        {
            var raw = _loc.Raw("orbs", orb.Id.Entry + ".smartDescription") ?? _loc.Raw("orbs", orb.Id.Entry + ".description");
            if (raw != null)
                text = FillTemplate(LocLookup.Clean(raw), new() { ["Passive"] = (int)orb.PassiveVal, ["Evoke"] = (int)orb.EvokeVal });
        }
        if (text != null)
            lock (_textCache)
                if (_textCache.Count < 50000) _textCache[cacheKey] = text;
        return text;
    }

    // ── Damage preview ──

    /// <summary>True if the card's damage is dealt by Osty (OstyDamageVar or the OstyAttack tag).</summary>
    private static bool IsOstyAttack(CardModel c)
    {
        try
        {
            if (c.DynamicVars.Values.Any(v => v is OstyDamageVar)) return true;
            return c.Tags != null && c.Tags.Contains(CardTag.OstyAttack);
        }
        catch { return false; }
    }

    /// <summary>Per-hit damage from a resolved var dict: calculateddamage, damage, or ostydamage.</summary>
    private static int? PerHitDamage(Dictionary<string, object?> tstats, CardModel c)
    {
        if (tstats.TryGetValue("calculateddamage", out var cdv) && cdv is int cdi && cdi > 0) return cdi;
        if (tstats.TryGetValue("damage", out var dv) && dv is int di) return di;
        if (tstats.TryGetValue("ostydamage", out var ov) && ov is int oi) return oi;
        try
        {
            var odv = c.DynamicVars.Values.FirstOrDefault(v => v is OstyDamageVar);
            if (odv != null) return (int)odv.PreviewValue;
        }
        catch { }
        return null;
    }
}
