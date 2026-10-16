using MegaCrit.Sts2.Core.Entities.Ancients;
using MegaCrit.Sts2.Core.Entities.Cards;
using MegaCrit.Sts2.Core.Entities.CardRewardAlternatives;
using MegaCrit.Sts2.Core.Entities.Merchant;
using MegaCrit.Sts2.Core.Entities.Players;
using MegaCrit.Sts2.Core.Entities.RestSite;
using MegaCrit.Sts2.Core.Events;
using MegaCrit.Sts2.Core.Hooks;
using MegaCrit.Sts2.Core.Localization;
using MegaCrit.Sts2.Core.Localization.DynamicVars;
using MegaCrit.Sts2.Core.Models;
using MegaCrit.Sts2.Core.Random;
using MegaCrit.Sts2.Core.Rewards;
using MegaCrit.Sts2.Core.Rooms;
using MegaCrit.Sts2.Core.Runs;
using MegaCrit.Sts2.Core.Saves;
using MegaCrit.Sts2.Core.Combat;

namespace Sts2Headless;

/// <summary>
/// Room parity helpers (events, shop, rest site, rewards, game over): what the game's room
/// screens show, serialized for the protocol.
/// </summary>
public partial class RunSimulator
{
    /// <summary>Drop per-run room state (and references to the previous run's models).</summary>
    private void ResetRoomParityState()
    {
        _autoRewardsScreen = false;
        _finishedEventAcked = null;
        _restChoiceRoom = null;
        _dialogueEvent = null;
        _dialogue = null;
        _dialogueDone = false;
    }

    // ─── Shared item serialization ───

    /// <summary>A card preview tooltip (CardHoverTip): the card's id, name and text.</summary>
    private static Dictionary<string, object?>? CardTipInfo(MegaCrit.Sts2.Core.HoverTips.CardHoverTip tip)
    {
        try
        {
            var card = tip.Card;
            var name = _loc.Card(card.Id.Entry) + (card.IsUpgraded ? "+" : "");
            return new Dictionary<string, object?>
            {
                ["title"] = name,
                ["description"] = CardText(card),
                ["card"] = card.Id.ToString(),
                ["upgraded"] = card.IsUpgraded,
            };
        }
        catch { return null; }
    }

    /// <summary>A potion as the game describes it on hover (belt, shop, rewards).</summary>
    private static Dictionary<string, object?> PotionInfo(PotionModel p, int? index = null)
    {
        var vars = new Dictionary<string, object?>();
        try { foreach (var dv in p.DynamicVars.Values) vars[dv.Name] = (int)dv.BaseValue; } catch { }
        var info = new Dictionary<string, object?>();
        if (index != null) info["index"] = index;
        info["id"] = p.Id.Entry;
        info["name"] = _loc.Potion(p.Id.Entry);
        info["description"] = PotionText(p);
        info["rarity"] = p.Rarity.ToString();
        info["usage"] = p.Usage.ToString();
        info["target_type"] = p.TargetType.ToString();
        info["tips"] = Tips(() => p.ExtraHoverTips);
        info["vars"] = vars.Count > 0 ? vars : null;
        return info;
    }

    /// <summary>A dynamic var's value for the "vars" field: StringVars carry text, not a number.</summary>
    private static object? VarValue(DynamicVar dv) =>
        dv is StringVar sv ? LocLookup.Clean(sv.StringValue ?? "") : (object)(int)dv.BaseValue;

    // ─── Events ───

    /// <summary>
    /// Prepare an option's LocStrings the way NEventOptionButton._Ready does before rendering:
    /// the event's dynamic vars (including StringVars naming relics/cards/enchantments) are added.
    /// </summary>
    private static void AddEventVars(EventModel ev, EventOption opt)
    {
        try
        {
            if (opt.Description != null) ev.DynamicVars.AddTo(opt.Description);
            if (opt.Title != null) ev.DynamicVars.AddTo(opt.Title);
        }
        catch { }
    }

    /// <summary>Hover tips, the relic an ancient option grants, the lethal warning and proceed flag.</summary>
    private Dictionary<string, object?> EventOptionExtras(EventModel ev, EventOption opt)
    {
        var extra = new Dictionary<string, object?>();
        extra["tips"] = Tips(() => opt.HoverTips);
        if (opt.Relic != null)
        {
            try { extra["relic"] = RelicInfo(opt.Relic); } catch { }
        }
        bool willKill = false;
        try { willKill = opt.WillKillPlayer?.Invoke(ev.Owner ?? _runState!.Players[0]) ?? false; } catch { }
        extra["will_kill"] = willKill;
        extra["is_proceed"] = opt.IsProceed;
        return extra;
    }

