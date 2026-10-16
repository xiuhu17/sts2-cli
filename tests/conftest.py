"""Pytest fixtures: Game process wrapper for unit tests."""

import json
import os
import subprocess
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "python"))
from engine import PROJECT, engine_command, find_dotnet  # noqa: E402

DOTNET = find_dotnet() or "dotnet"


class Game:
    """Wraps the headless C# process for testing."""

    def __init__(self):
        env = os.environ.copy()
        # Match the interactive launcher's default: resolve dependencies from lib,
        # never silently borrow missing assemblies from the Steam installation.
        env["STS2_GAME_DIR"] = os.path.join(os.path.dirname(os.path.dirname(PROJECT)), "..", "lib")
        # The suite sets up scenarios with the debug commands (set_player, enter_room, ...).
        env["STS2_DEBUG"] = "1"
        # stderr is discarded: an undrained PIPE fills up and blocks the engine.
        self.proc = subprocess.Popen(
            engine_command(DOTNET),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, bufsize=1, env=env,
        )
        ready = self._read()
        assert ready.get("type") == "ready", f"Expected ready, got: {ready}"

    def _read(self):
        while True:
            line = self.proc.stdout.readline().strip()
            if not line:
                raise RuntimeError("EOF from game process")
            if line.startswith("{"):
                msg = json.loads(line)
                if msg.get("type") == "fatal":
                    raise RuntimeError(f"Engine crashed: {msg.get('message')}\n{msg.get('stack_trace')}")
                return msg

    def send(self, cmd):
        self.proc.stdin.write(json.dumps(cmd) + "\n")
        self.proc.stdin.flush()
        return self._read()

    def start(self, character="Ironclad", seed="test", ascension=0, lang="en"):
        return self.send({"cmd": "start_run", "character": character,
                          "seed": seed, "ascension": ascension, "lang": lang})

    def act(self, action, **args):
        cmd = {"cmd": "action", "action": action}
        if args:
            cmd["args"] = args
        return self.send(cmd)

    def get_map(self):
        return self.send({"cmd": "get_map"})

    def set_player(self, **kwargs):
        cmd = {"cmd": "set_player", **kwargs}
        return self.send(cmd)

    def enter_room(self, room_type, **kwargs):
        cmd = {"cmd": "enter_room", "type": room_type, **kwargs}
        return self.send(cmd)

    def set_draw_order(self, cards):
        return self.send({"cmd": "set_draw_order", "cards": cards})

    def close(self):
        try:
            self.proc.stdin.write('{"cmd":"quit"}\n')
            self.proc.stdin.flush()
        except Exception:
            pass
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()

    # --- Test helpers: every choice is the test's (or a named, seeded policy's) ---

    @staticmethod
    def _legal(state, action):
        return [a for a in state.get("legal_actions") or [] if a.get("action") == action]

    def select(self, state, indices):
        """Resolve a card_select with the cards the test names (a list of indices or one index)."""
        assert state.get("decision") == "card_select", f"not a card_select: {state.get('decision')}"
        if isinstance(indices, int):
            indices = [indices]
        offered = {c["index"] for c in state.get("cards", [])}
        missing = [i for i in indices if i not in offered]
        assert not missing, f"cards {missing} not offered (have {sorted(offered)})"
        return self.act("select_cards", indices=",".join(str(i) for i in indices))

    def choose(self, state, option_index):
        """Choose the event / rest-site option the test names; it must be a legal choice."""
        legal = [a["args"]["option_index"] for a in self._legal(state, "choose_option")]
        assert option_index in legal, f"option {option_index} is not legal here (legal: {legal})"
        return self.act("choose_option", option_index=option_index)

    def policy(self, seed="tests", name="heuristic"):
        """A named seeded policy from python/policy.py (its rules are stated there)."""
        from policy import make_policy
        return make_policy(name, seed)

    def step(self, state, policy):
        """One action chosen by `policy` (see python/policy.py) among the legal actions."""
        return self.send({"cmd": "action", **policy.choose(state)})

    def fight(self, state, policy=None, max_steps=300):
        """Play the combat out with a seeded policy (default: HeuristicPolicy('tests'): play a
        random playable card, aim at the lowest-HP enemy, else end the turn; prompts opened
        mid-combat are answered by the same policy). Returns the first decision after combat."""
        policy = policy or self.policy()
        for _ in range(max_steps):
            in_combat = state.get("decision") == "combat_play" or (
                state.get("decision") == "card_select" and state.get("combat"))
            if not in_combat:
                return state
            state = self.step(state, policy)
        raise RuntimeError("Combat did not end")

    def open_card_reward(self, state):
        """From a post-combat 'rewards' screen, claim its card reward (opens 'card_reward').

        Rewards are always a decision: nothing is collected for the player. Returns the state
        unchanged when it isn't a rewards screen or has no card reward."""
        if state.get("decision") != "rewards":
            return state
        card = next((r for r in state["rewards"] if r["type"] == "card"), None)
        if card is None:
            return state
        return self.act("claim_reward", reward_index=card["index"])

    def neow(self, state, option, card_reward=None, bundle=None, cards=None, cards_if_required=None,
             rewards=None):
        """Resolve Neow with the option the test names, then its follow-ups, each answered by the
        test: card_reward ("skip" or a card index), bundle (a bundle index), cards (card_select
        indices, or "skip" for an optional selection; cards_if_required answers a selection that
        can't be skipped), rewards ("proceed" to leave a rewards screen). A follow-up the test
        didn't answer fails the test instead of being picked for it. The outcome page's Proceed
        (its only button) is pressed. Returns the map_select decision."""
        assert state.get("decision") == "event_choice" and state.get("event_id") == "NEOW", \
            f"not Neow: {state.get('decision')} {state.get('event_id')}"
        state = self.choose(state, option)
        for _ in range(20):
            dec = state.get("decision", "")
            if dec == "map_select":
                return state
            if dec == "event_choice" and state.get("finished"):
                proceed = next(o for o in state["options"] if o.get("is_proceed"))
                state = self.act("choose_option", option_index=proceed["index"])
            elif dec == "card_reward" and card_reward is not None:
                state = (self.act("skip_card_reward") if card_reward == "skip"
                         else self.act("select_card_reward", card_index=card_reward))
            elif dec == "bundle_select" and bundle is not None:
                state = self.act("select_bundle", bundle_index=bundle)
            elif dec == "card_select" and (cards is not None or cards_if_required is not None):
                can_skip = bool(self._legal(state, "skip_select"))
                pick = cards if (cards != "skip" or can_skip) else cards_if_required
                if cards_if_required is not None and not can_skip:
                    pick = cards_if_required
                assert pick is not None, f"this card_select can't be skipped: {state.get('min_select')}"
                state = self.act("skip_select") if pick == "skip" else self.select(state, pick)
            elif dec == "rewards" and rewards == "proceed":
                state = self.act("proceed")
            else:
                raise AssertionError(f"Neow follow-up {dec!r} needs an explicit choice from the test: "
                                     f"{json.dumps(state.get('legal_actions'))[:300]}")
        raise AssertionError("Neow did not reach the map")


_shared = None


@pytest.fixture
def game():
    """A game process for the test. One process is reused across tests (start_run resets the
    run, and engine start-up costs ~1s); it is replaced if a test leaves it unusable.
    Set STS2_TEST_FRESH=1 for a new process per test."""
    global _shared
    if os.environ.get("STS2_TEST_FRESH") == "1":
        g = Game()
        yield g
        g.close()
        return
    if _shared is None or _shared.proc.poll() is not None:
        _shared = Game()
    yield _shared
    try:
        # A test that broke the engine (crash, timeout, restart_required) gets a fresh one next.
        r = _shared.send({"cmd": "get_map"})
        if r.get("code") in ("engine_timeout", "restart_required"):
            raise RuntimeError(r.get("message"))
    except Exception:
        _shared.close()
        _shared = None


def pytest_sessionfinish(session, exitstatus):
    if _shared is not None:
        _shared.close()
