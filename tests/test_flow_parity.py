"""Room flow, card-selection constraints, strict actions and save/load (no choice is made for
the player, nothing the game doesn't offer is accepted)."""
import random

import pytest

from conftest import Game


def actions(state):
    return [a["action"] for a in state.get("legal_actions", [])]


def non_potion(state):
    return [a for a in state.get("legal_actions", []) if a["action"] not in ("use_potion", "discard_potion")]


def to_map(game, seed, **kw):
    return game.skip_neow(game.send({"cmd": "start_run", "character": "Ironclad", "seed": seed, **kw}))


def random_step(game, state, rng):
    """Policy for driving a run in tests: a seeded uniform pick among the non-potion legal
    actions (multi-card selections take the minimum number of cards, sampled)."""
    entry = rng.choice(non_potion(state))
    if "choose" in entry:
        ch = entry["choose"]
        picks = rng.sample(ch["from"], ch["min"])
        return game.act(entry["action"], indices=",".join(map(str, picks)))
    if "choose_cell" in entry:
        x, y = rng.choice(entry["choose_cell"]["cells"])
        return game.act(entry["action"], x=x, y=y, tool=rng.choice(entry["choose_cell"]["tools"]))
    return game.act(entry["action"], **(entry.get("args") or {}))


def save_load(game, path):
    r = game.send({"cmd": "write_continue_save", "path": str(path)})
    assert r["type"] == "save_result" and r["success"] is True, r
    return game.send({"cmd": "load_save", "path": str(path)})


class TestContinueSaveTwice:
    def test_combat_survives_repeated_save_load(self, tmp_path):
        """round1 HIGH: the second save/load in the same room used to skip the fight."""
        g = Game()
        try:
            state = to_map(g, "REPRO1")
            node = next(c for c in state["choices"] if c["type"] == "Monster")
            state = g.act("select_map_node", col=node["col"], row=node["row"])
            assert state["decision"] == "combat_play"
            floor = state["context"]["floor"]
            enemies = [e["name"] for e in state["enemies"]]
            for _ in range(3):
                state = save_load(g, tmp_path / "c.save")
                assert state["decision"] == "combat_play", state.get("decision")
                assert state["context"]["floor"] == floor
                assert [e["name"] for e in state["enemies"]] == enemies
        finally:
            g.close()

    def test_boss_room_survives_repeated_save_load(self, tmp_path):
        g = Game()
        try:
            state = to_map(g, "SLBOSS")
            g.set_player(hp=99999, max_hp=99999)
            rng = random.Random(7)
            for _ in range(3000):
                if state["decision"] == "combat_play" and state["context"].get("room_type") == "Boss":
                    break
                assert state["decision"] != "game_over"
                state = random_step(g, state, rng)
            assert state["context"].get("room_type") == "Boss", "did not reach the boss"
            floor = state["context"]["floor"]
            for _ in range(3):
                state = save_load(g, tmp_path / "b.save")
                assert state["decision"] == "combat_play", state.get("decision")
                assert state["context"]["floor"] == floor and state["legal_actions"]
        finally:
            g.close()