    /// <summary>
    /// The event's body text (NEventRoom.SetDescription): shown only if its key exists, with the
    /// character's details, IsMultiplayer and the event's dynamic vars added.
    /// </summary>
    private string? EventDescriptionText(EventModel ev)
    {
        var desc = ev.Description;
        if (desc == null) return null;
        try
        {
            if (!desc.Exists()) return null;
            ev.Owner?.Character?.AddDetailsTo(desc);
            desc.Add("IsMultiplayer", false);
            ev.DynamicVars.AddTo(desc);
        }
        catch { }
        var text = TryFormat(() => desc.GetFormattedText());
        if (text != null) return text;
        var raw = _loc.Bilingual(desc.LocTable, desc.LocEntryKey);
        if (raw == desc.LocEntryKey) return null;
        Dictionary<string, object?>? evVars = null;
        try { evVars = ev.DynamicVars?.Values.ToDictionary(v => v.Name, VarValue); } catch { }
        return FillTemplate(raw, evVars);
    }

    // The ancient's opening dialogue, picked once per visit (NEventRoom.SetupLayout) and shown
    // until the first option is chosen (RefreshEventState clears it).
    private EventModel? _dialogueEvent;
    private List<Dictionary<string, object?>>? _dialogue;
    private bool _dialogueDone;

    private List<Dictionary<string, object?>>? AncientDialogue(EventModel ev)
    {
        if (ev is not AncientEventModel ancient) return null;
        if (!ReferenceEquals(_dialogueEvent, ev))
        {
            _dialogueEvent = ev;
            _dialogueDone = false;
            _dialogue = BuildAncientDialogue(ancient);
        }
        return _dialogueDone ? null : _dialogue;
    }

    private void MarkEventOptionChosen() { if (_dialogueEvent != null) _dialogueDone = true; }

    private List<Dictionary<string, object?>>? BuildAncientDialogue(AncientEventModel ancient)
    {
        try
        {
            if (ancient.IsFinished) return null;  // pre-finished visits have no dialogue
            var owner = ancient.Owner ?? _runState!.Players[0];
            var charId = owner.Character.Id;
            int charVisits = 0, totalVisits = 0;
            try
            {
                var stats = SaveManager.Instance.Progress?.GetStatsForAncient(ancient.Id);
                charVisits = stats?.GetVisitsAs(charId) ?? 0;
                totalVisits = stats?.TotalVisits ?? 0;
            }
            catch { }
            var valid = ancient.DialogueSet.GetValidDialogues(charId, charVisits, totalVisits,
                !ancient.AnyCharacterDialogueBlacklist.Contains(owner.Character)).ToList();
            if (valid.Count == 0) return null;
            // The game draws from Rng.Chaotic (cosmetic); a seeded pick keeps runs reproducible.
            var rng = new Rng(_runState?.Rng.Seed ?? 0, "ancient_dialogue_" + ancient.Id.Entry);
            var chosen = rng.NextItem(valid) ?? valid[0];
            var lines = new List<Dictionary<string, object?>>();
            foreach (var line in chosen.Lines)
            {
                if (line.LineText == null) continue;
                try { line.LineText.Add("Act1Name", _runState!.Acts[0].Title); } catch { }
                var text = TryFormat(() => line.LineText.GetFormattedText())
                           ?? LocLookup.Clean(_loc.Bilingual("ancients", line.LineText.LocEntryKey));
                lines.Add(new Dictionary<string, object?>
                {
                    ["speaker"] = line.Speaker == AncientDialogueSpeaker.Ancient ? "ancient" : "character",
                    ["text"] = text.Trim(),
                    ["next"] = line.NextButtonText == null ? null : TryFormat(() => line.NextButtonText.GetFormattedText()),
                });
            }
            return lines.Count > 0 ? lines : null;
        }
        catch (Exception ex)
        {
            Log($"Ancient dialogue: {ex.Message}");
            return null;
        }
    }

    // A finished event shows its last page (the outcome text) with a single Proceed button
    // (NEventRoom.SetOptions); the room is left only when that is chosen.
    private EventModel? _finishedEventAcked;

