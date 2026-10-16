"""bin/sts2 messages: every engine error message has one explanation in the output language
(en / zh), a later 'skip' never leaves a rewards screen, potion steps only on the screen
instance shown, target errors in plain words, no raw fields / None / engine ids in the
views, zh labels and decision names, off-map moves, alt on a reward without alternatives."""

import itertools
import json
import os
import re

import pytest

import csharp_messages
from test_sts2_chains import FakeDaemon, _card, _combat, _hand3, _key, _removal, _shop, d

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src", "Sts2Headless")

# Engine messages bin/sts2 can't get: debug commands (never enabled in the daemon), save /
# load commands it doesn't send, and line framing / argument types of the JSON protocol
# (the daemon always sends well-formed commands). --json shows them as they are.
UNREACHABLE = [
    r"Empty line: .*", r"Invalid JSON: .*", r"Each line must be a JSON object.*", r"'action' must be a string.*",
    r"'args' must be an object.*", r"Provide 'path' or 'json' for load_save",
    r"smith_count: .*", r"Unknown relic: .*", r"card_select must be an object", r"card_select method must be .*",
    r"act must be 1\.\..*", r"Unknown encounter: .*", r"event requires 'event' parameter.*", r"Unknown event: .*",
    r"Unknown room type: .*", r"Cannot access draw pile",
    r"Save schema mismatch: .*", r"Failed to parse save file: .*", r"Save is not loadable: .*",
    r"Failed to create RunState from save", r"No active run to save", r"Cannot save checkpoint: .*",
    r"Cannot write save to .*",
]
SAMPLES = ("7", "", "Strike")
ENGINE_IDS = re.compile(r"\b(skip_select|select_cards|discard_potion|use_potion|claim_reward|play_card|target_index|"
                        r"card_index|potion_index|option_index|select_map_node|crystal_sphere_click|skip_treasure_relic|"
                        r"select_treasure_relic|buy_relic|leave_room|choose_option|card_select|map_select|AnyEnemy)\b")


def _fill(msg):
    """The message with its holes filled in every way SAMPLES allows (few holes: all combos)."""
    n = msg.count(csharp_messages.HOLE)
    for combo in itertools.product(SAMPLES, repeat=min(n, 3)):
        out = msg
        for x in combo:
            out = out.replace(csharp_messages.HOLE, x, 1)
        yield out.replace(csharp_messages.HOLE, SAMPLES[0])


@pytest.fixture(autouse=True)
def _english():
    d.LANG["lang"] = "en"
    yield
    d.LANG["lang"] = "en"


@pytest.fixture(scope="module")
def messages():
    msgs = csharp_messages.engine_messages(SRC)
    assert len(msgs) > 150, len(msgs)
    return msgs


def test_every_engine_error_message_is_localized(messages):
    unreachable_seen = set()
    for fn, line, msg in messages:
        skip = next((u for u in UNREACHABLE if re.fullmatch(u, msg.replace(csharp_messages.HOLE, "7"), re.S)), None)
        if skip:
            unreachable_seen.add(skip)
            continue
        hit = None
        for m in _fill(msg):
            if d.engine_reason_of(m) in SAMPLES:  # an envelope ('x' is not allowed: WHY): WHY is checked on its own
                hit = m
                break
            if d.engine_pattern(m):
                hit = m
                break
        assert hit is not None, f"{fn}:{line}: no localized explanation for {msg!r}"
        if d.engine_reason_of(hit) in SAMPLES:
            continue
        for lang in ("en", "zh"):
            d.LANG["lang"] = lang
            text, _, kind = d.engine_reason(d.engine_reason_of(hit))
            if kind in ("target", "notarget"):
                continue  # worded by _explain from the card / potion and the enemies
            assert text, (fn, line, msg)
            assert not ENGINE_IDS.search(text), f"{fn}:{line}: engine id in the {lang} text {text!r}"
            if lang == "zh":
                assert re.search(r"[一-鿿]", text), f"{fn}:{line}: no zh text for {msg!r}: {text!r}"
    assert unreachable_seen == set(UNREACHABLE), set(UNREACHABLE) - unreachable_seen  # no stale entries
    d.LANG["lang"] = "en"


