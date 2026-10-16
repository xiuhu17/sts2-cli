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
    # Another event page is another screen, even when its option 0 looks the same.
    with pytest.raises(d.StopStep, match="different event_choice screen .*now: event_choice: 0 Leave; shown: event_choice: 0 Light Door"):
        d.parse_verb("choose 0", dict(ev, options=[{"index": 0, "title": "Leave"}]), [], refs)
    with pytest.raises(d.StopStep, match="different event_choice screen"):
        d.parse_verb("choose 0", dict(ev, description="The door opens."), [], refs)
    assert d.parse_verb("choose 0", dict(ev), [], refs)["args"] == {"option_index": 0}  # the same page (a looping event)


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


# ───────────────────────────── screen instances, typed indices, no-screen errors ─────────────────────────────

def _sel(source, cards, sid, purpose="exhaust", prompt="Choose 1 card to Exhaust."):
    return {"type": "decision", "decision": "card_select", "context": {"act": 1, "floor": 7}, "selection_id": sid,
            "purpose": purpose, "prompt": prompt, "screen": "hand", "min_select": 1, "max_select": 1,
            "source": {"name": source, "kind": "card"},
            "cards": [dict(c, index=i) for i, c in enumerate(cards)],
            "legal_actions": [{"action": "select_cards", "args": {"indices": str(i)}} for i in range(len(cards))]}


def test_index_steps_only_on_the_screen_instance_shown():
    strike, defend = _card(0, 21, "Strike"), _card(1, 22, "Defend", "Skill", "Self")
    shown = _sel("Burning Pact", [strike, defend], 5)
    refs = d.Refs(shown, [])
    refs.first = False
    # Same decision type, same cards, but True Grit+'s screen (another selection_id / source).
    other = _sel("True Grit+", [strike, defend], 9)
    with pytest.raises(d.StopStep, match="'select 0' would pick from a different card_select screen.*from True Grit\\+"
                                         ".*shown: .*from Burning Pact"):
        d.parse_verb("select 0", other, [], refs)
    with pytest.raises(d.StopStep, match="different card_select screen"):
        refs.numbered_action(1, other, {"action": "select_cards", "args": {"indices": "0"}})
    # An engine without selection_id: the source / prompt / offered instances still tell them apart.
    no_id = lambda st: {k: v for k, v in st.items() if k != "selection_id"}
    refs = d.Refs(no_id(shown), [])
    refs.first = False
    with pytest.raises(d.StopStep):
        d.parse_verb("select 0", no_id(other), [], refs)
    with pytest.raises(d.StopStep):  # same source and prompt, other card instances
        d.parse_verb("select 0", no_id(_sel("Burning Pact", [_card(0, 31, "Strike"), defend], None)), [], refs)
    assert d.parse_verb("select 1", no_id(shown), [], refs)["args"] == {"indices": "1"}
    # Card rewards: a reroll / second card reward is another screen.
    cr = {"decision": "card_reward", "selection_id": 3, "cards": [{"index": 0, "uid": 40, "name": "Inflame"}]}
    refs = d.Refs(cr, [])
    refs.first = False
    with pytest.raises(d.StopStep, match="different card_reward screen"):
        d.parse_verb("pick 0", dict(cr, selection_id=4), [], refs)
    # Rewards: claiming removes items (same screen); an item that was not shown means another screen.
    rw = {"decision": "rewards", "context": {"act": 1, "floor": 7},
          "rewards": [{"index": 0, "type": "gold", "amount": 8}, {"index": 1, "type": "potion", "name": "Fire Potion"}]}
    refs = d.Refs(rw, [])
    refs.first = False
    assert d.parse_verb("claim 1", dict(rw, rewards=rw["rewards"][1:]), [], refs)["args"] == {"reward_index": 1}
    with pytest.raises(d.StopStep, match="different rewards screen"):
        d.parse_verb("claim 1", dict(rw, rewards=rw["rewards"][1:] + [{"index": 2, "type": "relic", "name": "Anchor"}]), [], refs)
    # Map: go N only on the map shown.
    mp = {"decision": "map_select", "context": {"act": 1, "floor": 3}, "choices": [{"col": 1, "row": 3, "type": "Shop"}]}
    refs = d.Refs(mp, [])
    refs.first = False
    with pytest.raises(d.StopStep, match="'go 0' means move 0 on the map you saw"):
        d.parse_verb("go 0", dict(mp, choices=[{"col": 2, "row": 3, "type": "Elite"}]), [], refs)


def test_error_names_the_typed_index_and_its_remap():
    st = _combat([_card(0, 12, "Bash"), _card(1, 13, "Defend", "Skill", "Self")])
    act = {"action": "play_card", "args": {"card_index": 1, "target_index": 0}}
    typed = {("H", 1): (2, "Defend"), ("E", 0): (1, "Toadpole")}
    out = d.describe_refs(act, st, typed=typed)
    assert out == ["H2 (Defend as shown when the chain started, now H1) is Defend (Skill, 1 energy, targets yourself)",
                   "E1 (Toadpole as shown when the chain started, now E0) is Toadpole (HP 20/20)"]
    d.LANG["lang"] = "zh"
    assert d.describe_refs(act, st, typed={("H", 1): (2, "防御")})[0].startswith("H2（指令链开始时显示的 防御，现在是 H1）是 ")


