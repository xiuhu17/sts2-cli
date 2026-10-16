"""Clients never choose for the player: bot policies, the sts2 text client and the bridge.

- python/policy.py picks only legal_actions entries, reproducibly from a seed.
- bin/sts2 (sts2_daemon): seedless runs are recovered on the same seed, batches stop when the
  decision changes, multi-word card names, the 8 KB budget never cuts the legal actions, and
  the text shows what a human sees (dialogue, score / cause of death, used removal, intents,
  the Crystal Sphere rules).
- agent/sts2_bridge.py: --compact keeps rules text; crash recovery keeps the seed.
"""

import json
import os
import random
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "python"))
sys.path.insert(0, os.path.join(ROOT, "agent"))
import policy  # noqa: E402
import sts2_daemon as d  # noqa: E402

STS2 = os.path.join(ROOT, "bin", "sts2")


# ───────────────────────────── policies ─────────────────────────────

def _combat_state():
    return {"type": "decision", "decision": "combat_play",
            "player": {"hp": 50, "max_hp": 80},
            "enemies": [{"index": 0, "hp": 30}, {"index": 1, "hp": 5}],
            "hand": [{"index": 0, "name": "Strike", "target_type": "AnyEnemy"}, {"index": 1, "name": "Defend"}],
            "legal_actions": [
                {"action": "play_card", "args": {"card_index": 0, "target_index": 0}},
                {"action": "play_card", "args": {"card_index": 0, "target_index": 1}},
                {"action": "play_card", "args": {"card_index": 1}},
                {"action": "end_turn"},
                {"action": "discard_potion", "args": {"potion_index": 0}}]}


@pytest.mark.parametrize("name", ["random", "heuristic"])
def test_policy_picks_only_legal_actions(name):
    st = _combat_state()
    legal = [json.dumps(a, sort_keys=True) for a in st["legal_actions"]]
    p = policy.make_policy(name, "s1")
    for _ in range(200):
        a = p.choose(st)
        assert json.dumps(a, sort_keys=True) in legal
        assert a["action"] != "discard_potion"  # weight 0 / never, as documented


def test_policy_is_reproducible_and_not_first():
    st = _combat_state()
    a = [policy.RandomPolicy("x").choose(st) for _ in range(1)]
    seq1 = [policy.RandomPolicy("seed").choose(st) for _ in range(5)]
    p1, p2 = policy.RandomPolicy("seed"), policy.RandomPolicy("seed")
    assert [p1.choose(st) for _ in range(30)] == [p2.choose(st) for _ in range(30)]
    picks = {json.dumps(policy.RandomPolicy(f"s{i}").choose(st), sort_keys=True) for i in range(60)}
    assert len(picks) >= 3, picks  # not always the first entry
    assert a and seq1


def test_heuristic_targets_lowest_hp_and_expands_choices():
    st = _combat_state()
    p = policy.HeuristicPolicy("t")
    for _ in range(30):
        a = p.choose(st)
        assert a["action"] == "play_card"
        if a["args"]["card_index"] == 0:
            assert a["args"]["target_index"] == 1
    sel = {"decision": "card_select", "cards": [{"index": i} for i in range(5)],
           "legal_actions": [{"action": "select_cards", "choose": {"from": [0, 1, 2, 3, 4], "min": 2, "max": 3}}]}
    for _ in range(20):
        idx = [int(x) for x in p.choose(sel)["args"]["indices"].split(",")]
        assert 2 <= len(idx) <= 3 and len(set(idx)) == len(idx)
    cs = {"decision": "crystal_sphere", "legal_actions": [
        {"action": "crystal_sphere_click", "choose_cell": {"cells": [[1, 2], [3, 4]], "tools": ["big", "small"]}}]}
    a = p.choose(cs)
    assert [a["args"]["x"], a["args"]["y"]] in ([1, 2], [3, 4]) and a["args"]["tool"] in ("big", "small")