def _handler_error(message):
    """A listed action the engine's handler turned down (no envelope; Act adds the code)."""
    return {"type": "error", "code": "illegal_action", "message": message}


def _select_screen(lo=2, hi=3):
    cards = [_card(i, 70 + i, n) for i, n in enumerate(["Strike", "Defend", "Bash", "Strike"])]
    return {"type": "decision", "decision": "card_select", "context": {"act": 1, "floor": 3}, "selection_id": 5,
            "purpose": "upgrade", "prompt": "Choose 3 cards to Upgrade.", "screen": "deck", "min_select": lo,
            "max_select": hi, "cards": cards,
            "legal_actions": [{"action": "select_cards", "choose": {"from": [0, 1, 2, 3], "min": lo, "max": hi}}]}


@pytest.mark.parametrize("lang", ["en", "zh"])
def test_card_select_errors_are_one_localized_explanation(lang):
    d.LANG["lang"] = lang
    st = _select_screen()
    cases = {
        "select 9": ("Invalid card index '9' (valid: 0-3)",
                     {"en": "there is no card 9 (have: 0-3). Legal now: [1] select 2-3 of [0, 1, 2, 3]",
                      "zh": "没有卡牌 9（现有：0-3）。现在可选：[1] 选择 2-3 张牌，可选 [0, 1, 2, 3]"}),
        "select 0,0": ("Card index 0 given twice",
                       {"en": "card 0 is Strike - card 0 is listed twice: each card can be selected once. Legal now:",
                        "zh": "卡牌 0 是 Strike；卡牌 0 列了两次：每张牌只能选一次。现在可选："}),
        "select 0": ("Select 2-3 cards (got 1)",
                     {"en": "card 0 is Strike - this screen takes 2-3 cards, not 1. Legal now:",
                      "zh": "卡牌 0 是 Strike；此界面需要选择 2-3 张牌，而不是 1 张。现在可选："}),
        "select none": ("This screen can't be confirmed with nothing selected: choose 2-3 card(s)",
                        {"en": "'confirm with no card selected' is not allowed now: this screen can't be confirmed with "
                               "nothing selected: choose 2-3 cards. Legal now:",
                         "zh": "现在不能执行“不选任何牌，直接确认”：此界面不能在不选牌时确认：请选择 2-3 张牌。现在可选："}),
    }
    for step, (engine_msg, want) in cases.items():
        action = d.parse_verb(step, st, list(enumerate(st["legal_actions"], 1)))
        dm = FakeDaemon(st, {_key(action["action"], **action.get("args", {})): _handler_error(engine_msg)})
        out, code = dm.cmd_do([step], False)
        line = out.splitlines()[0]
        assert code == 1 and line.startswith(f"error in '{step}': {want[lang]}"), line
        # Said once, and never the engine's English (the matching is case-insensitive).
        for raw in ("Invalid card index", "given twice", "(got ", "can't be confirmed with nothing selected: choose 2-3 card(s)",
                    "this screen takes 2-3 cards - this screen", "sts2 do \"select none\")'"):
            assert raw not in line, (raw, line)
        if lang == "zh":
            assert not re.search(r"\b(card|cards|screen|select \d|Legal)\b", line.split("': ", 1)[1].split("现在可选")[0]), line
    # Case-insensitive: the handler's capitalized form and the reason's lower-case one alike.
    for m in ("Invalid card index 5, hand has 3 cards", "invalid card index 5, hand has 3 cards"):
        assert d.engine_reason(m)[2] == "index"
    d.LANG["lang"] = "en"


