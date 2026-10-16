"""Tests for events."""
import pytest


class TestNeowEvent:
    def test_neow_is_first_event(self, game):
        state = game.start(seed="ne1")
        assert state["decision"] == "event_choice"
        assert "Neow" in str(state.get("event_name", ""))

    def test_neow_options(self, game):
        state = game.start(seed="ne2")
        for opt in state["options"]:
            assert "title" in opt
            assert isinstance(opt["title"], str)
            assert "is_locked" in opt

    def test_neow_option_vars(self, game):
        state = game.start(seed="ne3")
        for opt in state["options"]:
            if opt.get("vars"):
                for k, v in opt["vars"].items():
                    assert isinstance(v, (int, float, str))  # StringVars carry names

    def test_choose_neow(self, game):
        state = game.start(seed="ne4")
        opts = [o for o in state["options"] if not o.get("is_locked")]
        state = game.act("choose_option", option_index=opts[0]["index"])
        assert state.get("decision") is not None


class TestEventDescriptions:
    def test_no_ismultiplayer_tag(self, game):
        state = game.start(seed="ed1")
        for opt in state.get("options", []):
            d = opt.get("description") or ""
            assert "IsMultiplayer" not in d


class TestCrystalSphere:
    def test_minigame_is_playable(self, game):
        game.neow(game.start(seed="cs1"), 0)
        game.set_player(gold=500)
        state = game.enter_room("event", event="CRYSTAL_SPHERE")
        assert state["decision"] == "event_choice", state
        opt = next(o for o in state["options"] if not o.get("is_locked"))
        state = game.act("choose_option", option_index=opt["index"])
        assert state["decision"] == "crystal_sphere", state.get("decision")
        total = state["divinations_left"]
        for _ in range(total):
            assert state["decision"] == "crystal_sphere"
            x, y = state["hidden_cells"][0]
            state = game.act("crystal_sphere_click", x=x, y=y, tool="big")
        assert state["type"] == "decision"
        assert state["decision"] != "crystal_sphere"

    def test_click_validation(self, game):
        game.neow(game.start(seed="cs2"), 0, cards=[0])
        game.set_player(gold=500)
        state = game.enter_room("event", event="CRYSTAL_SPHERE")
        opt = next(o for o in state["options"] if not o.get("is_locked"))
        state = game.act("choose_option", option_index=opt["index"])
        assert game.act("crystal_sphere_click", x=99, y=0)["type"] == "error"
        assert game.act("crystal_sphere_click", x=0, y=0, tool="huge")["type"] == "error"


class TestEventsWithInlineUi:
    def test_amalgamator_adds_ultimate_strike(self, game):
        """The event calls NGame.Instance.ScreenShakeTrauma mid-logic; headless that used to
        throw after removing the Strikes, so Ultimate Strike was never added."""
        game.neow(game.start(seed="am1"), 0)
        state = game.enter_room("event", event="AMALGAMATOR")
        assert state["decision"] == "event_choice", state
        opt = next(o for o in state["options"] if "STRIKE" in (o.get("text_key") or "").upper())
        state = game.act("choose_option", option_index=opt["index"])
        assert state["decision"] == "card_select"
        assert state["min_select"] == 2, state["min_select"]
        state = game.select(state, [0, 1])  # the test names the two Strikes to combine
        names = [c["name"] for c in state["player"]["deck"]]
        assert "Ultimate Strike" in names, names


class TestCrystalSphereGate:
    def test_cannot_leave_mid_minigame(self, game):
        game.neow(game.start(seed="cs3"), 0)
        game.set_player(gold=500)
        state = game.enter_room("event", event="CRYSTAL_SPHERE")
        opt = next(o for o in state["options"] if not o.get("is_locked"))
        state = game.act("choose_option", option_index=opt["index"])
        assert state["decision"] == "crystal_sphere"
        err = game.act("leave_room")
        assert err["type"] == "error"
        x, y = state["hidden_cells"][0]
        assert game.act("crystal_sphere_click", x=x, y=y)["type"] == "decision"


def _trial_reject_page(game, lang="en"):
    game.start(seed="t1", lang=lang)
    state = game.enter_room("Event", event="TRIAL")
    reject = next(o for o in state["options"] if o["text_key"].endswith("INITIAL.options.REJECT"))
    state = game.choose(state, reject["index"])
    assert [o["text_key"].rsplit(".", 1)[-1] for o in state["options"]] == ["ACCEPT", "DOUBLE_DOWN"], state
    return state


