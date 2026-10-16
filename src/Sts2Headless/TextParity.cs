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
    /// A pile card's text as the pile screen's NCard.UpdateVisuals renders it: previews are
    /// refreshed first (outside the hand no global hooks run, but enchantments and calculated
    /// values such as Compile Driver's "(Draw 1 card)" do), then cleared again so the live card
    /// is left in its base state.
    /// </summary>
    private static string PileCardText(CardModel c, PileType pile, IReadOnlySet<CardKeyword>? keywords, int cost)
    {
        try
        {
            c.DynamicVars.ClearPreview();
            c.UpdateDynamicVarPreview(CardPreviewMode.Normal, null, c.DynamicVars);
            if (c.Enchantment != null)
            {
                c.Enchantment.DynamicVars.ClearPreview();
                c.UpdateDynamicVarPreview(CardPreviewMode.Normal, null, c.Enchantment.DynamicVars);
            }
            return CardText(c, pile, null, keywords, cost);
        }
        catch { return CardText(c, pile, null, keywords, cost); }
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
}