def test_one_card_screen_counts_and_bad_tokens():
    sel = _removal(1)
    numbered = list(enumerate(sel["legal_actions"], 1))
    nope = {"type": "error", "code": "illegal_action",
            "message": "'select_cards' is not allowed now: select_cards(indices=0,1) is not one of the legal actions. "
                       "Current decision: card_select. Legal: select_cards(indices=0|1); skip_select"}
    dm = FakeDaemon(sel, {_key("select_cards", indices="0,1"): nope, _key("select_cards", indices="7"): dict(
        nope, message=nope["message"].replace("0,1", "7"))})
    out, _ = dm.cmd_do(["select 0,1"], False)
    assert "card 0 is Strike; card 1 is Defend - this screen takes 1 card, not 2. Legal now:" in out, out
    out, _ = dm.cmd_do(["select 7"], False)
    line = out.splitlines()[0]
    assert line.startswith("error in 'select 7': there is no card 7 (have: 0, 1). Legal now:") and "not one of" not in line
    with pytest.raises(d.VerbError, match="card numbers must be numbers: select 0,2 \\(got 'x'\\)"):
        d.parse_verb("select x", sel, numbered)


# ───────────────── 1: a later 'skip' never leaves a rewards screen ─────────────────

def _rewards(rewards, ctx=None):
    legal = [{"action": "claim_reward", "args": {"reward_index": w["index"]}} for w in rewards] + [{"action": "proceed"}]
    return {"type": "decision", "decision": "rewards", "context": ctx or {"act": 1, "floor": 2}, "rewards": rewards,
            "legal_actions": legal}


GOLD = {"index": 0, "type": "gold", "amount": 12}
POTION = {"index": 1, "type": "potion", "name": "Fire Potion"}
CARDS = {"index": 2, "type": "card", "card_count": 3}


@pytest.mark.parametrize("lang", ["en", "zh"])
def test_later_skip_never_leaves_the_rewards_screen(lang):
    d.LANG["lang"] = lang
    rw = _rewards([GOLD, POTION, CARDS])
    left = _rewards([POTION, CARDS])
    mp = {"type": "decision", "decision": "map_select", "context": {"act": 1, "floor": 2}, "choices": [], "legal_actions": []}
    dm = FakeDaemon(rw, {_key("claim_reward", reward_index=0): left, _key("proceed"): mp})
    out, code = dm.cmd_do(["claim 0; skip"], False)
    want = {"en": "stopped before 'skip': not run: 'skip' on the rewards screen would be proceed, giving up the rewards "
                  "not claimed (1 Fire Potion, 2 card reward (3 cards)); a later step of a chain never does that",
            "zh": "stopped before 'skip': 未执行：在奖励界面上 'skip' 等于继续，会放弃未领取的奖励（1 Fire Potion, 2 卡牌奖励（3 张））"}
    assert code == 0 and want[lang] in out, out
    assert [s["action"] for s in dm.sent] == ["claim_reward"]  # never proceed
    # "pick 0; skip": back on the rewards screen after the card reward -> stop, too.
    cr = {"type": "decision", "decision": "card_reward", "context": {"act": 1, "floor": 2}, "selection_id": 3,
          "cards": [{"index": 0, "uid": 1, "name": "Inflame"}],
          "legal_actions": [{"action": "select_card_reward", "args": {"card_index": 0}}, {"action": "skip_card_reward"}]}
    dm = FakeDaemon(cr, {_key("select_card_reward", card_index=0): _rewards([GOLD]), _key("proceed"): mp})
    out, code = dm.cmd_do(["pick 0; skip"], False)
    assert code == 0 and "stopped before 'skip'" in out and [s["action"] for s in dm.sent] == ["select_card_reward"], out
    # Nothing left: still not proceed (write 'proceed').
    dm = FakeDaemon(cr, {_key("select_card_reward", card_index=0): _rewards([]), _key("proceed"): mp})
    out, _ = dm.cmd_do(["pick 0; skip"], False)
    assert "stopped before 'skip'" in out and "'proceed'" in out and len(dm.sent) == 1, out
    # 'claim 2; skip' skips the card reward that claim opened; 'proceed' later in a chain is as written.
    dm = FakeDaemon(rw, {_key("claim_reward", reward_index=2): cr, _key("skip_card_reward"): rw})
    out, code = dm.cmd_do(["claim 2; skip"], False)
    assert code == 0 and [s["action"] for s in dm.sent] == ["claim_reward", "skip_card_reward"], out
    dm = FakeDaemon(rw, {_key("claim_reward", reward_index=0): left, _key("proceed"): mp})
    out, code = dm.cmd_do(["claim 0; proceed"], False)
    assert code == 0 and [s["action"] for s in dm.sent] == ["claim_reward", "proceed"], out
    # As the first step (the screen is the one shown) 'skip' on a rewards screen is proceed.
    dm = FakeDaemon(rw, {_key("proceed"): mp})
    out, code = dm.cmd_do(["skip"], False)
    assert code == 0 and [s["action"] for s in dm.sent] == ["proceed"], out
    d.LANG["lang"] = "en"
    assert "rewards screen: = proceed, first step only" in d.HELP
    assert "A later 'skip' never leaves a rewards screen" in d.HELP