def test_no_screen_errors_name_the_decision_and_never_an_empty_range():
    st = _combat(_hand3())
    for step, what in (("go 0", "a map choice"), ("choose 1", "an event or rest site option"), ("claim 0", "a reward"),
                       ("buy card 0", "a shop item"), ("select 0", "cards on a card selection screen"),
                       ("select none", "cards"), ("alt 0", "a card reward alternative"), ("pick 0", "a card reward"),
                       ("skip", "a card reward"), ("bundle 0", "a card pack"), ("relic 0", "a treasure relic")):
        with pytest.raises(d.VerbError) as e:
            d.parse_verb(step, st, [])
        msg = str(e.value)
        assert f"'{step}' picks {what}" in msg and "the current decision is combat_play: there is none here now" in msg, msg
        assert "Legal now: [1] play H0 Defend; [2] play H1 Bash -> E0 Toadpole" in msg and "-1" not in msg, msg
    mp = {"decision": "map_select", "choices": [{"col": 1, "row": 3, "type": "Shop"}]}
    with pytest.raises(d.VerbError, match="there is no map choice 4: the map has 1 choice\\(s\\) now \\(go 0 = go 1 3 Shop\\)"):
        d.parse_verb("go 4", mp, [])
    sel = {"decision": "card_select", "cards": [], "min_select": 0, "max_select": 0}
    assert d.describe_refs({"action": "select_cards", "args": {"indices": "0"}}, sel)[0] == "there is no card 0 (have: none)"
    assert d.index_range([0, 1, 2, 3]) == "0-3" and d.index_range([]) == "none" and d.index_range([1, 4]) == "1, 4"
    d.LANG["lang"] = "zh"
    with pytest.raises(d.VerbError, match="'go 0' 用于选择地图选项，但当前决策是 combat_play：现在这里没有这类选项。现在可选：\\[1\\]"):
        d.parse_verb("go 0", st, [])


class EngineDaemon(d.Daemon):
    """A Daemon on the debug test engine (conftest Game), for realistic chains."""

    def __init__(self, game, st):
        super().__init__("pytest-engine")
        self.game = game
        self._set_last(st)

    def send(self, cmd, record=False):
        return self.game.send(cmd)


def _fight(game, deck, lang):
    d.LANG["lang"] = lang
    game.start("Ironclad", seed="LP3RCEHPWTWW", ascension=10, lang=lang)
    game.set_player(deck=deck)
    return EngineDaemon(game, game.enter_room("combat", encounter="SHRINKER_BEETLE_WEAK"))


def test_live_chain_stops_on_a_later_card_select(game):
    # The agent-run repro: Burning Pact's exhaust screen shown, then True Grit+'s opens mid-chain.
    dm = _fight(game, ["ASCENDERS_BANE", "DEFEND_IRONCLAD", "STRIKE_IRONCLAD", "BURNING_PACT", "TRUE_GRIT+"], "zh")
    out, code = dm.cmd_do(["play 燃烧契约"], False)
    a = dm.last
    assert code == 0 and a["decision"] == "card_select" and a["source"]["name"] == "燃烧契约", out
    assert a.get("selection_id") is not None and all(c.get("uid") for c in a["cards"])
    bane = next(c["index"] for c in a["cards"] if c["name"] == "进阶之灾")
    out, code = dm.cmd_do([f"select {bane}; play 坚毅+; select 0"], False)
    b = dm.last
    assert code == 0 and b["decision"] == "card_select" and b["source"]["name"] == "坚毅+", out
    assert b["selection_id"] != a["selection_id"]
    assert "ok: play H" in out and "stopped before 'select 0': 未执行：'select 0' 会在另一个 card_select 界面上选择" in out, out
    assert "来自 坚毅+" in out and "当时显示：card_select [exhaust]" in out
    assert "selection_id" not in out  # known field: not dumped as an extra line


def test_live_remapped_index_error_names_the_typed_index(game):
    # Armaments' upgrade screen; after "play H1" the card shown as H2 is H1 (or H0) now.
    for lang in ("en", "zh"):
        dm = _fight(game, ["ARMAMENTS", "STRIKE_IRONCLAD", "DEFEND_IRONCLAD", "BASH", "ASCENDERS_BANE"], lang)
        arm = next(c["index"] for c in dm.last["hand"] if c["id"] == "CARD.ARMAMENTS")
        dm.cmd_do([f"play H{arm}"], False)
        hand = dm.last["combat"]["hand"]
        assert dm.last["decision"] == "card_select" and [c["id"] for c in hand][1:3] == ["CARD.BASH", "CARD.ASCENDERS_BANE"], hand
        name = hand[2]["name"]
        out, code = dm.cmd_do(["select 0; play H1 E0; play H2 E0"], False)
        now = next(c["index"] for c in dm.last["hand"] if c["name"] == name)
        assert code == 1 and now != 2
        want = (f"error in 'play H2 E0': H2 ({name} as shown when the chain started, now H{now}) is {name} (Curse"
                if lang == "en" else f"error in 'play H2 E0': H2（指令链开始时显示的 {name}，现在是 H{now}）是 {name}（")
        assert want in out, out
        assert f"H{now} is now" not in out and f"H{now} 现在是" not in out
