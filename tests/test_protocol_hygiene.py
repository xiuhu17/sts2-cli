"""Protocol hygiene: required start_run fields, blank seeds, null args, field types, one error
shape for every failure, no events leaking from an abandoned run, bridge --compact."""

import importlib.util
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def to_combat(game, seed, deck=None, **neow):
    """Neow option 0 (its follow-ups answered by the caller via **neow), then a debug fight."""
    game.neow(game.start(seed=seed), 0, **neow)
    game.set_player(hp=80, max_hp=80, deck=deck or ["STRIKE_IRONCLAD"] * 5 + ["DEFEND_IRONCLAD"] * 5)
    state = game.enter_room("combat", encounter="SHRINKER_BEETLE_WEAK")
    assert state["decision"] == "combat_play"
    return state


def assert_full_error(r, code):
    """Every error: a code, and (a run is waiting) the decision with its legal_actions."""
    assert r["type"] == "error", r
    assert r.get("code") == code, r
    assert r.get("decision"), r
    assert isinstance(r.get("legal_actions"), list) and r["legal_actions"], r


class TestStartRunFields:
    @pytest.mark.parametrize("extra", [{}, {"character": ""}, {"character": "  "}, {"character": None}])
    def test_character_is_required(self, game, extra):
        r = game.send({"cmd": "start_run", "seed": "nochar", **extra})
        assert r["type"] == "error" and r["code"] == "bad_request", r
        assert "start_run requires character" in r["message"]
        assert "Ironclad|Silent|Defect|Regent|Necrobinder" in r["message"]

    def test_ascension_defaults_to_zero(self, game):
        r = game.send({"cmd": "start_run", "character": "Silent", "seed": "asc0"})
        assert r["type"] == "decision" and r["context"]["ascension"] == 0

    @pytest.mark.parametrize("seed", ["", "   "])
    def test_blank_seed_is_random(self, game, seed):
        a = game.send({"cmd": "start_run", "character": "Ironclad", "seed": seed})
        b = game.send({"cmd": "start_run", "character": "Ironclad", "seed": seed})
        sa, sb = a["context"]["seed"], b["context"]["seed"]
        assert sa.strip() and sb.strip() and sa != sb, (sa, sb)

    def test_unknown_character_code(self, game):
        game.start(seed="uc1")
        r = game.send({"cmd": "start_run", "character": "Bogus", "seed": "x"})
        assert_full_error(r, "unknown_character")
        assert r["decision"] == "event_choice"  # the current run is untouched

    @pytest.mark.parametrize("cmd,field", [
        ({"cmd": "start_run", "character": "Ironclad", "game_mode": 5}, "game_mode"),
        ({"cmd": "start_run", "character": "Ironclad", "modifiers": [1]}, "modifiers"),
        ({"cmd": "start_run", "character": "Ironclad", "modifiers": "DRAFT"}, "modifiers"),
        ({"cmd": "start_run", "character": "Ironclad", "profile": 5}, "profile"),
        ({"cmd": "start_run", "character": 5}, "character"),
        ({"cmd": "start_run", "character": "Ironclad", "seed": 7}, "seed"),
        ({"cmd": "start_run", "character": "Ironclad", "lang": "fr"}, "lang"),
        ({"cmd": "start_run", "character": "Ironclad", "manual_rewards": "yes"}, "manual_rewards"),
        ({"cmd": "start_run", "character": "Ironclad", "omit_deck": 1}, "omit_deck"),
        ({"cmd": "load_save", "path": 7}, "path"),
        ({"cmd": "load_save", "json": {}}, "json"),
        ({"cmd": "load_save", "json": "{}", "lang": "fr"}, "lang"),
        ({"cmd": "write_continue_save", "path": 5}, "path"),
        ({"cmd": "get_deck", "upgrades": "yes"}, "upgrades"),
    ])
    def test_wrong_field_type_is_bad_request(self, game, cmd, field):
        game.start(seed="ft1")
        r = game.send(cmd)
        assert_full_error(r, "bad_request")
        assert f"'{field}'" in r["message"], r["message"]
        assert "stack_trace" not in r and "InvalidOperation" not in r["message"]

    def test_save_io_errors(self, game, tmp_path):
        game.start(seed="io1")
        r = game.send({"cmd": "write_continue_save", "path": "/dev/null/x"})
        assert_full_error(r, "io_error")
        r = game.send({"cmd": "load_save", "path": str(tmp_path / "missing.save")})
        assert_full_error(r, "io_error")
        assert "not found" in r["message"]
        # Still the same run.
        assert game.send({"cmd": "get_state"})["decision"] == "event_choice"


