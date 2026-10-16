"""The shell client (bin/sts2 + python/sts2_daemon.py): one Bash command per step."""

import json
import os
import subprocess
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STS2 = os.path.join(ROOT, "bin", "sts2")


@pytest.fixture(scope="module")
def sts2():
    env = {k: v for k, v in os.environ.items() if not k.startswith("STS2_DEBUG")}
    env["STS2_SESSION"] = f"pytest{os.getpid()}"
    env["STS2_IDLE_TIMEOUT"] = "120"

    def run(*args):
        p = subprocess.run([STS2, *args], capture_output=True, text=True, env=env, timeout=120)
        return p.returncode, p.stdout

    yield run
    run("stop")


def test_play_a_few_steps(sts2):
    rc, out = sts2("start", "--character", "Ironclad", "--seed", "cli1")
    assert rc == 0 and "Neow" in out and "[1] choose 0" in out
    rc, out = sts2("do", "1")
    assert rc == 0 and out.startswith("ok: choose 0")
    assert "Since last decision:" in out
    # The event's last page (as in the game): a single Proceed.
    assert "Proceed" in out
    rc, out = sts2("do", "1")
    assert rc == 0 and out.startswith("ok: choose 0")
    rc, out = sts2("do", "go 0")
    assert rc == 0 and "Hand:" in out and "Enemies:" in out and "end turn" in out
    rc, js = sts2("--json", "state")
    st = json.loads(js)
    assert st["decision"] == "combat_play" and "deck" not in st["player"]
    # A batch: plays by name, stops at the first error.
    rc, out = sts2("do", "play Defend; play Defend; play Nonexistent; end")
    assert rc == 1 and "ok: play" in out and "no card matching 'Nonexistent'" in out
    rc, out = sts2("do", "end")
    assert rc == 0 and "enemy turn" in out


def test_errors_list_legal_actions(sts2):
    rc, out = sts2("do", "leave")
    assert rc == 1 and "not allowed" in out and "Legal:" in out
    rc, out = sts2("do", "99")
    assert rc == 1 and "no action number 99" in out


def test_batch_stops_when_the_decision_changes(sts2):
    rc, js = sts2("--json", "state")
    st = json.loads(js)
    assert st["decision"] == "combat_play"
    # 'choose' is not a combat verb: the batch stops before it.
    rc, out = sts2("do", "end; choose 0")
    assert "stopped before 'choose 0'" in out or "error in 'choose 0'" in out


def test_queries_render(sts2):
    for args in (["deck"], ["deck", "--upgrades"], ["relics"], ["potions"], ["piles"], ["piles", "draw"],
                 ["map"], ["card", "0"], ["card", "BASH", "--upgraded"], ["log", "5"], ["help"], ["status"]):
        rc, out = sts2(*args)
        assert rc == 0, (args, out)
        assert "Traceback" not in out and len(out.encode()) < 8192, args
    rc, out = sts2("--json", "deck")
    assert json.loads(out)["type"] == "deck"


def test_engine_crash_is_recovered(sts2):
    _, before = sts2("--json", "state")
    pid = subprocess.run(["pgrep", "-f", f"sts2_daemon.py pytest{os.getpid()}"], capture_output=True, text=True).stdout.split()
    assert pid
    engines = subprocess.run(["pgrep", "-P", pid[0]], capture_output=True, text=True).stdout.split()
    for e in engines:
        os.kill(int(e), 9)
    time.sleep(0.2)
    rc, after = sts2("--json", "state")
    a, b = json.loads(before), json.loads(after)
    assert a["decision"] == b["decision"]
    assert [c["name"] for c in a.get("hand", [])] == [c["name"] for c in b.get("hand", [])]


def test_zh(sts2):
    rc, out = sts2("start", "--character", "Silent", "--seed", "cli2", "--lang", "zh")
    assert rc == 0 and "可选操作" in out and "涅奥" in out


def test_latency(sts2):
    sts2("log", "1")
    t0 = time.time()
    for _ in range(10):
        sts2("log", "1")
    per_call = (time.time() - t0) / 10
    assert per_call < 0.2, per_call


def test_concurrent_first_calls_start_one_daemon():
    """Two clients that both find no daemon spawn one each; only one may serve (the other
    used to unlink its socket and leave an orphaned daemon + engine running)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("STS2_DEBUG")}
    session = f"pytestrace{os.getpid()}"
    env["STS2_SESSION"] = session
    env["STS2_IDLE_TIMEOUT"] = "60"
    procs = [subprocess.Popen([STS2, "status"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=env)
             for _ in range(3)]
    try:
        outs = [p.communicate(timeout=60)[0] for p in procs]
        assert all("daemon pid" in o for o in outs), outs
        assert len({o.split()[2] for o in outs}) == 1, outs
        time.sleep(1.0)  # losers exit once the winner's socket answers
        pids = subprocess.run(["pgrep", "-f", f"sts2_daemon.py {session}"], capture_output=True, text=True).stdout.split()
        assert len(pids) == 1, pids
    finally:
        subprocess.run([STS2, "stop"], env=env, capture_output=True, timeout=60)


def test_render_softlock_popup_and_dialogue_pool():
    import sys
    sys.path.insert(0, os.path.join(ROOT, "python"))
    import sts2_daemon
    soft = {"type": "decision", "decision": "event_choice", "event_name": "Stone of All Time",
            "options": [{"index": 0, "title": "Locked", "is_locked": True}], "legal_actions": [],
            "softlocked": True, "softlock_reason": "every option of this event is locked"}
    out = sts2_daemon.render_decision(soft, [])
    assert "softlock (every option of this event is locked)" in out and "softlocked:" not in out
    with pytest.raises(sts2_daemon.VerbError, match="softlock"):
        sts2_daemon.parse_verb("1", soft, [])
    pop = {"type": "decision", "decision": "event_choice", "event_name": "Neow",
           "dialogue": [{"speaker": "ancient", "text": "first", "key": "k1"}],
           "dialogue_pool": [{"id": "a", "lines": [{"text": "first", "key": "k1"}]},
                             {"id": "b", "lines": [{"text": "second", "key": "k2"}]}],
           "dialogue_random": True, "popup": {"header": "Are you sure?", "body": "A loss."},
           "options": [{"index": 0, "title": "Yes"}],
           "legal_actions": [{"action": "choose_option", "args": {"option_index": 0}}]}
    out = sts2_daemon.render_decision(pop, list(enumerate(pop["legal_actions"], 1)))
    assert "one of 2 dialogues at random" in out and "- second" in out
    assert "[Are you sure?] A loss." in out and "dialogue_pool:" not in out
