using System.Runtime.CompilerServices;
using HarmonyLib;
using MegaCrit.Sts2.Core.Entities.CardRewardAlternatives;
using MegaCrit.Sts2.Core.Entities.Cards;
using MegaCrit.Sts2.Core.Entities.Merchant;
using MegaCrit.Sts2.Core.Rewards;

namespace Sts2Headless;

/// <summary>
/// Stand-ins for what the game's reward / shop screens do through Godot nodes that do not exist
/// headless (NRun is null):
///
/// - Card reward alternatives. CardReward.OnSelect builds its alternatives once and hands that
///   same list to the selector on every pass, but the real screen is refreshed after a Driftwood
///   Reroll: Populate -> NCardRewardSelectionScreen.RefreshOptions(_cards,
///   CardRewardAlternative.Generate(this)), and CanReroll is false by then, so REROLL is gone.
///   A postfix on Generate remembers the latest list generated for each reward (keyed by the
///   reward's card list, which OnSelect passes to the selector), i.e. the buttons the screen
///   currently shows.
///
/// - Shop card removal. MerchantCardRemovalEntry.OnTryPurchase marks the service used through
///   NRun.Instance?.MerchantRoom?.Inventory.OnCardRemovalUsed() -> NMerchantCardRemoval.SetUsed.
///   Every removal (the paid one, and Lord's Parasol's free one) goes through it, so the postfix
///   calls SetUsed() whenever it succeeds, at the same point.
/// </summary>
internal static class RewardPatches
{
    /// <summary>Set when a patch could not be applied; runs refuse to start.</summary>
    public static string? Failure { get; private set; }

    private static readonly AccessTools.FieldRef<CardReward, List<CardCreationResult>> CardsRef =
        AccessTools.FieldRefAccess<CardReward, List<CardCreationResult>>("_cards");

    internal sealed class Shown
    {
        public required CardReward Reward;
        public required IReadOnlyList<CardRewardAlternative> Alternatives;
    }

    private static readonly ConditionalWeakTable<object, Shown> s_shown = new();

    /// <summary>
    /// The reward that owns <paramref name="cards"/> (the list CardReward.OnSelect passes to the
    /// selector) and the alternatives its screen shows now; null if unknown.
    /// </summary>
    public static Shown? ShownFor(object cards) => s_shown.TryGetValue(cards, out var s) ? s : null;

    public static void Install(Harmony harmony)
    {
        var errors = new List<string>();
        try
        {
            var gen = AccessTools.Method(typeof(CardRewardAlternative), nameof(CardRewardAlternative.Generate));
            if (gen == null) errors.Add("CardRewardAlternative.Generate: method not found");
            else harmony.Patch(gen, postfix: new HarmonyMethod(typeof(RewardPatches), nameof(GeneratePostfix)));
        }
        catch (Exception ex) { errors.Add($"CardRewardAlternative.Generate: {ex.Message}"); }

        try
        {
            var buy = AccessTools.Method(typeof(MerchantCardRemovalEntry), "OnTryPurchase",
                new[] { typeof(MerchantInventory), typeof(bool), typeof(bool) });
            if (buy == null) errors.Add("MerchantCardRemovalEntry.OnTryPurchase(inventory, ignoreCost, cancelable): method not found");
            else harmony.Patch(buy, postfix: new HarmonyMethod(typeof(RewardPatches), nameof(RemovalPurchasePostfix)));
        }
        catch (Exception ex) { errors.Add($"MerchantCardRemovalEntry.OnTryPurchase: {ex.Message}"); }

        if (errors.Count > 0)
        {
            Failure = "Reward/shop patches failed: " + string.Join("; ", errors);
            Console.Error.WriteLine("[ERROR] " + Failure);
        }
    }

    public static void GeneratePostfix(CardReward cardReward, IReadOnlyList<CardRewardAlternative> __result)
    {
        var cards = CardsRef(cardReward);
        if (cards == null) return;
        s_shown.AddOrUpdate(cards, new Shown { Reward = cardReward, Alternatives = __result });
    }

    public static void RemovalPurchasePostfix(MerchantCardRemovalEntry __instance, ref Task<(bool, int)> __result)
        => __result = MarkUsedOnSuccess(__instance, __result);

    private static async Task<(bool, int)> MarkUsedOnSuccess(MerchantCardRemovalEntry entry, Task<(bool, int)> purchase)
    {
        // Resumes on the purchase's context (the host context headless), so SetUsed runs
        // before the flow's next step and before the main thread serializes the shop.
        var result = await purchase;
        if (result.Item1) entry.SetUsed();  // NMerchantCardRemoval.OnCardRemovalUsed
        return result;
    }
}
