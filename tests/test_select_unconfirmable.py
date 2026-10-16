"""Card selections whose screen can't be confirmed with the cards it shows. The real game
doesn't clamp the requested count to the cards offered (CardSelectCmd only skips the screen
when n <= MinSelect without manual confirmation; NCombatPileCardSelectScreen alone clamps to
the cards shown), so the adapter doesn't either: a cancelable screen can only be closed
(skip_select), a non-cancelable one is a softlock (only the potion belt)."""
import os

from conftest import Game


def actions(state):
    return [a["action"] for a in state.get("legal_actions", [])]


def to_map(game, seed):
    return game.neow(game.send({"cmd": "start_run", "character": "Ironclad", "seed": seed}), 0,
                     cards=[0], rewards="proceed", bundle=0)


def deck_names(game):
    return [c["name"] + ("+" if c.get("upgraded") else "") for c in game.send({"cmd": "get_deck"})["cards"]]


class TestCancelableUnconfirmable:
    def test_smith_two_with_one_upgradable_card_can_only_close(self, game):
        """SmithRestSiteOption: prefs (upgrade, SmithCount), Cancelable, RequireManualConfirmation,
        so FromDeckForUpgrade shows NDeckUpgradeSelectScreen even with 1 upgradable card; its
        CheckIfSelectionComplete needs Count >= MaxSelect (2): only Close works."""
        to_map(game, "unc1")
        game.set_player(deck=["STRIKE_IRONCLAD", "DEFEND_IRONCLAD+", "BASH+"])
        state = game.enter_room("rest_site")
        assert game.set_player(smith_count=2)["type"] == "ok"
        smith = next(o for o in state["options"] if o["option_id"] == "SMITH")
        state = game.act("choose_option", option_index=smith["index"])
        assert state["decision"] == "card_select" and state["purpose"] == "upgrade", state
        assert len(state["cards"]) == 1 and state["cancelable"] is True
        assert state["min_select"] == 2 and state["max_select"] == 2  # not clamped to 1
        assert state["select_counts"] == []
        assert "softlocked" not in state and "skip_select" in state["message"]
        assert actions(state) == ["skip_select"], state["legal_actions"]
        err = game.act("select_cards", indices="0")
        assert err["type"] == "error" and err["code"] == "illegal_action", err
        assert "can't be confirmed" in err["message"] and "skip_select" in err["message"]
        state = game.act("skip_select")
        # Close: OnSelect returns false, the rest site keeps its options.
        assert state["decision"] == "rest_site", state
        assert any(o["option_id"] == "SMITH" for o in state["options"])
        assert "Strike" in deck_names(game)

    def test_cancelable_deck_screen_below_min_can_only_close(self, game):
        """NDeckCardSelectScreen with manual confirmation and MinSelect 3 > 2 cards: the bottom
        Confirm needs Count >= MinSelect and the preview opens at Count == MaxSelect."""
        to_map(game, "unc2")
        game.set_player(deck=["STRIKE_IRONCLAD", "DEFEND_IRONCLAD"])
        state = game.set_player(card_select={"method": "deck", "min": 3, "max": 3, "cancelable": True, "manual": True})
        assert state["decision"] == "card_select" and len(state["cards"]) == 2, state
        assert state["min_select"] == 3 and state["select_counts"] == []
        assert actions(state) == ["skip_select"]
        assert game.act("select_cards", indices="0,1")["type"] == "error"
        assert game.act("skip_select")["decision"] == "map_select"


