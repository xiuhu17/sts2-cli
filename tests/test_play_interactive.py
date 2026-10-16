"""python/play.py interactive mode: prompts are built from the engine's legal_actions.

A scripted "human" answers every prompt with a random entry of the options the prompt
accepts. Nothing the client offers may be rejected by the engine, the client must never send
an action the player did not choose, and the rewards / treasure screens must be prompts.
"""

from __future__ import annotations

import importlib.util
import pathlib
import random
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
spec = importlib.util.spec_from_file_location("play_module_interactive", ROOT / "python" / "play.py")
play = importlib.util.module_from_spec(spec)
spec.loader.exec_module(play)

META = {"help", "map", "deck", "potions", "relics", "piles", "quit", "abandon", "save", "saves"}


class ScriptedHuman:
    def __init__(self, seed, limit):
        self.rng = random.Random(seed)
        self.limit = limit
        self.prompts = []      # (decision, sorted options)
        self.chosen = []       # answers, in order

    def __call__(self, prompt, valid_options=None, state=None, multi_select=False, multi_min=1, multi_max=1):
        if len(self.chosen) >= self.limit:
            raise play._QuitRequested()
        opts = sorted(valid_options or [])
        dec = (state or {}).get("decision")
        self.prompts.append((dec, opts))
        assert opts, f"a prompt with no options at {dec}: {prompt}"
        assert not (set(opts) & META), opts
        if multi_select and multi_max > 1:
            singles = [o for o in opts if o != "s" and o.isdigit()]
            k = self.rng.randint(max(multi_min, 1), min(multi_max, len(singles)))
            ans = ",".join(self.rng.sample(singles, k))
        else:
            # Prefer progress over potion discards so the run goes somewhere.
            pool = [o for o in opts if not o.startswith("x")] or opts
            # Mostly play cards rather than ending the turn at once.
            if "e" in pool and len(pool) > 1 and self.rng.random() < 0.85:
                pool = [o for o in pool if o != "e" and not o.startswith("p")] or pool
            ans = self.rng.choice(pool)
        self.chosen.append(ans)
        return ans


@pytest.mark.parametrize("seed,character", [("ia1", "Ironclad"), ("ia2", "Silent"), ("ia3", "Necrobinder"), ("ia4", "Defect"), ("ia5", "Regent")])
def test_every_offered_choice_is_legal(monkeypatch, capsys, seed, character):
    human = ScriptedHuman(seed, limit=400)
    monkeypatch.setattr(play, "get_input", human)
    monkeypatch.setattr(play, "LANG", "en")
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")  # quit-save prompt: don't save
    sent, errors = [], []
    real_logger = play.GameLogger

    class SpyLogger(real_logger):
        def log_action(self, cmd):
            sent.append(cmd)

        def log_state(self, resp):
            if resp.get("type") == "error":
                errors.append((sent[-1] if sent else None, resp.get("message")))

    monkeypatch.setattr(play, "GameLogger", SpyLogger)
    play.play(character=character, seed=seed, log=False)
    out = capsys.readouterr().out
    assert "Traceback" not in out
    assert not errors, errors[:3]
    actions = [c for c in sent if c.get("cmd") == "action"]
    # One action per answer (potion use may ask for a target too), never more: nothing is
    # sent on the player's behalf.
    assert len(actions) <= len(human.chosen)
    decisions = {d for d, _ in human.prompts}
    assert {"event_choice", "map_select", "combat_play"} <= decisions, decisions
    print(sorted(decisions), len(human.chosen))


def test_rewards_and_treasure_are_prompts(monkeypatch):
    """The rewards screen offers each claimable reward plus proceed; nothing is auto-claimed."""
    st = {"type": "decision", "decision": "rewards", "player": {"hp": 1, "max_hp": 1},
          "rewards": [{"index": 0, "type": "gold", "amount": 10}, {"index": 1, "type": "potion", "name": "Fire Potion"}],
          "potion_slots_full": True,
          "legal_actions": [{"action": "claim_reward", "args": {"reward_index": 0}}, {"action": "proceed"},
                            {"action": "discard_potion", "args": {"potion_index": 0}}]}
    keys = play.potion_keys(st, "u")
    assert keys == {"x0": ("discard", 0)}
    assert play._arg_values(st, "claim_reward", "reward_index") == [0]
    tr = {"type": "decision", "decision": "treasure_relic", "relics": [{"index": 0, "name": "Anchor"}],
          "legal_actions": [{"action": "select_treasure_relic", "args": {"relic_index": 0}},
                            {"action": "skip_treasure_relic"}]}
    assert play._legal_of(tr, "skip_treasure_relic")


def test_out_of_combat_potion_use_is_offered():
    st = {"decision": "map_select", "legal_actions": [
        {"action": "select_map_node", "args": {"col": 1, "row": 1}},
        {"action": "use_potion", "args": {"potion_index": 1}},
        {"action": "discard_potion", "args": {"potion_index": 1}}]}
    assert play.potion_keys(st, "u") == {"u1": ("use", 1), "x1": ("discard", 1)}
    assert play.potion_command(st, "use", 1) == {"cmd": "action", "action": "use_potion", "args": {"potion_index": 1}}


def test_unknown_decision_lists_legal_actions(monkeypatch, capsys):
    st = {"type": "decision", "decision": "brand_new_screen", "foo": "bar",
          "legal_actions": [{"action": "proceed"}, {"action": "do_thing", "args": {"x": 1}}]}
    monkeypatch.setattr(play, "LANG", "en")
    play.show_generic(st)
    out = capsys.readouterr().out
    assert "foo: bar" in out and "[a1] do_thing x=1" in out
    monkeypatch.setattr(play, "get_input", lambda *a, **k: "a1")
    assert play.generic_choice(st) == {"cmd": "action", "action": "do_thing", "args": {"x": 1}}
