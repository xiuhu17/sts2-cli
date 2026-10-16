"""bin/sts2 `do` chains and text views: index steps resolved against the view the chain was
submitted from (H#/E#/P#, action numbers, screen indices), continuing across a decision
change when the next step is legal, explicit errors for index steps, events printed once,
labelled damage lines, seed/ascension in the header, map coordinates."""

import json
import os
import subprocess
import sys

import pytest

from conftest import stop_session

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "python"))
import sts2_daemon as d  # noqa: E402

STS2 = os.path.join(ROOT, "bin", "sts2")


@pytest.fixture(autouse=True)
def _english():
    d.LANG["lang"] = "en"
    yield
    d.LANG["lang"] = "en"


# ───────────────────────────── synthetic states ─────────────────────────────

def _card(i, uid, name, typ="Attack", target="AnyEnemy", **kw):
    return {"index": i, "uid": uid, "id": f"CARD.{name.upper()}_IRONCLAD", "name": name, "cost": 1, "type": typ,
            "target_type": target, "can_play": True, "description": "x", **kw}


def _combat(hand, enemies=None, rnd=1, potions=None, floor=2):
    enemies = enemies if enemies is not None else [{"index": 0, "combat_id": 7, "name": "Toadpole", "hp": 20, "max_hp": 20},
                                                   {"index": 1, "combat_id": 9, "name": "Seapunk", "hp": 30, "max_hp": 30}]
    legal = []
    for c in hand:
        if c["target_type"] == "AnyEnemy":
            legal += [{"action": "play_card", "args": {"card_index": c["index"], "target_index": e["index"]}} for e in enemies]
        else:
            legal.append({"action": "play_card", "args": {"card_index": c["index"]}})
    legal.append({"action": "end_turn"})
    for p in potions or []:
        legal.append({"action": "use_potion", "args": {"potion_index": p["index"]}})
    return {"type": "decision", "decision": "combat_play", "round": rnd, "energy": 3, "max_energy": 3,
            "context": {"act": 1, "floor": floor, "seed": "ABC", "ascension": 10},
            "player": {"name": "The Ironclad", "hp": 50, "max_hp": 80, "gold": 99, "potions": potions or []},
            "hand": hand, "enemies": enemies, "legal_actions": legal}


def _hand3():
    return [_card(0, 11, "Defend", "Skill", "Self"), _card(1, 12, "Bash"), _card(2, 13, "Defend", "Skill", "Self")]


def test_hand_index_resolves_to_the_card_shown():
    snap = _combat(_hand3())
    refs = d.Refs(snap, list(enumerate(snap["legal_actions"], 1)))
    refs.first = False
    # After playing the shown H0, Bash (shown as H1) is H0 and the second Defend is H1.
    now = _combat([_card(0, 12, "Bash"), _card(1, 13, "Defend", "Skill", "Self")])
    assert d.parse_verb("play H1 E1", now, [], refs) == {"action": "play_card", "args": {"card_index": 0, "target_index": 1}}
    assert refs.moved == ["H1 -> H0"]
    assert d.parse_verb("play H2", now, [], refs)["args"] == {"card_index": 1}
    # The shown H0 is gone: never another card in its place (the old "play H0; play H0" misplay).
    with pytest.raises(d.StopStep, match="H0 \\(Defend, as shown when this chain started\\) is no longer in your hand"):
        d.parse_verb("play H0", now, [], refs)
    # Names are read against the current hand.
    assert d.parse_verb("play Bash E0", now, [], refs)["args"] == {"card_index": 0, "target_index": 0}