class TestCardSelectPrefs:
    def test_single_card_smith_is_a_cancelable_screen(self, game):
        """CardSelectCmd only skips the screen when count <= MinSelect and no manual
        confirmation; Smith has Cancelable + RequireManualConfirmation, so one card still asks."""
        to_map(game, "sel1")
        game.set_player(deck=["STRIKE_IRONCLAD+", "DEFEND_IRONCLAD+", "BASH"])
        state = game.enter_room("rest_site")
        smith = next(o for o in state["options"] if o["option_id"] == "SMITH")
        state = game.act("choose_option", option_index=smith["index"])
        assert state["decision"] == "card_select"
        assert [c["name"] for c in state["cards"]] == ["Bash"]
        assert state["purpose"] == "upgrade" and state["cancelable"] is True and state["prompt"]
        assert state["cards"][0]["after_upgrade"]["stats"]["damage"] == 10
        assert "skip_select" in actions(state)
        state = game.act("skip_select")  # the screen's back button
        assert state["decision"] == "rest_site" and state["can_leave"] is False
        assert any(o["option_id"] == "SMITH" for o in state["options"])

    def test_enchant_screen_prompt_and_preview(self, game):
        to_map(game, "sel2")
        state = game.enter_room("event", event="SELF_HELP_BOOK")
        state = game.act("choose_option", option_index=0)
        assert state["decision"] == "card_select"
        assert state["purpose"] == "enchant" and state["prompt"] == "Choose a card to Enchant."
        assert state["enchantment"]["name"] == "Sharp" and state["enchantment"]["amount"] == 2
        strike = next(c for c in state["cards"] if c["name"] == "Strike")
        assert strike["stats"]["damage"] == 6 and strike["after_enchant"]["stats"]["damage"] == 8
        # Self-Help Book's prefs are not cancelable (no close button): a card must be chosen.
        assert state["cancelable"] is False and "skip_select" not in actions(state)
        assert game.act("skip_select")["code"] == "illegal_action"
        state = game.act("select_cards", indices=str(strike["index"]))
        assert state["decision"] == "event_choice" and state["finished"] is True

    @pytest.mark.parametrize("card,can_skip", [("ABUNDANCE", False), ("DISCOVERY", True)])
    def test_choose_a_card_skip_button(self, game, card, can_skip):
        """NChooseACardSelectionScreen has a skip button only when canSkip (Discovery yes,
        Abundance no), although the game asks the selector for 0-1 cards either way."""
        to_map(game, "sel4" + card)
        game.set_player(deck=[card] * 10, hp=80)
        state = game.enter_room("combat", encounter="SHRINKER_BEETLE_WEAK")
        playable = [c for c in state["hand"] if c["can_play"]]
        assert playable
        state = game.act("play_card", card_index=playable[0]["index"])
        assert state["decision"] == "card_select" and state["screen"] == "choose_a_card"
        assert state["min_select"] == (0 if can_skip else 1)
        assert ("skip_select" in actions(state)) is can_skip
        if not can_skip:
            assert game.act("skip_select")["code"] == "illegal_action"
            state = game.act("select_cards", indices="0")
        else:
            state = game.act("skip_select")
        assert state["decision"] == "combat_play"

    def test_mandatory_selection_rejects_empty(self, game):
        to_map(game, "sel3")
        state = game.enter_room("event", event="SELF_HELP_BOOK")
        state = game.act("choose_option", option_index=0)
        assert state["min_select"] == 1
        r = game.act("select_cards", indices="")
        assert r["type"] == "error"


class TestRoomFlow:
    def test_cannot_leave_neow_or_unfinished_event(self, game):
        state = game.start(seed="flow1")
        assert state["decision"] == "event_choice" and "leave_room" not in actions(state)
        assert game.act("leave_room")["code"] == "illegal_action"
        assert game.act("proceed")["code"] == "illegal_action"

    def test_finished_event_page_leave_room_and_proceed(self, game):
        """README: on the outcome page choose_option 0, leave_room or proceed leave the event."""
        for verb in ("leave_room", "proceed"):
            to_map(game, "flow2" + verb)
            game.enter_room("event", event="BYRDONIS_NEST")
            state = game.act("choose_option", option_index=0)
            assert state["decision"] == "event_choice" and state["finished"] is True
            state = game.act(verb)
            assert state["decision"] == "map_select", state

    def test_rest_site_proceed_step_and_tent(self, game):
        to_map(game, "flow3")
        game.set_player(hp=30, relics=["MINIATURE_TENT"])
        state = game.enter_room("rest_site")
        assert "leave_room" not in actions(state)
        heal = next(o for o in state["options"] if o["option_id"] == "HEAL")
        state = game.act("choose_option", option_index=heal["index"])
        assert state["decision"] == "rest_site" and state["can_leave"] is True
        smith = next(o for o in state["options"] if o["option_id"] == "SMITH")
        state = game.act("choose_option", option_index=smith["index"])
        assert state["decision"] == "card_select"
        state = game.act("select_cards", indices="0")
        # Every option used: the choices are gone and Proceed is the only way on.
        assert state["decision"] == "rest_site" and state["options"] == []
        assert [a["action"] for a in non_potion(state)] == ["leave_room"]
        assert game.act("leave_room")["decision"] == "map_select"

    def test_hoarder_disables_card_removal(self, game):
        state = game.send({"cmd": "start_run", "character": "Ironclad", "seed": "flow4", "modifiers": ["HOARDER"]})
        game.skip_neow(state)
        game.set_player(gold=500)
        state = game.enter_room("shop")
        assert state["card_removal_available"] is False and "remove_card" not in actions(state)
        assert game.act("remove_card")["code"] == "illegal_action"