def test_policy_does_not_reopen_a_skipped_card_reward():
    rewards = {"decision": "rewards", "context": {"act": 1, "floor": 3},
               "rewards": [{"index": 0, "type": "card"}],
               "legal_actions": [{"action": "claim_reward", "args": {"reward_index": 0}}, {"action": "proceed"}]}
    card = {"decision": "card_reward", "context": {"act": 1, "floor": 3},
            "legal_actions": [{"action": "select_card_reward", "args": {"card_index": 0}}, {"action": "skip_card_reward"}]}
    p = policy.HeuristicPolicy("r")
    assert p.choose(rewards)["action"] == "claim_reward"
    p._note(card, {"action": "skip_card_reward"})
    assert p.choose(rewards)["action"] == "proceed"


def test_no_legal_actions_raises():
    with pytest.raises(policy.NoLegalAction):
        policy.RandomPolicy(1).choose({"decision": "weird", "legal_actions": []})


# ───────────────────────────── text renderer ─────────────────────────────

def _numbered(st):
    return list(enumerate(st.get("legal_actions") or [], 1))


def test_clip_never_cuts_legal_actions():
    cards = [{"index": i, "name": f"Card{i}", "cost": 1, "type": "Skill", "description": "Gain 5 Block. " * 8,
              "after_upgrade": {"description": "Gain 8 Block. " * 8}} for i in range(90)]
    la = [{"action": "select_cards", "args": {"indices": str(i)}} for i in range(90)] + [{"action": "skip_select"}]
    st = {"type": "decision", "decision": "card_select", "purpose": "upgrade", "min_select": 1, "max_select": 1,
          "cancelable": True, "cards": cards, "legal_actions": la, "player": {"name": "X", "hp": 1, "max_hp": 2},
          "events": [{"type": "gold", "change": 1, "gold": 2}] * 300}
    out = d.render_decision(st, _numbered(st), prefix=["ok: something"] * 5)
    assert len(out.encode()) <= d.MAX_OUT
    assert "Actions:" in out and "[91] skip / cancel the selection" in out
    assert "[1-90] select_cards indices 0-89" in out
    # Small selections keep one line per action.
    st2 = dict(st, cards=cards[:3], legal_actions=la[:3] + la[-1:], events=[])
    out2 = d.render_decision(st2, _numbered(st2))
    assert "[1] select 0 Card0" in out2 and "[4] skip" in out2


def test_text_shows_what_a_human_sees():
    d.LANG["lang"] = "en"
    ev = {"type": "decision", "decision": "event_choice", "event_name": "Neow",
          "dialogue": [{"speaker": "ancient", "text": "I've... remade you."}, {"speaker": "character", "text": "Hm."}],
          "options": [{"index": 0, "title": "Arcane Scroll", "description": "Obtain a random Rare Card."}],
          "legal_actions": [{"action": "choose_option", "args": {"option_index": 0}}]}
    out = d.render_decision(ev, _numbered(ev))
    assert "Neow: I've... remade you." in out and "You: Hm." in out
    go = {"type": "decision", "decision": "game_over", "victory": False, "act": 1, "floor": 5, "score": 51,
          "score_breakdown": {"floors": 50, "gold": 1}, "legal_actions": [],
          "killed_by": {"type": "encounter", "name": "Nibbits", "quote": "A Lone Nibbit beat up The Ironclad."}}
    out = d.render_decision(go, [])
    assert "Score: 51" in out and "A Lone Nibbit beat up" in out and "run is over" in out
    shop = {"type": "decision", "decision": "shop", "card_removal_cost": 75, "card_removal_available": False,
            "legal_actions": [{"action": "leave_room"}]}
    assert "card removal: used" in d.render_decision(shop, _numbered(shop))
    assert d.intent_text({"type": "StatusCard", "card_count": 1}).startswith("Status (1 cards)")
    assert d.intent_text({"type": "StatusCard", "title": "Strategic"}).startswith("Strategic")
    d.LANG["lang"] = "zh"
    try:
        assert d.intent_text({"type": "StatusCard"}).startswith("策略")
    finally:
        d.LANG["lang"] = "en"
    cs = {"type": "decision", "decision": "crystal_sphere", "divinations_left": 3, "width": 3, "height": 2,
          "rows": ["??.", "?g?"], "hidden_cells": [[0, 0], [1, 0], [0, 1], [2, 1]],
          "items_seen": [{"id": 0, "type": "gold", "revealed_cells": [[1, 1]], "fully_revealed": False}],
          "tools": ["big", "small"],
          "legal_actions": [{"action": "crystal_sphere_click", "choose_cell": {"cells": [[0, 0]], "tools": ["big", "small"]}}]}
    out = d.render_decision(cs, _numbered(cs))
    assert "Legend:" in out and "3x3" in out and "hidden cells" not in out and " 0  1  2" in out
    # Fields the renderer doesn't know are still shown.
    unk = dict(ev, outcome_page={"text": "You feel stronger."})
    assert "outcome_page" in d.render_decision(unk, _numbered(unk))