    private Dictionary<string, object?> FinishedEventState(EventModel ev)
    {
        var proceed = new Dictionary<string, object?>
        {
            ["index"] = 0,
            ["title"] = _loc.Bilingual(ev.LocTable, "PROCEED.title"),
            ["description"] = null,
            ["text_key"] = "PROCEED",
            ["is_locked"] = false,
            ["is_proceed"] = true,
            ["will_kill"] = false,
        };
        return new Dictionary<string, object?>
        {
            ["type"] = "decision",
            ["decision"] = "event_choice",
            ["context"] = RunContext(),
            ["event_name"] = EventName(ev),
            ["event_id"] = ev.Id?.Entry,
            ["description"] = EventDescriptionText(ev),
            ["finished"] = true,
            ["options"] = new List<Dictionary<string, object?>> { proceed },
            ["player"] = PlayerSummary(_runState!.Players[0]),
        };
    }

    private static string EventName(EventModel ev)
    {
        var eventEntry = ev.Id?.Entry ?? ev.GetType().Name.ToUpperInvariant();
        var eventName = _loc.Bilingual("ancients", eventEntry + ".title");
        if (eventName == eventEntry + ".title")
            eventName = _loc.Event(eventEntry);
        return eventName;
    }

    /// <summary>EventSynchronizer.SaveEventOptionToHistory, which the direct Chosen() call skips.</summary>
    private void SaveEventOptionToHistory(Player player, EventOption option)
    {
        try
        {
            if (!option.ShouldSaveChoiceToHistory || option.HistoryName == null) return;
            var entry = _runState?.CurrentMapPointHistoryEntry?.GetEntry(player.NetId);
            if (entry == null) return;
            var hist = new MegaCrit.Sts2.Core.Runs.History.EventOptionHistoryEntry
            {
                Title = option.HistoryName,
                Variables = new Dictionary<string, object>(),
            };
            if (option.ShouldSaveVariablesToHistory)
                foreach (var kv in option.HistoryName.Variables) hist.Variables[kv.Key] = kv.Value;
            entry.EventChoices.Add(hist);
        }
        catch (Exception ex) { Log($"Event history: {ex.Message}"); }
    }

    // ─── Rest site ───

    // The room in which a rest site option has been used: the game then shows Proceed.
    private AbstractRoom? _restChoiceRoom;

    private Dictionary<string, object?> RestOptionInfo(RestSiteOption opt, int i, Player player)
    {
        var info = new Dictionary<string, object?>
        {
            ["index"] = i,
            ["option_id"] = opt.OptionId,
            ["name"] = TryFormat(() => opt.Title.GetFormattedText()) ?? opt.OptionId,
            ["description"] = TryFormat(() => opt.Description.GetFormattedText()),
            ["is_enabled"] = opt.IsEnabled,
            ["class"] = opt.GetType().Name,
        };
        if (opt is HealRestSiteOption)
        {
            try { info["heal_amount"] = (int)HealRestSiteOption.GetHealAmount(player); } catch { }
        }
        return info;
    }

    // ─── Shop ───

    /// <summary>Card removal is offered once per visit; modifiers like Hoarder disable it (NMerchantCardRemoval.FillSlot).</summary>
    private static void ApplyCardRemovalRules(MerchantCardRemovalEntry? removal, Player player)
    {
        if (removal == null || removal.Used) return;
        try
        {
            if (!Hook.ShouldAllowMerchantCardRemoval(player.RunState, player))
                removal.SetUsed();
        }
        catch { }
    }

    // ─── Rewards ───

    private static readonly System.Reflection.FieldInfo? SpecialRewardCard =
        typeof(SpecialCardReward).GetField("_card", System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.NonPublic);

    private Dictionary<string, object?> CardInfo(CardModel c)
    {
        var stats = new Dictionary<string, object?>();
        try { foreach (var dv in c.DynamicVars.Values) stats[dv.Name.ToLowerInvariant()] = (int)dv.BaseValue; } catch { }
        var kws = c.Keywords?.Where(k => k != CardKeyword.None).Select(k => k.ToString()).ToList();
        return new Dictionary<string, object?>
        {
            ["id"] = c.Id.ToString(),
            ["name"] = _loc.Card(c.Id.Entry),
            ["cost"] = c.EnergyCost?.GetResolved() ?? 0,
            ["type"] = c.Type.ToString(),
            ["rarity"] = c.Rarity.ToString(),
            ["upgraded"] = c.IsUpgraded,
            ["description"] = CardText(c),
            ["tips"] = Tips(() => c.HoverTips, _loc.Card(c.Id.Entry)),
            ["stats"] = stats.Count > 0 ? stats : null,
            ["keywords"] = kws?.Count > 0 ? kws : null,
        };
    }

