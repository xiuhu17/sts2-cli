using MegaCrit.Sts2.Core.Context;
using MegaCrit.Sts2.Core.Entities.Players;
using MegaCrit.Sts2.Core.Localization;
using MegaCrit.Sts2.Core.Models;
using MegaCrit.Sts2.Core.Models.Badges;
using MegaCrit.Sts2.Core.Random;
using MegaCrit.Sts2.Core.Runs;
using MegaCrit.Sts2.Core.Saves;
using MegaCrit.Sts2.Core.Timeline;

namespace Sts2Headless;

/// <summary>
/// What NGameOverScreen shows, computed once when the run ends:
///
/// - RunManager.OnEnded (death: enemy discovery after a loss) and, as the game does when it
///   saves (ShouldSave), SaveManager.UpdateProgressWithRunData on the in-memory progress (the
///   post-run epochs, the Architect damage). The profile file is never written.
/// - Page 1: the banner (BANNER.falseWin / a random BANNER.lose*), the random death quote,
///   the victory damage text.
/// - Page 2 (AnimateRunSummary): AnimateScoreLines, AnimateBadges, AnimateScoreBar (which can
///   grant the next epoch), AnimateDiscoveries (NRunSummary.AnimateInDiscoveries).
/// </summary>
public partial class RunSimulator
{
    private object? _gameOverFor;  // the RunState the cached screen belongs to
    private Dictionary<string, object?>? _gameOverScreen;

    private static string? GoText(string key, Action<LocString>? vars = null)
    {
        var ls = new LocString("game_over_screen", key);
        vars?.Invoke(ls);
        return TryFormat(() => ls.GetFormattedText());
    }

    /// <summary>The game-over screen's fields, computed once per run (the screen mutates progress).</summary>
    private Dictionary<string, object?> GameOverScreenInfo(bool isVictory)
    {
        if (_gameOverScreen != null && ReferenceEquals(_gameOverFor, _runState)) return _gameOverScreen;
        var info = new Dictionary<string, object?>();
        try { BuildGameOverScreen(info, isVictory); }
        catch (Exception ex) { Log($"Game over screen: {ex}"); }
        _gameOverFor = _runState;
        _gameOverScreen = info;
        return info;
    }