def test_enemy_and_potion_indices_follow_their_instance():
    pots = [{"index": 0, "uid": 50, "name": "Fire Potion", "target_type": "AnyEnemy"},
            {"index": 1, "uid": 51, "name": "Block Potion", "target_type": "Self"}]
    snap = _combat(_hand3(), potions=pots)
    refs = d.Refs(snap, [])
    refs.first = False
    # E0 died: the Seapunk shown as E1 is E0 now.
    now = _combat(_hand3(), enemies=[{"index": 0, "combat_id": 9, "name": "Seapunk", "hp": 30, "max_hp": 30}],
                  potions=[{"index": 1, "uid": 51, "name": "Block Potion", "target_type": "Self"}])
    assert d.parse_verb("play Bash E1", now, [], refs)["args"] == {"card_index": 1, "target_index": 0}
    with pytest.raises(d.StopStep, match="E0 \\(Toadpole.*no longer a target"):
        d.parse_verb("play Bash E0", now, [], refs)
    assert d.parse_verb("potion P1", now, [], refs)["args"] == {"potion_index": 1}
    with pytest.raises(d.StopStep, match="P0 \\(Fire Potion"):
        d.parse_verb("potion P0 E0", now, [], refs)


def test_numbered_action_is_the_one_shown():
    snap = _combat(_hand3())
    numbered = list(enumerate(snap["legal_actions"], 1))
    refs = d.Refs(snap, numbered)
    refs.first = False
    now = _combat([_card(0, 12, "Bash"), _card(1, 13, "Defend", "Skill", "Self")])
    now_numbered = list(enumerate(now["legal_actions"], 1))
    # [3] was "play H1 Bash -> E1"; the current list's [3] is something else.
    assert numbered[2][1] == {"action": "play_card", "args": {"card_index": 1, "target_index": 1}}
    assert d.parse_verb("3", now, now_numbered, refs) == {"action": "play_card", "args": {"card_index": 0, "target_index": 1}}
    with pytest.raises(d.StopStep, match="H0"):
        d.parse_verb("1", now, now_numbered, refs)  # [1] = the Defend that was played


def test_screen_indices_need_the_same_screen_and_item():
    rewards = {"decision": "rewards", "rewards": [{"index": 0, "type": "gold", "amount": 8},
                                                  {"index": 1, "type": "potion", "name": "Fire Potion"},
                                                  {"index": 2, "type": "card", "card_count": 3}]}
    refs = d.Refs(rewards, [])
    refs.first = False
    after = dict(rewards, rewards=rewards["rewards"][1:])
    assert d.parse_verb("claim 1", after, [], refs)["args"] == {"reward_index": 1}
    assert d.parse_verb("claim 0", after, [], refs)["args"] == {"reward_index": 0}  # engine + explanation
    card_reward = {"decision": "card_reward", "cards": [{"index": 0, "name": "Inflame"}]}
    with pytest.raises(d.StopStep, match="card_reward screen this chain has not shown you"):
        d.parse_verb("pick 0", card_reward, [], refs)
    ev = {"decision": "event_choice", "options": [{"index": 0, "title": "Light Door"}]}
    refs = d.Refs(ev, [])
    refs.first = False
    with pytest.raises(d.StopStep, match="choose 0 is now 'Leave' \\(it was 'Light Door'"):
        d.parse_verb("choose 0", dict(ev, options=[{"index": 0, "title": "Leave"}]), [], refs)


def test_index_errors_say_what_the_index_is_now():
    st = _combat(_hand3())
    refs = d.describe_refs({"action": "play_card", "args": {"card_index": 2, "target_index": 1}}, st)
    assert refs == ["H2 is now Defend (Skill, 1 energy, targets yourself)", "E1 is Seapunk (HP 30/30)"]
    assert d.describe_refs({"action": "play_card", "args": {"card_index": 7}}, st) == \
        ["there is no H7 (hand: H0 Defend, H1 Bash, H2 Defend)"]
    rw = {"decision": "rewards", "rewards": [{"index": 1, "type": "potion", "name": "Fire Potion"}]}
    out = d.describe_refs({"action": "claim_reward", "args": {"reward_index": 0}}, rw, {0: "8 Gold", 1: "Fire Potion"})
    assert out[0].startswith("reward 0 (8 Gold) was already claimed - reward numbers stay fixed")
    shop = {"decision": "shop", "player": {"gold": 40}, "cards": [{"index": 0, "name": "Stomp", "price": 74},
                                                                {"index": 1, "is_stocked": False}]}
    assert d.describe_refs({"action": "buy_card", "args": {"card_index": 0}}, shop) == ["buy card 0 is Stomp (74g; you have 40g)"]
    assert d.describe_refs({"action": "buy_card", "args": {"card_index": 1}}, shop) == ["buy card 1 is sold out"]
    m = {"decision": "map_select", "choices": [{"col": 3, "row": 2, "type": "Unknown"}]}
    assert "legal: go 3 2 Unknown" in d.describe_refs({"action": "select_map_node", "args": {"col": 9, "row": 9}}, m)[0]
    d.LANG["lang"] = "zh"
    assert d.card_ref_text(_card(0, 1, "防御", "Skill", "Self", id="CARD.DEFEND_IRONCLAD")) == "防御（Defend，技能，1 能量，目标：自己）"