    /// <summary>Fields for reward kinds beyond gold/relic/potion/card, plus the reward's own description.</summary>
    private void AddSpecialRewardInfo(Reward reward, Dictionary<string, object?> info)
    {
        switch (reward)
        {
            case SpecialCardReward scr:
                info["type"] = "special_card";
                if (SpecialRewardCard?.GetValue(scr) is CardModel card)
                    info["card"] = CardInfo(card);
                break;
            case CardRemovalReward:
                info["type"] = "card_removal";
                break;
        }
        if (!info.ContainsKey("description") || info["description"] == null)
        {
            var d = TryFormat(() => reward.Description.GetFormattedText());
            if (d != null) info["description"] = d;
        }
    }

    // ─── Card reward alternatives (Skip / Reroll / Pael's Wing Sacrifice) ───

    private static List<Dictionary<string, object?>> AlternativesInfo(IReadOnlyList<CardRewardAlternative>? alts)
    {
        var list = new List<Dictionary<string, object?>>();
        if (alts == null) return list;
        for (int i = 0; i < alts.Count; i++)
        {
            var a = alts[i];
            list.Add(new Dictionary<string, object?>
            {
                ["index"] = i,
                ["id"] = a.OptionId,
                ["name"] = TryFormat(() => a.Title.GetFormattedText()) ?? a.OptionId,
                ["after"] = a.AfterSelected.ToString(),
            });
        }
        return list;
    }

    private Dictionary<string, object?> DoSelectCardAlternative(Dictionary<string, object?>? args)
    {
        if (!_cardSelector.HasPendingReward)
            return Error("No pending card reward");
        if (args == null || !args.ContainsKey("alternative_index"))
            return Error("select_card_alternative requires 'alternative_index'");
        var idx = Convert.ToInt32(args["alternative_index"]);
        var alts = _cardSelector.PendingRewardAlternatives;
        if (alts == null || idx < 0 || idx >= alts.Count)
            return Error($"Invalid alternative index {idx}, {alts?.Count ?? 0} alternatives available");
        Log($"Card reward alternative: {alts[idx].OptionId}");
        _cardSelector.ResolveRewardAlternative(idx);
        AwaitBackground();
        WaitForActionExecutor();
        return DetectDecisionPoint();
    }

    // ─── Game over ───

    private void AddRunStats(Dictionary<string, object?> state, bool isVictory)
    {
        var rs = _runState;
        if (rs == null) return;
        try
        {
            var history = rs.MapPointHistory;
            state["total_floor"] = history.Sum(a => a.Count);
            state["score"] = ScoreUtility.CalculateScore(rs, isVictory);
            state["score_breakdown"] = new Dictionary<string, object?>
            {
                ["floors"] = ScoreUtility.GetScoreForFloor(history),
                ["gold"] = ScoreUtility.GetScoreForGoldGained(history, rs.Players.Count),
                ["elites_killed"] = ScoreUtility.GetElitesKilledCount(history),
                ["bosses_slain"] = ScoreUtility.GetBossesSlainCount(history, isVictory),
                ["ascension"] = rs.AscensionLevel,
            };
        }
        catch (Exception ex) { Log($"Run stats: {ex.Message}"); }
        if (isVictory) return;
        try
        {
            // RunHistoryUtilities: the last room of the last map point says what killed you.
            var room = rs.MapPointHistory.LastOrDefault()?.LastOrDefault()?.Rooms.LastOrDefault();
            if (room == null) return;
            var character = rs.Players[0].Character;
            if (room.RoomType.IsCombatRoom() && ModelDb.GetByIdOrNull<EncounterModel>(room.ModelId) is { } enc)
            {
                state["killed_by"] = new Dictionary<string, object?>
                {
                    ["type"] = "encounter",
                    ["id"] = enc.Id.Entry,
                    ["name"] = TryFormat(() => enc.Title.GetFormattedText()) ?? enc.Id.Entry,
                    ["quote"] = TryFormat(() => enc.GetLossMessageFor(character).GetFormattedText()),
                };
            }
            else if (room.RoomType == RoomType.Event && ModelDb.GetByIdOrNull<EventModel>(room.ModelId) is { } ev)
            {
                string? quote = null;
                var loss = LocString.GetIfExists(ev.LocTable, ev.Id.Entry + ".loss");
                if (loss != null)
                {
                    character.AddDetailsTo(loss);
                    loss.Add("event", ev.Title);
                    quote = TryFormat(() => loss.GetFormattedText());
                }
                state["killed_by"] = new Dictionary<string, object?>
                {
                    ["type"] = "event",
                    ["id"] = ev.Id.Entry,
                    ["name"] = EventName(ev),
                    ["quote"] = quote,
                };
            }
        }
        catch (Exception ex) { Log($"Killed by: {ex.Message}"); }
    }

