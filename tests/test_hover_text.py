"""What the game's UI says about a model, and nothing it doesn't: titles never shown as loc
keys (temporary powers named after their card/potion), the game's own hover tips on cards,
potions, relics and powers (two tips may share a title), owner-aware power text, selections
the game resolves itself (card_auto_selected), gold changes one by one at the shop, and the
bin/sts2 texts built on them."""
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "python"))
import sts2_daemon as d  # noqa: E402

# Test fixtures of the game itself (never in a run).
_NOT_IN_GAME = ("MOCK_", "DEPRECATED_")


@pytest.fixture(autouse=True)
def _english():
    d.LANG["lang"] = "en"
    yield
    d.LANG["lang"] = "en"


def _hand_index(s, cid):
    return next(c["index"] for c in s["hand"] if c["id"] == "CARD." + cid)


# ───────────────────────────── names ─────────────────────────────

@pytest.mark.parametrize("lang,potion,power", [("en", "Speed Potion", "Dexterity"), ("zh", "速度药水", "敏捷")])
def test_temporary_power_is_named_after_its_potion_everywhere(game, lang, potion, power):
    game.start(seed="sp", lang=lang)
    game.set_player(potions=["SPEED_POTION"], deck=["STRIKE_IRONCLAD"] * 5 + ["DEFEND_IRONCLAD"] * 5)
    game.enter_room("combat", encounter="SHRINKER_BEETLE_WEAK")
    s = game.act("use_potion", potion_index=0)
    names = [e["power"] for e in s["events"] if e["type"] == "power"]
    assert names == [power, potion], names       # PowerModel.Title: the potion's title
    assert {p["name"] for p in s["player_powers"]} == {power, potion}
    assert not any(".title" in json.dumps(s, ensure_ascii=False) for _ in [0])
    text = "\n".join(d.event_lines(s["events"]))
    assert "SPEED_POTION_POWER" not in text and f"{potion} +5" in text


@pytest.mark.parametrize("lang", ["en", "zh"])
def test_no_model_name_renders_as_a_loc_key(game, lang):
    """Every card/relic/potion/power/enchantment/affliction/monster/event title, as the
    adapter names it (ModelName: the models' Title overrides), is real text."""
    game.start(seed="au", lang=lang)
    r = game.send({"cmd": "__audit_text"})
    assert r["type"] == "text_audit" and r["names_checked"] > 1000
    raw = [x for x in r["raw_names"] if not x["id"].startswith(_NOT_IN_GAME)]
    assert raw == [], raw


@pytest.mark.parametrize("lang", ["en", "zh"])
def test_every_hover_tip_the_game_shows_is_in_tips(game, lang):
    """For every card, relic, potion and power, each hover tip the game draws next to it
    (CardModel.HoverTips, RelicModel.HoverTipsExcludingRelic, PotionModel.ExtraHoverTips,
    PowerModel.ExtraHoverTips, after IHoverTip.RemoveDupes) is in our tips list. [gold]
    terms the game itself gives no tip for (Stoke's Exhaust, ...) stay unexplained: nothing
    is added that the game doesn't show."""
    game.start(seed="au", lang=lang)
    r = game.send({"cmd": "__audit_text"})
    assert r["tips_checked"] > 1000
    assert r["dropped_tips"] == [], r["dropped_tips"][:20]
    # The audit found the game's own gaps (informational); it is a list of [gold] terms only.
    assert all(set(u) == {"kind", "id", "term"} for u in r["unexplained"])


# ───────────────────────────── tips ─────────────────────────────