class FakeDaemon(d.Daemon):
    """A Daemon whose engine is a script: action -> next state."""

    def __init__(self, start, script):
        super().__init__("pytest-fake")
        self.script = script
        self.sent = []
        self._set_last(start)

    def send(self, cmd, record=False):
        if cmd.get("cmd") == "get_state":
            return self.last
        self.sent.append({k: v for k, v in cmd.items() if k != "cmd"})
        key = json.dumps({"action": cmd["action"], **({"args": cmd["args"]} if cmd.get("args") else {})}, sort_keys=True)
        return self.script.get(key) or {"type": "error", "code": "illegal_action",
                                        "message": f"'{cmd['action']}' is not allowed now: nope. Current decision: x."}


def _key(action, **args):
    return json.dumps({"action": action, **({"args": args} if args else {})}, sort_keys=True)


def test_chain_continues_when_the_next_step_is_legal_on_the_new_decision():
    ctx = {"act": 1, "floor": 2}
    card_reward = {"type": "decision", "decision": "card_reward", "context": ctx, "cards": [{"index": 1, "name": "Inflame"}],
                   "legal_actions": [{"action": "select_card_reward", "args": {"card_index": 1}}]}
    rewards = {"type": "decision", "decision": "rewards", "context": ctx, "rewards": [],
               "legal_actions": [{"action": "proceed"}]}
    mp = {"type": "decision", "decision": "map_select", "context": ctx, "choices": [{"col": 3, "row": 3, "type": "Monster"}],
          "legal_actions": [{"action": "select_map_node", "args": {"col": 3, "row": 3}}]}
    fight = _combat(_hand3(), floor=3)
    dm = FakeDaemon(card_reward, {_key("select_card_reward", card_index=1): rewards, _key("proceed"): mp,
                                  _key("select_map_node", col=3, row=3): fight})
    out, code = dm.cmd_do(["pick 1; proceed; go 3 3; play Bash E0"], False)
    assert code == 0 and "ok: pick 1 Inflame" in out and "ok: proceed" in out and "ok: go 3 3 Monster" in out
    assert "stopped before 'play Bash E0': not run: the run moved on to a new room" in out
    assert len(dm.sent) == 3


def test_chain_after_a_mid_turn_selection_and_stops():
    hand = _hand3()
    fight = _combat(hand)
    sel = {"type": "decision", "decision": "card_select", "context": fight["context"], "min_select": 1, "max_select": 1,
           "cards": [{"index": 0, "name": "Defend"}], "combat": {"round": 1, "hand": hand, "enemies": fight["enemies"]},
           "legal_actions": [{"action": "select_cards", "args": {"indices": "0"}}]}
    next_turn = _combat(hand, rnd=2)
    dm = FakeDaemon(sel, {_key("select_cards", indices="0"): fight, _key("end_turn"): next_turn})
    out, code = dm.cmd_do(["select 0; end; end"], False)
    assert code == 0 and "ok: select 0 Defend" in out and "ok: end turn" in out
    assert "stopped before 'end': not run: a new turn" in out
    # A card selection pops up mid-chain: 'play' does not apply there -> stop, not an error.
    dm = FakeDaemon(fight, {_key("play_card", card_index=1, target_index=0): sel})
    out, code = dm.cmd_do(["play Bash E0; play Defend"], False)
    assert code == 0 and "stopped before 'play Defend': not run: the game is now at card_select" in out
    # A potion step only runs on the screen the chain started at.
    pots = [{"index": 0, "uid": 5, "name": "Fire Potion", "target_type": "AnyEnemy"}]
    fight_p = _combat(hand, potions=pots)
    sel_p = dict(sel, player={"potions": pots})
    dm = FakeDaemon(fight_p, {_key("play_card", card_index=1, target_index=0): sel_p})
    out, _ = dm.cmd_do(["play Bash E0; potion P0 E0"], False)
    assert "a potion step only runs on the screen the chain started at" in out