class TestNonCancelableUnconfirmable:
    def test_grid_without_manual_confirm_below_max_is_a_softlock(self, game):
        """NSimpleCardSelectScreen, prefs (1, 3), no manual confirmation, 2 cards: the Confirm
        button is disabled (MinSelect != 0, no manual confirm) and the screen only completes at
        Count >= MaxSelect; it has no close button."""
        to_map(game, "unc3")
        game.set_player(deck=["STRIKE_IRONCLAD", "DEFEND_IRONCLAD"],
                        potions=["BLOOD_POTION", "FIRE_POTION"])
        state = game.set_player(card_select={"method": "grid", "min": 1, "max": 3})
        assert state["decision"] == "card_select" and len(state["cards"]) == 2, state
        assert state["min_select"] == 1 and state["max_select"] == 3
        assert state["softlocked"] is True and state["select_counts"] == []
        assert "shows only 2" in state["softlock_reason"]
        acts = state["legal_actions"]
        assert all(a["action"] in ("use_potion", "discard_potion") for a in acts), acts
        # Blood Potion is AnyTime (usable here), Fire Potion needs an enemy.
        assert {"action": "use_potion", "args": {"potion_index": 0}} in acts
        assert not any(a["action"] == "use_potion" and a["args"]["potion_index"] == 1 for a in acts)
        for a in ("select_cards", "skip_select"):
            err = game.act(a, indices="0,1") if a == "select_cards" else game.act(a)
            assert err["type"] == "error" and err["code"] == "illegal_action", err
        err = game.send({"cmd": "force_leave_room"})
        assert err["code"] == "softlock_escape_disabled"

    def test_grid_min_zero_keeps_its_open_confirm(self, game):
        """The same screen with MinSelect 0: Confirm is enabled at open, so confirming nothing is
        the only size left (it is not a softlock)."""
        to_map(game, "unc4")
        game.set_player(deck=["STRIKE_IRONCLAD", "DEFEND_IRONCLAD"])
        state = game.set_player(card_select={"method": "grid", "min": 0, "max": 3})
        assert state["decision"] == "card_select" and "softlocked" not in state, state
        assert state["min_select"] == 0 and state["max_select"] == 0
        assert state["legal_actions"] == [{"action": "select_cards", "args": {"indices": ""}}]
        assert game.act("select_cards", indices="0")["type"] == "error"
        assert game.act("select_cards", indices="")["decision"] == "map_select"

    def test_upgrade_screen_not_cancelable_is_a_softlock(self, game):
        """NDeckUpgradeSelectScreen, prefs (1, 2) not cancelable, 1 upgradable card (n > MinSelect,
        so no auto-pick): Count can never reach MaxSelect."""
        to_map(game, "unc5")
        game.set_player(deck=["STRIKE_IRONCLAD", "BASH+"], potions=[])
        state = game.set_player(card_select={"method": "upgrade", "min": 0, "max": 2})
        assert state["decision"] == "card_select" and state["softlocked"] is True, state
        assert state["legal_actions"] == []

    def test_force_leave_room_closes_the_selection(self, monkeypatch):
        monkeypatch.setenv("STS2_SOFTLOCK_ESCAPE", "1")
        g = Game()
        try:
            to_map(g, "unc6")
            g.set_player(deck=["STRIKE_IRONCLAD", "DEFEND_IRONCLAD"])
            state = g.set_player(card_select={"method": "grid", "min": 1, "max": 3})
            assert state["softlocked"] is True and "closes the selection" in state["message"]
            state = g.send({"cmd": "force_leave_room"})
            assert state["decision"] == "map_select", state
        finally:
            g.close()


class TestGameSideClamps:
    def test_auto_pick_when_offered_at_most_min(self, game):
        """FromDeckGeneric without manual confirmation and n <= MinSelect returns every card
        without a screen (kept: no decision)."""
        to_map(game, "unc7")
        game.set_player(deck=["STRIKE_IRONCLAD", "DEFEND_IRONCLAD"])
        state = game.set_player(card_select={"method": "deck", "min": 3, "max": 3})
        assert state["decision"] == "map_select", state

    def test_combat_pile_screen_clamps_to_cards_shown(self, game):
        """Neow's Fury: FromCombatPile(discard, prefs (0, 2)), no manual confirmation. With 1 card in
        the discard pile NCombatPileCardSelectScreen completes at Min(MaxSelect, cards shown) = 1:
        the game's own clamp is kept (0 via the open-time Confirm, or the 1 card)."""
        to_map(game, "unc8")
        game.set_player(deck=["NEOWS_FURY"] + ["STRIKE_IRONCLAD"] * 4)
        state = game.enter_room("combat", encounter="KAISER_CRAB_BOSS")
        assert state["decision"] == "combat_play", state
        strike = next(c for c in state["hand"] if c["id"] == "CARD.STRIKE_IRONCLAD")
        target = state["enemies"][0]["index"]
        state = game.act("play_card", card_index=strike["index"], target_index=target)
        fury = next(c for c in state["hand"] if c["id"] == "CARD.NEOWS_FURY")
        state = game.act("play_card", card_index=fury["index"], target_index=target)
        assert state["decision"] == "card_select", state
        assert len(state["cards"]) == 1 and "softlocked" not in state
        assert state["min_select"] == 0 and state["max_select"] == 1
        sel = sorted(a["args"]["indices"] for a in state["legal_actions"] if a["action"] == "select_cards")
        assert sel == ["", "0"], state["legal_actions"]
        state = game.act("select_cards", indices="0")
        assert state["decision"] == "combat_play", state
        assert sum(1 for c in state["hand"] if c["id"] == "CARD.STRIKE_IRONCLAD") == 4