def test_tainted_card_explains_the_affliction_and_the_power(game):
    """Tainted (污染) on a Skill: the affliction's tip and the Tainted power it applies share
    the title; the game shows both (FromPowerWithPowerHoverTips)."""
    game.start(seed="ip", lang="zh")
    game.set_player(deck=["DEFEND_IRONCLAD"] * 5 + ["STRIKE_IRONCLAD"] * 5)
    s = game.enter_room("combat", encounter="INFESTED_PRISMS_ELITE")
    defend = next(c for c in s["hand"] if c["id"] == "CARD.DEFEND_IRONCLAD")
    assert defend["affliction"] == "污染"
    tainted = [t["description"] for t in defend["tips"] if t["title"] == "污染"]
    assert len(tainted) == 2 and any("攻击伤害" in x for x in tainted), defend["tips"]
    # The enemy's Vital Spark lists the same tips (its ExtraHoverTips).
    spark = next(p for e in s["enemies"] for p in e.get("powers") or [] if p["id"] == "VITAL_SPARK_POWER")
    assert [t["title"] for t in spark["tips"]] == ["污染", "污染"]
    # bin/sts2 keeps both lines in the glossary.
    d.LANG["lang"] = "zh"
    out = d.render_decision(s, [])
    assert sum(1 for line in out.splitlines() if line.startswith("  污染: ")) == 2, out
    # Played: the player has the power, with its smart text.
    s = game.act("play_card", card_index=defend["index"])
    pw = next(p for p in s["player_powers"] if p["id"] == "TAINTED_POWER")
    assert pw["name"] == "污染" and "额外受到2点攻击伤害" in pw["description"], pw


def test_frantic_escape_explains_sandpit(game):
    """Frantic Escape's 'Get farther away.' (远离) is plain text; the card's tip is Sandpit:
    its canonical text before The Insatiable applies it, the live one after."""
    game.start(seed="sp", lang="zh")
    game.set_player(deck=["DEFEND_IRONCLAD"] * 10)
    game.enter_room("combat", encounter="THE_INSATIABLE_BOSS")
    s = game.act("end_turn")
    fe = next(c for c in s["hand"] if c["id"] == "CARD.FRANTIC_ESCAPE")
    assert fe["description"].startswith("远离。")
    tip = next(t for t in fe["tips"] if t["title"] == "沙坑")
    assert "被吞下" in tip["description"], tip


def test_power_tips_and_enemy_owned_text(game):
    """PowerModel.HoverTips: the smart text gets OnPlayer/OwnerName, so a power whose loc
    entry branches on the owner (Ritual) reads 'its turn' on an enemy; one that doesn't
    (Plating) keeps the game's 'your turn' wording on enemies too, listed under the enemy."""
    game.start(seed="cu", lang="en")
    game.set_player(deck=["DEFEND_IRONCLAD"] * 10)
    game.enter_room("combat", encounter="CULTISTS_NORMAL")
    s = game.act("end_turn")
    ritual = next(p for e in s["enemies"] for p in e.get("powers") or [] if p["id"] == "RITUAL_POWER")
    assert ritual["description"].startswith("At the end of its turn, gains "), ritual
    assert [t["title"] for t in ritual["tips"]] == ["Strength"]
    out = d.render_decision(s, [])
    assert "Calcified Cultist's powers:" in out and "  Strength: " in out
    game.start(seed="pl", lang="zh")
    game.set_player(deck=["DEFEND_IRONCLAD"] * 10)
    s = game.enter_room("combat", encounter="SLUMBERING_BEETLE_NORMAL")
    plating = next(p for e in s["enemies"] for p in e.get("powers") or [] if p["id"] == "PLATING_POWER")
    assert plating["description"].startswith("在你的回合结束时获得"), plating   # the game's own text
    assert [t["title"] for t in plating["tips"]] == ["格挡"]


# ───────────────────────────── selections the game resolves itself ─────────────────────────────