def test_engine_error_is_explained_with_the_referent():
    fight = _combat(_hand3())
    dm = FakeDaemon(fight, {})
    dm.script[_key("play_card", card_index=2, target_index=1)] = {
        "type": "error", "code": "illegal_action",
        "message": "'play_card' is not allowed now: play_card(card_index=2, target_index=1) is not one of the legal "
                   "actions ('target_index' does not apply: it does not target a single enemy). Current decision: "
                   "combat_play. Legal: play_card(card_index=0|1|2); end_turn"}
    out, code = dm.cmd_do(["play H2 E1"], False)
    assert code == 1
    assert ("error in 'play H2 E1': H2 is now Defend (Skill, 1 energy, targets yourself); E1 is Seapunk (HP 30/30) - "
            "'target_index' does not apply: it does not target a single enemy. Legal: play_card") in out


# ───────────────────────────── rendering ─────────────────────────────

def test_damage_and_block_lines_are_labelled():
    ev = [{"type": "damage", "source": "Seapunk", "target": "The Ironclad", "amount": 12, "blocked": 12},
          {"type": "damage", "source": "Seapunk", "target": "The Ironclad", "amount": 0, "blocked": 6},
          {"type": "damage", "target": "Toadpole", "amount": 6, "card": "Strike", "killed": True},
          {"type": "block", "target": "The Ironclad", "amount": 5},
          {"type": "hp", "change": -3, "hp": 47}]
    line = d.event_lines(ev)[0]
    assert line == ("Seapunk -> The Ironclad took 12 damage (12 more blocked); Seapunk -> The Ironclad blocked all 6 "
                    "damage; Toadpole took 6 damage [Strike] - KILLED; The Ironclad gained 5 Block; HP -3 (now 47)")
    d.LANG["lang"] = "zh"
    line = d.event_lines(ev[:2])[0]
    assert line == "Seapunk -> The Ironclad 受到 12 点伤害（另有 12 点被格挡）; Seapunk -> The Ironclad 格挡了全部 6 点伤害"


def test_header_and_game_over_show_seed_and_ascension():
    st = _combat(_hand3())
    assert d.header(st)[0].endswith("| Ascension 10 | Seed ABC ==")
    go = {"decision": "game_over", "victory": False, "act": 1, "floor": 14, "context": {"seed": "ABC", "ascension": 10}}
    assert "  Ascension 10 | Seed ABC" in d.game_over_lines(go)
    d.LANG["lang"] = "zh"
    assert "进阶10 | 种子：ABC" in d.header(st)[0]


def test_rewards_view_says_indices_are_stable():
    st = {"decision": "rewards", "rewards": [{"index": 1, "type": "gold", "amount": 8}], "legal_actions": []}
    assert "these numbers stay fixed" in "\n".join(d.render_body(st, d.Glossary()))