    // ─── Fake Merchant (custom-layout event: a relic shop, and a fight if you throw a Foul Potion) ───

    private bool FakeMerchantOpen(MegaCrit.Sts2.Core.Models.Events.FakeMerchant fm) =>
        !fm.StartedFight && fm.Inventory != null && !ReferenceEquals(_finishedEventAcked, fm);

    private Dictionary<string, object?> FakeMerchantState(MegaCrit.Sts2.Core.Models.Events.FakeMerchant fm)
    {
        var player = _runState!.Players[0];
        var relics = fm.Inventory.RelicEntries.Select((e, i) =>
        {
            var d = e.Model != null ? RelicInfo(e.Model, i)
                : new Dictionary<string, object?> { ["index"] = i, ["name"] = null, ["description"] = null };
            d["cost"] = e.Cost;
            d["is_stocked"] = e.IsStocked;
            return d;
        }).ToList();
        // NFakeMerchant: throwing a Foul Potion at the merchant starts the fight.
        var foul = player.Potions?.Select((p, i) => (p, i))
            .Where(t => t.p is MegaCrit.Sts2.Core.Models.Potions.FoulPotion).Select(t => t.i).ToList() ?? new();
        return new Dictionary<string, object?>
        {
            ["type"] = "decision",
            ["decision"] = "shop",
            ["context"] = RunContext(),
            ["event"] = fm.Id.Entry,
            ["event_name"] = EventName(fm),
            ["cards"] = new List<Dictionary<string, object?>>(),
            ["relics"] = relics,
            ["potions"] = new List<Dictionary<string, object?>>(),
            ["card_removal_cost"] = null,
            ["card_removal_available"] = false,
            ["throwable_potions"] = foul,
            ["player"] = PlayerSummary(player),
        };
    }

    /// <summary>The merchant inventory the player is browsing: the shop's, or the Fake Merchant's.</summary>
    private MerchantInventory? CurrentMerchantInventory()
    {
        if (_runState?.CurrentRoom is MerchantRoom mr) return mr.GetLocalInventory();
        if (_runState?.CurrentRoom is EventRoom
            && RunManager.Instance.EventSynchronizer?.GetLocalEvent() is MegaCrit.Sts2.Core.Models.Events.FakeMerchant fm
            && FakeMerchantOpen(fm))
            return fm.Inventory;
        return null;
    }

    /// <summary>
    /// FoulPotion.OnUse only reaches FakeMerchant.FoulPotionThrown through the NFakeMerchant node;
    /// headless the potion is used normally and the throw is started here.
    /// </summary>
    private bool TryThrowFoulPotionAtFakeMerchant(Player player, PotionModel potion, out Dictionary<string, object?>? result)
    {
        result = null;
        if (potion is not MegaCrit.Sts2.Core.Models.Potions.FoulPotion foul || CombatManager.Instance.IsInProgress) return false;
        if (_runState?.CurrentRoom is not EventRoom
            || RunManager.Instance.EventSynchronizer?.GetLocalEvent() is not MegaCrit.Sts2.Core.Models.Events.FakeMerchant fm
            || !FakeMerchantOpen(fm))
            return false;
        Log("Throwing a Foul Potion at the Fake Merchant");
        try
        {
            var action = new MegaCrit.Sts2.Core.GameActions.UsePotionAction(potion, null, false);
            RunManager.Instance.ActionQueueSet.EnqueueWithoutSynchronizing(action);
            WaitForActionExecutor();
            _syncCtx.Pump();
            if ((player.Potions?.Contains(potion)) == true)
            {
                MegaCrit.Sts2.Core.Commands.PotionCmd.Discard(potion).GetAwaiter().GetResult();
                _syncCtx.Pump();
            }
            if (!RunInBackground(() => fm.FoulPotionThrown(foul)))
                WaitForActionExecutor();
        }
        catch (Exception ex) { Log($"Foul Potion throw: {ex.Message}"); }
        result = DetectDecisionPoint();
        return true;
    }
}