def test_headbutt_with_one_card_in_discard_is_auto_selected(game):
    game.start(seed="hb", lang="zh")
    game.set_player(deck=["HEADBUTT", "DEFEND_IRONCLAD", "STRIKE_IRONCLAD", "ARMAMENTS", "BASH"])
    s = game.enter_room("combat", encounter="SHRINKER_BEETLE_WEAK")
    s = game.act("play_card", card_index=_hand_index(s, "STRIKE_IRONCLAD"))
    s = game.act("play_card", card_index=_hand_index(s, "HEADBUTT"))
    assert s["decision"] == "combat_play"
    auto = [e for e in s["events"] if e["type"] == "card_auto_selected"]
    assert len(auto) == 1 and auto[0]["source"] == "头槌" and auto[0]["cards"] == ["打击"], auto
    assert auto[0]["available"] == 1 and auto[0]["screen"] == "discard"
    d.LANG["lang"] = "zh"
    assert "头槌：自动选择了 打击（只有 1 张可选）" in d.event_lines(s["events"])[-1]
    # A screen that does open records nothing (Armaments with two upgradable cards).
    s = game.act("play_card", card_index=_hand_index(s, "ARMAMENTS"))
    assert s["decision"] == "card_select"
    assert not [e for e in s.get("events") or [] if e["type"] == "card_auto_selected"]


def _auto_fight():
    hand = [{"index": 0, "uid": 1, "id": "CARD.HEADBUTT", "name": "Headbutt", "cost": 1, "type": "Attack",
             "target_type": "AnyEnemy", "can_play": True},
            {"index": 1, "uid": 2, "id": "CARD.DEFEND_IRONCLAD", "name": "Defend", "cost": 1, "type": "Skill",
             "target_type": "Self", "can_play": True}]
    enemies = [{"index": 0, "combat_id": 7, "name": "Toadpole", "hp": 20, "max_hp": 20}]
    legal = [{"action": "play_card", "args": {"card_index": 0, "target_index": 0}},
             {"action": "play_card", "args": {"card_index": 1}}, {"action": "end_turn"}]
    return {"type": "decision", "decision": "combat_play", "round": 1, "energy": 3, "max_energy": 3,
            "context": {"act": 1, "floor": 2}, "player": {"name": "The Ironclad", "hp": 50, "max_hp": 80},
            "hand": hand, "enemies": enemies, "legal_actions": legal}


class _Fake(d.Daemon):
    def __init__(self, start, script):
        super().__init__("pytest-fake-hover")
        self.script = script
        self._set_last(start)

    def send(self, cmd, record=False):
        if cmd.get("cmd") == "get_state":
            return self.last
        key = json.dumps({"action": cmd["action"], **({"args": cmd["args"]} if cmd.get("args") else {})}, sort_keys=True)
        return self.script.get(key) or {"type": "error", "code": "illegal_action", "message": "'x' is not allowed now: nope."}


def test_chained_select_after_an_auto_selection_stops_with_the_reason():
    fight = _auto_fight()
    after = dict(_auto_fight(), events=[
        {"type": "card_played", "card": "Headbutt", "target": "Toadpole"},
        {"type": "card_auto_selected", "source": "Headbutt", "purpose": "choose", "screen": "discard",
         "cards": ["Strike"], "available": 1, "min_select": 1}])
    key = json.dumps({"action": "play_card", "args": {"card_index": 0, "target_index": 0}}, sort_keys=True)
    dm = _Fake(fight, {key: after})
    out, code = dm.cmd_do(["play Headbutt E0; select 0"], False)
    assert code == 0, out
    assert ("stopped before 'select 0': not run: the game already picked the cards for that selection: "
            "Headbutt: auto-selected Strike (only 1 card could be chosen; no selection screen opened)") in out
    # Typed later on its own: an error that says the same.
    out, code = dm.cmd_do(["select 0"], False)
    assert code == 1 and "the game already picked the cards for that selection" in out


# ───────────────────────────── shop gold ─────────────────────────────

