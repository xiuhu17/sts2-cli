"""The command watchdog turns a wedged engine into an error instead of a hang."""
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "python"))
from engine import engine_command  # noqa: E402


def _engine(timeout_s):
    env = dict(os.environ, STS2_DEBUG_COMMANDS="1", STS2_COMMAND_TIMEOUT=str(timeout_s))
    p = subprocess.Popen(engine_command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, text=True, bufsize=1, env=env)

    def send(cmd):
        p.stdin.write(json.dumps(cmd) + "\n")
        p.stdin.flush()
        while True:
            line = p.stdout.readline()
            if line.startswith("{"):
                return json.loads(line)
    assert json.loads(p.stdout.readline())["type"] == "ready"
    return p, send


def test_stuck_command_times_out_and_engine_refuses_more():
    p, send = _engine(2)
    try:
        t = time.time()
        r = send({"cmd": "__debug_sleep", "ms": 60000})
        assert r["code"] == "engine_timeout"
        assert time.time() - t < 10
        assert send({"cmd": "start_run", "character": "Ironclad", "seed": "w"})["code"] == "restart_required"
        assert send({"cmd": "quit"})["type"] == "quit_result"
        p.wait(timeout=10)
    finally:
        p.kill()


def test_normal_commands_unaffected():
    p, send = _engine(5)
    try:
        assert send({"cmd": "__debug_sleep", "ms": 100})["type"] == "ok"
        r = send({"cmd": "start_run", "character": "Ironclad", "seed": "w2"})
        assert r["type"] == "decision"
    finally:
        p.kill()