class TestTrialAbandonPopup:
    """Trial: Double Down opens NAbandonRunConfirmPopup (Yes: RunManager.Abandon, No: close)."""

    def test_double_down_opens_the_confirm_popup(self, game):
        state = _trial_reject_page(game)
        dd = next(o for o in state["options"] if o["text_key"].endswith("DOUBLE_DOWN"))
        assert dd["will_kill"] is True
        state = game.choose(state, dd["index"])
        assert state["decision"] == "event_choice" and state["event_id"] == "TRIAL"
        assert state["popup"] == {"id": "abandon_run_confirm", "header": "Are you sure?",
                                  "body": "Abandoning the game will result in a loss."}
        assert [(o["title"], o["text_key"], o["will_kill"]) for o in state["options"]] == [
            ("Yes", "GENERIC_POPUP.confirm", True), ("No", "GENERIC_POPUP.cancel", False)]
        assert [a["args"]["option_index"] for a in state["legal_actions"] if a["action"] == "choose_option"] == [0, 1]
        assert "leave_room" not in [a["action"] for a in state["legal_actions"]]

    def test_cancel_returns_to_the_reject_page(self, game):
        page = _trial_reject_page(game)
        dd = next(o for o in page["options"] if o["text_key"].endswith("DOUBLE_DOWN"))
        state = game.choose(game.choose(page, dd["index"]), 1)
        assert "popup" not in state
        assert [o["text_key"] for o in state["options"]] == [o["text_key"] for o in page["options"]]
        assert state["description"] == page["description"]

    def test_confirm_abandons_the_run(self, game):
        page = _trial_reject_page(game)
        dd = next(o for o in page["options"] if o["text_key"].endswith("DOUBLE_DOWN"))
        state = game.choose(game.choose(page, dd["index"]), 0)
        assert state["decision"] == "game_over"
        assert state["victory"] is False and state["abandoned"] is True
        assert state["legal_actions"] == []

    def test_popup_text_in_chinese(self, game):
        page = _trial_reject_page(game, lang="zh")
        dd = next(o for o in page["options"] if o["text_key"].endswith("DOUBLE_DOWN"))
        state = game.choose(page, dd["index"])
        assert state["popup"]["header"] == "你确定吗？"
        assert state["popup"]["body"] == "放弃游戏会被视为本局失败。"
        assert [o["title"] for o in state["options"]] == ["好的", "不了"]

    def test_normal_death_is_not_abandoned(self, game):
        game.start(seed="t2")
        game.set_player(hp=1)
        state = game.fight(game.enter_room("combat", encounter="SHRINKER_BEETLE_WEAK"),
                           policy=game.policy("t2"))
        assert state["decision"] == "game_over", state["decision"]  # 1 HP: the beetle kills you
        assert state["victory"] is False and state["abandoned"] is False


class TestArchitectEpilogue:
    """After the last act's boss, RunManager.EnterNextAct enters The Architect's event; its
    Proceed (TheArchitect.WinRun) wins the run."""

    def _beat_last_boss(self, game, seed, profile=None):
        cmd = {"cmd": "start_run", "character": "Ironclad", "seed": seed}
        if profile:
            cmd["profile"] = profile
        game.send(cmd)
        game.set_player(hp=99999, max_hp=99999, deck=["BLUDGEON"] * 10)
        state = game.enter_room("combat", encounter="KAISER_CRAB_BOSS", act=3)
        assert state["context"]["act"] == 3 and state["context"]["room_type"] == "Boss", state["context"]
        state = game.fight(state, policy=game.policy(seed))
        if state["decision"] == "rewards":  # the boss's reward screen: leave it
            state = game.act("proceed")
        return state

    def test_architect_dialogue_then_victory(self, game):
        state = self._beat_last_boss(game, "arch1")
        assert state["decision"] == "event_choice" and state["event_id"] == "THE_ARCHITECT", state
        shown = []
        for _ in range(10):
            assert state["decision"] == "event_choice", state
            assert len(state["options"]) == 1 and state["legal_actions"] == [
                {"action": "choose_option", "args": {"option_index": 0}}]
            lines = state["dialogue"]
            assert lines[:len(shown)] == shown and len(lines) == len(shown) + 1, lines
            assert all(ln["text"] and ln["key"].startswith("THE_ARCHITECT.talk.IRONCLAD.") for ln in lines)
            shown = lines
            last = state["options"][0]["text_key"] == "PROCEED"
            state = game.choose(state, 0)
            if last:
                break
        assert state["decision"] == "game_over", state
        assert state["victory"] is True and state["abandoned"] is False
        # No profile: the Ironclad has 0 wins, so the VisitIndex 0 dialogue (seeded event RNG).
        assert shown[0]["key"] == "THE_ARCHITECT.talk.IRONCLAD.0-0r.ancient"
        assert [ln["speaker"] for ln in shown] == ["ancient", "character"]

    def test_architect_respond_buttons(self, game):
        state = self._beat_last_boss(game, "arch2")
        # Line 0's button is its .next text (Threaten); the last line's is Proceed.
        assert state["options"][0]["title"] == "Threaten"
        state = game.choose(state, 0)
        assert state["options"][0]["title"] == "Proceed" and state["options"][0]["text_key"] == "PROCEED"

    @pytest.mark.skipif(not __import__("os").path.isfile(__import__("os").environ.get("STS2_PARITY_PROFILE", "")),
                        reason="STS2_PARITY_PROFILE (the recorded real-game profile) not set")
    def test_architect_dialogue_follows_profile_wins(self, game):
        import os
        # The recorded profile's Ironclad has 6 wins: no VisitIndex-6 dialogue, so
        # TheArchitect.LoadDialogue draws among the repeating ones (VisitIndex <= 6) with the
        # event's seeded RNG; the draw is the game's, so it is pinned per seed.
        prof = os.environ["STS2_PARITY_PROFILE"]
        picks = {seed: self._beat_last_boss(game, seed, profile=prof)["dialogue"][0]["key"]
                 for seed in ("arch1", "arch3")}
        assert picks == {"arch1": "THE_ARCHITECT.talk.IRONCLAD.1-0r.ancient",
                         "arch3": "THE_ARCHITECT.talk.IRONCLAD.0-0r.ancient"}, picks
        assert self._beat_last_boss(game, "arch1", profile=prof)["dialogue"][0]["key"] == picks["arch1"]