# ───────────────── 3: potion steps only on the screen instance shown ─────────────────

def test_potion_step_stops_on_a_reopened_screen():
    pots = [{"index": 0, "uid": 50, "name": "Fire Potion", "target_type": "AnyEnemy", "usage": "CombatOnly"}]
    first = dict(_removal(1), player={"potions": pots})
    again = dict(_removal(2), player={"potions": pots})
    shop = dict(_shop(), player={"potions": pots})
    dm = FakeDaemon(first, {_key("skip_select"): shop, _key("remove_card"): again,
                            _key("discard_potion", potion_index=0): again})
    out, code = dm.cmd_do(["skip; remove; discard P0"], False)
    assert code == 0 and "stopped before 'discard P0'" in out and "closed and opened again" in out, out
    assert "a potion step only runs on the screen the chain started at" in out
    assert [s["action"] for s in dm.sent] == ["skip_select", "remove_card"]
    # In a fight, potions still follow plays in the same turn.
    fight = _combat(_hand3(), potions=pots)
    after = _combat([_card(0, 12, "Bash"), _card(1, 13, "Defend", "Skill", "Self")], potions=pots)
    dm = FakeDaemon(fight, {_key("play_card", card_index=0): after,
                            _key("use_potion", potion_index=0, target_index=1): after})
    out, code = dm.cmd_do(["play H0; potion P0 E1"], False)
    assert code == 0 and out.count("ok:") == 2, out
    assert "a fight: the same turn" in d.HELP and "skip; remove; discard P0" in d.HELP


# ───────────────── 4, 5, 9: labels and views ─────────────────

def test_labels_have_no_none_and_zh_labels_are_chinese():
    st = {"decision": "combat_play", "hand": [], "enemies": []}
    assert d.action_label({"action": "remove_card"}, st) == "remove a card"
    assert d.action_label({"action": "remove_card"}, {"card_removal_cost": 75}) == "remove a card (75g)"
    assert d.action_label({"action": "buy_card", "args": {"card_index": 3}}, {"cards": []}) == "buy card 3"
    assert d.action_label({"action": "claim_reward", "args": {"reward_index": 4}}, {"rewards": []}) == "claim 4"
    sel = {"action": "select_cards", "choose": {"from": [0, 1, 2], "min": 2, "max": 2}}
    assert d.action_label(sel, {}) == "select 2 of [0, 1, 2]"
    assert d.action_label(sel, {}, hints=True) == 'select 2 of [0, 1, 2]  (sts2 do "select 0,2")'
    none = {"action": "select_cards", "args": {"indices": ""}}
    assert d.action_label(none, {}) == "confirm with no card selected"  # no hint inside a quoted label
    d.LANG["lang"] = "zh"
    try:
        pots = {"player": {"potions": [{"index": 0, "name": "火焰药水"}]}}
        assert d.action_label({"action": "discard_potion", "args": {"potion_index": 0}}, pots) == "丢弃药水 P0 火焰药水"
        assert d.action_label(sel, {}, hints=True) == '选择 2 张牌，可选 [0, 1, 2]  (sts2 do "select 0,2")'
        assert d.action_label({"action": "remove_card"}, {}) == "移除一张牌"
        assert d.dec_name("card_select") == "选牌界面" and d.dec_name("map_select") == "地图"
        assert d.dec_name("event_choice") == "事件页面"
        body = "\n".join(d.render_body(dict(_select_screen(2, 2), screen="deck"), d.Glossary()))
        assert "选牌 [升级]" in body and "选择 2 张牌" in body and "牌组中的牌" in body and "deck" not in body, body
    finally:
        d.LANG["lang"] = "en"


