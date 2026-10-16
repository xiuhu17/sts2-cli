"""Room flow, card-selection constraints, strict actions and save/load (no choice is made for
the player, nothing the game doesn't offer is accepted)."""
import json
import random

import pytest

from conftest import Game


def actions(state):
    return [a["action"] for a in state.get("legal_actions", [])]


def non_potion(state):
    return [a for a in state.get("legal_actions", []) if a["action"] not in ("use_potion", "discard_potion")]


def to_map(game, seed, **kw):
    return game.neow(game.send({"cmd": "start_run", "character": "Ironclad", "seed": seed, **kw}), 0, cards=[0], rewards="proceed", bundle=0)


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


    def test_mid_room_save_is_frozen_at_the_game_save(self, tmp_path):
        """The continue save is what the game wrote when it saved (after the won fight), not
        the live run: a card picked afterwards must not leak into its map history."""
        g = Game()
        try:
            state = to_map(g, "rv4", manual_rewards=True)
            g.set_player(hp=9999, max_hp=9999)
            node = next(c for c in state["choices"] if c["type"] == "Monster")
            state = g.act("select_map_node", col=node["col"], row=node["row"])
            rng = random.Random(1)
            while state["decision"] == "combat_play":
                entry = rng.choice([a for a in state["legal_actions"] if a["action"] == "play_card"]
                                   or [a for a in state["legal_actions"] if a["action"] == "end_turn"])
                state = g.act(entry["action"], **(entry.get("args") or {}))
            assert state["decision"] == "rewards"
            first = tmp_path / "a.save"
            assert g.send({"cmd": "write_continue_save", "path": str(first)})["success"] is True
            card = next(r for r in state["rewards"] if r["type"] == "card")
            state = g.act("claim_reward", reward_index=card["index"])
            assert state["decision"] == "card_reward"
            state = g.act("select_card_reward", card_index=rng.choice([c["index"] for c in state["cards"]]))
            assert state["decision"] == "rewards"
            second = tmp_path / "b.save"
            assert g.send({"cmd": "write_continue_save", "path": str(second)})["success"] is True
            assert json.loads(first.read_text()) == json.loads(second.read_text())
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
        # A card is picked by clicking it (no confirm-with-nothing); leaving is the Skip button.
        assert state["min_select"] == 1 and state["cancelable"] is can_skip
        assert ("skip_select" in actions(state)) is can_skip
        assert not any(a.get("args", {}).get("indices") == "" for a in state["legal_actions"])
        assert game.act("select_cards", indices="")["type"] == "error"
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

    def test_kifuda_requires_a_card(self, game):
        """Kifuda.AfterObtained: CardSelectorPrefs(enchant, 0, 3), Cancelable false. The bottom
        Confirm of NDeckEnchantSelectScreen opens the preview with nothing selected, but the
        preview's ConfirmSelection is `if (_selectedCards.Count != 0) CheckIfSelectionComplete()`
        and Close is disabled: the screen can only be left with 1-3 cards."""
        to_map(game, "kif1")
        game.set_player(deck=["STRIKE_IRONCLAD"] * 3 + ["DEFEND_IRONCLAD"] * 3)
        state = game.set_player(obtain_relic="KIFUDA")
        assert state["decision"] == "card_select" and state["purpose"] == "enchant"
        assert state["min_select"] == 1 and state["max_select"] == 3 and state["cancelable"] is False
        assert state["legal_actions"] == [{"action": "select_cards", "choose": {
            "from": [c["index"] for c in state["cards"]], "min": 1, "max": 3}}], state["legal_actions"]
        for empty in ([], ""):
            err = game.act("select_cards", indices=empty)
            assert err["type"] == "error" and "nothing selected" in err["message"], err
        err = game.act("skip_select")
        assert err["code"] == "illegal_action"
        assert game.act("select_cards", indices="0,1,2,3")["type"] == "error"
        state = game.act("select_cards", indices="4")
        assert state["decision"] == "map_select", state
        assert sum(1 for c in game.send({"cmd": "get_deck"})["cards"] if c.get("enchantment")) == 1

    def test_gnarled_hammer_requires_a_card(self, game):
        """GnarledHammer: the same NDeckEnchantSelectScreen with prefs (0, 3), not cancelable."""
        to_map(game, "gnh1")
        game.set_player(deck=["STRIKE_IRONCLAD"] * 3 + ["DEFEND_IRONCLAD"] * 3)
        state = game.set_player(obtain_relic="GNARLED_HAMMER")
        assert state["decision"] == "card_select" and state["purpose"] == "enchant"
        assert state["min_select"] == 1 and state["cancelable"] is False
        assert game.act("select_cards", indices="")["type"] == "error"
        state = game.act("select_cards", indices="0")
        assert state["decision"] == "map_select", state

    def test_smith_upgrade_screen_never_confirms_nothing(self, game):
        """NDeckUpgradeSelectScreen has no bottom Confirm: the preview opens at Count ==
        MaxSelect and its ConfirmSelection needs Count != 0. Leaving is the Close button."""
        to_map(game, "smith0")
        game.set_player(deck=["STRIKE_IRONCLAD"] * 3 + ["DEFEND_IRONCLAD"] * 3)
        state = game.enter_room("rest_site")
        smith = next(o for o in state["options"] if o["option_id"] == "SMITH")
        state = game.act("choose_option", option_index=smith["index"])
        assert state["decision"] == "card_select" and state["purpose"] == "upgrade"
        assert state["min_select"] == 1 and state["max_select"] == 1 and state["cancelable"] is True
        assert not any(a.get("args", {}).get("indices") == "" for a in state["legal_actions"])
        assert game.act("select_cards", indices="")["type"] == "error"
        assert "skip_select" in actions(state)

    def test_claws_transform_confirms_nothing(self, game):
        """Claws: NDeckTransformSelectScreen, prefs (0, 6), not cancelable. Its ConfirmSelection
        is `if (_selectedCards.Count >= _prefs.MinSelect)`: nothing selected is accepted."""
        to_map(game, "claw0")
        game.set_player(deck=["STRIKE_IRONCLAD"] * 3 + ["DEFEND_IRONCLAD"] * 3)
        state = game.set_player(obtain_relic="CLAWS")
        assert state["decision"] == "card_select" and state["purpose"] == "transform"
        assert state["min_select"] == 0 and state["cancelable"] is False
        choose = next(a["choose"] for a in state["legal_actions"] if a["action"] == "select_cards")
        assert choose["min"] == 0
        state = game.act("select_cards", indices="")
        assert state["decision"] == "map_select", state
        assert [c["name"] for c in game.send({"cmd": "get_deck"})["cards"]].count("Strike") == 3

    def test_kifuda_enchants_the_chosen_cards(self, game):
        to_map(game, "kif2")
        game.set_player(deck=["STRIKE_IRONCLAD"] * 3 + ["DEFEND_IRONCLAD"] * 3)
        state = game.set_player(obtain_relic="KIFUDA")
        state = game.act("select_cards", indices="0,1")
        assert state["decision"] == "map_select", state
        assert sum(1 for c in game.send({"cmd": "get_deck"})["cards"] if c.get("enchantment")) == 2

    def test_optional_hand_selection_confirms_nothing(self, game):
        """Purity: FromHand(prefs 0..N). NPlayerHand's confirm button is enabled once
        MinSelect (0) cards are picked, and the hand has no cancel button."""
        to_map(game, "pur1")
        game.set_player(deck=["PURITY"] * 5 + ["STRIKE_IRONCLAD"] * 5)
        state = game.enter_room("combat", encounter="SHRINKER_BEETLE_WEAK")
        purity = next(c for c in state["hand"] if c["name"] == "Purity")
        hand_before = len(state["hand"])
        state = game.act("play_card", card_index=purity["index"])
        assert state["decision"] == "card_select" and state["screen"] == "hand"
        assert state["min_select"] == 0 and state["cancelable"] is False
        assert "skip_select" not in actions(state)
        choose = next(a["choose"] for a in state["legal_actions"] if a["action"] == "select_cards")
        assert choose["min"] == 0
        state = game.act("select_cards", indices="")
        assert state["decision"] == "combat_play"
        assert len(state["hand"]) == hand_before - 1  # only Purity left the hand

    def test_select_cards_needs_indices(self, game):
        to_map(game, "kif3")
        game.set_player(obtain_relic="KIFUDA")
        r = game.send({"cmd": "action", "action": "select_cards", "args": {"indices": None}})
        assert r["type"] == "error"
        r = game.send({"cmd": "action", "action": "select_cards"})
        assert r["type"] == "error"


