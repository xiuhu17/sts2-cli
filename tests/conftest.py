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

    # --- Auto-play helpers ---

    def select_first(self, state):
        """Resolve a card_select with the first min_select cards (at least one)."""
        k = min(max(state.get("min_select", 1), 1), len(state.get("cards", [])))
        return self.act("select_cards", indices=",".join(str(i) for i in range(k)))

    def auto_combat(self, state):
        """Play one card or end turn."""
        hand = state.get("hand", [])
        energy = state.get("energy", 0)
        playable = [c for c in hand if c.get("can_play") and c.get("cost", 99) <= energy]
        if playable:
            card = playable[0]
            args = {"card_index": card["index"]}
            if card.get("target_type") == "AnyEnemy":
                enemies = state.get("enemies", [])
                if enemies:
                    args["target_index"] = enemies[0]["index"]
            return self.act("play_card", **args)
        return self.act("end_turn")

    def auto_play_combat(self, state, max_steps=300):
        """Auto-play combat until it ends."""
        for _ in range(max_steps):
            if state.get("decision") != "combat_play":
                return state
            state = self.auto_combat(state)
        raise RuntimeError("Combat did not end")

    def skip_neow(self, state):
        """Skip the Neow event and all follow-up rewards until map_select."""
        for _ in range(20):
            dec = state.get("decision", "")
            if dec == "map_select":
                return state
            if dec == "event_choice":
                opts = [o for o in state["options"] if not o.get("is_locked")]
                state = self.act("choose_option", option_index=opts[0]["index"])
            elif dec == "card_reward":
                state = self.act("skip_card_reward")
            elif dec == "crystal_sphere":
                x, y = state["hidden_cells"][0]
                state = self.act("crystal_sphere_click", x=x, y=y, tool="big")
            elif dec == "bundle_select":
                state = self.act("select_bundle", bundle_index=0)
            elif dec == "card_select":
                if state.get("min_select", 0) == 0:
                    state = self.act("skip_select")
                else:
                    state = self.select_first(state)
            else:
                state = self.act("proceed")
        return state


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