def test_raw_fields_are_rendered():
    st = {"type": "decision", "decision": "map_select", "context": {"act": 1, "floor": 3}, "free_travel_available": True,
          "choices": [{"col": 1, "row": 4, "type": "Shop"}, {"col": 5, "row": 4, "type": "Elite", "free_travel": True}],
          "legal_actions": [{"action": "select_map_node", "args": {"col": 1, "row": 4}},
                            {"action": "select_map_node", "args": {"col": 5, "row": 4}}]}
    text = d.render_decision(st, list(enumerate(st["legal_actions"], 1)))
    assert "free_travel" not in text and "You may ignore paths when choosing the next room to travel to. (moves off the paths are marked 'off path')" in text
    assert "[2] go 5 4 Elite (off path)" in text
    star = {"index": 0, "name": "Stardust", "cost": 0, "star_cost": 0, "star_cost_x": True, "type": "Attack",
            "description": "Deal 5 damage X times.", "glow": "gold"}
    line = d.card_line(star, d.Glossary()) + d.card_extras(star)
    assert line == "Stardust (0 X*) Attack [glows gold: its condition is met] - Deal 5 damage X times.", line
    sel = dict(_select_screen(2, 2), screen="choose_a_card", select_counts=[0, 2])
    body = "\n".join(d.render_body(sel, d.Glossary()))
    assert "select 0 or 2 cards" in body and "choose one of the cards shown" in body and "screen" not in body, body
    assert "select_counts" not in body
    fight = _combat([_card(0, 1, "Mind Blast", damage_by_target=[{"target_index": 0, "damage": None, "total_damage": None}])])
    hand_line = next(ln for ln in d.render_decision(fight, []).splitlines() if ln.startswith(" H0 "))
    assert hand_line.endswith("{E0:?}") and "None" not in hand_line, hand_line
    rw = {"decision": "rewards", "rewards": [{"index": 0, "type": "special_card", "description": "Take your stolen card back.",
                                              "card": {"name": "Bash"}}, {"index": 1, "type": "card_removal"}]}
    body = "\n".join(d.render_body(rw, d.Glossary()))
    assert "0) Bash - Take your stolen card back." in body and "1) Remove a card from your deck." in body
    assert "special_card" not in body and "card_removal" not in body
    unc = dict(_select_screen(2, 2), select_counts=[], cancelable=True,
               message="Can't be confirmed: ... (skip_select).", cards=_select_screen()["cards"][:1])
    body = "\n".join(d.render_body(unc, d.Glossary()))
    assert "This screen can't be confirmed: it asks for 2 cards but shows only 1" in body and "skip_select" not in body


# ───────────────── 6: target errors ─────────────────

def _target_err(action, why):
    return {"type": "error", "code": "illegal_action", "message": f"'{action}' is not allowed now: {why}. "
            "Current decision: combat_play. Legal: end_turn"}


