"""The game-over screen as the real game shows it (NGameOverScreen / NRunSummary), and card_added
lines that always name the pile.

Page 1: banner (BANNER.falseWin, or a random BANNER.lose*) and quote; page 2 (AnimateRunSummary):
score lines (floors climbed / gold gained always; elites, bosses, ascension only when > 0) with
their raw amounts and the points shown ("+490", "x1.5"), the badges (ScoreUtility.GetBadges), the
unlock score bar (only while unlocks remain) and the discoveries (Player.Discovered*).
"""

import json
import os

import pytest

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
PARITY_PROFILE = os.environ.get("STS2_PARITY_PROFILE", "")


def _replay_full_run(game):
    """Replay the recorded real-game run (seed R3B0F9WFJUMG, Ironclad, the recorded profile)
    from the start to its death at the act 2 boss. The log has a few commands the CLI refuses
    (map clicks sent before the CLI-only reward 'proceed'; the proceed follows): they are skipped."""
    with open(os.path.join(FIXTURES, "r3b0f9wfjumg_full_run.jsonl"), encoding="utf-8") as f:
        acts = [json.loads(line) for line in f if line.strip()]
    steps = [game.send(dict(acts[0], profile=PARITY_PROFILE))]
    errors = []
    for i, a in enumerate(acts[1:], 1):
        r = game.send(a)
        if r.get("type") == "error":
            errors.append((i, a["action"]))
            continue
        steps.append(r)
    return steps, errors


@pytest.mark.skipif(not os.path.isfile(PARITY_PROFILE), reason="STS2_PARITY_PROFILE (the recorded real-game profile) not set")
class TestRecordedRealGameOver:
    def test_game_over_screen_matches_the_real_game(self, game):
        steps, errors = _replay_full_run(game)
        assert [a for _, a in errors] == ["select_map_node"] * 5, errors
        go = steps[-1]
        assert go["decision"] == "game_over" and go["victory"] is False
        assert go["killed_by"]["id"] == "KAISER_CRAB_BOSS"
        assert go["score"] == 645
        # The real game's summary page (seed R3B0F9WFJUMG): four score lines, no ascension line.
        assert [(ln["text"], ln["points"]) for ln in go["score_lines"]] == [
            ("攀爬楼层：33", "+490"), ("获得金币：511", "+5"), ("击杀精英：1", "+50"), ("击杀Boss：1", "+100")]
        assert [ln["amount"] for ln in go["score_lines"]] == [33, 511, 1, 1]
        assert [ln["key"] for ln in go["score_lines"]] == ["floorsClimbed", "goldGained", "elitesKilled", "bossesSlain"]
        bd = go["score_breakdown"]
        assert (bd["floors"], bd["gold"], bd["elites_killed"], bd["bosses_slain"]) == (490, 5, 1, 1)
        assert (bd["floors_climbed"], bd["gold_gained"], bd["elites_killed_points"], bd["bosses_slain_points"]) == (33, 511, 50, 100)
        assert go["badges"] == []
        # The profile has every score-bar unlock: no bar.
        assert go["score_progress"]["unlocks_remaining"] == 0 and go["score_progress"]["visible"] is False
        # Discoveries: one relic (Biiig Hug), as the real screen showed.
        assert go["discoveries_header"] == "发现"
        assert [(d["kind"], d["count"], d["names"]) for d in go["discoveries"]] == [("relics", 1, ["大～抱抱"])]
        rel = go["discoveries"][0]
        assert rel["header"] == "发现遗物" and rel["body"] == "你在本局游戏中发现了1件新遗物。\n\n大～抱抱"
        assert go["banner"] in {"被击败", "死亡……", "败北", "败北！", "尸骨无存", "惨败", "消亡", "你已被杀死！"}
        assert go["main_menu_button"] == "主菜单"
        # Asked again: the same screen (computed once).
        again = game.send({"cmd": "get_state"})
        assert again["score_lines"] == go["score_lines"] and again["discoveries"] == go["discoveries"]
        assert again["banner"] == go["banner"]

        # Every card_added names its pile. Act 2 floor 14: Anger+ kills the last enemy, the
        # combat is ending, its copy is never added (CardPileCmd.Add) and the game shows nothing.
        for s in steps:
            for e in s.get("events") or []:
                if e.get("type") == "card_added":
                    assert e.get("pile"), (s.get("context"), e)
        kill = next(s for s in steps if any(e.get("type") == "card_played" and e.get("card") == "愤怒+"
                                            for e in s.get("events") or [])
                    and s.get("decision") == "rewards")
        assert not [e for e in kill["events"] if e.get("type") == "card_added"], kill["events"]

    def test_text_view_shows_the_screen(self, game):
        import sts2_daemon as d
        steps, _ = _replay_full_run(game)
        d.LANG["lang"] = "zh"
        try:
            out = "\n".join(d.game_over_lines(steps[-1]))
        finally:
            d.LANG["lang"] = "en"
        for s in ("攀爬楼层：33", "+490", "获得金币：511", "击杀Boss：1", "+100", "发现遗物: 1 (大～抱抱)"):
            assert s in out, out
        assert "剩余解锁" not in out


