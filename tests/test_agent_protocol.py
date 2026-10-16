"""Agent-facing protocol: what card selections are for, strict action validation, clean
errors, read-only queries, the per-decision events log, omit_deck, debug-command gating."""

import json
import os
import subprocess

import pytest

from conftest import DOTNET, Game, engine_command, PROJECT


def hand_names(state):
    return [c["name"] for c in state["hand"]]


def legal_names(state):
    return {a["action"] for a in state.get("legal_actions", [])}


def combat_with(game, deck, seed="ap1"):
    game.neow(game.start(seed=seed), 0, rewards="proceed")
    game.set_player(hp=80, max_hp=80, deck=deck)
    state = game.enter_room("combat", encounter="SHRINKER_BEETLE_WEAK")
    assert state["decision"] == "combat_play"
    return state


class TestCardSelectPurpose:
    def test_exhaust_from_hand_says_so(self, game):
        state = combat_with(game, ["BURNING_PACT"] * 7 + ["DEFEND_IRONCLAD"] * 3)
        state = game.act("play_card", card_index=hand_names(state).index("Burning Pact"))
        assert state["decision"] == "card_select"
        assert state["purpose"] == "exhaust"
        assert "Exhaust" in state["prompt"]
        assert state["cancelable"] is False
        assert state["screen"] == "hand"
        assert state["source"] == {"name": "Burning Pact", "kind": "card"}
        # The battlefield stays visible during the selection, as with the game's peek button.
        assert state["combat"]["enemies"] and "energy" in state["combat"]
        # A mandatory selection can't be skipped.
        assert "skip_select" not in legal_names(state)
        err = game.act("skip_select")
        assert err["type"] == "error" and err["code"] == "illegal_action"
        assert err["decision"] == "card_select" and err["legal_actions"]
        state = game.act("select_cards", indices="0")
        assert state["decision"] == "combat_play"
        assert any(e["type"] == "exhaust" for e in state["events"])

    def test_hand_upgrade(self, game):
        state = combat_with(game, ["ARMAMENTS"] * 6 + ["STRIKE_IRONCLAD"] * 4)
        state = game.act("play_card", card_index=hand_names(state).index("Armaments"))
        assert state["decision"] == "card_select"
        assert state["purpose"] == "upgrade" and state["screen"] == "hand"
        assert state["source"]["name"] == "Armaments"

    def test_smith_is_cancelable(self, game):
        game.neow(game.start(seed="ap2"), 0)
        state = game.enter_room("rest_site")
        smith = next(o for o in state["options"] if o["option_id"] == "SMITH")
        state = game.act("choose_option", option_index=smith["index"])
        assert state["decision"] == "card_select"
        assert state["purpose"] == "upgrade" and state["cancelable"] is True
        assert "skip_select" in legal_names(state)
        state = game.act("skip_select")
        assert state["decision"] == "rest_site"

    def test_single_card_cancelable_prompt_is_not_auto_picked(self, game):
        game.neow(game.start(seed="ap3"), 0)
        game.set_player(deck=["BASH"], gold=500)
        state = game.enter_room("rest_site")
        smith = next(o for o in state["options"] if o["option_id"] == "SMITH")
        state = game.act("choose_option", option_index=smith["index"])
        # The game still shows the screen (confirm or cancel) for one upgradable card.
        assert state["decision"] == "card_select" and len(state["cards"]) == 1

    def test_shop_removal(self, game):
        game.neow(game.start(seed="ap4"), 0)
        game.set_player(gold=500)
        state = game.enter_room("shop")
        card = state["cards"][0]
        assert card["price"] == card["cost"] and card["energy_cost"] == card["card_cost"]
        state = game.act("remove_card")
        assert state["purpose"] == "remove" and state["cancelable"] is True
        state = game.act("skip_select")
        assert state["decision"] == "shop"

    def test_enchant_preview(self, game):
        game.neow(game.start(seed="ap5"), 0)
        game.set_player(deck=["STRIKE_IRONCLAD"] * 3 + ["DEFEND_IRONCLAD"] * 3)
        state = game.enter_room("event", event="SELF_HELP_BOOK")
        opt = next(o for o in state["options"] if not o.get("is_locked"))
        state = game.act("choose_option", option_index=opt["index"])
        assert state["decision"] == "card_select"
        assert state["purpose"] == "enchant"
        ench = state["enchantment"]
        assert ench["name"] and ench["amount"] >= 1 and ench["description"]
        strike = next(c for c in state["cards"] if c["name"] == "Strike")
        if ench["id"] == "SHARP":
            assert strike["after_enchant"]["stats"]["damage"] == strike["stats"]["damage"] + ench["amount"]
        state = game.act("select_cards", indices="0")
        assert any(e["type"] == "deck_changed" for e in state.get("events", []))

    def test_transform_purpose(self, game):
        game.neow(game.start(seed="ap6"), 0)
        state = game.enter_room("event", event="AROMA_OF_CHAOS")
        state = game.act("choose_option", option_index=0)
        assert state["decision"] == "card_select"
        assert state["purpose"] == "transform" and state["source"]["kind"] == "event"


