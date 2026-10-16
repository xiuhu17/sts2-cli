using HarmonyLib;
using MegaCrit.Sts2.Core.Entities.Ancients;
using MegaCrit.Sts2.Core.Entities.Creatures;
using MegaCrit.Sts2.Core.Entities.Players;
using MegaCrit.Sts2.Core.Events;
using MegaCrit.Sts2.Core.Localization;
using MegaCrit.Sts2.Core.Models;
using MegaCrit.Sts2.Core.Models.Events;
using MegaCrit.Sts2.Core.Rooms;
using MegaCrit.Sts2.Core.Runs;

namespace Sts2Headless;

/// <summary>
/// Event flows whose real-game step is a UI node the headless engine doesn't have:
/// <list type="bullet">
/// <item>The Trial's Double Down opens NAbandonRunConfirmPopup (Yes: RunManager.Abandon, No:
/// close). It is surfaced as an event_choice page with the popup's two buttons.</item>
/// <item>The Architect (the last act's epilogue event, RunManager.EnterNextAct) speaks through a
/// speech bubble on its combat-layout creature; headless there is no creature node, so the
/// speaker falls back to the player's creature (the bubble is presentation only) and the lines
/// said so far are serialized as "dialogue".</item>
/// <item>Softlocks: rooms where the game shows no clickable button (every event option locked,
/// an unfinished event with no options, a rest site whose options are all disabled before any
/// was used). The decision is returned with "softlocked": true and only the potion belt's actions
/// in legal_actions (use AnyTime potions, discard); the only way out is the non-game
/// <c>force_leave_room</c> command behind STS2_SOFTLOCK_ESCAPE=1.</item>
/// </list>
/// </summary>
public partial class RunSimulator
{
    // ─── Harmony hooks ───

    internal static void InstallEventFlowPatches()
    {
        var harmony = new Harmony("sts2headless.eventflow");
        void Patch(Type type, string method, string? prefix = null, string? postfix = null)
        {
            try
            {
                var m = AccessTools.Method(type, method);
                if (m == null) { Console.Error.WriteLine($"[ERROR] event flow patch: {type.Name}.{method} not found"); return; }
                harmony.Patch(m,
                    prefix: prefix == null ? null : new HarmonyMethod(AccessTools.Method(typeof(RunSimulator), prefix)),
                    postfix: postfix == null ? null : new HarmonyMethod(AccessTools.Method(typeof(RunSimulator), postfix)));
            }
            catch (Exception ex) { Console.Error.WriteLine($"[ERROR] event flow patch {type.Name}.{method}: {ex.Message}"); }
        }
        Patch(typeof(Trial), "DoubleDown", prefix: nameof(TrialDoubleDownPrefix));
        Patch(typeof(TheArchitect), "GetSpeaker", postfix: nameof(ArchitectSpeakerPostfix));
    }

    // ─── The Trial: abandon-run confirmation ───

    /// <summary>The event whose Double Down opened the abandon-run popup (cleared by Yes/No).</summary>
    private static EventModel? s_abandonPopupEvent;

    /// <summary>Trial.DoubleDown: NModalContainer.Add(NAbandonRunConfirmPopup.Create(null)).</summary>
    private static bool TrialDoubleDownPrefix(Trial __instance, ref Task __result)
    {
        s_abandonPopupEvent = __instance;
        __result = Task.CompletedTask;
        return false;
    }

    private static bool AbandonPopupOpen(EventModel? ev) => ev != null && ReferenceEquals(s_abandonPopupEvent, ev);

    private static string MainMenuText(string key) =>
        TryFormat(() => new LocString("main_menu_ui", key).GetFormattedText()) ?? _loc.Bilingual("main_menu_ui", key);

