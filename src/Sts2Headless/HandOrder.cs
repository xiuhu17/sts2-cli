using System.Runtime.CompilerServices;
using MegaCrit.Sts2.Core.CardSelection;
using MegaCrit.Sts2.Core.Commands;
using MegaCrit.Sts2.Core.Entities.Cards;
using MegaCrit.Sts2.Core.Models;
using HarmonyLib;

namespace Sts2Headless;

/// <summary>
/// The hand in the order the game shows it (NPlayerHand's holder order), which is not always
/// the order of the Hand pile:
///
/// - A hand selection (CardSelectCmd.FromHand / FromHandForUpgrade / FromHandForDiscard through
///   NPlayerHand.SelectCards) lifts the chosen cards out of the hand row into the selected-card
///   container (or the upgrade preview). When the model that asked finishes executing
///   (AbstractModel.ExecutionFinished, NPlayerHand.OnSelectModeSourceFinished) the cards still in
///   hand are re-added with NPlayerHand.Add(card), i.e. at the END of the row. The Hand pile is
///   never reordered, so Armaments / Touch of Insanity / Dual Wield ... leave the chosen card on
///   the right of the hand in the real game while the pile keeps it in place.
/// - Every card entering the hand (draw, generated, returned) is appended to the row
///   (CardPileCmd: handNode.Add(cardNode)), whatever its pile position.
/// - A card transformed in hand keeps its slot (the transform VFX swaps the card on its holder).
///
/// The pile order still drives the game (random picks, iteration order), so it is left alone;
/// the adapter shows and indexes the hand in this view's order.
/// </summary>
internal static class HandOrder
{
    private sealed class View
    {
        public readonly CardPile Pile;
        // Holders in the hand row, left to right.
        public readonly List<CardModel> Row = new();
        // Cards lifted out by a hand selection, in selection order; they return to the end of
        // the row when the selection's source finishes.
        public readonly List<CardModel> Lifted = new();
        // Transform originals still holding their slot: original -> its Hand pile index.
        public readonly Dictionary<CardModel, int> Ghosts = new();
        public View(CardPile pile) { Pile = pile; }
    }

    private static readonly object Lock = new();
    private static readonly ConditionalWeakTable<CardPile, View> Views = new();

    private static View Of(CardPile pile)
    {
        lock (Lock)
        {
            if (Views.TryGetValue(pile, out var v)) return v;
            v = new View(pile);
            v.Row.AddRange(pile.Cards);
            var view = v;
            pile.CardAdded += c => OnAdded(view, c);
            pile.CardRemoved += c => OnRemoved(view, c);
            Views.Add(pile, v);
            return v;
        }
    }

    private static int IndexIn(IReadOnlyList<CardModel> list, CardModel c)
    {
        for (int i = 0; i < list.Count; i++) if (list[i] == c) return i;
        return -1;
    }

    private static bool IsHand(CardPile? pile)
    {
        try { return pile != null && pile.Type == PileType.Hand; } catch { return false; }
    }

    private static void OnAdded(View v, CardModel c)
    {
        lock (Lock)
        {
            if (v.Row.Contains(c) || v.Lifted.Contains(c)) return;
            int idx;
            try { idx = IndexIn(v.Pile.Cards, c); } catch { idx = -1; }
            foreach (var (ghost, at) in v.Ghosts)
            {
                if (at != idx) continue;
                v.Ghosts.Remove(ghost);
                int r = v.Row.IndexOf(ghost);
                if (r >= 0) { v.Row[r] = c; return; }
                int l = v.Lifted.IndexOf(ghost);
                if (l >= 0) { v.Lifted[l] = c; return; }
                break;
            }
            v.Row.Add(c);
        }
    }

    private static void OnRemoved(View v, CardModel c)
    {
        lock (Lock)
        {
            if (v.Ghosts.ContainsKey(c)) return;  // the replacement takes this slot
            v.Row.Remove(c);
            v.Lifted.Remove(c);
        }
    }

    /// <summary>The hand as the game shows it, left to right (lifted cards last).</summary>
    public static List<CardModel> Cards(CardPile? pile)
    {
        if (pile == null) return new List<CardModel>();
        var v = Of(pile);
        lock (Lock)
        {
            List<CardModel> cards;
            try { cards = pile.Cards.ToList(); } catch { return v.Row.Concat(v.Lifted).ToList(); }
            var inPile = new HashSet<CardModel>(cards);
            v.Ghosts.Clear();
            v.Row.RemoveAll(c => !inPile.Contains(c));
            v.Lifted.RemoveAll(c => !inPile.Contains(c));
            foreach (var c in cards)
                if (!v.Row.Contains(c) && !v.Lifted.Contains(c)) v.Row.Add(c);
            return v.Row.Concat(v.Lifted).ToList();
        }
    }

    /// <summary>Hand-selection options in the order the hand shows them.</summary>
    public static List<CardModel> Sort(List<CardModel> options)
    {
        var pile = options.Select(c => c.Pile).FirstOrDefault(IsHand);
        if (pile == null) return options;
        var order = Cards(pile);
        return options.OrderBy(c => { int i = order.IndexOf(c); return i < 0 ? int.MaxValue : i; }).ToList();
    }

    /// <summary>A hand selection resolved: lift the chosen cards until <paramref name="source"/>
    /// finishes executing (immediately when the game passed no source).</summary>
    public static void Selected(IEnumerable<CardModel> selected, AbstractModel? source)
    {
        var chosen = selected.Where(c => c != null && IsHand(c.Pile)).ToList();
        if (chosen.Count == 0) return;
        var v = Of(chosen[0].Pile!);
        lock (Lock)
        {
            foreach (var c in chosen)
            {
                v.Row.Remove(c);
                if (!v.Lifted.Contains(c)) v.Lifted.Add(c);
            }
        }
        if (source == null) { Release(v); return; }
        Action<AbstractModel>? handler = null;
        handler = m =>
        {
            m.ExecutionFinished -= handler;
            Release(v);
        };
        source.ExecutionFinished += handler;
    }

    private static void Release(View v)
    {
        lock (Lock)
        {
            v.Row.AddRange(v.Lifted.Where(c => !v.Row.Contains(c)));
            v.Lifted.Clear();
        }
    }

    // CardCmd.Transform removes the originals before adding the replacements at the same pile
    // indices; the hand node keeps its slot, so the replacement must too.
    public static void TransformPrefix(IEnumerable<CardTransformation> transformations)
    {
        try
        {
            foreach (var t in transformations)
            {
                var pile = t.Original?.Pile;
                if (!IsHand(pile)) continue;
                var v = Of(pile!);
                lock (Lock) v.Ghosts[t.Original!] = IndexIn(pile!.Cards, t.Original!);
            }
        }
        catch { }
    }

    public static void Install()
    {
        var harmony = new Harmony("sts2headless.handorder");
        foreach (var m in typeof(CardCmd).GetMethods(System.Reflection.BindingFlags.Public | System.Reflection.BindingFlags.Static)
                     .Where(m => m.Name == "Transform" && m.GetParameters().FirstOrDefault()?.ParameterType == typeof(IEnumerable<CardTransformation>)))
        {
            try
            {
                harmony.Patch(m, prefix: new HarmonyMethod(typeof(HandOrder).GetMethod(nameof(TransformPrefix))));
            }
            catch (Exception ex) { Console.Error.WriteLine($"[WARN] hand order transform patch: {ex.Message}"); }
        }
    }
}