def test_single_enemy_damage_preview_is_shown():
    st = {"type": "decision", "decision": "combat_play", "enemies": [{"index": 0, "name": "Toadpole", "hp": 5, "max_hp": 9}],
          "hand": [{"index": 0, "name": "Strike", "cost": 1, "type": "Attack", "description": "Deal 6 damage.",
                    "damage_by_target": [{"target_index": 0, "damage": 9}]}],
          "legal_actions": [{"action": "play_card", "args": {"card_index": 0}}, {"action": "end_turn"}]}
    assert "{E0:9}" in d.render_decision(st, _numbered(st))


def test_verbs_multiword_ambiguous_and_click():
    hand = [{"index": 0, "name": "Body Slam", "can_play": True}, {"index": 1, "name": "Strike", "can_play": True},
            {"index": 2, "name": "Strike", "can_play": True, "upgraded": True, "description": "Deal 9 damage."}]
    enemies = [{"index": 0, "name": "Jaw Worm"}, {"index": 1, "name": "Seapunk"}]
    st = {"decision": "combat_play", "hand": hand, "enemies": enemies}
    assert d.parse_verb("play Body Slam", st, [])["args"] == {"card_index": 0}
    assert d.parse_verb("play Body Slam Seapunk", st, [])["args"] == {"card_index": 0, "target_index": 1}
    assert d.parse_verb('play "Body Slam" -> E0', st, [])["args"] == {"card_index": 0, "target_index": 0}
    with pytest.raises(d.VerbError, match="matches several"):
        d.parse_verb("play Strike", st, [])  # Strike and the upgraded Strike differ
    same = dict(st, hand=hand[:2] + [{"index": 2, "name": "Strike", "can_play": True}])
    assert d.parse_verb("play Strike", same, [])["args"] == {"card_index": 1}
    twins = dict(same, enemies=[{"index": 0, "name": "Toadpole"}, {"index": 1, "name": "Toadpole"}])
    with pytest.raises(d.VerbError, match="use the E number"):
        d.parse_verb("play Body Slam Toadpole", twins, [])
    assert d.parse_verb("play Body Slam E1", twins, [])["args"]["target_index"] == 1
    with pytest.raises(d.VerbError, match="not in combat"):
        d.parse_verb("play Strike", {"decision": "map_select"}, [])
    with pytest.raises(d.VerbError, match="big\\|small"):
        d.parse_verb("click 1 2", {"decision": "crystal_sphere"}, [])
    assert d.parse_verb("click 1 2 small", {"decision": "crystal_sphere"}, [])["args"]["tool"] == "small"
    with pytest.raises(d.VerbError, match="run is over"):
        d.parse_verb("1", {"decision": "game_over"}, [])


# ───────────────────────────── live: sts2 shell client ─────────────────────────────

@pytest.fixture(scope="module")
def sts2():
    env = {k: v for k, v in os.environ.items() if not k.startswith("STS2_DEBUG")}
    session = f"pytestch{os.getpid()}"
    env["STS2_SESSION"] = session
    env["STS2_IDLE_TIMEOUT"] = "120"

    def run(*args):
        p = subprocess.run([STS2, *args], capture_output=True, text=True, env=env, timeout=120)
        return p.returncode, p.stdout

    run.session = session
    yield run
    run("stop")
    sock, _ = d.paths(session)
    try:
        os.unlink(sock[:-len(".sock")] + ".run.jsonl")
    except OSError:
        pass