class TestGameOverScreen:
    def _win(self, game, seed="gow1"):
        game.start(seed=seed)
        game.set_player(hp=99999, max_hp=99999, deck=["BLUDGEON"] * 10)
        state = game.fight(game.enter_room("combat", encounter="KAISER_CRAB_BOSS", act=3), policy=game.policy(seed))
        if state["decision"] == "rewards":
            state = game.act("proceed")
        for _ in range(15):
            if state["decision"] != "event_choice":
                break
            state = game.choose(state, 0)
        assert state["decision"] == "game_over" and state["victory"] is True, state["decision"]
        return state

    def test_victory_screen(self, game):
        go = self._win(game)
        assert go["banner"] == "Victory...?"
        assert go["victory_text"].startswith(f"You dealt {go['score']} damage to the Architect")
        assert [ln["key"] for ln in go["score_lines"]][:2] == ["floorsClimbed", "goldGained"]
        floors = go["score_lines"][0]
        assert floors["text"] == f"Floors Climbed: {floors['amount']}" and floors["points"].startswith("+")
        # Won with a 10-card deck and huge max HP: Tiny Deck and Glutton, with the game's text.
        badges = {b["id"]: b for b in go["badges"]}
        assert badges["TINY_DECK"]["title"] == "Tiny Deck" and badges["TINY_DECK"]["rarity"] == "silver"
        assert badges["GLUTTON"]["description"]
        # No profile: a fresh progress, so the score bar fills toward the first unlock.
        sp = go["score_progress"]
        assert sp["visible"] is True and sp["unlocks_remaining"] > 0
        assert sp["unlocks_remaining_text"] == f"Unlocks Remaining: {sp['unlocks_remaining']}"
        assert sp["progress_text"] == f"[{sp['score_after']}/{sp['threshold_after']}]"
        kinds = {d["kind"]: d for d in go["discoveries"]}
        assert kinds["enemies"]["count"] == len(kinds["enemies"]["names"]) > 0
        assert kinds["enemies"]["header"] == "Discovered Enemies"
        assert kinds["enemies"]["body"].startswith(f"You discovered {kinds['enemies']['count']} new enemies this run.")
        # A discovered epoch relabels the Main Menu button Unlock.
        assert "epochs" in kinds and go["main_menu_button"] == "Unlock"

    def test_ascension_line(self, game):
        game.start(seed="goa1", ascension=5)
        game.set_player(hp=1)
        state = game.fight(game.enter_room("combat", encounter="SHRINKER_BEETLE_WEAK"), policy=game.policy("goa1"))
        assert state["decision"] == "game_over" and state["victory"] is False
        asc = state["score_lines"][-1]
        assert asc["key"] == "ascension" and asc["amount"] == 5 and asc["points"] == "x1.5"
        assert asc["text"] == "Ascension: 5"
        assert "elitesKilled" not in [ln["key"] for ln in state["score_lines"]]
        assert state["death_quote"] and state["banner"]


class TestCardAddedPile:
    def test_anger_killing_blow_has_no_pileless_card_added(self, game):
        game.start(seed="anger1")
        game.set_player(deck=["ANGER"] * 10, hp=999, max_hp=999)
        state = game.enter_room("combat", encounter="SHRINKER_BEETLE_WEAK")
        seen_added = False
        for _ in range(200):
            if state.get("decision") != "combat_play":
                break
            playable = [a for a in state["legal_actions"] if a["action"] == "play_card"]
            prev = state
            state = game.send({"cmd": "action", **(playable[0] if playable else {"action": "end_turn"})})
            for e in state.get("events") or []:
                if e.get("type") == "card_added":
                    assert e.get("pile") == "discard", e
                    seen_added = True
        assert state["decision"] != "combat_play"
        assert seen_added  # the copies from the non-lethal plays went to the discard pile
        # The killing blow: the copy is never added (the combat is ending) and is not reported.
        assert prev["decision"] == "combat_play"
        assert [e["type"] for e in state["events"] if e["type"] == "card_played"] == ["card_played"]
        assert not [e for e in state["events"] if e["type"] == "card_added"], state["events"]


def test_renderers_show_score_lines_badges_bar_and_discoveries():
    import game_over
    st = {"decision": "game_over", "victory": True, "banner": "Victory...?", "score": 120,
          "score_lines": [{"key": "floorsClimbed", "text": "Floors Climbed: 3", "amount": 3, "points": "+30"},
                          {"key": "ascension", "text": "Ascension: 5", "amount": 5, "points": "x1.5"}],
          "badges": [{"id": "TINY_DECK", "rarity": "silver", "title": "Tiny Deck", "description": "Won with 10 cards."}],
          "score_progress": {"visible": True, "unlocks_remaining_text": "Unlocks Remaining: 3", "progress_text": "[50/200]"},
          "discoveries": [{"kind": "cards", "count": 2, "names": ["Bash", "Anger"], "header": "Discovered Cards"}]}
    out = "\n".join(game_over.summary_lines(st, lambda en, zh: en))
    for s in ("[Victory...?]", "Floors Climbed: 3", "+30", "x1.5", "Tiny Deck (silver)", "Unlocks Remaining: 3  [50/200]",
              "Discovered Cards: 2 (Bash, Anger)"):
        assert s in out, out
    st["score_progress"]["visible"] = False
    assert "Unlocks Remaining" not in "\n".join(game_over.summary_lines(st, lambda en, zh: en))