def test_map_has_coordinates_position_and_legal_moves():
    m = {"context": {"floor": 2}, "current_coord": {"col": 1, "row": 1},
         "start": {"col": 1, "row": 0, "type": "Ancient", "children": [{"col": 1, "row": 1}], "visited": True},
         "rows": [[{"col": 1, "row": 1, "type": "Monster", "visited": True, "current": True,
                    "children": [{"col": 0, "row": 2}, {"col": 2, "row": 2}]}],
                  [{"col": 0, "row": 2, "type": "Elite", "children": []}, {"col": 2, "row": 2, "type": "Shop", "children": []}]],
         "boss": {"col": 1, "row": 3, "type": "Boss", "name": "Vantom"}}
    st = {"decision": "map_select", "choices": [{"col": 0, "row": 2, "type": "Elite"}, {"col": 2, "row": 2, "type": "Shop"}]}
    text = d.render_map(m, st)
    lines = text.splitlines()
    assert "You are at (1,1) Monster" in text
    assert "Legal now:  go 0 2 = Elite   go 2 2 = Shop" in text
    col_header = next(ln for ln in lines if ln.strip().startswith("0   1   2"))
    row2 = next(ln for ln in lines if ln.startswith("  2   f3"))
    assert row2.endswith("<- go 0 2, go 2 2") and "*E*" in row2 and "*$*" in row2
    # Column numbers sit over their cells.
    assert col_header.index("0") == row2.index("E") and col_header.index("2") == row2.index("$")
    assert any(ln.startswith("  1   f2") and "<M>" in ln for ln in lines)
    assert any(ln.startswith("  3   f4") and "B" in ln for ln in lines)
    assert d.action_label({"action": "select_map_node", "args": {"col": 0, "row": 2}}, st) == "go 0 2 Elite"
    assert d.parse_verb("go (0,2)", st, [])["args"] == {"col": 0, "row": 2}


# ───────────────────────────── live ─────────────────────────────

@pytest.fixture(scope="module")
def sts2():
    env = {k: v for k, v in os.environ.items() if not k.startswith("STS2_DEBUG")}
    env["STS2_SESSION"] = f"pytestchain{os.getpid()}"
    env["STS2_IDLE_TIMEOUT"] = "120"

    def run(*args):
        p = subprocess.run([STS2, *args], capture_output=True, text=True, env=env, timeout=120)
        return p.returncode, p.stdout

    yield run
    stop_session(env["STS2_SESSION"], env)


def test_live_map_header_and_events_once(sts2):
    rc, out = sts2("start", "--character", "Ironclad", "--seed", "cli1")
    assert rc == 0 and "| Ascension 0 | Seed CL11 ==" in out
    sts2("do", "1")
    rc, out = sts2("do", "1")
    assert rc == 0 and "[1] go 0 1 Monster" in out
    rc, out = sts2("map")
    assert "Legal now:  go 0 1 = Monster" in out and "<- go 0 1" in out and "You are at (" in out
    rc, out = sts2("do", "go 0 1")
    assert rc == 0 and "ok: go 0 1 Monster" in out and "Since last decision:" in out and "drew " in out
    # state right after do: the events were shown already; --json still carries them.
    rc, out = sts2("state")
    assert "Since last decision:" not in out and "drew " not in out and "shown in the previous reply" in out
    assert json.loads(sts2("--json", "state")[1]).get("events")
    rc, out = sts2("log", "3")
    assert "drew " in out


def test_live_hand_indices_never_slip(sts2):
    st = json.loads(sts2("--json", "state")[1])
    assert st["decision"] == "combat_play" and all("uid" in c for c in st["hand"])
    names = [c["name"] for c in st["hand"]]
    rc, out = sts2("do", "play H0; play H0")
    assert out.count("ok: play") == 1 and "no longer in your hand" in out, out
    after = json.loads(sts2("--json", "state")[1])
    assert [c["name"] for c in after["hand"]] == names[1:]
    # An index error says what the index is now.
    assert after["hand"][0]["target_type"] == "Self"
    rc, out = sts2("do", "play H0 E0")
    assert rc == 1 and f"H0 is now {after['hand'][0]['name']} (Skill, 1 energy, targets yourself)" in out \
        and "'target_index' does not apply" in out, out
    # The cards shown as H1 and H2: the second one moved to H1 after the first was played.
    shown = after["hand"]
    rc, out = sts2("do", "play H1; play H2")
    assert out.count("ok: play") == 2 and "(shown as H2)" in out, out
    now = json.loads(sts2("--json", "state")[1])
    assert [c["uid"] for c in now["hand"]] == [shown[0]["uid"]] + [c["uid"] for c in shown[3:]]