    private void BuildGameOverScreen(Dictionary<string, object?> info, bool win)
    {
        var rs = _runState!;
        var me = LocalContext.GetMe(rs) ?? rs.Players[0];

        // RunManager.OnEnded: the serializable run the screen reads, and (after a loss) the
        // monsters of the encounter that killed you join DiscoveredEnemies if never fought.
        SerializableRun run;
        try { run = RunManager.Instance.OnEnded(win); }
        catch (Exception ex) { Log($"OnEnded: {ex.Message}"); run = RunManager.Instance.ToSave(null); }
        // OnEnded's ShouldSave branch (off headless): progress gets the run, and the epochs
        // granted post-run join the player's discoveries.
        try
        {
            SaveManager.Instance.UpdateProgressWithRunData(run, win);
            var me2 = LocalContext.GetMe(run);
            if (me2 != null)
                foreach (var e in me2.DiscoveredEpochs)
                    if (!me.DiscoveredEpochs.Contains(e)) me.DiscoveredEpochs.Add(e);
        }
        catch (Exception ex) { Log($"Progress update: {ex.Message}"); }

        var history = run.MapPointHistory;
        int score = ScoreUtility.CalculateScore(run, win);

        // ── Page 1 (InitializeBannerAndQuote) ──
        if (win)
        {
            info["banner"] = _loc.Bilingual("game_over_screen", "BANNER.falseWin");
            var text = GoText("VICTORY_DAMAGE_LOCAL", ls =>
            {
                ls.Add("PlayerDamage", score);
                ls.Add("PersonalDamage", MegaCrit.Sts2.Core.Platform.StatsManager.GetPersonalArchitectDamage());
            });
            int asc = rs.AscensionLevel;
            if (asc < 10 && asc > 0 && asc >= me.MaxAscensionWhenRunStarted && rs.GameMode == GameMode.Standard)
                text += "\n\n" + GoText("VICTORY_UNLOCKED_ASCENSION", ls => ls.Add("AscensionLevel", asc + 1));
            info["victory_text"] = text;
        }
        else
        {
            // Rng.Chaotic picks among the table's BANNER.lose* and QUOTES.* lines.
            var banners = _loc.KeysWithPrefix("game_over_screen", "BANNER.lose");
            if (banners.Count > 0) info["banner"] = _loc.Bilingual("game_over_screen", Rng.Chaotic.NextItem(banners)!);
            var quotes = _loc.KeysWithPrefix("game_over_screen", "QUOTES");
            // Page 1's quote; the summary page replaces it with killed_by.quote.
            if (quotes.Count > 0) info["death_quote"] = _loc.Bilingual("game_over_screen", Rng.Chaotic.NextItem(quotes)!);
        }

        // ── AnimateScoreLines ──
        var lines = new List<Dictionary<string, object?>>();
        void Line(string key, string amountKey, int amount, string points) => lines.Add(new()
        {
            ["key"] = key,
            ["text"] = GoText("SCORE_LINE." + key, ls => ls.Add(amountKey, amount)),
            ["amount"] = amount,
            ["points"] = points,
        });
        Line("floorsClimbed", "FloorCount", rs.TotalFloor, $"+{ScoreUtility.GetScoreForFloor(history)}");
        int gold = history.SelectMany(a => a).Sum(e => e.GetEntry(me.NetId)?.GoldGained ?? 0);
        Line("goldGained", "GoldAmount", gold, $"+{ScoreUtility.GetScoreForGoldGained(history, run.Players.Count)}");
        int elites = ScoreUtility.GetElitesKilledCount(history);
        if (elites > 0) Line("elitesKilled", "EliteCount", elites, $"+{ScoreUtility.GetScoreForElitesKilled(elites)}");
        int bosses = ScoreUtility.GetBossesSlainCount(history, win);
        if (bosses > 0) Line("bossesSlain", "BossCount", bosses, $"+{ScoreUtility.GetScoreForBossesSlain(bosses)}");
        int ascension = run.Ascension;
        if (ascension > 0) Line("ascension", "AscensionLevel", ascension, "x" + AscensionMulti(ascension));
        info["score_lines"] = lines;

        // ── AnimateBadges ──
        var badges = new List<Dictionary<string, object?>>();
        foreach (var b in ScoreUtility.GetBadges(run, me.NetId, win))
        {
            // NBadge.Create: the rarity's title/description when the table has one.
            var prefix = b.Rarity switch { BadgeRarity.Bronze => "bronze", BadgeRarity.Silver => "silver", BadgeRarity.Gold => "gold", _ => "ERROR" };
            bool rar = LocString.Exists("badges", $"{b.Id}.{prefix}Title");
            var title = new LocString("badges", rar ? $"{b.Id}.{prefix}Title" : $"{b.Id}.title");
            var desc = new LocString("badges", rar ? $"{b.Id}.{prefix}Description" : $"{b.Id}.description");
            badges.Add(new()
            {
                ["id"] = b.Id,
                ["rarity"] = b.Rarity.ToString().ToLowerInvariant(),
                ["title"] = TryFormat(() => title.GetFormattedText()),
                ["description"] = TryFormat(() => desc.GetFormattedText()),
            });
        }
        info["badges"] = badges;

        // ── AnimateScoreBar ──
        info["score_progress"] = ScoreBar(score, me);

        // ── AnimateInDiscoveries ──
        var disc = new List<Dictionary<string, object?>>();
        void Cat<T>(string kind, string suffix, string countKey, List<T> ids, Func<T, string?> title)
        {
            if (ids.Count == 0) return;
            var names = ids.Select(id => { try { return title(id); } catch { return null; } }).Select((n, i) => n ?? ids[i]!.ToString()!).ToList();
            var body = GoText("DISCOVERY_BODY_" + suffix, ls => ls.Add(countKey, ids.Count));
            // NRunSummary.GetDiscoveryBodyText: the first 10 names under the count line (not for epochs).
            if (kind != "epochs")
                body += "\n\n" + string.Join("\n", names.Take(10)) + (names.Count > 10 ? "\n...." : "");
            disc.Add(new()
            {
                ["kind"] = kind,
                ["count"] = ids.Count,
                ["names"] = names,
                ["header"] = GoText("DISCOVERY_HEADER_" + suffix),
                ["body"] = body,
            });
        }
        Cat("cards", "CARD", "CardCount", me.DiscoveredCards, id => TryFormat(() => SaveUtil.CardOrDeprecated(id).Title));
        Cat("relics", "RELIC", "RelicCount", me.DiscoveredRelics, id => TryFormat(() => SaveUtil.RelicOrDeprecated(id).Title.GetFormattedText()));
        Cat("potions", "POTION", "PotionCount", me.DiscoveredPotions, id => TryFormat(() => SaveUtil.PotionOrDeprecated(id).Title.GetFormattedText()));
        Cat("enemies", "ENEMY", "EnemyCount", me.DiscoveredEnemies, id => TryFormat(() => SaveUtil.MonsterOrDeprecated(id).Title.GetFormattedText()));
        Cat("epochs", "EPOCH", "EpochCount", me.DiscoveredEpochs, id => TryFormat(() => EpochModel.Get(id).Title.GetFormattedText()));
        info["discoveries_header"] = disc.Count > 0 ? GoText("DISCOVERY_HEADER") : null;
        info["discoveries"] = disc;

        // The bottom button: Main Menu, labelled Unlock when an epoch was discovered (SetLabelForUnlock).
        info["main_menu_button"] = GoText(me.DiscoveredEpochs.Count > 0 ? "BUTTON.unlock" : "BUTTON.mainMenu");
    }

