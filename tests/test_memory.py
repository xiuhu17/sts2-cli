"""One engine process must stay bounded over many runs (RL / batch use)."""
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "python"))
from engine import engine_command  # noqa: E402


def _engine():
    env = dict(os.environ, STS2_DEBUG_COMMANDS="1")
    p = subprocess.Popen(engine_command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, text=True, bufsize=1, env=env)

    def send(cmd):
        p.stdin.write(json.dumps(cmd) + "\n")
        p.stdin.flush()
        return json.loads(p.stdout.readline())
    assert json.loads(p.stdout.readline())["type"] == "ready"
    return p, send


def _to_first_combat(send, seed):
    """start_run, then take the first legal action (map node first) until a combat starts."""
    r = send({"cmd": "start_run", "character": "Ironclad", "seed": seed})
    for _ in range(60):
        if r.get("decision") == "combat_play":
            return r
        acts = r.get("legal_actions") or []
        act = next((a for a in acts if a["action"] == "select_map_node"), acts[0] if acts else None)
        assert act, r
        args = act.get("args") or {}
        if "choose" in act:
            args = {"indices": ",".join(str(i) for i in act["choose"]["from"][:max(1, act["choose"]["min"])])}
        if "choose_cell" in act:
            x, y = act["choose_cell"]["cells"][0]
            args = {"x": x, "y": y, "tool": act["choose_cell"]["tools"][0]}
        r = send({"cmd": "action", "action": act["action"], "args": args})
    raise AssertionError(f"no combat reached: {r.get('decision')}")


def test_abandoning_runs_mid_combat_does_not_accumulate():
    p, send = _engine()
    try:
        stats = []
        for i in range(8):
            _to_first_combat(send, f"leak{i}")
            stats.append(send({"cmd": "__debug_stats"}))
        first, last = stats[1], stats[-1]
        # Each live combat holds one set of pile subscriptions and one CombatEnded handler;
        # abandoned combats must not add more.
        assert last["combat_ended_handlers"] == first["combat_ended_handlers"], stats
        assert last["net_card_db_subscriptions"] == first["net_card_db_subscriptions"], stats
        # One net service per run would otherwise stay in NetLoadingHandle's static dict.
        assert last["net_loading_handles"] <= 2, stats
        # The mock save store must not keep a log of every progress.save written.
        assert last["save_store_calls"] < 50, stats
    finally:
        p.kill()
