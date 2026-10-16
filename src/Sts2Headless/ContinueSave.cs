using HarmonyLib;
using MegaCrit.Sts2.Core.Rooms;
using MegaCrit.Sts2.Core.Runs;
using MegaCrit.Sts2.Core.Saves;
using MegaCrit.Sts2.Core.Saves.Managers;
using MegaCrit.Sts2.Core.Saves.Runs;

namespace Sts2Headless;

/// <summary>
/// Keeps the run save the real game would have on disk right now (its "Continue" file).
///
/// The game saves when a map node is entered, before the room is built
/// (RunManager.EnterMapPointInternal -> SaveManager.SaveRun(null)), after a combat is won
/// (CombatManager -> SaveRun(room)) and when an Ancient finishes (EventRoom -> SaveRun(this)).
/// Continue re-enters the last visited coord from that save (NGame.LoadRun ->
/// RunManager.LoadIntoLatestMapCoord), so a mid-room save always resumes the same room with
/// the same encounter instead of letting the player pick another path or reroll it.
///
/// RunSaveManager.SaveRun(AbstractRoom?) is replaced by a capture of RunManager.ToSave: nothing is
/// written to the save store, and write_continue_save writes the captured run when it is asked
/// for mid-room.
/// </summary>
internal static class ContinueSave
{
    /// <summary>The latest save the game made (null until the first save of the run).</summary>
    internal static SerializableRun? Last;

    /// <summary>
    /// While loading a save into a combat that was already won, the CLI generates the room-end
    /// rewards itself (as after every fight); the game's own OfferRoomEndRewards would run the
    /// test reward selector on the main thread instead.
    /// </summary>
    internal static bool SuppressRoomEndRewards;

    private static bool _installed;

    public static void Install()
    {
        if (_installed) return;
        _installed = true;
        var harmony = new Harmony("sts2headless.continuesave");
        try
        {
            var save = typeof(RunSaveManager).GetMethod(nameof(RunSaveManager.SaveRun), new[] { typeof(AbstractRoom) });
            if (save != null)
                harmony.Patch(save, prefix: new HarmonyMethod(typeof(ContinueSave).GetMethod(nameof(SaveRunPrefix))));
            else
                Console.Error.WriteLine("[WARN] ContinueSave: RunSaveManager.SaveRun(AbstractRoom) not found");
        }
        catch (Exception ex) { Console.Error.WriteLine($"[WARN] ContinueSave: {ex.Message}"); }
        try
        {
            var offer = typeof(CombatRoom).GetMethod(nameof(CombatRoom.OfferRoomEndRewards));
            if (offer != null)
                harmony.Patch(offer, prefix: new HarmonyMethod(typeof(ContinueSave).GetMethod(nameof(OfferRoomEndRewardsPrefix))));
        }
        catch (Exception ex) { Console.Error.WriteLine($"[WARN] ContinueSave rewards: {ex.Message}"); }
    }

    /// <summary>A fresh, unshared copy of a save (null if it does not parse).</summary>
    internal static SerializableRun? Snapshot(string saveJson)
    {
        try
        {
            var r = SaveManager.FromJson<SerializableRun>(saveJson);
            return r.Success ? r.SaveData : null;
        }
        catch { return null; }
    }

    public static bool SaveRunPrefix(AbstractRoom? preFinishedRoom, ref Task __result)
    {
        try { Last = RunManager.Instance.ToSave(preFinishedRoom); }
        catch (Exception ex)
        {
            Console.Error.WriteLine($"[SIM] Continue-save capture failed: {ex.Message}");
            Last = null;
        }
        __result = Task.CompletedTask;
        return false;
    }

    public static bool OfferRoomEndRewardsPrefix(ref Task __result)
    {
        if (!SuppressRoomEndRewards) return true;
        __result = Task.CompletedTask;
        return false;
    }

    /// <summary>The save's position matches the live run (same act, same visited path).</summary>
    internal static bool MatchesPosition(SerializableRun save, RunState state)
    {
        try
        {
            if (save.CurrentActIndex != state.CurrentActIndex) return false;
            var saved = save.VisitedMapCoords;
            var live = state.VisitedMapCoords;
            if (saved == null || saved.Count != live.Count) return false;
            for (int i = 0; i < live.Count; i++)
                if (saved[i] != live[i]) return false;
            return true;
        }
        catch { return false; }
    }

    internal static bool IsReloadableRoom(SerializableRoom? room) =>
        room != null && room.RoomType is RoomType.Monster or RoomType.Elite or RoomType.Boss or RoomType.Event;

    /// <summary>
    /// The save was made on entering its last visited node (before the room existed) or after
    /// its room was won: Continue re-enters that node. A CLI map checkpoint (the room was left)
    /// has a history entry for every visited node and no pre-finished room.
    /// </summary>
    internal static bool ShouldReenterLatestCoord(SerializableRun save)
    {
        try
        {
            var visited = save.VisitedMapCoords?.Count ?? 0;
            if (visited == 0) return false;
            // CLI map checkpoints carry the MapRoom here; the game only stores won combats
            // and finished events (AbstractRoom.FromSerializable accepts nothing else).
            if (IsReloadableRoom(save.PreFinishedRoom)) return true;
            var history = save.MapPointHistory;
            var actHistory = history != null && save.CurrentActIndex < history.Count
                ? history[save.CurrentActIndex]?.Count ?? 0
                : 0;
            return actHistory < visited;
        }
        catch { return false; }
    }
}