    /// <summary>NGameOverScreen.GetAscensionMulti: "1.5" for A5, "2" for A10.</summary>
    private static string AscensionMulti(int ascension)
    {
        int whole = ascension / 10 + 1, tenth = ascension % 10;
        return tenth != 0 ? $"{whole}.{tenth}" : whole.ToString();
    }

    /// <summary>NGameOverScreen.GetScoreThreshold.</summary>
    private static int ScoreThreshold(int unlocksRemaining)
    {
        int n = SaveManager.TotalAgnosticUnlocks - unlocksRemaining;
        if (n < 0 || n >= EpochModel.AgnosticUnlocks.Count) return 0;
        return EpochModel.AgnosticUnlocks[n].ScoreThreshold;
    }

    /// <summary>
    /// NGameOverScreen.AnimateScoreBar: "Unlocks remaining: N" and, while unlocks remain, the
    /// bar [current/threshold] filled by this run's score. Filling it grants the next epoch
    /// (SaveManager.GrantNextUnlock), which joins the run's discoveries.
    /// </summary>
    private Dictionary<string, object?> ScoreBar(int score, Player me)
    {
        var sm = SaveManager.Instance;
        int remaining = sm.GetUnlocksRemaining();
        var bar = new Dictionary<string, object?>
        {
            ["unlocks_remaining"] = remaining,
            ["unlocks_remaining_text"] = GoText("SCORE.unlocksRemaining", ls => ls.Add("UnlockCount", remaining)),
            ["visible"] = remaining > 0,
            ["score_added"] = score,
        };
        if (remaining <= 0) return bar;
        int current = sm.GetCurrentScore();
        int threshold = ScoreThreshold(remaining);
        bar["score_before"] = current;
        bar["threshold"] = threshold;
        if (current + score >= threshold)
        {
            current -= threshold;
            int next = ScoreThreshold(remaining - 1);
            bar["unlocked"] = true;
            bar["unlock_text"] = GoText(next == 0 ? "SCORE.unlockedAllMessage" : "SCORE.unlockedEpochMessage");
            var epoch = sm.GrantNextUnlock();
            if (epoch != null)
            {
                if (!me.DiscoveredEpochs.Contains(epoch.Id)) me.DiscoveredEpochs.Add(epoch.Id);
                bar["unlocked_epoch"] = new Dictionary<string, object?>
                {
                    ["id"] = epoch.Id,
                    ["title"] = TryFormat(() => epoch.Title.GetFormattedText()),
                };
            }
            bar["unlocks_remaining"] = remaining - 1;
            bar["unlocks_remaining_text"] = GoText("SCORE.unlocksRemaining", ls => ls.Add("UnlockCount", remaining - 1));
            current += score;
            int saved = next == 0 || current == 0 ? 0 : current >= next ? next - 1 : current;
            sm.Progress.CurrentScore = saved;
            bar["score_after"] = saved;
            bar["threshold_after"] = next;
            // The bar's last frame: the overflow, or 99% when it would unlock twice.
            int shown = next == 0 || current == 0 ? threshold : current >= next ? next * 99 / 100 : current;
            bar["progress_text"] = next == 0 || current == 0 ? $"[{threshold}/{threshold}]" : $"[{shown}/{next}]";
        }
        else
        {
            bar["unlocked"] = false;
            sm.Progress.CurrentScore += score;
            bar["score_after"] = current + score;
            bar["threshold_after"] = threshold;
            bar["progress_text"] = $"[{current + score}/{threshold}]";
        }
        return bar;
    }
}