@pytest.mark.parametrize("lang", ["en", "zh"])
def test_target_errors_in_plain_words(lang):
    d.LANG["lang"] = lang
    hand = [_card(0, 1, "Defend", "Skill", "Self"), _card(1, 2, "Strike"), _card(2, 3, "Thunderclap", "Attack", "AllEnemies"),
            _card(3, 4, "Bodyguard", "Skill", "AnyAlly")]
    pots = [{"index": 0, "uid": 9, "name": "Fire Potion", "target_type": "AnyEnemy", "usage": "CombatOnly"},
            {"index": 1, "uid": 8, "name": "Block Potion", "target_type": "AnyPlayer", "usage": "CombatOnly"}]
    fight = _combat(hand, potions=pots)
    no_t = "'target_index' does not apply: it does not target a single enemy"
    cases = [
        ("play H0 E1", _key("play_card", card_index=0, target_index=1), _target_err("play_card", "play_card(card_index=0, target_index=1) is not one of the legal actions (" + no_t + ")"),
         {"en": "H0 is now Defend (Skill, 1 energy, targets yourself) - it takes no target: play H0 without an E#.",
          "zh": "H0 现在是 Defend（技能，1 能量，目标：自己）；它不需要目标：play H0，不要加 E#。"}),
        ("play H2 E0", _key("play_card", card_index=2, target_index=0), _target_err("play_card", "'target_index' does not apply to play_card here"),
         {"en": "H2 is now Thunderclap (Attack, 1 energy, hits all enemies) - it takes no target: play H2 without an E#.",
          "zh": "H2 现在是 Thunderclap（攻击，1 能量，目标：所有敌人）；它不需要目标：play H2，不要加 E#。"}),
        ("play H3 E0", _key("play_card", card_index=3, target_index=0), _target_err("play_card", no_t),
         {"en": "H3 is now Bodyguard (Skill, 1 energy, targets an ally) - it takes no target: play H3 without an E#.",
          "zh": "H3 现在是 Bodyguard（技能，1 能量，目标：友方）；它不需要目标：play H3，不要加 E#。"}),
        ("play H1", _key("play_card", card_index=1), _target_err("play_card", "play_card needs target_index (several are legal)"),
         {"en": "H1 is now Strike (Attack, 1 energy, targets one enemy) - with several enemies it needs a target: "
                "E0 Toadpole, E1 Seapunk (e.g. play H1 E0).",
          "zh": "H1 现在是 Strike（攻击，1 能量，目标：单个敌人）；敌人不止一个，需要指定目标：E0 Toadpole, E1 Seapunk（例如 play H1 E0）。"}),
        ("potion P0", _key("use_potion", potion_index=0), {"type": "error", "code": "illegal_action", "message":
            "Potion FIRE_POTION targets a single enemy (AnyEnemy); 'target_index' is required when multiple enemies are alive (2)."},
         {"en": "P0 is now Fire Potion (targets one enemy, combat only) - with several enemies it needs a target: "
                "E0 Toadpole, E1 Seapunk (e.g. potion P0 E0).",
          "zh": "P0 现在是 Fire Potion（目标：单个敌人，仅限战斗）；敌人不止一个，需要指定目标：E0 Toadpole, E1 Seapunk（例如 potion P0 E0）。"}),
        ("potion P1 E0", _key("use_potion", potion_index=1, target_index=0), _target_err("use_potion", no_t),
         {"en": "P1 is now Block Potion (targets a player, combat only) - it takes no target: potion P1 without an E#.",
          "zh": "P1 现在是 Block Potion（目标：玩家，仅限战斗）；它不需要目标：potion P1，不要加 E#。"}),
    ]
    for step, key, err, want in cases:
        dm = FakeDaemon(fight, {key: err})
        out, code = dm.cmd_do([step], False)
        line = out.splitlines()[0]
        assert code == 1 and line.startswith(f"error in '{step}': {want[lang]}"), line
        assert "target_index" not in line and "AnyEnemy" not in line and "play_card" not in line, line
    d.LANG["lang"] = "en"


# ───────────────── 7: an action number that never was in the list ─────────────────

@pytest.mark.parametrize("lang", ["en", "zh"])
def test_action_number_never_listed_is_an_error_with_the_current_list(lang):
    d.LANG["lang"] = lang
    rw = _rewards([GOLD])
    mp = {"type": "decision", "decision": "map_select", "context": {"act": 1, "floor": 2},
          "choices": [{"col": 3, "row": 3, "type": "Monster"}],
          "legal_actions": [{"action": "select_map_node", "args": {"col": 3, "row": 3}}]}
    dm = FakeDaemon(rw, {_key("proceed"): mp})
    out, code = dm.cmd_do(["proceed; 9"], False)
    want = {"en": "error in '9': no action number 9 in the list this chain was given (it had 1, 2). Legal now: [1] go 3 3 Monster",
            "zh": "error in '9': 这条指令链所依据的列表中没有操作编号 9（当时有：1, 2）。现在可选：[1] 前往 3 3 敌人"}
    assert code == 1 and want[lang] in out, out
    assert "stopped before" not in out
    # A step for a screen the game is not at: the decision is named once.
    dm = FakeDaemon(rw, {_key("proceed"): mp})
    out, code = dm.cmd_do(["proceed; pick 0"], False)
    line = next(ln for ln in out.splitlines() if ln.startswith("stopped before"))
    assert code == 0 and line.count("map_select" if lang == "en" else "地图") == 1, line
    assert "the game is now at" not in line and "游戏现在处于" not in line, line
    d.LANG["lang"] = "en"