def test_shop_purchase_and_relic_refund_are_separate_gold_lines(game):
    """MerchantCardEntry.OnTryPurchase adds the card first (Lucky Fysh pays +15) and then
    charges the price shown (MerchantEntry.Cost)."""
    game.start(seed="shop", lang="en")
    game.set_player(relics=["LUCKY_FYSH"], gold=500)
    s = game.enter_room("shop")
    card = next(c for c in s["cards"] if c.get("is_stocked"))
    s = game.act("buy_card", card_index=card["index"])
    gold = [e for e in s["events"] if e["type"] == "gold"]
    assert [g["change"] for g in gold] == [15, -card["cost"]], gold
    assert gold[0]["source"] == "Lucky Fysh" and gold[1]["reason"] == "spent" and gold[1]["item"] == card["name"]
    assert s["player"]["gold"] == 500 + 15 - card["cost"]
    text = "; ".join(d.event_lines(s["events"]))
    assert f"Gold +15 (Lucky Fysh; now 515)" in text and f"Gold -{card['cost']} (bought {card['name']}; now" in text


@pytest.mark.parametrize("relic", ["MEMBERSHIP_CARD", "THE_COURIER"])
def test_shop_charges_the_price_it_shows(game, relic):
    game.start(seed="price", lang="en")
    game.set_player(relics=[relic], gold=3000)
    s = game.enter_room("shop")
    for kind, act, arg in (("cards", "buy_card", "card_index"), ("relics", "buy_relic", "relic_index"),
                           ("potions", "buy_potion", "potion_index")):
        item = next((x for x in s[kind] if x.get("is_stocked")), None)
        if item is None:
            continue
        shown = item["cost"]
        s = game.act(act, **{arg: item["index"]})
        spent = [e for e in s["events"] if e["type"] == "gold" and e.get("reason") == "spent"]
        assert [e["change"] for e in spent] == [-shown], (kind, spent, shown)


# ───────────────────────────── bin/sts2 texts ─────────────────────────────

def test_potion_full_note_only_with_a_potion_reward():
    base = {"type": "decision", "decision": "rewards", "potion_slots_full": True, "context": {"act": 1, "floor": 3},
            "legal_actions": [{"action": "proceed"}]}
    gold_only = dict(base, rewards=[{"index": 0, "type": "gold", "amount": 12}])
    assert "potion slots full" not in d.render_decision(gold_only, [])
    with_potion = dict(base, rewards=[{"index": 0, "type": "potion", "name": "Fire Potion"}])
    assert "potion slots full" in d.render_decision(with_potion, [])


def test_select_counts_and_card_numbers_are_both_shown():
    cards = [{"index": i, "name": f"C{i}", "type": "Skill", "cost": 1} for i in range(6)]
    st = {"type": "decision", "decision": "card_select", "purpose": "discard", "min_select": 0, "max_select": 6,
          "select_counts": [0, 1, 2, 3, 4, 5, 6], "cards": cards, "legal_actions": []}
    assert "select 0-6 cards (card numbers 0-5)" in d.render_decision(st, [])
    d.LANG["lang"] = "zh"
    assert "选择 0-6 张牌（卡牌编号 0-5）" in d.render_decision(st, [])


def test_play_and_end_on_a_combat_selection_say_it_is_still_combat():
    sel = {"decision": "card_select", "combat": {"round": 1}, "source": {"name": "Headbutt"}, "min_select": 1,
           "max_select": 1, "cards": [{"index": 0}, {"index": 1}]}
    for step, verb in (("play Strike", "'play' plays a card"), ("end", "'end' ends your turn")):
        with pytest.raises(d.NoScreen) as e:
            d.parse_verb(step, sel, [])
        assert "you are in combat, but a card selection by Headbutt is open: finish it first" in str(e.value)
        assert verb in str(e.value) and "not in combat" not in str(e.value)
    d.LANG["lang"] = "zh"
    with pytest.raises(d.NoScreen, match="仍在战斗中，但选牌界面（由Headbutt打开）正在打开：请先完成选择"):
        d.parse_verb("play Strike", sel, [])
    # Outside combat the old wording stays true.
    d.LANG["lang"] = "en"
    with pytest.raises(d.NoScreen, match="not in combat"):
        d.parse_verb("play Strike", {"decision": "map_select"}, [])
    # The engine says the same when the action is sent anyway.
    assert d.engine_text("'play_card' is not allowed now: you are in combat, but a card selection is open: "
                         "finish it first. Current decision: card_select. Legal: select_cards") is not None