    /// <summary>The popup over the event page (NAbandonRunConfirmPopup._Ready): header, body, Yes / No.</summary>
    private Dictionary<string, object?> AbandonPopupState(EventModel ev)
    {
        var header = MainMenuText("ABANDON_RUN_CONFIRMATION.header");
        var body = MainMenuText("ABANDON_RUN_CONFIRMATION.body");
        Dictionary<string, object?> Button(int index, string key, bool kills) => new()
        {
            ["index"] = index,
            ["title"] = MainMenuText(key),
            ["description"] = null,
            ["text_key"] = key,
            ["is_locked"] = false,
            ["is_proceed"] = false,
            ["will_kill"] = kills,
        };
        return new Dictionary<string, object?>
        {
            ["type"] = "decision",
            ["decision"] = "event_choice",
            ["context"] = RunContext(),
            ["event_name"] = EventName(ev),
            ["event_id"] = ev.Id?.Entry,
            ["description"] = EventDescriptionText(ev),
            ["popup"] = new Dictionary<string, object?>
            {
                ["id"] = "abandon_run_confirm",
                ["header"] = header,
                ["body"] = body,
            },
            ["options"] = new List<Dictionary<string, object?>>
            {
                Button(0, "GENERIC_POPUP.confirm", true),
                Button(1, "GENERIC_POPUP.cancel", false),
            },
            ["player"] = PlayerSummary(_runState!.Players[0]),
        };
    }

    /// <summary>Yes: RunManager.Abandon (the run ends as a loss). No: the popup closes on the same page.</summary>
    private Dictionary<string, object?> DoAbandonPopup(int optionIndex)
    {
        if (optionIndex is not (0 or 1))
            return Error($"Invalid option index {optionIndex}, 2 options available (Yes / No)");
        s_abandonPopupEvent = null;
        if (optionIndex == 1) return DetectDecisionPoint();
        Log("Abandoning the run (Trial: Double Down confirmed)");
        RunManager.Instance.Abandon();
        var player = _runState!.Players[0];
        for (int i = 0; i < 200 && player.Creature?.IsDead != true; i++)
        {
            _syncCtx.Pump();
            WaitForActionExecutor();
            if (player.Creature?.IsDead != true) Thread.Sleep(5);
        }
        return DetectDecisionPoint();
    }

    // ─── The Architect ───

    private static void ArchitectSpeakerPostfix(TheArchitect __instance, AncientDialogueSpeaker speaker, ref Creature? __result)
    {
        // The Architect's creature node only exists in NCombatRoom; without it PlayCurrentLine
        // returns before setting the line's option and the event would have no button.
        if (__result == null && speaker == AncientDialogueSpeaker.Ancient)
            __result = __instance.Owner?.Creature;
    }

    private static readonly System.Reflection.FieldInfo? s_architectDialogue = AccessTools.Field(typeof(TheArchitect), "_dialogue");
    private static readonly System.Reflection.FieldInfo? s_architectLine = AccessTools.Field(typeof(TheArchitect), "_currentLineIndex");

    /// <summary>The Architect's lines spoken so far (one speech bubble per line, the latest last).</summary>
    private List<Dictionary<string, object?>>? ArchitectDialogue(TheArchitect arch)
    {
        try
        {
            if (s_architectDialogue?.GetValue(arch) is not AncientDialogue dialogue || dialogue.Lines.Count == 0) return null;
            var current = s_architectLine?.GetValue(arch) is int i ? i : 0;
            var lines = DialogueLines(dialogue, includeNext: false);
            return lines.Take(Math.Min(current + 1, lines.Count)).ToList();
        }
        catch (Exception ex) { Log($"Architect dialogue: {ex.Message}"); return null; }
    }

    /// <summary>The run was won: the player is killed by RunManager.WinRun in the Architect's room.</summary>
    private bool WonAtArchitect() =>
        _runState?.CurrentRoom?.IsVictoryRoom == true && !RunManager.Instance.IsAbandoned;

    // ─── Ancient dialogue lines ───