class TestStrictActions:
    def test_leave_room_cannot_skip_neow(self, game):
        state = game.start(seed="ap7")
        assert state["decision"] == "event_choice"
        err = game.act("leave_room")
        assert err["type"] == "error" and err["code"] == "illegal_action"
        assert "event_choice" in err["message"] and "choose_option" in err["message"]
        assert game.send({"cmd": "get_state"})["decision"] == "event_choice"

    def test_wrong_phase_action_is_an_error(self, game):
        state = combat_with(game, ["STRIKE_IRONCLAD"] * 10)
        for action in ("proceed", "choose_option", "select_map_node", "skip_card_reward"):
            err = game.act(action, option_index=0) if action == "choose_option" else game.act(action)
            assert err["type"] == "error" and err["code"] == "illegal_action", (action, err)

    def test_unknown_action_suggests(self, game):
        game.start(seed="ap8")
        err = game.act("play_crd")
        assert err["code"] == "unknown_action" and "play_card" in err["message"]

    def test_bad_arguments(self, game):
        state = combat_with(game, ["STRIKE_IRONCLAD"] * 10)
        err = game.act("play_card", card_index=1.5)
        assert err["code"] == "bad_request" and "card_index" in err["message"]
        err = game.act("play_card", card_index=0, target_index=7)
        assert err["type"] == "error" and "target_index" in err["message"]
        # Numeric strings are accepted.
        assert game.act("play_card", card_index="0")["type"] == "decision"


class TestPotionRules:
    """use_potion follows the game's Use button (NPotionPopup.RefreshButtons)."""

    def test_combat_only_potion_rejected_outside_combat(self, game):
        game.neow(game.start(seed="ap9"), 0)
        game.set_player(hp=50, max_hp=80, potions=["FIRE_POTION", "BLOOD_POTION"])
        state = game.send({"cmd": "get_state"})
        assert state["decision"] == "map_select"
        uses = [a for a in state["legal_actions"] if a["action"] == "use_potion"]
        assert uses == [{"action": "use_potion", "args": {"potion_index": 1}}]
        err = game.act("use_potion", potion_index=0)
        assert err["type"] == "error" and "combat" in err["message"]
        # Not lost: before, the potion was queued and then force-discarded.
        state = game.send({"cmd": "get_state"})
        assert [p["name"] for p in state["player"]["potions"]][:1] == ["Fire Potion"]
        state = game.act("use_potion", potion_index=1)
        assert state["type"] == "decision" and state["player"]["hp"] > 50

    def test_combat_only_potion_rejected_in_card_select(self, game):
        state = combat_with(game, ["BURNING_PACT"] * 7 + ["DEFEND_IRONCLAD"] * 3, seed="ap10")
        game.set_player(potions=["FIRE_POTION"])
        state = game.send({"cmd": "get_state"})
        state = game.act("play_card", card_index=hand_names(state).index("Burning Pact"))
        assert state["decision"] == "card_select"
        assert "use_potion" not in legal_names(state)
        err = game.act("use_potion", potion_index=0)
        assert err["type"] == "error" and "selection" in err["message"]
        state = game.act("select_cards", indices="0")
        assert state["decision"] == "combat_play"
        assert any(p["name"] == "Fire Potion" for p in state["player"]["potions"])


class TestQueries:
    def test_queries_do_not_advance(self, game):
        state = combat_with(game, ["STRIKE_IRONCLAD"] * 5 + ["BASH", "DEFEND_IRONCLAD"] * 2)
        again = game.send({"cmd": "get_state"})
        assert again["decision"] == "combat_play" and hand_names(again) == hand_names(state)
        deck = game.send({"cmd": "get_deck"})
        assert deck["type"] == "deck" and deck["deck_size"] == 9 and deck["cards"][0]["description"]
        assert game.send({"cmd": "get_relics"})["relics"][0]["description"]
        assert game.send({"cmd": "get_potions"})["type"] == "potions"
        piles = game.send({"cmd": "get_piles"})
        assert {"draw", "discard", "exhaust", "hand"} <= set(piles)
        draw_names = [c["name"] for c in piles["draw"]]
        assert draw_names == sorted(draw_names)  # order hidden
        card = game.send({"cmd": "get_card", "hand_index": 0})["card"]
        assert card["name"] == state["hand"][0]["name"]
        bash = game.send({"cmd": "get_card", "id": "BASH"})["card"]
        assert bash["after_upgrade"]["description"]
        assert game.send({"cmd": "help"})["type"] == "help"
        # Still the same decision.
        assert hand_names(game.send({"cmd": "get_state"})) == hand_names(state)

    def test_piles_out_of_combat(self, game):
        game.start(seed="ap9")
        assert game.send({"cmd": "get_piles"})["type"] == "error"