# ───────────────── 8: alt N without alternatives ─────────────────

def test_alt_on_a_reward_without_alternatives():
    skip_only = {"type": "decision", "decision": "card_reward", "context": {"act": 1, "floor": 2}, "selection_id": 3,
                 "cards": [{"index": 0, "uid": 1, "name": "Inflame"}], "alternatives": [{"index": 0, "id": "Skip", "name": "Skip"}],
                 "legal_actions": [{"action": "select_card_reward", "args": {"card_index": 0}}, {"action": "skip_card_reward"}]}
    with pytest.raises(d.VerbError, match="this card reward has no alternatives \\(its only other button is Skip\\): "
                                          "use 'skip' \\(action \\[2\\]\\) or pick a card"):
        d.parse_verb("alt 1", skip_only, [])
    assert d.parse_verb("alt 0", skip_only, []) == {"action": "skip_card_reward"}
    no_skip = dict(skip_only, alternatives=[], legal_actions=skip_only["legal_actions"][:1])
    with pytest.raises(d.VerbError, match="no alternatives and can't be skipped: pick a card. Legal now: \\[1\\] pick 0 Inflame"):
        d.parse_verb("alt 0", no_skip, [])
    d.LANG["lang"] = "zh"
    try:
        with pytest.raises(d.VerbError, match="这个卡牌奖励没有其他选项.*'skip'（操作 \\[2\\]）"):
            d.parse_verb("alt 1", skip_only, [])
    finally:
        d.LANG["lang"] = "en"


# ───────────────── 10: go COL ROW off the map ─────────────────

class MapDaemon(FakeDaemon):
    def send(self, cmd, record=False):
        if cmd.get("cmd") == "get_map":
            return {"type": "map", "rows": [[{"col": c, "row": r} for c in range(7)] for r in range(1, 16)],
                    "boss": {"col": 3, "row": 16}}
        return super().send(cmd, record)


@pytest.mark.parametrize("lang", ["en", "zh"])
def test_go_off_the_map_says_so_once(lang):
    d.LANG["lang"] = lang
    mp = {"type": "decision", "decision": "map_select", "context": {"act": 1, "floor": 2},
          "choices": [{"col": 3, "row": 3, "type": "Monster"}, {"col": 4, "row": 3, "type": "Shop"}],
          "legal_actions": [{"action": "select_map_node", "args": {"col": 3, "row": 3}},
                            {"action": "select_map_node", "args": {"col": 4, "row": 3}}]}
    err = {"type": "error", "code": "illegal_action", "message": "'select_map_node' is not allowed now: select_map_node(col=9, "
           "row=9) is not one of the legal actions (node (9,9) is not reachable from here). Current decision: map_select. "
           "Legal: select_map_node(col=3, row=3); select_map_node(col=4, row=3)"}
    dm = MapDaemon(mp, {_key("select_map_node", col=9, row=9): err,
                        _key("select_map_node", col=2, row=3): json.loads(json.dumps(err).replace("9", "2").replace("(2,2)", "(2,3)"))})
    out, code = dm.cmd_do(["go 9 9"], False)
    line = out.splitlines()[0]
    want = {"en": "error in 'go 9 9': (9,9) is outside the map (columns 0-6, rows 0-16; sts2 map). Legal now: [1] go 3 3 Monster; "
                  "[2] go 4 3 Shop",
            "zh": "error in 'go 9 9': (9,9) 在地图之外（列 0-6，行 0-16；sts2 map）。现在可选：[1] 前往 3 3 敌人；[2] 前往 4 3 商店"}
    assert code == 1 and line == want[lang], line
    assert line.count("3 3") == 1  # the legal moves once
    d.LANG["lang"] = "en"