    /// <summary>A dialogue's lines as NAncientEventLayout shows them (Act1Name added like NEventRoom.SetupLayout).</summary>
    private List<Dictionary<string, object?>> DialogueLines(AncientDialogue dialogue, bool includeNext = true)
    {
        var lines = new List<Dictionary<string, object?>>();
        foreach (var line in dialogue.Lines)
        {
            if (line.LineText == null) continue;
            try { line.LineText.Add("Act1Name", _runState!.Acts[0].Title); } catch { }
            var text = TryFormat(() => line.LineText.GetFormattedText())
                       ?? LocLookup.Clean(_loc.Bilingual("ancients", line.LineText.LocEntryKey));
            var entry = new Dictionary<string, object?>
            {
                ["speaker"] = line.Speaker == AncientDialogueSpeaker.Ancient ? "ancient" : "character",
                ["text"] = text.Trim(),
                ["key"] = line.LineText.LocEntryKey,
            };
            if (includeNext)
                entry["next"] = line.NextButtonText == null ? null : TryFormat(() => line.NextButtonText.GetFormattedText());
            lines.Add(entry);
        }
        return lines;
    }

    /// <summary>"NEOW.talk.ANY.2-0r" for a dialogue whose first line is NEOW.talk.ANY.2-0r.ancient.</summary>
    private static string? DialogueId(AncientDialogue dialogue)
    {
        var key = dialogue.Lines.FirstOrDefault()?.LineText?.LocEntryKey;
        if (key == null) return null;
        var dot = key.LastIndexOf('.');
        return dot > 0 ? key[..dot] : key;
    }

    // ─── Softlocks ───

    /// <summary>
    /// Mark a decision the game offers no button for. NEventRoom has no exit while options are
    /// shown (all locked, or none on an unfinished event); NRestSiteRoom shows Proceed only when
    /// it has no options or after one was used, and disabled options can't be clicked.
    /// </summary>
    private static void MarkSoftlock(Dictionary<string, object?> state, string why)
    {
        state["softlocked"] = true;
        state["softlock_reason"] = why;
        state["message"] = "Softlock: " + why + ". The real game has no button here that moves on, so the run can't go on " +
                           "(the potion belt still works: legal_actions lists the potions you can use or discard). " +
                           (SoftlockEscapeEnabled
                               ? "STS2_SOFTLOCK_ESCAPE=1 is set: {\"cmd\":\"force_leave_room\"} leaves the room (not game behaviour)."
                               : "Start the engine with STS2_SOFTLOCK_ESCAPE=1 to allow {\"cmd\":\"force_leave_room\"} (not game behaviour).");
    }

    internal static bool SoftlockEscapeEnabled => Environment.GetEnvironmentVariable("STS2_SOFTLOCK_ESCAPE") == "1";

    /// <summary>
    /// force_leave_room: leave a softlocked room for the map. Not game behaviour: only with
    /// STS2_SOFTLOCK_ESCAPE=1, and only while the current decision is softlocked.
    /// </summary>
    public Dictionary<string, object?> ForceLeaveRoom()
    {
        if (!SoftlockEscapeEnabled)
        {
            var e = Error("force_leave_room is not game behaviour: start the engine with STS2_SOFTLOCK_ESCAPE=1 to use it");
            e["code"] = "softlock_escape_disabled";
            return e;
        }
        if (_runState == null) return Error("No run in progress");
        var cur = SafeCurrentDecision();
        if (cur?.GetValueOrDefault("softlocked") is not true)
            return Error("force_leave_room only applies to a softlocked decision (\"softlocked\": true)");
        Log("[WARN] force_leave_room: leaving a softlocked room (not game behaviour)");
        if (_runState.CurrentRoom is EventRoom)
            _finishedEventAcked = RunManager.Instance.EventSynchronizer?.GetLocalEvent();
        if (_runState.CurrentRoom is RestSiteRoom rest) _restChoiceRoom = rest;
        ForceToMap();
        return DetectDecisionPoint();
    }
}