class TestEvents:
    def test_combat_events(self, game):
        state = combat_with(game, ["STRIKE_IRONCLAD"] * 10)
        assert any(e["type"] == "draw" for e in state["events"])
        state = game.act("play_card", card_index=0, target_index=0)
        types = [e["type"] for e in state["events"]]
        assert "card_played" in types and "damage" in types
        dmg = next(e for e in state["events"] if e["type"] == "damage")
        assert dmg["target"] == "Shrinker Beetle" and dmg["amount"] > 0
        state = game.act("end_turn")
        types = [e["type"] for e in state["events"]]
        assert "turn" in types and "enemy_move" in types
        # Events are since the previous decision only.
        state = game.send({"cmd": "get_state"})
        assert [e["type"] for e in state["events"]] == types

    def test_run_level_events(self, game):
        state = game.start(seed="ap10")
        state = game.act("choose_option", option_index=0)
        assert any(e["type"] in ("relic_obtained", "deck_added", "potion_obtained", "gold", "max_hp")
                   for e in state.get("events", []))

    def test_key_order(self, game):
        state = game.start(seed="ap11")
        assert list(state)[:3] == ["type", "decision", "legal_actions"]


class TestOmitDeck:
    def test_omit_deck(self, game):
        state = game.send({"cmd": "start_run", "character": "Ironclad", "seed": "ap12", "omit_deck": True})
        p = state["player"]
        assert "deck" not in p and p["deck_size"] == 10 and p["deck_version"]
        before = p["deck_version"]
        game.set_player(deck=["STRIKE_IRONCLAD"] * 11)
        assert game.send({"cmd": "get_state"})["player"]["deck_version"] != before
        assert game.send({"cmd": "get_deck"})["deck_size"] == 11
        # Default stays as before.
        state = game.start(seed="ap12")
        assert isinstance(state["player"]["deck"], list)


class TestModifiers:
    def test_character_cards_needs_character(self, game):
        err = game.send({"cmd": "start_run", "character": "Ironclad", "seed": "m1", "modifiers": ["CHARACTER_CARDS"]})
        assert err["type"] == "error" and "CHARACTER_CARDS:" in err["message"]
        ok = game.send({"cmd": "start_run", "character": "Ironclad", "seed": "m1",
                        "modifiers": ["CHARACTER_CARDS:Silent"]})
        assert ok["type"] == "decision"


class TestLocalizedPrompt:
    def test_zh_prompt(self, game):
        game.neow(game.start(seed="ap13", lang="zh"), 0)
        state = game.enter_room("rest_site")
        smith = next(o for o in state["options"] if o["option_id"] == "SMITH")
        state = game.act("choose_option", option_index=smith["index"])
        assert state["purpose"] == "upgrade"
        assert any("一" <= ch <= "鿿" for ch in state["prompt"])


# ── a plain engine (no STS2_DEBUG): what an agent gets ──

@pytest.fixture(scope="module")
def plain():
    env = {k: v for k, v in os.environ.items() if k not in ("STS2_DEBUG", "STS2_DEBUG_COMMANDS", "STS2_DEBUG_TRACES")}
    proc = subprocess.Popen(engine_command(DOTNET), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True, bufsize=1, env=env)

    def send(line):
        proc.stdin.write((line if isinstance(line, str) else json.dumps(line)) + "\n")
        proc.stdin.flush()
        while True:
            out = proc.stdout.readline()
            assert out, "engine exited"
            if out.startswith("{"):
                return json.loads(out)

    while not proc.stdout.readline().startswith('{"type":"ready"'):
        pass
    yield send
    proc.kill()


class TestPlainEngine:
    @pytest.mark.parametrize("line", ['[1,2]', '{}', '{"cmd":5}', 'nope', '{"cmd":"nope"}',
                                      '{"cmd":"start_run","ascension":99}', '{"cmd":"start_run","ascension":"3"}',
                                      '{"cmd":"action"}', '{"cmd":"action","action":"end_turn","args":[1]}'])
    def test_malformed_input_is_a_clean_error(self, plain, line):
        r = plain(line)
        assert r["type"] == "error" and r["message"]
        assert "stack_trace" not in r

    def test_debug_commands_disabled(self, plain):
        plain({"cmd": "start_run", "character": "Ironclad", "seed": "p1"})
        for cmd in ({"cmd": "set_player", "hp": 999}, {"cmd": "enter_room", "type": "shop"},
                    {"cmd": "set_draw_order", "cards": ["BASH"]}):
            r = plain(cmd)
            assert r["type"] == "error" and r["code"] == "debug_disabled"
        assert plain({"cmd": "get_state"})["player"]["hp"] == 80