def _engine_pids(session):
    pid = subprocess.run(["pgrep", "-f", f"sts2_daemon.py {session}"], capture_output=True, text=True).stdout.split()
    assert pid
    return pid[0], subprocess.run(["pgrep", "-P", pid[0]], capture_output=True, text=True).stdout.split()


def test_seedless_run_recovers_on_the_same_seed(sts2):
    rc, out = sts2("start", "--character", "Defect")
    assert rc == 0
    seed = json.loads(sts2("--json", "state")[1])["context"]["seed"]
    for _ in range(8):  # through Neow (seedless: the options vary) to the map
        before = json.loads(sts2("--json", "state")[1])
        if before["decision"] == "map_select":
            break
        sts2("do", "1")
    _, engines = _engine_pids(sts2.session)
    for e in engines:
        os.kill(int(e), 9)
    time.sleep(0.3)
    # The engine died while idle: the next command restores the run first, then applies.
    rc, out = sts2("do", "1")
    assert rc == 0 and "replayed" in out and "DIVERGED" not in out and out.count("ok: ") == 1
    after = json.loads(sts2("--json", "state")[1])
    assert after["context"]["seed"] == seed
    assert before["decision"] == "map_select" and after["decision"] != "map_select"


def test_killed_daemon_restores_the_run(sts2):
    before = json.loads(sts2("--json", "state")[1])
    dpid, engines = _engine_pids(sts2.session)
    for e in engines:
        os.kill(int(e), 9)
    os.kill(int(dpid), 9)
    time.sleep(0.3)
    rc, out = sts2("state")
    assert rc == 0 and "daemon restarted" in out
    after = json.loads(sts2("--json", "state")[1])
    assert after["context"]["seed"] == before["context"]["seed"]
    assert after["decision"] == before["decision"]
    assert [c["name"] for c in after.get("hand", [])] == [c["name"] for c in before.get("hand", [])]


def test_batch_stops_at_new_turn_and_on_changed_numbering(sts2):
    st = json.loads(sts2("--json", "state")[1])
    assert st["decision"] == "combat_play"
    rc, out = sts2("do", "end; end")
    assert "ok: end turn" in out and ("stopped before 'end'" in out)
    st = json.loads(sts2("--json", "state")[1])
    if st["decision"] == "combat_play" and len(st["legal_actions"]) > 2:
        rc, out = sts2("do", "1; 1")
        assert out.count("ok: ") == 1 and "stopped before '1'" in out


# ───────────────────────────── live: bridge ─────────────────────────────

def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="module")
def bridge():
    port = _free_port()
    env = {k: v for k, v in os.environ.items() if not k.startswith("STS2_DEBUG")}
    proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "agent", "sts2_bridge.py"), str(port), "--compact"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), 0.2).close()
            break
        except OSError:
            time.sleep(0.05)

    def post(obj):
        req = urllib.request.Request(f"http://127.0.0.1:{port}", json.dumps(obj).encode(),
                                     {"Content-Type": "application/json"})
        return json.loads(urllib.request.urlopen(req, timeout=120).read())

    post.proc = proc
    yield post
    try:
        post({"cmd": "shutdown"})
    except Exception:
        pass
    proc.kill()


def test_bridge_compact_keeps_rules_text(bridge):
    st = bridge({"cmd": "start_run", "character": "Ironclad", "seed": "brc1"})
    assert st["decision"] == "event_choice"
    assert all(o.get("description") for o in st["options"]), st["options"]
    assert "legal_actions" in st


def test_bridge_seedless_crash_replays_same_seed(bridge):
    st = bridge({"cmd": "start_run", "character": "Ironclad"})
    seed = st["context"]["seed"]
    engines = subprocess.run(["pgrep", "-P", str(bridge.proc.pid)], capture_output=True, text=True).stdout.split()
    for e in engines:
        os.kill(int(e), 9)
    time.sleep(0.3)
    r = bridge({"cmd": "action", "action": "choose_option", "args": {"option_index": 0}})
    assert r.get("type") == "decision", r
    assert any("restarted" in n for n in r.get("notes", [])), r.get("notes")
    st = bridge({"cmd": "get_state"})
    assert st["context"]["seed"] == seed