class TestUpgradePreviewLiveCost:
    def test_temporary_cost_shows_in_after_upgrade(self, game):
        """NUpgradePreview upgrades a clone of the card itself (CardScope.CloneCard), so a cost
        changed this turn (Enlightenment: SetThisTurnOrUntilPlayed(1)) stays on the preview.
        The deck's Bash (2) was cached first; the hand's Bash costs 1 and must preview 1."""
        to_map(game, "up1")
        game.set_player(deck=["ENLIGHTENMENT", "ARMAMENTS", "BASH", "BASH", "STRIKE_IRONCLAD"])
        state = game.enter_room("combat", encounter="SHRINKER_BEETLE_WEAK")
        deck_bash = next(c for c in game.send({"cmd": "get_deck", "upgrades": True})["cards"] if c["name"] == "Bash")
        assert deck_bash["cost"] == 2 and deck_bash["after_upgrade"]["cost"] == 2
        state = game.act("play_card", card_index=next(c["index"] for c in state["hand"] if c["name"] == "Enlightenment"))
        assert [c["cost"] for c in state["hand"] if c["name"] == "Bash"] == [1, 1]
        state = game.act("play_card", card_index=next(c["index"] for c in state["hand"] if c["name"] == "Armaments"))
        assert state["decision"] == "card_select" and state["purpose"] == "upgrade"
        bashes = [c for c in state["cards"] if c["name"] == "Bash"]
        assert [(c["cost"], c["after_upgrade"]["cost"]) for c in bashes] == [(1, 1), (1, 1)], bashes
        # The deck copy is unchanged (combat costs live on the combat card).
        deck_bash = next(c for c in game.send({"cmd": "get_deck", "upgrades": True})["cards"] if c["name"] == "Bash")
        assert deck_bash["after_upgrade"]["cost"] == 2


