using MegaCrit.Sts2.Core.Entities.Cards;
using MegaCrit.Sts2.Core.Models;

namespace Sts2Headless;

/// <summary>
/// Text the game's UI shows that the tables alone can't produce: model Title/Description
/// overrides (CharacterCards -> "Defect Cards"), revealed map icons, ...
/// </summary>
public partial class RunSimulator
{
    /// <summary>
    /// A run modifier as the top-bar icon shows it: ModifierModel.Title / Description, which
    /// CharacterCards overrides with the character's cardsModifierTitle/Description. Falls back
    /// to the modifiers table when formatting fails.
    /// </summary>
    private static Dictionary<string, object?> ModifierInfo(ModifierModel m, bool withDescription)
    {
        var entry = m.Id.Entry;
        var d = new Dictionary<string, object?>
        {
            ["id"] = entry,
            ["name"] = FormatTitle(() => m.Title) ?? _loc.Bilingual("modifiers", entry + ".title"),
        };
        if (withDescription)
            d["description"] = TryFormat(() => m.Description.GetFormattedText())
                               ?? _loc.Bilingual("modifiers", entry + ".description");
        try
        {
            if (m is MegaCrit.Sts2.Core.Models.Modifiers.CharacterCards cc)
                d["character"] = cc.CharacterModel.Entry;
        }
        catch { }
        return d;
    }

    /// <summary>
    /// What a traveled "?" node turned out to be, as NNormalMapPoint.UpdateIcon draws it: the
    /// first room of this act's map-point history entry at the node's row (Monster, Elite,
    /// Treasure, Shop or Event).
    /// </summary>
    private string? RevealedRoomType(MegaCrit.Sts2.Core.Map.MapPoint point)
    {
        try
        {
            var history = _runState!.MapPointHistory;
            var act = _runState.CurrentActIndex;
            if (history.Count <= act) return null;
            var entries = history[act];
            if (entries.Count <= point.coord.row) return null;
            return entries[point.coord.row].Rooms.First().RoomType.ToString();
        }
        catch { return null; }
    }

    /// <summary>
    /// A pile card's text as the pile screen's NCard.UpdateVisuals renders it (see PreviewedCard).
    /// </summary>
    private static string PileCardText(CardModel c, PileType pile, IReadOnlySet<CardKeyword>? keywords, int cost) =>
        PreviewedCard(c, pile, keywords, cost).text;

    /// <summary>
    /// A card as every card list draws it (NCard.UpdateVisuals(pile, Normal): deck view, grid
    /// selections, card rewards, shop, piles): the card's and its enchantment's DynamicVar
    /// previews are refreshed, then the text and the previewed values are read. Outside the hand
    /// no global hooks run, but enchantments (Sharp's extra damage) and calculated values do.
    /// The previews are cleared before and after, so the live card is left in its base state.
    /// </summary>
    private static (string text, Dictionary<string, object?> stats) PreviewedCard(CardModel c, PileType pile = PileType.Deck,
        IReadOnlySet<CardKeyword>? keywords = null, int? cost = null)
    {
        var stats = new Dictionary<string, object?>();
        try
        {
            c.DynamicVars.ClearPreview();
            c.UpdateDynamicVarPreview(CardPreviewMode.Normal, null, c.DynamicVars);
            if (c.Enchantment != null)
            {
                c.Enchantment.DynamicVars.ClearPreview();
                c.UpdateDynamicVarPreview(CardPreviewMode.Normal, null, c.Enchantment.DynamicVars);
            }
            foreach (var dv in c.DynamicVars.Values) stats[dv.Name.ToLowerInvariant()] = (int)dv.PreviewValue;
            return (CardText(c, pile, null, keywords, cost), stats);
        }
        catch
        {
            stats.Clear();
            try { c.DynamicVars.ClearPreview(); c.Enchantment?.DynamicVars.ClearPreview(); } catch { }
            try { foreach (var dv in c.DynamicVars.Values) stats[dv.Name.ToLowerInvariant()] = (int)dv.BaseValue; } catch { }
            return (CardText(c, pile, null, keywords, cost), stats);
        }
        finally
        {
            try
            {
                c.DynamicVars.ClearPreview();
                c.Enchantment?.DynamicVars.ClearPreview();
            }
            catch { }
        }
    }

    /// <summary>
    /// The enchantment / affliction a card list shows on the card (NCard.UpdateEnchantmentVisuals
    /// and the affliction overlay), with their amounts.
    /// </summary>
    private static void AddCardMods(Dictionary<string, object?> d, CardModel c)
    {
        try
        {
            if (c.Enchantment != null)
            {
                d["enchantment"] = _loc.Bilingual("enchantments", c.Enchantment.Id.Entry + ".title");
                if (c.Enchantment.Amount != 0) d["enchantment_amount"] = c.Enchantment.Amount;
            }
            if (c.Affliction != null)
            {
                d["affliction"] = _loc.Bilingual("afflictions", c.Affliction.Id.Entry + ".title");
                if (c.Affliction.Amount != 0) d["affliction_amount"] = c.Affliction.Amount;
            }
        }
        catch { }
    }

    /// <summary>
    /// The fields every out-of-combat card list shares: description and stats as previewed
    /// (PreviewedCard), keywords, cost/upgrade meta and enchantment/affliction. Callers add
    /// their own index/price/after_upgrade fields.
    /// </summary>
    private static Dictionary<string, object?> ListCardInfo(CardModel c, PileType pile = PileType.Deck, bool withTips = true)
    {
        var (text, stats) = PreviewedCard(c, pile);
        var kws = c.Keywords?.Where(k => k != CardKeyword.None).Select(k => k.ToString()).ToList();
        var d = new Dictionary<string, object?>
        {
            ["id"] = c.Id.ToString(),
            ["name"] = CardName(c),
            ["cost"] = c.EnergyCost?.GetResolved() ?? 0,
            ["type"] = c.Type.ToString(),
            ["rarity"] = c.Rarity.ToString(),
            ["upgraded"] = c.IsUpgraded,
            ["description"] = text,
            ["stats"] = stats.Count > 0 ? stats : null,
            ["keywords"] = kws?.Count > 0 ? kws : null,
        };
        if (withTips) d["tips"] = Tips(() => c.HoverTips, _loc.Card(c.Id.Entry));
        AddCardMeta(d, c);
        AddCardMods(d, c);
        return d;
    }

    /// <summary>A selectable card in a list (card_select, card_reward): its index, the shared
    /// list fields and the upgrade preview.</summary>
    private Dictionary<string, object?> IndexedCardInfo(CardModel c, int index, PileType pile = PileType.Deck)
    {
        var d = new Dictionary<string, object?> { ["index"] = index };
        foreach (var kv in ListCardInfo(c, pile)) d[kv.Key] = kv.Value;
        d["after_upgrade"] = GetUpgradedInfo(c);
        return d;
    }
}