class TestArgs:
    def test_null_args_are_absent(self, game):
        state = to_combat(game, "na1")
        r = game.act("end_turn", target_index=None)
        assert r["type"] == "decision", r
        state = game.neow(game.start(seed="na2"), 0)
        assert state["decision"] == "map_select"
        ch = state["choices"][0]
        r = game.act("select_map_node", col=ch["col"], row=ch["row"], target_index=None)
        assert r["type"] == "decision", r

    def test_null_required_arg_is_named(self, game):
        game.start(seed="na4")
        for r in (game.act("choose_option", option_index=None), game.act("choose_option")):
            assert_full_error(r, "illegal_action")
            assert "choose_option needs option_index" in r["message"], r["message"]

    def test_argument_the_verb_does_not_take_is_illegal(self, game):
        to_combat(game, "na3")
        r = game.act("end_turn", x="a")
        assert_full_error(r, "illegal_action")
        assert "'x' is not an argument of end_turn" in r["message"]
        r = game.act("end_turn", foo=1)
        assert_full_error(r, "illegal_action")


class TestErrorShape:
    def test_every_error_has_code_decision_and_legal_actions(self, game):
        state = to_combat(game, "es1", rewards="proceed")
        assert_full_error(game.act("fly"), "unknown_action")
        assert_full_error(game.act("play_card", card_index="abc"), "bad_request")
        assert_full_error(game.act("play_card", card_index=99), "illegal_action")
        game.proc.stdin.write("{not json\n")
        game.proc.stdin.flush()
        assert_full_error(game._read(), "bad_request")
        assert_full_error(game.send({"cmd": "get_card"}), "bad_request")
        assert state["decision"] == "combat_play"

    def test_event_option_type_error(self, game):
        game.start(seed="es2")
        assert_full_error(game.act("choose_option", option_index="abc"), "bad_request")

    def test_select_cards_errors_have_codes(self, game):
        # Single pick: each card is its own legal entry; anything else is not one of them.
        state = to_combat(game, "es3", ["BURNING_PACT"] * 7 + ["DEFEND_IRONCLAD"] * 3, cards=[0])
        state = game.act("play_card", card_index=[c["name"] for c in state["hand"]].index("Burning Pact"))
        assert state["decision"] == "card_select" and state["max_select"] == 1
        assert_full_error(game.act("select_cards", indices="x"), "illegal_action")
        # Multi-pick (Sealed Deck): the handler checks the indices.
        r = game.send({"cmd": "start_run", "character": "Ironclad", "seed": "es4", "modifiers": ["SEALED_DECK"]})
        r = game.act("choose_option", option_index=0)
        assert r["decision"] == "card_select" and r["min_select"] > 1
        assert_full_error(game.act("select_cards", indices="x"), "bad_request")
        assert_full_error(game.act("select_cards"), "bad_request")
        assert_full_error(game.act("select_cards", indices="0"), "illegal_action")
        assert_full_error(game.act("select_cards", indices="999"), "illegal_action")

    def test_no_run_errors(self):
        from conftest import Game
        g = Game()
        try:
            r = g.send({"cmd": "get_deck"})
            assert r["type"] == "error" and r["code"] == "no_run", r
            r = g.send({"cmd": "action", "action": "end_turn"})
            assert r["type"] == "error" and r["code"] == "no_run", r
            g.proc.stdin.write("{\n")
            g.proc.stdin.flush()
            r = g._read()
            assert r["type"] == "error" and r["code"] == "bad_request", r
        finally:
            g.close()


class TestRunTeardown:
    def test_abandoned_combat_selection_leaves_no_events(self, game):
        """Start a new run while a combat card_select is open (the abandoned flow unwinds
        during start_run): the new run's first decision must not report its events."""
        state = to_combat(game, "ab1", ["ARMAMENTS"] * 6 + ["STRIKE_IRONCLAD"] * 4)
        state = game.act("play_card", card_index=[c["name"] for c in state["hand"]].index("Armaments"))
        assert state["decision"] == "card_select"
        r = game.send({"cmd": "start_run", "character": "Defect", "seed": "ab2"})
        assert r["decision"] == "event_choice"
        assert not r.get("events"), r.get("events")
        fresh = game.send({"cmd": "start_run", "character": "Defect", "seed": "ab2"})
        assert fresh.get("events") == r.get("events")