class TestStrictActions:
    def _combat(self, game, seed, encounter):
        to_map(game, seed)
        game.set_player(deck=["STRIKE_IRONCLAD"] * 5 + ["DEFEND_IRONCLAD"] * 5, hp=80)
        return game.enter_room("combat", encounter=encounter)

    def test_invalid_target_is_an_error_not_retargeted(self, game):
        state = self._combat(game, "st1", "NIBBITS_NORMAL")
        strike = next(c for c in state["hand"] if c["name"] == "Strike")
        hp = [e["hp"] for e in state["enemies"]]
        r = game.act("play_card", card_index=strike["index"], target_index=9)
        assert r["code"] == "illegal_action" and "out of range" in r["message"]
        if len(state["enemies"]) > 1:
            r = game.act("play_card", card_index=strike["index"])
            assert r["code"] == "illegal_action" and "target_index" in r["message"]
        state = game.send({"cmd": "get_state"})
        assert [e["hp"] for e in state["enemies"]] == hp

    def test_args_that_do_not_apply_are_rejected(self, game):
        state = self._combat(game, "st2", "SHRINKER_BEETLE_WEAK")
        defend = next(c for c in state["hand"] if c["name"] == "Defend")
        r = game.act("play_card", card_index=defend["index"], target_index=0)
        assert r["code"] == "illegal_action" and "does not apply" in r["message"]
        # A single hittable enemy: the target can be left out (the only legal entry).
        strike = next(c for c in state["hand"] if c["name"] == "Strike")
        state = game.act("play_card", card_index=strike["index"])
        assert state["decision"] == "combat_play" and state["energy"] == 2

    def test_unlisted_map_node_and_option(self, game):
        state = to_map(game, "st3")
        r = game.act("select_map_node", col=99, row=1)
        assert r["code"] == "illegal_action" and "not reachable" in r["message"]
        r = game.act("choose_option", option_index=0)
        assert r["code"] == "illegal_action"

    def test_every_legal_entry_is_accepted(self, game):
        """The other direction: entries exactly as listed are always accepted."""
        state = to_map(game, "st4")
        rng = random.Random(3)
        for _ in range(150):
            if state["decision"] == "game_over":
                break
            state = random_step(game, state, rng)
            assert state["type"] == "decision", state


class TestCrystalSphereTool:
    def test_click_without_tool_uses_the_selected_tool(self, game):
        """The minigame keeps the selected tool (starts on Big); no default is invented."""
        to_map(game, "cs1")
        game.set_player(gold=300)
        game.enter_room("event", event="CRYSTAL_SPHERE")
        state = game.act("choose_option", option_index=0)
        assert state["decision"] == "crystal_sphere" and state["current_tool"] == "big"
        x, y = state["hidden_cells"][0]
        state = game.act("crystal_sphere_click", x=x, y=y, tool="small")
        assert state["current_tool"] == "small"
        left = state["divinations_left"]
        x, y = state["hidden_cells"][-1]
        state = game.act("crystal_sphere_click", x=x, y=y)
        assert state["current_tool"] == "small" and state["divinations_left"] == left - 1