class TestUpgradePreviewSavedState:
    def test_genetic_algorithm_preview_keeps_grown_block(self, game):
        """NUpgradePreview / NDeckUpgradeSelectScreen upgrade a clone of the card itself, so
        [SavedProperty] state carries over: a played Genetic Algorithm's deck copy has block
        1 + 3 = 4 and its upgraded preview must too (not the canonical card's 1)."""
        to_map(game, "ga1")
        game.set_player(deck=["GENETIC_ALGORITHM", "STRIKE_IRONCLAD", "STRIKE_IRONCLAD",
                              "DEFEND_IRONCLAD", "DEFEND_IRONCLAD"])
        deck = game.send({"cmd": "get_deck", "upgrades": True})["cards"]
        ga = next(c for c in deck if c["id"] == "CARD.GENETIC_ALGORITHM")
        assert ga["stats"]["block"] == 1 and ga["after_upgrade"]["stats"]["block"] == 1  # cached at 1
        state = game.enter_room("combat", encounter="SHRINKER_BEETLE_WEAK")
        state = game.act("play_card", card_index=next(c["index"] for c in state["hand"] if c["id"] == "CARD.GENETIC_ALGORITHM"))
        assert state["type"] == "decision", state
        deck = game.send({"cmd": "get_deck", "upgrades": True})["cards"]
        ga = next(c for c in deck if c["id"] == "CARD.GENETIC_ALGORITHM")
        assert ga["stats"]["block"] == 4, ga
        assert ga["after_upgrade"]["stats"]["block"] == 4, ga["after_upgrade"]
        assert ga["after_upgrade"]["stats"]["increase"] == 4  # the upgrade: Increase 3 -> 4
        assert "4" in ga["after_upgrade"]["description"], ga["after_upgrade"]["description"]


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
        game.neow(state, 0)
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

    def test_single_pick_indices_formats(self, game):
        """A listed single pick may be sent as "0", " 0", 0 or [0]; other picks are rejected."""
        to_map(game, "st5")
        for value in (" 0", [0], 0):
            game.set_player(gold=500)
            state = game.enter_room("shop")
            assert game.act("remove_card")["decision"] == "card_select"
            r = game.act("select_cards", indices="0,1")
            assert r["code"] == "illegal_action"
            state = game.act("select_cards", indices=value)
            assert state["decision"] == "shop" and "remove_card" not in actions(state), value
            game.act("leave_room")

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