def _load_bridge():
    argv = sys.argv
    sys.argv = ["sts2_bridge.py"]
    try:
        spec = importlib.util.spec_from_file_location("sts2_bridge_hygiene", os.path.join(ROOT, "agent", "sts2_bridge.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.argv = argv


class TestBridgeCompact:
    def test_enchant_select_keeps_structured_enchantment(self, game):
        bridge = _load_bridge()
        game.start(seed="shb1")
        state = game.enter_room("Event", event="SELF_HELP_BOOK")
        state = game.act("choose_option", option_index=0)
        assert state["decision"] == "card_select" and state["purpose"] == "enchant"
        c = bridge.compact_json(state)
        assert c["enchantment"] == {k: v for k, v in state["enchantment"].items() if k != "id"}
        assert c["enchantment"]["name"] and c["enchantment"]["description"]
        assert c.get("name") is None  # nothing folded into a bogus top-level name
        card = c["cards"][0]
        assert card["after_upgrade"] == state["cards"][0]["after_upgrade"]
        assert card["after_enchant"]["description"] == state["cards"][0]["after_enchant"]["description"]

    def test_card_enchantment_is_folded_into_its_name(self):
        bridge = _load_bridge()
        c = bridge.compact_json({"type": "decision", "hand": [
            {"name": "Strike", "enchantment": "Sharp", "enchantment_amount": 2, "description": "Deal 8 damage."}]})
        assert c["hand"][0]["name"] == "Strike [Sharp 2]" and "enchantment" not in c["hand"][0]


class TestInputLimits:
    """Every line gets one reply; client input is never echoed back unbounded."""

    def _raw(self, game, line):
        game.proc.stdin.write(line + "\n")
        game.proc.stdin.flush()
        return game._read()

    def test_blank_lines_get_bad_request(self, game):
        game.start(seed="il1")
        for line in ("", "   ", "\t "):
            r = self._raw(game, line)
            assert_full_error(r, "bad_request")
            assert "Empty line" in r["message"]
        # Still in lockstep: the next reply answers the next command.
        assert game.send({"cmd": "get_state"})["type"] == "decision"

    def test_long_unknown_names_are_truncated(self, game):
        import time
        game.start(seed="il2")
        t0 = time.time()
        r = game.send({"cmd": "action", "action": "x" * 200000})
        assert_full_error(r, "unknown_action")
        assert len(r["message"]) < 2000 and "(200000 chars)" in r["message"]
        assert time.time() - t0 < 0.5
        r = game.send({"cmd": "y" * 1000000})
        assert r["code"] == "unknown_command" and len(r["message"]) < 2000
        r = game.send({"cmd": "start_run", "character": "z" * 100000})
        assert r["code"] == "unknown_character" and len(r["message"]) < 2000
        # Near-misses still get a suggestion.
        assert "Did you mean 'end_turn'" in game.act("end_trun")["message"]

    def test_oversized_seed_is_refused_and_keeps_the_run(self, game):
        before = game.start(seed="il3")
        r = game.send({"cmd": "start_run", "character": "Ironclad", "seed": "S" * 65})
        assert_full_error(r, "bad_request")
        assert "at most 64" in r["message"]
        assert game.send({"cmd": "get_state"})["context"]["seed"] == before["context"]["seed"]
        assert game.start(seed="S" * 64)["type"] == "decision"


class TestFailedLoadKeepsRun:
    """load_save checks that the game can build a run from the save before ending the current
    one: a save it can't load (unknown character id, no players) leaves the run as it was."""

    @pytest.mark.parametrize("corrupt", ["unknown_character", "no_players"])
    def test_unloadable_save_keeps_the_current_run(self, game, tmp_path, corrupt):
        import json
        state = game.start(character="Defect", seed="sv1")
        path = tmp_path / "save.json"
        assert game.send({"cmd": "write_continue_save", "path": str(path)})["success"] is True
        text = path.read_text()
        if corrupt == "unknown_character":
            text = text.replace("CHARACTER.DEFECT", "CHARACTER.NOBODY")
        else:
            d = json.loads(text)
            d.pop("players")
            text = json.dumps(d)
        r = game.send({"cmd": "load_save", "json": text})
        assert_full_error(r, "bad_request")
        assert "not loadable" in r["message"]
        after = game.send({"cmd": "get_state"})
        assert after["type"] == "decision" and after["decision"] == state["decision"]
        assert after["context"]["seed"] == state["context"]["seed"]
        # The intact save still loads.
        assert game.send({"cmd": "load_save", "path": str(path)})["type"] == "decision"
