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
                    assert isinstance(v, (int, float))

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
        game.skip_neow(game.start(seed="cs1"))
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
        game.skip_neow(game.start(seed="cs2"))
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
        game.skip_neow(game.start(seed="am1"))
        state = game.enter_room("event", event="AMALGAMATOR")
        assert state["decision"] == "event_choice", state
        opt = next(o for o in state["options"] if "STRIKE" in (o.get("text_key") or "").upper())
        state = game.act("choose_option", option_index=opt["index"])
        assert state["decision"] == "card_select"
        state = game.select_first(state)
        names = [c["name"] for c in state["player"]["deck"]]
        assert "Ultimate Strike" in names, names


class TestCrystalSphereGate:
    def test_cannot_leave_mid_minigame(self, game):
        game.skip_neow(game.start(seed="cs3"))
        game.set_player(gold=500)
        state = game.enter_room("event", event="CRYSTAL_SPHERE")
        opt = next(o for o in state["options"] if not o.get("is_locked"))
        state = game.act("choose_option", option_index=opt["index"])
        assert state["decision"] == "crystal_sphere"
        err = game.act("leave_room")
        assert err["type"] == "error"
        x, y = state["hidden_cells"][0]
        assert game.act("crystal_sphere_click", x=x, y=y)["type"] == "decision"
