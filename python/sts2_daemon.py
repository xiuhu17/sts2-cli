"""Daemon and text renderer behind the `sts2` shell client (python/sts2.py).

The daemon owns one engine process and answers one request per connection on a unix
socket. Requests come from sts2.py as: one flag byte (b"j" for --json, b"-" otherwise),
then the argv joined with NUL bytes; the client then shuts down its write side. The reply
is one status byte (b"0".."9") followed by UTF-8 text.
"""

import json
import os
import socket
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import game_over  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
MAX_OUT = 7800  # keep every reply well under ~8 KB


def _runtime_dir():
    d = os.environ.get("TMPDIR") or "/tmp"
    # AF_UNIX paths are limited to ~104 bytes on macOS.
    if len(d) > 70:
        d = "/tmp"
    return d


def paths(session):
    base = os.path.join(_runtime_dir(), f"sts2-{os.getuid()}-{session}")
    return base + ".sock", base + ".log"


# ───────────────────────────── localization ─────────────────────────────

LANG = {"lang": "en"}


def t(en, zh):
    return zh if LANG["lang"] == "zh" else en


# Official zh terms (localization_zhs: gameplay_ui CARD_TYPE/CARD_RARITY, intents, map,
# static_hover_tips, rest_site_ui).
_ZH = {
    "card_type": {"Attack": "攻击", "Skill": "技能", "Power": "能力", "Status": "状态", "Curse": "诅咒", "Quest": "任务"},
    "rarity": {"Basic": "基础", "Common": "普通", "Uncommon": "罕见", "Rare": "稀有", "Ancient": "先古",
               "Curse": "诅咒", "Status": "状态", "Event": "事件", "Token": "衍生", "Quest": "任务"},
    "intent": {"Attack": "攻势", "Buff": "强化", "CardDebuff": "恶意", "DeathBlow": "濒死一击", "Debuff": "策略",
               "DebuffStrong": "策略", "Defend": "守势", "Escape": "懦弱", "Heal": "回复", "Sleep": "沉睡",
               "Status": "策略", "StatusCard": "策略", "Stun": "击晕", "Summon": "召唤", "Unknown": "未知"},
    "room": {"Monster": "敌人", "Elite": "精英", "Event": "事件", "Shop": "商店", "RestSite": "休息处",
             "Treasure": "宝箱", "Boss": "Boss", "Unknown": "未知", "Ancient": "先古之民", "Map": "地图"},
}


def zh_term(kind, value):
    if LANG["lang"] != "zh" or value is None:
        return value
    return _ZH.get(kind, {}).get(value, value)


_LOC_CACHE = {}


def loc(table, key):
    """Entry from the official localization tables (output language, English fallback)."""
    lang = "zhs" if LANG["lang"] == "zh" else "eng"
    return _loc_lang(lang, table, key) or _loc_lang("eng", table, key)


def _loc_lang(lang, table, key):
    for l in (lang,):
        k = (l, table)
        if k not in _LOC_CACHE:
            try:
                with open(os.path.join(ROOT, f"localization_{l}", f"{table}.json"), encoding="utf-8") as f:
                    _LOC_CACHE[k] = json.load(f)
            except OSError:
                _LOC_CACHE[k] = {}
        v = _LOC_CACHE[k].get(key)
        if v:
            import re
            return re.sub(r"\[/?[a-zA-Z_][a-zA-Z0-9_=]*\]", "", v)
    return None


# ───────────────────────────── rendering ─────────────────────────────

def _one_line(s):
    return " ".join(str(s).split()) if s else ""


def _first(*vals):
    """The first value that is not None (never prints None)."""
    return next((v for v in vals if v is not None), None)


def _desc(s):
    s = _one_line(s)
    return f" - {s}" if s else ""


class Glossary:
    """Keyword tooltips (Vulnerable, Exhaust, ...) shown once per reply."""

    def __init__(self):
        self.items = {}

    def add(self, tips):
        for tip in tips or []:
            if isinstance(tip, dict) and tip.get("title") and tip.get("description"):
                self.items.setdefault(tip["title"], _one_line(tip["description"]))

    def render(self):
        if not self.items:
            return []
        return [t("Keywords:", "关键词："), *[f"  {k}: {v}" for k, v in self.items.items()]]


# The hand card's glow (NHandCardHolder): gold = playable with its bonus condition met.
_GLOW = {"gold": ("glows gold: its condition is met", "金色高亮：条件已满足"), "red": ("glows red", "红色高亮")}


def card_line(c, gl, show_upgrade=False, prefix=""):
    name = c.get("name", "?")
    if c.get("upgraded") and not name.endswith("+"):
        name += "+"
    cost = c.get("cost")
    if c.get("cost_x"):
        cost = "X"
    star = " X*" if c.get("star_cost_x") else (f" {c['star_cost']}*" if c.get("star_cost") else "")
    extra = []
    if c.get("enchantment"):
        extra.append(f"{c['enchantment']}{' ' + str(c['enchantment_amount']) if c.get('enchantment_amount') else ''}")
    if c.get("affliction"):
        extra.append(f"{c['affliction']}{' ' + str(c['affliction_amount']) if c.get('affliction_amount') else ''}")
    ex = f" <{', '.join(extra)}>" if extra else ""
    glow = _GLOW.get(c.get("glow"))
    ex += f" [{t(*glow)}]" if glow else ""
    typ = zh_term("card_type", c.get("type"))
    s = f"{prefix}{name} ({cost}{star}) {typ}{ex}{_desc(c.get('description'))}"
    gl.add(c.get("tips"))
    if show_upgrade and isinstance(c.get("after_upgrade"), dict):
        s += f" || {t('Upgraded', '升级后')}{_desc(c['after_upgrade'].get('description'))}".replace(" - ", ": ", 1)
    return s


_INTENT_EN = {"StatusCard": "Status", "CardDebuff": "Card debuff", "DeathBlow": "Death blow",
              "DebuffStrong": "Strong debuff"}


def intent_text(it):
    typ = it.get("type", "?")
    # The game's own intent title (hover tip) when the engine sends it, else the type label.
    s = _one_line(it.get("title")) if it.get("title") else (
        zh_term("intent", typ) if LANG["lang"] == "zh" else _INTENT_EN.get(typ, typ))
    if it.get("damage") is not None:
        s += f" {it['damage']}"
        if it.get("hits"):
            s += f"x{it['hits']}"
            if it.get("total_damage") is not None:
                s += f"={it['total_damage']}"
    if it.get("card_count"):
        s += f" ({it['card_count']} {t('cards', '张牌')})"
    if it.get("label"):
        s += f" [{_one_line(it['label'])}]"
    return s


def powers_text(powers, gl):
    out = []
    for p in powers or []:
        amt = p.get("amount")
        out.append(f"{p.get('name')}{' ' + str(amt) if amt not in (None, 0) else ''}{_desc(p.get('description'))}")
    return out


def run_id_parts(ctx):
    """Ascension and seed, as the game's top bar / run history show them."""
    out = []
    if ctx.get("ascension") is not None:
        out.append(t(f"Ascension {ctx['ascension']}", f"进阶{ctx['ascension']}"))  # gameplay_ui ASCENSION_LEVEL
    if ctx.get("seed"):
        out.append(t(f"Seed {ctx['seed']}", f"种子：{ctx['seed']}"))  # main_menu_ui CUSTOM_RUN_SCREEN.SEED_LABEL
    return out


def header(st):
    ctx = st.get("context") or {}
    p = st.get("player") or {}
    parts = []
    if ctx:
        parts.append(f"{t('Act ', '第')}{ctx.get('act')}{t('', '幕')} {ctx.get('act_name', '')}".strip())
        parts.append(f"{t('Floor', '楼层')} {ctx.get('floor')}")
        if ctx.get("room_type"):
            parts.append(zh_term("room", ctx.get("room_type")))
        if (ctx.get("boss") or {}).get("name"):
            parts.append(f"Boss: {ctx['boss']['name']}")
        parts.extend(run_id_parts(ctx))
    lines = [f"== {' | '.join(str(x) for x in parts)} =="] if parts else []
    if p:
        hp = f"{t('HP', '生命')} {p.get('hp')}/{p.get('max_hp')}"
        blk = f"  {t('Block', '格挡')} {p['block']}" if p.get("block") else ""
        lines.append(f"{p.get('name', '')}  {hp}{blk}  {t('Gold', '金币')} {p.get('gold')}  "
                     f"{t('Deck', '牌组')} {p.get('deck_size')}")
        relics = []
        for r in p.get("relics") or []:
            n = r.get("name", "?")
            if r.get("counter") is not None:
                n += f"({r['counter']})"
            relics.append(n)
        if relics:
            lines.append(f"{t('Relics', '遗物')}: {', '.join(relics)}  ({t('sts2 relics: full text', 'sts2 relics 查看说明')})")
        pots = [x for x in p.get("potions") or [] if isinstance(x, dict)]
        slots = p.get("potion_slots")
        if pots or slots:
            lines.append(f"{t('Potions', '药水')} ({len(pots)}/{slots}):" + ("" if pots else f" {t('none', '无')}"))
            for x in pots:
                lines.append(f"  P{x.get('index')} {x.get('name')} [{potion_tags(x)}]{_desc(x.get('description'))}")
    return lines


_POTION_USAGE = {"CombatOnly": ("combat only", "仅限战斗"), "AnyTime": ("any time", "随时可用"),
                 "Automatic": ("automatic, not used by hand", "自动触发，不能手动使用"), "None": ("not usable", "不能使用")}


def potion_tags(x):
    """'targets one enemy, combat only': a potion's target and when it can be used."""
    tt = _TARGET_TEXT.get(x.get("target_type"))
    us = _POTION_USAGE.get(x.get("usage"))
    bits = [t(*tt) if tt else x.get("target_type"), t(*us) if us else x.get("usage")]
    return t(", ", "，").join(str(b) for b in bits if b)


_PILE_NAMES = {"hand": ("hand", "手牌"), "draw": ("draw pile", "抽牌堆"), "discard": ("discard pile", "弃牌堆"),
               "exhaust": ("exhaust pile", "消耗堆")}

def damage_text(e):
    """One damage event: `amount` is the damage that got through (HP lost), `blocked` what
    Block absorbed on top of it."""
    amt, blk = e.get("amount") or 0, e.get("blocked") or 0
    src = f"{e['source']} -> " if e.get("source") else ""
    if blk and not amt:
        s = src + t(f"{e.get('target')} blocked all {blk} damage", f"{e.get('target')} 格挡了全部 {blk} 点伤害")
    else:
        s = src + t(f"{e.get('target')} took {amt} damage", f"{e.get('target')} 受到 {amt} 点伤害")
        if blk:
            s += t(f" ({blk} more blocked)", f"（另有 {blk} 点被格挡）")
    if e.get("card"):
        s += f" [{e['card']}]"
    if e.get("killed"):
        s += t(" - KILLED", "，击杀")
    return s


EVENT_FMT = {
    "turn": lambda e: (t("-- your turn ", "-- 你的回合 ") if e.get("side") == "player" else t("-- enemy turn ", "-- 敌人回合 ")) + f"{e.get('round')} --",
    "card_played": lambda e: t("played ", "打出 ") + e.get("card", "?") + (f" -> {e['target']}" if e.get("target") else "") + (t(" (auto)", "（自动）") if e.get("auto") else ""),
    "damage": lambda e: damage_text(e),
    "block": lambda e: t(f"{e.get('target')} gained {e.get('amount')} Block", f"{e.get('target')} 获得 {e.get('amount')} 点格挡"),
    "power": lambda e: f"{e.get('target')}{t(': ', '：')}{e.get('power')} {'+' if (e.get('amount') or 0) >= 0 else ''}{e.get('amount')}"
             + (f" ({t('from', '来自')} {e['source']})" if e.get("source") else ""),
    "enemy_move": lambda e: f"{e.get('enemy')} {t('acts', '行动')}" + (f" [{', '.join(zh_term('intent', i) for i in e['intents'])}]" if e.get("intents") else ""),
    "draw": lambda e: t("drew ", "抽到 ") + ", ".join(e.get("cards") or []),
    "discard": lambda e: t("discarded ", "丢弃 ") + str(e.get("card")),
    "exhaust": lambda e: t("exhausted ", "消耗 ") + str(e.get("card")),
    "card_added": lambda e: t("card added: ", "加入卡牌：") + str(e.get("card"))
                  + (" -> " + t(*_PILE_NAMES.get(e["pile"], (e["pile"], e["pile"]))) if e.get("pile") else "")
                  + (t(" (hand full)", "（手牌已满）") if e.get("redirected") == "hand_full" else ""),
    "speech": lambda e: f"{e.get('speaker')}: \"{_one_line(e.get('text'))}\"",
    "thought": lambda e: f"{e.get('speaker')} ({t('thinks', '心想')}): \"{_one_line(e.get('text'))}\"",
    "afflicted": lambda e: f"{e.get('card')} <{e.get('affliction')}>",
    "orb_channeled": lambda e: t("channeled ", "生成 ") + str(e.get("orb")),
    "potion_used": lambda e: t("used ", "使用 ") + str(e.get("potion")) + (f" -> {e['target']}" if e.get("target") else ""),
    "stars": lambda e: f"{t('Stars', '辉星')} {'+' if e.get('amount', 0) >= 0 else ''}{e.get('amount')}",
    "summon": lambda e: t(f"summoned {e.get('amount')}", f"召唤 {e.get('amount')}"),
    "relic": lambda e: t("relic: ", "遗物：") + str(e.get("relic")),
    "enemy_died": lambda e: f"{e.get('enemy')} {t('died', '死亡')}",
    "gold": lambda e: f"{t('Gold', '金币')} {e['change']:+d} ({t('now', '现为')} {e.get('gold')})",
    "max_hp": lambda e: f"{t('Max HP', '最大生命')} {e['change']:+d} ({t('now', '现为')} {e.get('max_hp')})",
    "hp": lambda e: f"{t('HP', '生命')} {e['change']:+d} ({t('now', '现为')} {e.get('hp')})",
    "deck_added": lambda e: t("deck + ", "牌组 + ") + str(e.get("card")),
    "deck_removed": lambda e: t("deck - ", "牌组 - ") + str(e.get("card")),
    "deck_changed": lambda e: t("deck: ", "牌组：") + f"{e.get('from')} -> {e.get('to')}",
    "relic_obtained": lambda e: t("relic + ", "遗物 + ") + str(e.get("relic")),
    "relic_lost": lambda e: t("relic - ", "遗物 - ") + str(e.get("relic")),
    "potion_obtained": lambda e: t("potion + ", "药水 + ") + str(e.get("potion")),
    "potion_lost": lambda e: t("potion - ", "药水 - ") + str(e.get("potion")),
    "truncated": lambda e: "...",
}


def event_lines(events):
    """Events grouped into lines, a new line at each turn marker."""
    lines, cur = [], []
    for e in events or []:
        f = EVENT_FMT.get(e.get("type"))
        try:
            s = f(e) if f else json.dumps(e, ensure_ascii=False)
        except Exception:
            s = json.dumps(e, ensure_ascii=False)
        if e.get("type") == "turn":
            if cur:
                lines.append("; ".join(cur))
            cur = []
            lines.append(s)
        else:
            cur.append(s)
    if cur:
        lines.append("; ".join(cur))
    return lines


def action_label(a, st, hints=False):
    """Short text for one legal action, in the output language. hints=True (the numbered
    action list) adds the command to type for actions that need arguments; the command
    itself (sts2 do "select 0,2") is never translated."""
    act = a.get("action")
    args = a.get("args") or {}
    st = st or {}
    hand = {c.get("index"): c for c in (Refs.combat_of(st) or {}).get("hand") or []}
    enemies = {e.get("index"): e for e in (Refs.combat_of(st) or {}).get("enemies") or []}
    pots = {p.get("index"): p for p in (st.get("player") or {}).get("potions") or [] if isinstance(p, dict)}
    hint = lambda cmd: f"  (sts2 do \"{cmd}\")" if hints else ""
    tgt = ""
    if "target_index" in args:
        e = enemies.get(args["target_index"])
        tgt = f" -> E{args['target_index']} {e.get('name') if e else ''}".rstrip()
    if act == "play_card":
        c = hand.get(args.get("card_index"), {})
        return f"{t('play', '打出')} H{args.get('card_index')} {c.get('name', '?')}{tgt}"
    if act == "end_turn":
        return t("end turn", "结束回合")
    if act == "use_potion":
        return f"{t('potion', '使用药水')} P{args.get('potion_index')} {pots.get(args.get('potion_index'), {}).get('name', '?')}{tgt}"
    if act == "discard_potion":
        return f"{t('discard', '丢弃药水')} P{args.get('potion_index')} {pots.get(args.get('potion_index'), {}).get('name', '?')}"
    if act == "select_map_node":
        ch = next((c for c in st.get("choices") or [] if c.get("col") == args.get("col") and c.get("row") == args.get("row")), {})
        free = t(" (off path)", "（无视路线）") if ch.get("free_travel") else ""
        return f"{t('go', '前往')} {args.get('col')} {args.get('row')} {zh_term('room', ch.get('type', ''))}".rstrip() + free
    if act == "choose_option":
        o = next((o for o in st.get("options") or [] if o.get("index") == args.get("option_index")), {})
        title = o.get("title") or rest_title(o)
        return f"{t('choose', '选择')} {args.get('option_index')} {title or ''}".rstrip()
    if act == "select_cards":
        if "choose" in a:
            ch = a["choose"]
            n = count_text(ch.get("min"), ch.get("max"), ch.get("counts"))
            frm = ", ".join(map(str, ch.get("from") or []))
            return t(f"select {n} of [{frm}]", f"选择 {n} 张牌，可选 [{frm}]") + hint("select 0,2")
        idx = str(args.get("indices"))
        if idx == "":
            return t("confirm with no card selected", "不选任何牌，直接确认") + hint("select none")
        cards = {c.get("index"): c for c in st.get("cards") or []}
        c = cards.get(int(idx)) if idx.isdigit() else None
        return f"{t('select', '选择')} {idx} {c.get('name') if c else ''}".rstrip()
    if act == "select_card_reward":
        c = next((c for c in st.get("cards") or [] if c.get("index") == args.get("card_index")), {})
        return f"{t('pick', '选择卡牌')} {args.get('card_index')} {c.get('name', '')}".rstrip()
    if act in ("buy_card", "buy_relic", "buy_potion"):
        key = {"buy_card": ("cards", "card_index", "card", "卡牌"), "buy_relic": ("relics", "relic_index", "relic", "遗物"),
               "buy_potion": ("potions", "potion_index", "potion", "药水")}[act]
        it = next((x for x in st.get(key[0]) or [] if x.get("index") == args.get(key[1])), {})
        price = _first(it.get("price"), it.get("cost"))
        cost = t(f" ({price}g)", f"（{price} 金币）") if price is not None else ""
        return f"{t('buy ' + key[2], '购买' + key[3])} {args.get(key[1])} {it.get('name', '')}".rstrip() + cost
    if act == "remove_card":
        price = st.get("card_removal_cost")
        return t("remove a card", "移除一张牌") + (t(f" ({price}g)", f"（{price} 金币）") if price is not None else "")
    if act == "claim_reward":
        w = next((x for x in st.get("rewards") or [] if x.get("index") == args.get("reward_index")), None)
        return f"{t('claim', '领取')} {args.get('reward_index')}" + (f" {reward_text(w)}" if w else "")
    if act == "select_treasure_relic":
        w = next((x for x in st.get("relics") or [] if x.get("index") == args.get("relic_index")), {})
        return f"{t('take relic', '拿取遗物')} {args.get('relic_index')} {w.get('name', '')}".rstrip()
    if act == "leave_room":
        return t("leave", "离开")
    if act == "proceed":
        if st.get("decision") == "rewards" and st.get("rewards"):
            return t("proceed (skips the rewards not claimed)", "继续（放弃未领取的奖励）")
        return t("proceed", "继续")
    if act == "skip_card_reward":
        return t("skip the card reward", "跳过卡牌奖励")
    if act == "skip_select":
        return t("skip / cancel the selection", "跳过 / 取消选择")
    if act == "skip_treasure_relic":
        return t("skip the relic", "跳过遗物")
    if act == "select_bundle":
        return f"{t('pack', '选择卡包')} {args.get('bundle_index')}"
    if act == "crystal_sphere_click":
        return t("click X Y big|small", "点击 X Y big|small") + (
            t('  (sts2 do "click 2 3 big"; big = 3x3, small = 1 cell)', '  (sts2 do "click 2 3 big"；big = 3x3，small = 1 格)')
            if hints else "")
    if act == "select_card_alternative":
        alt = next((x for x in st.get("alternatives") or [] if x.get("index") == args.get("alternative_index")), {})
        return f"{t('alternative', '其他选项')} {args.get('alternative_index')} {alt.get('name') or alt.get('id') or ''}".rstrip()
    if args:
        return f"{act} {' '.join(f'{k}={v}' for k, v in args.items())}"
    return act


def rest_title(o):
    if not o.get("option_id"):
        return None
    return loc("rest_site_ui", f"OPTION_{o['option_id']}.name") or o.get("option_id")


def reward_text(w):
    typ = w.get("type")
    if typ == "gold":
        return f"{w.get('amount')} {t('Gold', '金币')}"
    if typ == "card":
        return t(f"card reward ({w.get('card_count')} cards)", f"卡牌奖励（{w.get('card_count')} 张）")
    if typ == "special_card" and isinstance(w.get("card"), dict):
        # A card of your own given back (the Thieving Hopper's stolen card).
        return f"{w['card'].get('name', '?')}{_desc(w.get('description'))}"
    if typ == "card_removal" and not w.get("name"):
        return loc("gameplay_ui", "COMBAT_REWARD_CARD_REMOVAL") or t("Remove a card from your deck.", "从你的牌组中移除一张牌。")
    name = w.get("name") or _REWARD_TYPE.get(typ, (typ, typ))[LANG["lang"] == "zh"]
    return f"{name}{_desc(w.get('description'))}"


_REWARD_TYPE = {"relic": ("relic", "遗物"), "potion": ("potion", "药水"), "special_card": ("card", "卡牌"),
                "card_removal": ("card removal", "移除卡牌")}


def render_combat_core(st, gl, hand_title=None):
    out = []
    bits = [f"{t('Round', '回合')} {st.get('round')}", f"{t('Energy', '能量')} {st.get('energy')}/{st.get('max_energy')}"]
    if st.get("stars") is not None:
        bits.append(f"{t('Stars', '辉星')} {st['stars']}")
    bits.append(f"{t('Draw', '抽牌堆')} {st.get('draw_pile_count')}  {t('Discard', '弃牌堆')} {st.get('discard_pile_count')}  "
                f"{t('Exhaust', '消耗堆')} {st.get('exhaust_pile_count')}")
    out.append(" | ".join(bits))
    pw = powers_text(st.get("player_powers"), gl)
    if pw:
        out.append(t("Your powers:", "你的能力："))
        out.extend(f"  {x}" for x in pw)
    if st.get("orbs") is not None or st.get("orb_slots"):
        orbs = ", ".join(f"{o.get('name')}({t('passive', '被动')} {o.get('passive')}, {t('evoke', '激发')} {o.get('evoke')})"
                         for o in st.get("orbs") or [])
        out.append(f"{t('Orbs', '充能球')} [{len(st.get('orbs') or [])}/{st.get('orb_slots')}]: {orbs or t('none', '无')}")
    osty = st.get("osty")
    if osty:
        out.append(f"{t('Osty', '奥斯提')}: " + (f"{t('HP', '生命')} {osty.get('hp')}/{osty.get('max_hp')} {t('Block', '格挡')} {osty.get('block')}"
                                if osty.get("alive") else t("dead", "死亡")))
    out.append(t("Enemies:", "敌人："))
    for e in st.get("enemies") or []:
        intents = " + ".join(intent_text(i) for i in e.get("intents") or []) or "?"
        blk = f" {t('Block', '格挡')} {e['block']}" if e.get("block") else ""
        out.append(f" E{e.get('index')} {e.get('name')}  {t('HP', '生命')} {e.get('hp')}/{e.get('max_hp')}{blk}  "
                   f"{t('Intent', '意图')}: {intents}")
        for i in e.get("intents") or []:
            if i.get("description"):
                out.append(f"     ({_one_line(i['description'])})")
        for x in powers_text(e.get("powers"), gl):
            out.append(f"     {x}")
    hand = st.get("hand") or []
    out.append(hand_title or t("Hand: (H#/E# renumber as cards/enemies leave; within one `do` chain they mean this view)",
                               "手牌：（卡牌/敌人离场后 H#/E# 会重新编号；同一条 do 指令链里它们指本视图中的牌和敌人）"))
    for c in hand:
        why = _one_line(c.get("unplayable_message") or ", ".join(c.get("unplayable_reasons") or [])).rstrip("。.")
        flag = "" if c.get("can_play", True) else (t(" [unplayable", " [不可打出") + (t(": ", "：") + why if why else "") + "]")
        dmg = ""
        # Per-target damage (what hovering each enemy shows), even with one enemy: Vulnerable,
        # Weak and other modifiers change it from the card text.
        if c.get("damage_by_target"):
            dmg = "  {" + ", ".join(f"E{d.get('target_index')}:{_first(d.get('total_damage'), d.get('damage'), '?')}"
                                   for d in c["damage_by_target"]) + "}"
        out.append(card_line(c, gl, prefix=f" H{c.get('index')} ") + flag + dmg)
    return out


def render_actions(st, numbered, compact=False):
    """The numbered legal actions. compact=True (used only when the full list would not fit
    in a reply) folds the one-per-card select actions into a single line; every action
    number stays usable and nothing is dropped."""
    out = [t("Actions:", "可选操作：")]
    discards = []
    selects = []
    for n, a in numbered:
        if a.get("action") == "discard_potion":
            discards.append(f"[{n}] P{(a.get('args') or {}).get('potion_index')}")
            continue
        if compact and a.get("action") in ("select_cards", "select_card_reward", "claim_reward",
                                           "select_treasure_relic", "buy_card") and "args" in a \
                and (a.get("args") or {}).get("indices") != "":
            selects.append((n, a))
            continue
        out.append(f" [{n}] {action_label(a, st, hints=True)}")
    groups = {}
    for n, a in selects:
        groups.setdefault(a["action"], []).append((n, a))
    pos = 1
    words = {"select_cards": ("select", "选择"), "select_card_reward": ("pick", "选择卡牌"), "claim_reward": ("claim", "领取"),
             "select_treasure_relic": ("take relic", "拿取遗物"), "buy_card": ("buy card", "购买卡牌")}
    for verb, items in groups.items():
        key = next(iter(items[0][1]["args"]))
        word = t(*words.get(verb, (verb, verb)))
        nums = [n for n, _ in items]
        vals = [items[i][1]["args"].get(key) for i in range(len(items))]
        ints = all(isinstance(v, int) or str(v).isdigit() for v in vals)
        if ints and nums == list(range(nums[0], nums[0] + len(nums))) \
                and [int(v) for v in vals] == list(range(int(vals[0]), int(vals[0]) + len(vals))):
            line = (f" [{nums[0]}-{nums[-1]}] {word} {vals[0]}-{vals[-1]} "
                    f"([{nums[0]}] = {word} {vals[0]}, [{nums[0] + 1}] = {word} {int(vals[0]) + 1}, ...)" if len(nums) > 1
                    else f" [{nums[0]}] {word} {vals[0]}")
        else:
            line = f" {word}: " + " ".join(f"[{n}] = {v}" for n, v in zip(nums, vals))
        out.insert(pos, line)
        pos += 1
    if discards:
        out.append(f" {t('discard potion', '丢弃药水')}{t(': ', '：')}{' '.join(discards)}")
    return out


# Top-level fields each decision's renderer already shows; anything else the engine sends
# is printed generically (so new fields reach the agent without a client change).
_COMMON_KEYS = {"type", "decision", "legal_actions", "player", "context", "events"}
_KNOWN_KEYS = {
    "combat_play": {"round", "energy", "max_energy", "stars", "draw_pile", "discard_pile", "exhaust_pile",
                    "draw_pile_count", "discard_pile_count", "exhaust_pile_count", "player_powers", "orbs",
                    "orb_slots", "osty", "enemies", "hand", "inactive_enemies"},
    "card_select": {"purpose", "source", "min_select", "max_select", "select_counts", "prompt", "screen", "cancelable",
                    "enchantment", "cards", "combat", "selection_id", "message", "softlocked", "softlock_reason"},
    "card_reward": {"cards", "can_skip", "alternatives", "from_event", "gold_earned", "selection_id"},
    "bundle_select": {"bundles", "selection_id"},
    "event_choice": {"event_name", "event_id", "description", "options", "dialogue", "finished", "popup",
                     "dialogue_pool", "dialogue_random", "softlocked", "softlock_reason", "message"},
    "rest_site": {"options", "can_leave", "softlocked", "softlock_reason", "message"},
    "shop": {"cards", "relics", "potions", "card_removal_cost", "card_removal_available"},
    "rewards": {"rewards", "potion_slots_full", "gold_earned", "source", "can_skip"},
    "treasure_relic": {"relics", "can_skip"},
    "map_select": {"act", "act_name", "choices", "floor", "free_travel_available"},
    "crystal_sphere": {"divinations_left", "width", "height", "rows", "hidden_cells", "items_seen", "tools"},
    "game_over": {"act", "floor", "victory", "killed_by", "score", "score_breakdown", "total_floor", "banner",
                  "death_quote", "victory_text", "score_lines", "badges", "score_progress", "discoveries",
                  "discoveries_header", "main_menu_button", "abandoned"},
}
# Per-card fields shown by card_line / the card_select renderer.
_CARD_KEYS = {"index", "uid", "id", "name", "cost", "cost_x", "star_cost", "type", "rarity", "description", "tips",
              "keywords", "upgraded", "upgrade_level", "enchantment", "enchantment_amount", "affliction",
              "affliction_amount", "after_upgrade", "after_enchant", "transforms_to", "transform_options", "stats",
              "can_play", "unplayable", "target_type", "damage_by_target", "vars", "count", "price", "on_sale",
              "is_stocked", "energy_cost", "card_cost", "reason", "upgradable", "base_cost", "cost_changed",
              "unplayable_reasons", "blocked_by", "costs_x", "x_value", "colorless", "unplayable_message",
              "cost_crossed", "uid", "play_count", "star_cost_x", "glow"}


def _short_json(v, limit=400):
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, separators=(",", ":"))
    s = _one_line(s)
    return s if len(s) <= limit else s[:limit] + "..."


def extra_fields(st, decision):
    known = _COMMON_KEYS | _KNOWN_KEYS.get(decision, set())
    out = []
    for k, v in st.items():
        if k in known or v is None or v == [] or v == {} or v == "":
            continue
        out.append(f"  {k}: {_short_json(v)}")
    return out


def card_extras(c):
    """Preview fields on a card the renderer has no special text for (after_*, preview...)."""
    out = []
    for k, v in c.items():
        if k in _CARD_KEYS or v is None or v == [] or v == {} or v == "":
            continue
        if isinstance(v, dict) and v.get("description"):
            out.append(f"{k}: {v.get('name', '')}{_desc(v.get('description'))}")
        elif isinstance(v, (str, int, float, bool)) and k not in ("is_locked",):
            out.append(f"{k}: {v}")
        else:
            out.append(f"{k}: {_short_json(v, 200)}")
    return (" || " + " | ".join(out)) if out else ""


def dialogue_lines(st):
    out = []
    for d in st.get("dialogue") or []:
        if not isinstance(d, dict) or not d.get("text"):
            continue
        who = t("You", "你") if d.get("speaker") == "character" else (st.get("event_name") or t("Ancient", "先古之民"))
        out.append(f"  {who}: {_one_line(d['text'])}")
    pool = st.get("dialogue_pool") or []
    if st.get("dialogue_random") and len(pool) > 1:
        # The game draws the ancient's dialogue at random (unseeded): any of these could show.
        out.append("  " + t(f"[the game picks one of {len(pool)} dialogues at random; the others:]",
                            f"[游戏会从 {len(pool)} 段对话中随机挑一段；其余几段：]"))
        shown = [d.get("key") for d in st.get("dialogue") or [] if isinstance(d, dict)]
        for p in pool:
            lines = [ln for ln in p.get("lines") or [] if isinstance(ln, dict)]
            if [ln.get("key") for ln in lines] == shown:
                continue
            out.append("    - " + " / ".join(_one_line(ln.get("text") or "") for ln in lines))
    return out


def crystal_sphere_lines(st):
    out = [f"CRYSTAL SPHERE  {t('divinations left', '剩余占卜')}: {st.get('divinations_left')}"]
    instr = loc("events", "CRYSTAL_SPHERE.minigame.instructions.description")
    if instr:
        out.append(f"  {_one_line(instr)}")
    big = loc("events", "CRYSTAL_SPHERE.button.DIVINATION_LABEL_BIG") or "Big Divination"
    small = loc("events", "CRYSTAL_SPHERE.button.DIVINATION_LABEL_SMALL") or "Small Divination"
    out.append("  " + t(f"Tools: big = {big}: clears the clicked cell and its 8 neighbours (3x3); "
                        f"small = {small}: clears only the clicked cell. Each click uses 1 divination; "
                        "items are received when the session ends.",
                        f"工具：big = {big}：揭开所点格子及其周围 8 格（3x3）；small = {small}：只揭开所点格子。"
                        "每次点击消耗 1 次占卜；物品在占卜结束时获得。"))
    out.append("  " + t("Legend: ? hidden  . empty  g gold  p potion  c card  r relic  x curse; "
                        "columns are x, rows are y",
                        "图例：? 未揭开  . 空  g 金币  p 药水  c 卡牌  r 遗物  x 诅咒；列为 x，行为 y"))
    rows = [r if isinstance(r, str) else "".join(map(str, r)) for r in st.get("rows") or []]
    width = st.get("width") or max((len(r) for r in rows), default=0)
    out.append("      " + " ".join(f"{x:>2}" for x in range(width)))
    for y, row in enumerate(rows):
        out.append(f"   {y:>2} " + " ".join(f"{ch:>2}" for ch in row))
    for it in st.get("items_seen") or []:
        cells = " ".join(f"({x},{y})" for x, y in it.get("revealed_cells") or [])
        ext = (f" {t('at', '位于')} ({it.get('x')},{it.get('y')}) {it.get('width')}x{it.get('height')}"
               if it.get("fully_revealed") else t(" (partly revealed)", "（部分揭开）"))
        out.append(f"  {t('item', '物品')} {it.get('id')}: {it.get('type')}{ext}; {t('seen at', '已见')} {cells}")
    return out


def game_over_lines(st):
    out = [(t("VICTORY", "胜利") if st.get("victory") else t("DEFEAT", "失败")) +
           f"  {t('Act', '幕')} {st.get('act')}  {t('Floor', '楼层')} {st.get('floor')}"
           + (f"  ({t('total floors', '总楼层')} {st['total_floor']})" if st.get("total_floor") is not None else "")]
    ids = run_id_parts(st.get("context") or {})
    if ids:
        out.append("  " + " | ".join(ids))
    kb = st.get("killed_by")
    if isinstance(kb, dict):
        out.append(f"  {t('Killed by', '死因')}: {kb.get('name')}" + (f" - {_one_line(kb['quote'])}" if kb.get("quote") else ""))
    if st.get("score") is not None:
        out.append(f"  {t('Score', '分数')}: {st['score']}")
        if not st.get("score_lines"):  # an older engine: the raw breakdown
            bd = st.get("score_breakdown") or {}
            if bd:
                out[-1] += " (" + ", ".join(f"{k} {v}" for k, v in bd.items()) + ")"
    out.extend(game_over.summary_lines(st, t))
    out.append(f"  ({t('sts2 start ... for a new run', 'sts2 start ... 开始新一局')})")
    return out


# The game screen a card selection is shown on (CardSelectContext.Screen).
_SCREEN_KIND = {"hand": ("cards in your hand", "手牌中的牌"), "deck": ("cards in your deck", "牌组中的牌"),
                "draw": ("cards in your draw pile", "抽牌堆中的牌"), "discard": ("cards in your discard pile", "弃牌堆中的牌"),
                "exhaust": ("cards in your exhaust pile", "消耗堆中的牌"), "grid": ("cards laid out in a grid", "卡牌平铺展示"),
                "choose_a_card": ("choose one of the cards shown", "从展示的卡牌中选择"),
                "reward_grid": ("cards laid out in a grid", "卡牌平铺展示")}


def count_text(lo, hi, counts=None):
    """'1' / '2-3' / '0 or 2': how many cards a selection takes."""
    if counts:
        xs = sorted(counts)
        if xs == list(range(xs[0], xs[-1] + 1)) and len(xs) > 1:
            return f"{xs[0]}-{xs[-1]}"
        return t(" or ", " 或 ").join(map(str, xs))
    if lo is None and hi is None:
        return "?"
    if lo == hi or hi is None:
        return str(lo)
    return f"{lo}-{hi}"


def select_count_text(st):
    st = st or {}
    return count_text(st.get("min_select"), st.get("max_select"), st.get("select_counts"))


def render_body(st, gl):
    d = st.get("decision")
    body = []
    if d == "combat_play":
        body = render_combat_core(st, gl)
        inactive = st.get("inactive_enemies") or []
        if inactive:
            body.append(t("Not targetable:", "不可选中：") + " " + ", ".join(
                f"{e.get('name')}" + (f" ({_one_line(e.get('reason'))})" if e.get("reason") else "") for e in inactive
                if isinstance(e, dict)))
    elif d == "card_select":
        purpose = st.get("purpose", "choose")
        src = (st.get("source") or {}).get("name")
        mn, mx = st.get("min_select"), st.get("max_select")
        body.append(f"{t('CARD SELECT', '选牌')} [{purpose_text(purpose)}] {_one_line(st.get('prompt')) or ''}".rstrip())
        scr = _SCREEN_KIND.get(st.get("screen"))
        nsel = select_count_text(st)
        body.append(f"  {t('select', '选择')} {nsel} {t('card' if nsel == '1' else 'cards', '张牌')}"
                    + (f" | {t('from', '来自')} {src}" if src else "")
                    + (f" | {t(*scr)}" if scr else (f" | {st['screen']}" if st.get("screen") else ""))
                    + (f" | {t('cancelable', '可取消')}" if st.get("cancelable") else ""))
        if st.get("select_counts") == [] and st.get("cancelable") and not st.get("softlocked"):
            # MarkUnconfirmableSelect: its confirm button can never be used.
            body.append("  " + t(f"This screen can't be confirmed: it asks for {select_count_text(dict(st, select_counts=None))} "
                                 f"{'card' if select_count_text(dict(st, select_counts=None)) == '1' else 'cards'} but shows only "
                                 f"{len(st.get('cards') or [])}; as in the game, the only way out is "
                                 "its close button (skip).",
                                 f"此界面无法确认：需要 {select_count_text(dict(st, select_counts=None))} 张牌，但只显示了 "
                                 f"{len(st.get('cards') or [])} 张；和游戏中一样，只能用关闭按钮离开（skip）。"))
        if st.get("enchantment"):
            en = st["enchantment"]
            body.append(f"  {t('Enchantment', '附魔')}: {en.get('name')} {en.get('amount') or ''}{_desc(en.get('description'))}")
        for c in st.get("cards") or []:
            line = card_line(c, gl, show_upgrade=purpose == "upgrade", prefix=f" {c.get('index')}) ")
            if c.get("after_enchant"):
                line += f" || {t('Enchanted', '附魔后')}: {_one_line(c['after_enchant'].get('description'))}"
            if c.get("transforms_to"):
                line += f" || {t('becomes', '变为')} {c['transforms_to'].get('name')}{_desc(c['transforms_to'].get('description'))}"
            elif c.get("transform_options"):
                line += f" || {t('becomes one of', '变为其一')}: {', '.join(map(str, c['transform_options']))}"
            body.append(line + card_extras(c))
        if st.get("combat"):
            body.append(t("-- battlefield --", "-- 战场 --"))
            body.extend(render_combat_core(st["combat"], gl, hand_title=t("Hand (now):", "当前手牌：")))
    elif d == "card_reward":
        body.append(t("CARD REWARD", "卡牌奖励") + (t(" (skippable)", "（可跳过）") if st.get("can_skip", True) else
                                                   t(" (cannot be skipped)", "（不可跳过）")))
        for c in st.get("cards") or []:
            body.append(card_line(c, gl, prefix=f" {c.get('index')}) ") + card_extras(c))
        for a in st.get("alternatives") or []:
            # Skip is the skip action (skip_card_reward), not an alternative to pick.
            if isinstance(a, dict) and a.get("id") != "Skip":
                body.append(f" {t('alternative', '其他选项')} {a.get('index')}) {a.get('name') or a.get('id')}{_desc(a.get('description'))}")
    elif d == "bundle_select":
        body.append(t("CHOOSE A PACK", "选择卡包"))
        for b in st.get("bundles") or []:
            body.append(f" {t('Pack', '卡包')} {b.get('index')}:")
            for c in b.get("cards") or []:
                body.append(card_line(c, gl, prefix="    "))
    elif d == "event_choice":
        body.append(f"{t('EVENT', '事件')}: {st.get('event_name')}")
        body.extend(dialogue_lines(st))
        if st.get("description"):
            body.append(f"  {_one_line(st['description'])}")
        if isinstance(st.get("popup"), dict):
            pop = st["popup"]
            body.append(f"  [{pop.get('header')}] {_one_line(pop.get('body') or '')}")
        for o in st.get("options") or []:
            lock = t(" [locked]", " [锁定]") if o.get("is_locked") else ""
            body.append(f" {o.get('index')}) {o.get('title')}{lock}{_desc(o.get('description'))}")
            gl.add(o.get("tips"))
    elif d == "rest_site":
        body.append(t("REST SITE", "休息处"))
        for o in st.get("options") or []:
            title = o.get("title") or rest_title(o)
            desc = o.get("description")
            if desc is None and o.get("option_id"):
                desc = loc("rest_site_ui", f"OPTION_{o['option_id']}.description")
                if desc and "{" in desc:  # unfilled template: the engine didn't send the real text
                    desc = None
            dis = t(" [disabled]", " [不可用]") if o.get("is_enabled") is False else ""
            body.append(f" {o.get('index')}) {title}{dis}{_desc(desc)}")
    elif d == "shop":
        if st.get("card_removal_available") is False:
            removal = t("card removal: used", "移除卡牌：已使用")
        else:
            removal = f"{t('card removal', '移除卡牌')}: {st.get('card_removal_cost')}g"
        body.append(t("SHOP", "商店") + f"  ({removal})")
        for c in st.get("cards") or []:
            if c.get("is_stocked") is False:
                continue
            sale = t(" SALE", " 打折") if c.get("on_sale") else ""
            c2 = dict(c, cost=c.get("energy_cost", c.get("card_cost")))
            body.append(card_line(c2, gl, prefix=f" card {c.get('index')}) {c.get('price', c.get('cost'))}g{sale} "))
        for r in st.get("relics") or []:
            if r.get("is_stocked") is False:
                continue
            body.append(f" relic {r.get('index')}) {r.get('price', r.get('cost'))}g {r.get('name')}{_desc(r.get('description'))}")
            gl.add(r.get("tips"))
        for p in st.get("potions") or []:
            if p.get("is_stocked") is False:
                continue
            body.append(f" potion {p.get('index')}) {p.get('price', p.get('cost'))}g {p.get('name')}{_desc(p.get('description'))}")
    elif d == "rewards":
        body.append(t("REWARDS", "奖励") + (t(" (potion slots full: discard a potion to take one)",
                                               "（药水栏已满：丢弃药水后才能领取药水）") if st.get("potion_slots_full") else "")
                    + t("  (claim N: these numbers stay fixed; claiming one does not renumber the rest)",
                        "（claim N：编号固定，领取后其余奖励不会重新编号）"))
        if st.get("can_skip") is False and st.get("rewards"):
            # RewardsSet.WithSkippingDisallowed (Neow's Bones): no Proceed until all are taken.
            body.append("  " + t("Every reward must be taken: there is no proceed until all are claimed.",
                                 "必须领取全部奖励：全部领取之前不能继续。"))
        for w in st.get("rewards") or []:
            nos = t(" (cannot be skipped)", "（不可跳过）") if w.get("type") == "card" and w.get("can_skip") is False else ""
            body.append(f" {w.get('index')}) {reward_text(w)}{nos}")
    elif d == "treasure_relic":
        body.append(t("TREASURE", "宝箱"))
        for r in st.get("relics") or []:
            body.append(f" {r.get('index')}) {r.get('name')}{_desc(r.get('description'))}")
            gl.add(r.get("tips"))
    elif d == "map_select":
        body.append(t("MAP: choose the next room (sts2 map: whole act)", "地图：选择下一个房间（sts2 map 查看整幕）"))
        if st.get("free_travel_available"):
            # The game's own words for it (modifiers FLIGHT.description).
            rule = loc("modifiers", "FLIGHT.description") or t("You may ignore paths when choosing the next room to travel to.",
                                                               "在选择每一层房间时可以无视路线。")
            body.append("  " + rule + t(" (moves off the paths are marked 'off path')", "（不在路线上的走法标有“无视路线”）"))
    elif d == "crystal_sphere":
        body.extend(crystal_sphere_lines(st))
    elif d == "game_over":
        body.extend(game_over_lines(st))
    else:
        body.append(f"{t('DECISION', '决策')}: {dec_name(d)}")
    body.extend(extra_fields(st, d))
    return body


def _size(lines):
    return sum(len(x.encode("utf-8")) + 1 for x in lines)


def render_decision(st, numbered, extra_events=None, prefix=None):
    """Text for one decision, at most MAX_OUT bytes. The legal actions are never cut: when
    something has to go, the glossary goes first, then older events, then the end of the body."""
    gl = Glossary()
    prefix = list(prefix or [])
    ev = event_lines(extra_events if extra_events is not None else st.get("events"))
    head = header(st)
    body = render_body(st, gl)
    actions = render_actions(st, numbered) if numbered else []
    if not numbered and st.get("decision") == "game_over":
        actions = [t("No actions: the run is over.", "没有可选操作：本局已结束。")]
    elif st.get("softlocked"):
        why = engine_text(st.get("softlock_reason") or "?")
        note = t(f"No way on: softlock ({why}); the game shows no button that moves on here"
                 + ("; only the potion belt works." if numbered else "."),
                 f"软锁（{why}）：游戏在这里没有能继续的按钮"
                 + ("；只能使用药水栏。" if numbered else "。"))
        actions = [note] + actions
    budget = MAX_OUT - 200
    if _size(prefix + head + actions) + _size(body) > budget and numbered:
        actions = render_actions(st, numbered, compact=True)
    room = budget - _size(prefix + head + actions)
    note = f"... ({t('cut to fit; use --json or the query commands for the rest', '已截断；用 --json 或查询命令查看其余部分')})"
    if _size(body) > room:
        keep = []
        for line in body:
            if _size(keep) + len(line.encode("utf-8")) + 1 > room - 200:
                break
            keep.append(line)
        body = keep + [note]
    room -= _size(body)
    ev_lines = []
    if ev:
        ev_lines = [t("Since last decision:", "上次决策以来："), *[f"  {x}" for x in ev]]
        if _size(ev_lines) > room:
            tail = []
            for line in reversed(ev_lines[1:]):
                if _size(tail) + len(line.encode("utf-8")) + 1 > room - 200:
                    break
                tail.insert(0, line)
            ev_lines = [ev_lines[0], "  ... (" + t("earlier events: sts2 log", "更早的事件：sts2 log") + ")"] + tail \
                if tail or room > 200 else []
    room -= _size(ev_lines)
    gloss = gl.render()
    out = prefix + ev_lines + head + body + actions
    if gloss and _size(gloss) < room:
        out += gloss
    return "\n".join(out)


def clip(text):
    b = text.encode("utf-8")
    if len(b) <= MAX_OUT:
        return text
    cut = b[:MAX_OUT].decode("utf-8", "ignore")
    return cut + f"\n... ({t('truncated; use --json or the query commands for the rest', '已截断；用 --json 或查询命令查看其余部分')})"


_MAP_SYM = {"Monster": "M", "Elite": "E", "RestSite": "R", "Shop": "$", "Treasure": "T", "Unknown": "?",
            "Event": "?", "Boss": "B", "Ancient": "A"}


def render_map(m, st):
    """The act map with column numbers on top and row numbers (the ROW of `go COL ROW`, plus
    the floor it is) on the left; each row with legal moves lists them at its right."""
    rows = [r for r in m.get("rows") or [] if r]
    sym = _MAP_SYM
    on_map = bool(st and st.get("decision") == "map_select")
    choices = {(c.get("col"), c.get("row")): c for c in (st or {}).get("choices") or []} if on_map else {}
    ctx = m.get("context") or (st or {}).get("context") or {}
    cur = m.get("current_coord") or {}
    # Floor of a row: the act's floor counter at the current node minus its row.
    off = (ctx.get("floor") - cur.get("row")) if isinstance(ctx.get("floor"), int) and isinstance(cur.get("row"), int) else None
    nodes = [n for n in [m.get("start")] if isinstance(n, dict)] + [n for r in rows for n in r]
    if isinstance(m.get("boss"), dict) and m["boss"].get("row") is not None:
        nodes.append(m["boss"])
    width = max([7] + [(n.get("col") or 0) + 1 for n in nodes])
    by_row = {}
    for n in nodes:
        by_row.setdefault(n.get("row"), []).append(n)
    lines = [t("Map (bottom = start; [x] visited, <x> you are here, *x* reachable now; "
               "M monster E elite R rest $ shop T treasure ? unknown B boss A ancient; "
               "a visited ? shows what it was in lowercase: m e $ t, ? = event)",
               "地图（底部为起点；[x] 已走过，<x> 当前位置，*x* 现在可去；M 敌人 E 精英 R 休息 $ 商店 T 宝箱 ? 未知 B Boss A 先古之民；"
               "去过的 ? 用小写显示实际房间：m e $ t，? 为事件）"),
           t("Columns (COL) are numbered on top, rows (ROW) on the left with their floor (f): go COL ROW.",
             "列号（COL）在上方，行号（ROW）及其楼层（f）在左侧：go COL ROW。")]
    if m.get("boss"):
        b = m["boss"]
        lines.append(f"Boss: {b.get('name') if isinstance(b, dict) else b}")
    if cur.get("col") is not None:
        here = next((n for n in nodes if n.get("col") == cur.get("col") and n.get("row") == cur.get("row")), {})
        lines.append(t("You are at", "当前位置") + f" ({cur.get('col')},{cur.get('row')}) "
                     + str(zh_term("room", here.get("revealed_type") or here.get("type")) or ""))
    if choices:
        lines.append(t("Legal now:", "现在可走：") + "  " + "   ".join(
            f"go {c} {r} = {zh_term('room', ch.get('type', '?'))}" for (c, r), ch in sorted(choices.items(), key=lambda kv: kv[0])))
    pad = "         "
    lines.append(pad + " ".join(f"{c:^3}" for c in range(width)).rstrip())
    for row_i in sorted((r for r in by_row if r is not None), reverse=True):
        cells = ["   "] * width
        links = [" "] * (width * 4)
        for n in by_row[row_i]:
            c = n.get("col") or 0
            s = sym.get(n.get("type"), "o")
            if n.get("revealed_type"):
                # A visited "?" shows what it turned out to be (lowercase), like the game's icon.
                s = sym.get(n["revealed_type"], "?").lower()
            if n.get("current") or (cur.get("col") == c and cur.get("row") == row_i):
                s = f"<{s}>"
            elif (c, row_i) in choices:
                s = f"*{s}*"
            elif n.get("visited"):
                s = f"[{s}]"
            else:
                s = f" {s} "
            cells[c] = s
            # Edges to the row above: | straight up, / up-right, \ up-left.
            for ch in n.get("children") or []:
                dc = (ch.get("col") or 0) - c
                p = c * 4 + 1 + (2 if dc > 0 else -2 if dc < 0 else 0)
                if 0 <= p < len(links):
                    links[p] = "|" if dc == 0 else ("/" if dc > 0 else "\\")
        fl = f"f{row_i + off}" if off is not None else ""
        here_opts = [f"go {c} {row_i}" for (c, r) in sorted(choices) if r == row_i]
        if "".join(links).strip():
            lines.append(pad + "".join(links).rstrip())
        lines.append(f"{row_i:>3} {fl:>4} " + " ".join(cells).rstrip()
                     + (f"   <- {', '.join(here_opts)}" if here_opts else ""))
    return "\n".join(lines)


# ───────────────────────────── verbs ─────────────────────────────

NATIVE = {
    "map_select": {"select_map_node"},
    "combat_play": {"play_card", "end_turn", "use_potion"},
    "card_reward": {"select_card_reward", "skip_card_reward", "select_card_alternative"},
    "card_select": {"select_cards", "skip_select"},
    "bundle_select": {"select_bundle"},
    "event_choice": {"choose_option", "leave_room"},
    "rest_site": {"choose_option", "leave_room"},
    "shop": {"buy_card", "buy_relic", "buy_potion", "remove_card", "leave_room"},
    "rewards": {"claim_reward", "proceed", "leave_room", "skip_rewards"},
    "crystal_sphere": {"crystal_sphere_click"},
    "treasure_relic": {"select_treasure_relic", "skip_treasure_relic", "proceed", "leave_room"},
    "unknown": {"proceed", "leave_room"},
}

ENGINE_VERBS = {
    "select_map_node", "play_card", "end_turn", "choose_option", "select_card_reward", "skip_card_reward",
    "buy_card", "buy_relic", "buy_potion", "remove_card", "select_bundle", "crystal_sphere_click",
    "select_cards", "skip_select", "use_potion", "discard_potion", "leave_room", "proceed",
    "claim_reward", "skip_rewards", "select_treasure_relic", "skip_treasure_relic", "select_card_alternative",
}


class VerbError(Exception):
    pass


class NoScreen(VerbError):
    """The step picks from a screen the current decision doesn't have (the message names
    the decision and lists the legal actions)."""


class BadNumber(VerbError):
    """An action number that was never in the list the step was read against."""


class StopStep(VerbError):
    """A step of a `do` chain that can't be applied safely to the current state (its H#/E#/P#
    or screen index no longer means what the agent saw): the chain stops before it."""


def _ident(i, *_):
    return i


class Refs:
    """Resolves the index references of `do` steps (H#, E#, P#, action numbers, choose / pick /
    claim / buy / select / go N) against the state that was shown when the chain was
    submitted, so a step never silently lands on a different card, enemy, potion or option:

    - H# / P#: the card / potion that had that index in the shown view, found again by its
      uid (a per-instance id the engine sends) at its current index; gone -> stop.
    - E#: the enemy shown as E#, found again by combat_id; dead or untargetable -> stop.
    - screen indices (choose N, pick N, claim N, buy card N, select i,j, go N, alt N, ...):
      only on the very screen instance that was shown (screen_sig: decision, room, source,
      purpose, prompt, the offered options' identities and the engine's selection_id) and
      only while the item at that index is still the same; otherwise stop. Two card
      selections with the same cards (Burning Pact's, then True Grit+'s) are different
      screens. A claimed reward / bought item is left to the engine, whose error says so.
    - action number N: the action numbered N in the shown list, remapped the same way.

    The first step of a chain is read against that very state, so nothing changes for it."""

    _SCREEN = {"choose_option": ("options", "option_index", "choose"), "select_card_reward": ("cards", "card_index", "pick"),
               "claim_reward": ("rewards", "reward_index", "claim"), "select_treasure_relic": ("relics", "relic_index", "relic"),
               "select_bundle": ("bundles", "bundle_index", "bundle"),
               "select_card_alternative": ("alternatives", "alternative_index", "alt"),
               "buy_card": ("cards", "card_index", "buy card"), "buy_relic": ("relics", "relic_index", "buy relic"),
               "buy_potion": ("potions", "potion_index", "buy potion")}

    def __init__(self, snap, numbered):
        self.snap = snap or {}
        self.numbered = list(numbered or [])
        self.first = True
        self.moved = []     # "H2 -> H0" notes for the current step
        self.typed = {}     # (prefix, current index) -> (typed index, name shown) for the current step

    @staticmethod
    def combat_of(st):
        st = st or {}
        if st.get("decision") == "combat_play":
            return st
        return st.get("combat") if isinstance(st.get("combat"), dict) else None

    def _instance(self, i, st, key, idkey, prefix, what):
        if self.first:
            return i
        if key == "potions":
            old_list = (self.snap.get("player") or {}).get("potions") or []
            cur_list = ((st or {}).get("player") or {}).get("potions") or []
        else:
            sc, cc = self.combat_of(self.snap), self.combat_of(st)
            if sc is None:
                raise StopStep(t(f"{prefix}{i}: no {what}s were shown when this chain started (it started at "
                                 f"{self.snap.get('decision')}); look at the new state first",
                                 f"{prefix}{i}：这条指令链开始时（{dec_name(self.snap.get('decision'))}）没有显示这些内容；请先查看新状态"))
            old_list, cur_list = sc.get(key) or [], (cc or {}).get(key) or []
        old = next((x for x in old_list if isinstance(x, dict) and x.get("index") == i), None)
        if old is None:
            have = ", ".join(f"{prefix}{x.get('index')} {x.get('name')}" for x in old_list if isinstance(x, dict)) or t("none", "无")
            raise StopStep(t(f"{prefix}{i} was not in the view this chain started from (it had: {have})",
                             f"{prefix}{i} 不在这条指令链开始时的视图中（当时有：{have}）"))
        oid = old.get(idkey)
        if oid is None:  # an engine without instance ids: only an unchanged list is safe
            same = [x.get("name") for x in old_list if isinstance(x, dict)] == [x.get("name") for x in cur_list if isinstance(x, dict)]
            if same:
                return i
            raise StopStep(t(f"{prefix}{i} ({old.get('name')}): the list changed and this engine sends no instance ids",
                             f"{prefix}{i}（{old.get('name')}）：列表已变化，且引擎没有提供实例编号"))
        cur = next((x for x in cur_list if isinstance(x, dict) and x.get(idkey) == oid), None)
        if cur is None:
            gone = {"card": t("is no longer in your hand (played, discarded, exhausted or transformed by an earlier step)",
                              "已不在手牌中（已被前面的步骤打出、丢弃、消耗或变化）"),
                    "enemy": t("is no longer a target (dead or untargetable)", "已不能被选为目标（死亡或不可选中）"),
                    "potion": t("is no longer in your potion belt", "已不在药水栏中")}[what]
            raise StopStep(t(f"{prefix}{i} ({old.get('name')}, as shown when this chain started) {gone}; "
                             f"not using a different {what} in its place",
                             f"{prefix}{i}（{old.get('name')}，指令链开始时显示的那个）{gone}；"
                             f"不会改用{ {'card': '另一张牌', 'enemy': '另一个敌人', 'potion': '另一瓶药水'}[what] }"))
        if cur.get("index") != i:
            self.moved.append(f"{prefix}{i} -> {prefix}{cur.get('index')}")
            self.typed[(prefix, cur.get("index"))] = (i, old.get("name"))
        return cur.get("index")

    def hand(self, i, st):
        return self._instance(i, st, "hand", "uid", "H", "card")

    def enemy(self, i, st):
        return self._instance(i, st, "enemies", "combat_id", "E", "enemy")

    def potion(self, i, st):
        return self._instance(i, st, "potions", "uid", "P", "potion")

    def screen(self, key, i, st, what):
        if self.first:
            return i
        self.same_screen(f"{what} {i}", st)
        old = next((x for x in self.snap.get(key) or [] if isinstance(x, dict) and x.get("index") == i), None)
        new = next((x for x in (st or {}).get(key) or [] if isinstance(x, dict) and x.get("index") == i), None)
        if old is None:
            if new is None:
                return i  # not there before either: the engine's error explains
            raise StopStep(t(f"{what} {i} was not on the screen this chain started from; it is now {_item_label(new)}",
                             f"{what} {i} 不在指令链开始时的界面上；现在是 {_item_label(new)}"))
        if new is None or new.get("is_stocked") is False:
            return i  # claimed / sold: the engine's error (with our explanation) says so
        if _item_key(old) != _item_key(new):
            raise StopStep(t(f"{what} {i} is now {_item_label(new)} (it was {_item_label(old)} when this chain started)",
                             f"{what} {i} 现在是 {_item_label(new)}（指令链开始时是 {_item_label(old)}）"))
        return i

    def same_screen(self, step, st, verb=("pick from", "选择")):
        """Stop unless `st` is the screen instance the chain started on, saying what differs."""
        if self.first or same_screen(self.snap, st):
            return
        dec = (st or {}).get("decision")
        diff = screen_diff(self.snap, st)
        if dec != self.snap.get("decision"):
            raise StopStep(t(f"'{step}' would {verb[0]} a {dec} screen this chain has not shown you: {diff}; look at it first",
                             f"'{step}' 会在指令链未显示过的{dec_name(dec)}上{verb[1]}：{diff}；请先查看"))
        raise StopStep(t(f"'{step}' would {verb[0]} a different {dec} screen than the one this chain started on: {diff}; "
                         "look at it first",
                         f"'{step}' 会在另一个{dec_name(dec)}上{verb[1]}，而不是指令链开始时的那个：{diff}；请先查看"))

    def numbered_action(self, n, st, act):
        """The action numbered n in the shown list, its references resolved against `st`."""
        a = act.get("action")
        args = dict(act.get("args") or {})
        if self.first:
            return {"action": a, **({"args": args} if args else {})}
        # Every action number belongs to the list printed with the state the chain started
        # from: it only runs on that very screen instance (rewards / shop: the items left of
        # it, see same_screen), never on a screen that opened since - even one that looks the
        # same (the removal screen closed by 'skip' and opened again by 'remove').
        self.same_screen(f"{n}", st, (f"run action {n} ({action_label(act, self.snap)}) on",
                                      f"执行操作 {n}（{action_label(act, self.snap)}）"))
        if a == "play_card":
            args["card_index"] = self.hand(args.get("card_index"), st)
        if a in ("use_potion", "discard_potion"):
            args["potion_index"] = self.potion(args.get("potion_index"), st)
        if "target_index" in args and a in ("play_card", "use_potion"):
            args["target_index"] = self.enemy(args["target_index"], st)
        if a == "select_cards" and str(args.get("indices", "")) != "":
            for x in str(args["indices"]).split(","):
                if x.strip().isdigit():
                    self.screen("cards", int(x), st, "select")
        if a in self._SCREEN:
            key, arg, what = self._SCREEN[a]
            self.screen(key, args.get(arg), st, what)
        return {"action": a, **({"args": args} if args else {})}


def _opt_id(x):
    """One offered option's identity: its instance id when the engine sends one."""
    if not isinstance(x, dict):
        return json.dumps(x, sort_keys=True)
    if x.get("uid") is not None:
        return ("uid", x["uid"], x.get("index"))
    return (x.get("index"),) + _item_key(x)


def screen_sig(st):
    """What identifies one screen instance (see Refs): two states with the same signature
    are the same screen as far as an index step can tell. Rewards / shop screens lose items
    as they are taken: same_screen compares their items as a subset."""
    st = st or {}
    d = st.get("decision")
    ctx = st.get("context") or {}
    room = (ctx.get("act"), ctx.get("floor"), ctx.get("total_floor"))
    src = st.get("source") if isinstance(st.get("source"), dict) else {"name": st.get("source")}
    opts = lambda key: tuple(_opt_id(x) for x in st.get(key) or [])
    if d == "card_select":
        return (d, room, st.get("selection_id"), src.get("name"), src.get("kind"), st.get("purpose"),
                st.get("prompt"), st.get("screen"), st.get("min_select"), st.get("max_select"), opts("cards"))
    if d == "card_reward":
        return (d, room, st.get("selection_id"), st.get("from_event"), opts("cards"), opts("alternatives"))
    if d == "bundle_select":
        return (d, room, st.get("selection_id"), opts("bundles"))
    if d == "event_choice":
        return (d, room, st.get("event_id") or st.get("event_name"), _one_line(st.get("description")),
                tuple((o.get("index"), o.get("title"), o.get("option_id"), o.get("text_key"))
                      for o in st.get("options") or [] if isinstance(o, dict)))
    if d == "rest_site":
        return (d, room, tuple((o.get("index"), o.get("option_id"), o.get("title"))
                               for o in st.get("options") or [] if isinstance(o, dict)))
    if d == "treasure_relic":
        return (d, room, opts("relics"))
    if d == "map_select":
        return (d, ctx.get("act"), ctx.get("floor"),
                tuple((c.get("col"), c.get("row")) for c in st.get("choices") or [] if isinstance(c, dict)))
    if d in ("rewards", "shop"):
        return (d, room, src.get("name"))
    return (d, room)


def same_screen(a, b):
    if screen_sig(a) != screen_sig(b):
        return False
    d = (a or {}).get("decision")
    if d in ("rewards", "shop"):
        # Taking an item removes it (or marks it sold): every item still listed must be one
        # that was shown, at the same index.
        for key in ("rewards",) if d == "rewards" else ("cards", "relics", "potions"):
            shown = {x.get("index"): _item_key(x) for x in (a or {}).get(key) or [] if isinstance(x, dict)}
            for x in (b or {}).get(key) or []:
                if isinstance(x, dict) and x.get("is_stocked") is not False and shown.get(x.get("index")) != _item_key(x):
                    return False
    return True


def screen_desc(st):
    """A short description of a screen instance, for "different screen" messages."""
    st = st or {}
    d = st.get("decision")
    dn = dec_name(d)
    if d == "card_select":
        src = (st.get("source") or {}).get("name") if isinstance(st.get("source"), dict) else st.get("source")
        bits = [f"[{purpose_text(st.get('purpose', 'choose'))}]"]
        if _one_line(st.get("prompt")):
            bits.append(_one_line(st.get("prompt")))
        if src:
            bits.append(t(f"from {src}", f"来自 {src}"))
        bits.append(t("cards: ", "卡牌：") + ", ".join(f"{c.get('index')} {c.get('name')}" for c in st.get("cards") or []
                                                      if isinstance(c, dict)))
        return f"{dn} " + " ".join(bits)
    if d == "card_reward":
        return f"{dn} " + ", ".join(f"{c.get('index')} {c.get('name')}" for c in st.get("cards") or [] if isinstance(c, dict))
    if d == "event_choice":
        return f"{dn}{' ' + str(st['event_name']) if st.get('event_name') else ''}: " + ", ".join(
            f"{o.get('index')} {o.get('title')}" for o in st.get("options") or [] if isinstance(o, dict))
    if d == "map_select":
        return f"{dn} " + ", ".join(f"go {c.get('col')} {c.get('row')}" for c in st.get("choices") or [] if isinstance(c, dict))
    if d == "rewards":
        return f"{dn}: {_items_text(st, 'rewards')}"
    if d == "treasure_relic":
        return f"{dn}: {_items_text(st, 'relics')}"
    return str(dn)


_PURPOSE_ZH = {"upgrade": "升级", "remove": "移除", "transform": "变化", "enchant": "附魔", "discard": "丢弃",
               "exhaust": "消耗", "choose": "选择"}


def purpose_text(p):
    return _PURPOSE_ZH.get(p, p) if LANG["lang"] == "zh" else p


_SELECT_SCREEN = {"remove": ("removal screen", "移除界面"), "upgrade": ("upgrade screen", "升级界面"),
                  "transform": ("transform screen", "变化界面"), "enchant": ("enchant screen", "附魔界面"),
                  "discard": ("discard screen", "丢弃界面"), "exhaust": ("exhaust screen", "消耗界面")}
_SCREEN_NAME = {"card_select": ("card selection screen", "选牌界面"), "game_over": ("game over screen", "结算界面"), "card_reward": ("card reward screen", "卡牌奖励界面"),
                "bundle_select": ("pack selection screen", "卡包选择界面"), "event_choice": ("event page", "事件页面"),
                "rest_site": ("rest site", "休息处"), "shop": ("shop", "商店"), "rewards": ("rewards screen", "奖励界面"),
                "treasure_relic": ("treasure chest", "宝箱"), "map_select": ("map", "地图"),
                "combat_play": ("combat turn", "战斗回合"), "crystal_sphere": ("Crystal Sphere", "水晶球")}


def dec_name(d):
    """A decision's name in messages: its id in English (as in --json), the screen's name in zh."""
    if LANG["lang"] != "zh":
        return str(d)
    return _SCREEN_NAME.get(d, (str(d), str(d)))[1]


def screen_name(st):
    """'the removal screen' / '移除界面': what one screen is, for messages."""
    st = st or {}
    d = st.get("decision")
    if d == "card_select" and st.get("purpose") in _SELECT_SCREEN:
        en, zh = _SELECT_SCREEN[st["purpose"]]
    else:
        en, zh = _SCREEN_NAME.get(d, (str(d), str(d)))
    return t(f"the {en}", zh)


def _items_text(st, key):
    return ", ".join(f"{x.get('index')} {_item_label(x).strip(chr(39))}" for x in (st or {}).get(key) or []
                     if isinstance(x, dict) and x.get("is_stocked") is not False) or t("none", "无")


def screen_diff(a, b):
    """What differs between the screen a chain was shown (a) and the one the game is at (b):
    the reason a step written for `a` does not run on `b`."""
    a, b = a or {}, b or {}
    da, db = a.get("decision"), b.get("decision")
    now_shown = t(f"now: {screen_desc(b)}; shown: {screen_desc(a)}", f"现在：{screen_desc(b)}；当时显示：{screen_desc(a)}")
    if da != db:
        return t(f"the game is now at {screen_desc(b)}, the chain started at {screen_desc(a)}",
                 f"游戏现在处于 {screen_desc(b)}，指令链开始于 {screen_desc(a)}")
    ca, cb = a.get("context") or {}, b.get("context") or {}
    if (ca.get("act"), ca.get("floor")) != (cb.get("act"), cb.get("floor")):
        return t(f"this is another room (act {cb.get('act')} floor {cb.get('floor')}; the chain started on floor "
                 f"{ca.get('floor')})", f"这是另一个房间（第{cb.get('act')}幕 楼层 {cb.get('floor')}；指令链开始于楼层 {ca.get('floor')}）")
    strip = lambda st: {k: v for k, v in st.items() if k != "selection_id"}
    if a.get("selection_id") != b.get("selection_id") and same_screen(strip(a), strip(b)):
        return t(f"{screen_name(b)} was closed and opened again (new selection) - it was not shown to you",
                 f"{screen_name(b)}已关闭并重新打开（新的选择）——它没有显示给你")
    bits = []
    src = lambda st: (st.get("source") or {}).get("name") if isinstance(st.get("source"), dict) else st.get("source")
    if src(a) != src(b):
        bits.append(t(f"it is from {src(b) or '?'} (the one shown was from {src(a) or '?'})",
                      f"它来自 {src(b) or '?'}（显示的那个来自 {src(a) or '?'}）"))
    if a.get("purpose") != b.get("purpose") or _one_line(a.get("prompt")) != _one_line(b.get("prompt")):
        bits.append(t(f"it asks '{_one_line(b.get('prompt')) or b.get('purpose')}' (shown: "
                      f"'{_one_line(a.get('prompt')) or a.get('purpose')}')",
                      f"它要求“{_one_line(b.get('prompt')) or purpose_text(b.get('purpose'))}”（显示的是“{_one_line(a.get('prompt')) or purpose_text(a.get('purpose'))}”）"))
    if da == "event_choice" and _one_line(a.get("description")) != _one_line(b.get("description")):
        bits.append(t("the event moved on to another page", "事件已进入另一页"))
    for key, (en, zh) in (("cards", ("cards", "卡牌")), ("options", ("options", "选项")), ("rewards", ("rewards", "奖励")),
                          ("relics", ("relics", "遗物")), ("potions", ("potions", "药水")), ("bundles", ("packs", "卡包")),
                          ("alternatives", ("alternatives", "其他选项")), ("choices", ("map choices", "地图选项"))):
        if key not in a and key not in b:
            continue
        if da in ("rewards", "shop"):
            shown = {x.get("index"): _item_key(x) for x in a.get(key) or [] if isinstance(x, dict)}
            new = [x for x in b.get(key) or [] if isinstance(x, dict) and x.get("is_stocked") is not False
                   and shown.get(x.get("index")) != _item_key(x)]
            if new:
                bits.append(t(f"it lists {en} that were not shown: " + ", ".join(f"{x.get('index')} {_item_label(x)}" for x in new),
                              f"它列出了没有显示过的{zh}：" + "，".join(f"{x.get('index')} {_item_label(x)}" for x in new)))
            continue
        ids = lambda st: [_opt_id(x) for x in st.get(key) or []] if key != "choices" else \
            [(c.get("col"), c.get("row")) for c in st.get(key) or [] if isinstance(c, dict)]
        if ids(a) != ids(b):
            if key == "choices":
                bits.append(t("the map choices differ", "地图选项不同"))
            elif [_item_key(x) for x in a.get(key) or [] if isinstance(x, dict)] == \
                    [_item_key(x) for x in b.get(key) or [] if isinstance(x, dict)]:
                bits.append(t(f"the same {en}, but other instances (another screen)", f"{zh}相同，但不是同一批（另一个界面）"))
            else:
                bits.append(t(f"other {en} ({_items_text(b, key)}; shown: {_items_text(a, key)})",
                              f"{zh}不同（{_items_text(b, key)}；当时显示：{_items_text(a, key)}）"))
    if not bits and a.get("selection_id") != b.get("selection_id"):
        bits.append(t("another selection (new selection id)", "另一个选择（新的 selection_id）"))
    if LANG["lang"] == "zh":
        return "；".join(bits) + (f"（{now_shown}）" if bits else now_shown)
    return "; ".join(bits) + (f" ({now_shown})" if bits else now_shown)


def _item_key(x):
    return tuple(json.dumps(x.get(k), sort_keys=True) for k in
                 ("id", "name", "title", "type", "description", "amount", "price", "cost", "upgraded", "enchantment",
                  "affliction", "col", "row", "card_count", "is_locked", "option_id", "cards"))


def _item_label(x):
    if x.get("type") in ("gold", "card") and "name" not in x:
        return reward_text(x)
    name = x.get("title") or x.get("name") or x.get("option_id") or x.get("id") or x.get("type")
    if x.get("col") is not None and x.get("row") is not None:
        return f"({x['col']},{x['row']}) {zh_term('room', x.get('type'))}"
    return f"'{name}'"


def index_range(indices, word=""):
    """'0-3' / '0, 2, 5' / 'none' for a list of indices: never an empty or negative range."""
    xs = sorted({x for x in indices if isinstance(x, int)})
    if not xs:
        return t("none", "无")
    if xs == list(range(xs[0], xs[-1] + 1)) and len(xs) > 2:
        return f"{word}{xs[0]}-{word}{xs[-1]}"
    return ", ".join(f"{word}{x}" for x in xs)


def legal_now(st, limit=12):
    """The legal actions of `st` as short labels (for "not here" errors)."""
    acts = [a for a in (st or {}).get("legal_actions") or [] if isinstance(a, dict)]
    labels = [f"[{n}] {action_label(a, st, hints=True)}" for n, a in enumerate(acts, 1)]
    if not labels:
        return t("none", "无")
    more = len(labels) - limit
    return t("; ", "；").join(labels[:limit]) + (t(f"; ... ({more} more: sts2 state)", f"；……（另有 {more} 项：sts2 state）") if more > 0 else "")


# verb -> (decisions it picks from, what it picks: en, zh)
_SCREEN_VERBS = {
    "choose": (("event_choice", "rest_site"), ("an event or rest site option", "事件或休息处选项")),
    "go": (("map_select",), ("a map choice", "地图选项")),
    "pick": (("card_reward", "treasure_relic", "rewards", "card_select", "bundle_select"),
             ("a card reward card, treasure relic, reward, card or pack", "卡牌奖励、宝箱遗物、奖励、卡牌或卡包")),
    "select": (("card_select",), ("cards on a card selection screen", "选牌界面上的卡牌")),
    "alt": (("card_reward",), ("a card reward alternative", "卡牌奖励的其他选项")),
    "claim": (("rewards",), ("a reward on the rewards screen", "奖励界面上的奖励")),
    "buy": (("shop",), ("a shop item", "商店商品")),
    "bundle": (("bundle_select",), ("a card pack", "卡包")),
    "relic": (("treasure_relic",), ("a treasure relic", "宝箱遗物")),
    "click": (("crystal_sphere",), ("a Crystal Sphere cell", "水晶球的格子")),
    "skip": (("card_reward", "card_select", "treasure_relic", "rewards"),
             ("a card reward, card selection, treasure or rewards screen", "卡牌奖励、选牌、宝箱或奖励界面")),
}


def _needs_screen(verb, step, st):
    """VerbError unless the current decision has what `verb` picks from."""
    decs, (en, zh) = _SCREEN_VERBS[verb]
    dec = (st or {}).get("decision")
    if dec in decs:
        return
    raise NoScreen(t(f"'{step}' picks {en}, but the current decision is {dec}: there is none here now. "
                      f"Legal now: {legal_now(st)}",
                      f"'{step}' 用于选择{zh}，但现在处于{dec_name(dec)}：这里没有这类选项。现在可选：{legal_now(st)}"))


_WHAT_ZH = {"card": "牌", "enemy": "敌人", "potion": "药水", "option": "选项", "choice": "地图选项", "index": "编号",
            "reward": "奖励", "alternative": "其他选项", "bundle": "卡包", "relic": "遗物", "col": "列号", "row": "行号"}


def _int(s, what):
    s = str(s).strip()
    if s[:1].upper() in ("H", "E", "P") and s[1:].isdigit():
        s = s[1:]
    try:
        return int(s)
    except ValueError:
        raise VerbError(t(f"{what} must be a number, got '{s}'", f"{_WHAT_ZH.get(what, what)}必须是数字，收到的是“{s}”")) from None


def _strip_quotes(s):
    s = str(s).strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        s = s[1:-1].strip()
    return s


def _same_card(a, b):
    keys = ("name", "description", "cost", "star_cost", "upgraded", "enchantment", "enchantment_amount",
            "affliction", "affliction_amount", "can_play", "target_type")
    return all(a.get(k) == b.get(k) for k in keys)


def _find_by_name(items, ref, what, prefix, remap=_ident):
    """Index of the item `ref` names: P3 / 3 / an exact name / a unique name prefix.
    Several matches are only accepted when they are indistinguishable (two identical
    Strikes): then any of them is the same play. Otherwise the step is ambiguous.
    remap(i) turns an index reference into the current index (see Refs)."""
    ref_s = _strip_quotes(ref)
    if ref_s[:1].upper() == prefix and ref_s[1:].isdigit():
        return remap(int(ref_s[1:]))
    if ref_s.lstrip("-").isdigit():
        return remap(int(ref_s))
    low = ref_s.lower()
    exact = [x for x in items if str(x.get("name", "")).lower() == low]
    pref = exact or [x for x in items if str(x.get("name", "")).lower().startswith(low)]
    if not pref:
        names = ", ".join(f"{prefix}{x.get('index')} {x.get('name')}" for x in items)
        raise VerbError(t(f"no {what} matching '{ref_s}' (have: {names or 'none'})",
                          f"没有与“{ref_s}”匹配的{_WHAT_ZH.get(what, what)}（现有：{names or '无'}）"))
    # Every match counts, playable or not: picking the playable one would play a card the
    # player did not name. Enemies with the same name are different targets; identical cards
    # (same text, cost and playability) are the same play.
    if len(pref) > 1 and (what != "card" or any(not _same_card(pref[0], x) for x in pref[1:])):
        names = ", ".join(f"{prefix}{x.get('index')} {x.get('name')}"
                          + ("" if what != "card" or x.get("can_play", True) else t(" (not playable)", "（不能打出）"))
                          for x in pref)
        raise VerbError(t(f"'{ref_s}' matches several {what}s ({names}); use the {prefix} number",
                          f"“{ref_s}”匹配到多个{_WHAT_ZH.get(what, what)}（{names}）；请用 {prefix} 编号"))
    if what == "card" and not pref[0].get("can_play", True):
        why = pref[0].get("unplayable_message") or ", ".join(pref[0].get("unplayable_reasons") or []) or "?"
        raise VerbError(t(f"{prefix}{pref[0].get('index')} {pref[0].get('name')} is not playable: {why}",
                          f"{prefix}{pref[0].get('index')} {pref[0].get('name')} 不能打出：{why}"))
    return min(x.get("index") for x in pref)


def _split_card_target(words, hand, enemies):
    """play <card words...> [target words...]: the longest run of leading words that names
    a card in hand is the card (multi-word names: Body Slam); the rest is the target. An
    explicit separator also works: play Body Slam -> Jaw Worm / play Body Slam @ E1."""
    words = [w for w in words]
    for sep in ("->", "@"):
        if sep in words[1:]:
            i = words.index(sep, 1)
            return " ".join(words[:i]), " ".join(words[i + 1:]) or None
    names = [str(c.get("name", "")).lower() for c in hand]
    for k in range(len(words), 0, -1):
        cand = _strip_quotes(" ".join(words[:k])).lower()
        if not cand:
            continue
        if (cand[:1] == "h" and cand[1:].isdigit()) or cand.isdigit() or any(n == cand or n.startswith(cand) for n in names):
            rest = " ".join(words[k:])
            return " ".join(words[:k]), (rest or None)
    return words[0], (" ".join(words[1:]) or None)


def parse_verb(text, st, numbered, refs=None):
    """One step of `sts2 do` -> engine action dict {action, args}. `refs` (a Refs) resolves
    index references against the state shown when the chain started; without it they are
    read against `st`."""
    if refs is None:
        refs = Refs(st, numbered)
    words = text.split()
    if not words:
        raise VerbError(t("empty step", "空步骤"))
    v = words[0].lower()
    a = words[1:]
    dec = (st or {}).get("decision")
    if v.isdigit() and not a:
        n = int(v)
        if not numbered:
            raise VerbError(t("no legal actions: the run is over (sts2 start ... for a new run)",
                              "没有可选操作：本局已结束（sts2 start ... 开始新一局）")
                            if dec == "game_over" else
                            t(f"softlock: {st.get('softlock_reason')} (the game shows no button here)",
                              f"软锁：{st.get('softlock_reason')}（游戏在这里没有可点的按钮）")
                            if (st or {}).get("softlocked") else t(f"no legal actions at {dec}", f"{dec_name(dec)}没有可选操作"))
        shown = refs.numbered if not refs.first else numbered
        for num, act in shown:
            if num == n:
                if "choose" in act or "choose_cell" in act:
                    raise VerbError(t(f"action {n} needs arguments: {action_label(act, refs.snap if not refs.first else st, True)}",
                                      f"操作 {n} 需要参数：{action_label(act, refs.snap if not refs.first else st, True)}"))
                return refs.numbered_action(n, st, act)
        have = index_range([m for m, _ in shown])
        if refs.first:
            raise BadNumber(t(f"no action number {n} (have {have}; run 'sts2 state')",
                              f"没有操作编号 {n}（现有：{have}；运行 'sts2 state'）"))
        # Numbers mean the list shown with the chain's state: this one never was a legal action.
        raise BadNumber(t(f"no action number {n} in the list this chain was given (it had {have}). Legal now: {legal_now(st)}",
                          f"这条指令链所依据的列表中没有操作编号 {n}（当时有：{have}）。现在可选：{legal_now(st)}"))
    if v == "raw":
        try:
            obj = json.loads(" ".join(a))
        except ValueError as e:
            raise VerbError(f"raw needs a JSON action object: {e}") from None
        return obj
    if v in ENGINE_VERBS:
        args = {}
        for kv in a:
            if "=" not in kv:
                raise VerbError(t(f"engine verb arguments are key=value, got '{kv}'", f"引擎动词的参数格式是 key=value，收到的是“{kv}”"))
            k, val = kv.split("=", 1)
            args[k] = int(val) if val.lstrip("-").isdigit() else val
        return {"action": v, **({"args": args} if args else {})}
    if v in ("play", "p"):
        if not a:
            raise VerbError("play <card: H0 | 0 | name> [target: E1 | 1 | name]")
        if dec != "combat_play":
            raise NoScreen(t(f"not in combat (decision: {dec}); 'play' plays a card from your hand",
                              f"不在战斗中（现在处于{dec_name(dec)}）；play 用于打出手牌"))
        hand = (st or {}).get("hand") or []
        enemies = (st or {}).get("enemies") or []
        card_ref, target_ref = _split_card_target(a, hand, enemies)
        args = {"card_index": _find_by_name(hand, card_ref, "card", "H", lambda i: refs.hand(i, st))}
        if target_ref:
            args["target_index"] = _find_by_name(enemies, target_ref, "enemy", "E", lambda i: refs.enemy(i, st))
        return {"action": "play_card", "args": args}
    if v in ("end", "e", "endturn", "end_turn"):
        return {"action": "end_turn"}
    if v in ("potion", "use", "pot"):
        if not a:
            raise VerbError("potion <P0 | 0> [target]")
        args = {"potion_index": refs.potion(_int(a[0], "potion"), st)}
        if len(a) > 1:
            args["target_index"] = _find_by_name((st or {}).get("enemies") or [], " ".join(a[1:]), "enemy", "E",
                                                 lambda i: refs.enemy(i, st))
        return {"action": "use_potion", "args": args}
    if v in ("discard", "toss"):
        return {"action": "discard_potion", "args": {"potion_index": refs.potion(_int(a[0] if a else "", "potion"), st)}}
    if v in ("choose", "opt", "option", "o", "c"):
        _needs_screen("choose", text, st)
        return {"action": "choose_option", "args": {"option_index": refs.screen("options", _int(a[0] if a else "", "option"), st, "choose")}}
    if v in ("go", "node", "n", "path"):
        a = " ".join(a).replace("(", " ").replace(")", " ").replace(",", " ").split()  # go (3,5) / go 3,5
        if len(a) >= 2:
            _needs_screen("go", text, st)
            return {"action": "select_map_node", "args": {"col": _int(a[0], "col"), "row": _int(a[1], "row")}}
        choices = (st or {}).get("choices") or []
        i = _int(a[0] if a else "", "choice")
        if not refs.first:
            # go N = the Nth listed move: only on the map screen the chain started from.
            if not same_screen(refs.snap, st):
                raise StopStep(t(f"'go {i}' means move {i} on the map you saw; the game is now at {screen_desc(st)} "
                                 "- use go COL ROW",
                                 f"'go {i}' 指你看到的地图上的第 {i} 个选项；游戏现在处于 {screen_desc(st)}——请用 go COL ROW"))
        _needs_screen("go", text, st)
        if not 0 <= i < len(choices):
            have = "; ".join(f"go {n} = go {c.get('col')} {c.get('row')} {zh_term('room', c.get('type'))}"
                             for n, c in enumerate(choices)) or t("none", "无")
            raise VerbError(t(f"there is no map choice {i}: the map has {len(choices)} choice(s) now ({have}); "
                              "or go COL ROW (sts2 map)",
                              f"没有地图选项 {i}：地图现在有 {len(choices)} 个选项（{have}）；也可以用 go COL ROW（sts2 map）"))
        return {"action": "select_map_node", "args": {"col": choices[i]["col"], "row": choices[i]["row"]}}
    if v in ("pick", "take"):
        n = _int(a[0] if a else "", "index")
        _needs_screen("pick", text, st)
        if dec == "card_reward":
            return {"action": "select_card_reward", "args": {"card_index": refs.screen("cards", n, st, "pick")}}
        if dec == "treasure_relic":
            return {"action": "select_treasure_relic", "args": {"relic_index": refs.screen("relics", n, st, "pick")}}
        if dec == "rewards":
            return {"action": "claim_reward", "args": {"reward_index": refs.screen("rewards", n, st, "pick")}}
        if dec == "card_select":
            return {"action": "select_cards", "args": {"indices": str(refs.screen("cards", n, st, "pick"))}}
        if dec == "bundle_select":
            return {"action": "select_bundle", "args": {"bundle_index": refs.screen("bundles", n, st, "pick")}}
    if v in ("select", "sel", "s"):
        if [x.lower() for x in a] in (["none"], ["-"]):
            _needs_screen("select", text, st)
            return {"action": "select_cards", "args": {"indices": ""}}  # confirm nothing (min_select 0)
        idx = ",".join(x for part in a for x in part.split(",") if x.strip())
        if not idx:
            raise VerbError("select <i,j,...> (or select none: confirm with no card when min_select is 0)")
        _needs_screen("select", text, st)
        bad = [x.strip() for x in idx.split(",") if not x.strip().isdigit()]
        if bad:
            raise VerbError(t(f"card numbers must be numbers: select 0,2 (got '{bad[0]}')",
                              f"卡牌编号必须是数字：select 0,2（收到的是“{bad[0]}”）"))
        for x in idx.split(","):
            if x.strip().isdigit():
                refs.screen("cards", int(x), st, "select")
        return {"action": "select_cards", "args": {"indices": idx}}
    if v == "skip":
        m = {"card_reward": "skip_card_reward", "card_select": "skip_select", "treasure_relic": "skip_treasure_relic",
             "rewards": "proceed"}
        # A min-0 screen without a cancel button: "skip" is confirming with nothing selected.
        if dec == "card_select" and not (st or {}).get("cancelable") and (st or {}).get("min_select") == 0:
            return {"action": "select_cards", "args": {"indices": ""}}
        _needs_screen("skip", text, st)
        if dec == "rewards" and not refs.first:
            # 'skip' here is proceed, which leaves the screen and gives up what is left on it:
            # only as the first step (the agent sees what it gives up), never later in a chain.
            left = [x for x in (st or {}).get("rewards") or [] if isinstance(x, dict)]
            if left:
                raise StopStep(t(f"'skip' on the rewards screen would be proceed, giving up the rewards not claimed "
                                 f"({_items_text(st, 'rewards')}); a later step of a chain never does that - claim them, "
                                 "or write 'proceed' to leave them",
                                 f"在奖励界面上 'skip' 等于继续，会放弃未领取的奖励（{_items_text(st, 'rewards')}）；"
                                 "指令链中后面的步骤不会这样做——请领取它们，或写 'proceed' 放弃它们"))
            raise StopStep(t("'skip' on the rewards screen would be proceed (leaving the screen); as a later step of a "
                             "chain it only skips a card reward - write 'proceed' to leave",
                             "在奖励界面上 'skip' 等于继续（离开此界面）；指令链中后面的 skip 只用于跳过卡牌奖励——"
                             "要离开请写 'proceed'"))
        return {"action": m[dec]}
    if v in ("alt", "alternative", "reroll"):
        _needs_screen("alt", text, st)
        i = refs.screen("alternatives", _int(a[0] if a else "", "alternative"), st, "alt")
        alts = [x for x in (st or {}).get("alternatives") or [] if isinstance(x, dict)]
        alt = next((x for x in alts if x.get("index") == i), None)
        if alt is None and not any(x.get("id") != "Skip" for x in alts):
            skip_n = next((n for n, x in enumerate((st or {}).get("legal_actions") or [], 1)
                           if isinstance(x, dict) and x.get("action") == "skip_card_reward"), None)
            if skip_n is not None:
                raise VerbError(t(f"this card reward has no alternatives (its only other button is Skip): "
                                  f"use 'skip' (action [{skip_n}]) or pick a card",
                                  f"这个卡牌奖励没有其他选项（除选牌外只有跳过按钮）：请用 'skip'（操作 [{skip_n}]）或选择一张牌"))
            raise VerbError(t(f"this card reward has no alternatives and can't be skipped: pick a card. Legal now: {legal_now(st)}",
                              f"这个卡牌奖励没有其他选项，也不能跳过：请选择一张牌。现在可选：{legal_now(st)}"))
        if alt and alt.get("id") == "Skip":
            # The game's Skip button is one of the reward's alternatives; it is the skip action.
            skip_n = next((n for n, x in enumerate((st or {}).get("legal_actions") or [], 1)
                           if isinstance(x, dict) and x.get("action") == "skip_card_reward"), None)
            if skip_n is None:
                raise VerbError(t(f"alt {i} is {alt.get('name') or 'Skip'}, and this card reward can't be skipped now. "
                                  f"Legal now: {legal_now(st)}",
                                  f"alt {i} 是{alt.get('name') or '跳过'}，而这个卡牌奖励现在不能跳过。现在可选：{legal_now(st)}"))
            return {"action": "skip_card_reward"}
        return {"action": "select_card_alternative", "args": {"alternative_index": i}}
    if v == "claim":
        _needs_screen("claim", text, st)
        return {"action": "claim_reward", "args": {"reward_index": refs.screen("rewards", _int(a[0] if a else "", "reward"), st, "claim")}}
    if v == "buy":
        if not a:
            raise VerbError("buy card|relic|potion <index>  (or buy c0 / r1 / p2)")
        kind, idx = (a[0], a[1] if len(a) > 1 else None)
        if idx is None and kind[:1].lower() in "crp" and kind[1:].isdigit():
            kind, idx = kind[:1], kind[1:]
        k = kind[:1].lower()
        verb = {"c": ("buy_card", "card_index"), "r": ("buy_relic", "relic_index"), "p": ("buy_potion", "potion_index")}.get(k)
        if not verb or idx is None:
            raise VerbError("buy card|relic|potion <index>")
        key = {"c": "cards", "r": "relics", "p": "potions"}[k]
        _needs_screen("buy", text, st)
        return {"action": verb[0], "args": {verb[1]: refs.screen(key, _int(idx, "index"), st, f"buy {key[:-1]}")}}
    if v in ("remove", "removal"):
        return {"action": "remove_card"}
    if v in ("proceed", "continue", "next"):
        return {"action": "proceed"}
    if v in ("leave", "exit"):
        return {"action": "leave_room"}
    if v in ("bundle", "pack"):
        _needs_screen("bundle", text, st)
        return {"action": "select_bundle", "args": {"bundle_index": refs.screen("bundles", _int(a[0] if a else "", "bundle"), st, "bundle")}}
    if v == "relic":
        _needs_screen("relic", text, st)
        return {"action": "select_treasure_relic", "args": {"relic_index": refs.screen("relics", _int(a[0] if a else "", "relic"), st, "relic")}}
    if v == "click":
        _needs_screen("click", text, st)
        if len(a) < 2:
            raise VerbError("click <x> <y> big|small")
        if len(a) < 3 or a[2].lower() not in ("big", "small"):
            raise VerbError("click <x> <y> big|small  (the tool is part of the choice: big = 3x3, small = 1 cell)")
        return {"action": "crystal_sphere_click", "args": {"x": _int(a[0], "x"), "y": _int(a[1], "y"),
                                                           "tool": a[2].lower()}}
    raise VerbError(t(f"unknown step '{text}' (sts2 help lists the verbs)", f"未知步骤“{text}”（sts2 help 列出所有动词）"))


_TARGET_TEXT = {"Self": ("targets yourself", "目标：自己"), "AnyEnemy": ("targets one enemy", "目标：单个敌人"),
                "AllEnemies": ("hits all enemies", "目标：所有敌人"), "RandomEnemy": ("hits a random enemy", "目标：随机敌人"),
                "None": ("no target", "无目标"), "AnyAlly": ("targets an ally", "目标：友方"),
                "AnyPlayer": ("targets a player", "目标：玩家"), "AllAllies": ("all allies", "目标：所有友方")}


def card_ref_text(c):
    """'Defend (Skill, 1 energy, targets yourself)': what a hand card is, for error messages."""
    name = c.get("name", "?")
    bits = []
    if LANG["lang"] == "zh" and str(c.get("id", "")).startswith("CARD."):
        en = _loc_lang("eng", "cards", c["id"][5:] + ".title")
        if en and en != name:
            bits.append(en)
    bits.append(str(zh_term("card_type", c.get("type")) or "?"))
    cost = "X" if c.get("cost_x") else c.get("cost")
    if cost is not None:
        bits.append(t(f"{cost} energy", f"{cost} 能量"))
    tt = c.get("target_type")
    if tt:
        en_zh = _TARGET_TEXT.get(tt)
        bits.append(t(*en_zh) if en_zh else tt)
    s = f"{name} ({', '.join(bits)})" if LANG["lang"] != "zh" else f"{name}（{'，'.join(bits)}）"
    if c.get("can_play") is False:
        why = str(c.get("unplayable_message") or ", ".join(c.get("unplayable_reasons") or [])).rstrip("。.")
        s += t(" - not playable" + (f": {why}" if why else ""), "，不能打出" + (f"：{why}" if why else ""))
    return s


# What a screen index names, for "there is no ... N" (the step word otherwise: buy card 0 is ...).
_REF_NOUN = {"select_card_reward": ("card", "卡牌"), "choose_option": ("option", "选项"), "claim_reward": ("reward", "奖励"),
             "select_treasure_relic": ("relic", "遗物"), "select_bundle": ("pack", "卡包"),
             "select_card_alternative": ("alternative", "其他选项"), "buy_card": ("card for sale", "出售的卡牌"),
             "buy_relic": ("relic for sale", "出售的遗物"), "buy_potion": ("potion for sale", "出售的药水")}


def describe_refs(action, st, rewards_seen=None, typed=None, map_bounds=None):
    """What each index in an engine action refers to in `st` (for error messages). `typed`
    (Refs.typed) maps a remapped index back to the one the step was written with, so the
    message talks about the index that was typed: "H2 (Defend as shown when the chain
    started, now H1) is ..."."""
    a = action.get("action")
    args = action.get("args") or {}
    out = []
    cb = Refs.combat_of(st) or {}
    typed = typed or {}

    def ref(prefix, i):
        if (prefix, i) not in typed:
            return f"{prefix}{i}"
        n, name = typed[(prefix, i)]
        return t(f"{prefix}{n} ({name} as shown when the chain started, now {prefix}{i})",
                 f"{prefix}{n}（指令链开始时显示的 {name}，现在是 {prefix}{i}）")

    def listing(items, prefix):
        return ", ".join(f"{prefix}{x.get('index')} {x.get('name') or x.get('title')}" for x in items if isinstance(x, dict)) \
            or t("none", "无")

    if "card_index" in args and a == "play_card":
        i, hand = args["card_index"], cb.get("hand") or []
        c = next((x for x in hand if x.get("index") == i), None)
        out.append((t(f"{ref('H', i)} is {card_ref_text(c)}", f"{ref('H', i)}是 {card_ref_text(c)}") if ("H", i) in typed else
                    t(f"H{i} is now {card_ref_text(c)}", f"H{i} 现在是 {card_ref_text(c)}")) if c else
                   t(f"there is no H{i} (hand: {listing(hand, 'H')})", f"没有 H{i}（手牌：{listing(hand, 'H')}）"))
    if "potion_index" in args and a in ("use_potion", "discard_potion"):
        i = args["potion_index"]
        pots = [x for x in (st.get("player") or {}).get("potions") or [] if isinstance(x, dict)]
        p = next((x for x in pots if x.get("index") == i), None)
        if p:
            out.append(t(f"{ref('P', i)} is{'' if ('P', i) in typed else ' now'} {p.get('name')} ({potion_tags(p)}"
                         + ("" if p.get("can_use", True) else ", can't be used now") + ")",
                         f"{ref('P', i)}{'是' if ('P', i) in typed else ' 现在是'} {p.get('name')}（{potion_tags(p)}"
                         + ("" if p.get("can_use", True) else "，现在不能使用") + "）"))
        else:
            out.append(t(f"there is no P{i} (potions: {listing(pots, 'P')})", f"没有 P{i}（药水：{listing(pots, 'P')}）"))
    if "target_index" in args:
        j, ens = args["target_index"], cb.get("enemies") or []
        e = next((x for x in ens if x.get("index") == j), None)
        out.append(t(f"{ref('E', j)} is {e.get('name')} (HP {e.get('hp')}/{e.get('max_hp')})", f"{ref('E', j)}{'' if ('E', j) in typed else ' '}是 {e.get('name')}（生命 {e.get('hp')}/{e.get('max_hp')}）")
                   if e else t(f"there is no E{j} (targets: {listing(ens, 'E')})", f"没有 E{j}（可选目标：{listing(ens, 'E')}）"))
    if a in Refs._SCREEN:
        key, arg, what = Refs._SCREEN[a]
        i = args.get(arg)
        items = [x for x in st.get(key) or [] if isinstance(x, dict)]
        x = next((y for y in items if y.get("index") == i), None)
        if a == "claim_reward" and x is None and i in (rewards_seen or {}):
            left = ", ".join(f"{y.get('index')} {reward_text(y) if y.get('type') in ('gold', 'card') else y.get('name') or y.get('type')}"
                             for y in items) or t("none", "无")
            out.append(t(f"reward {i} ({rewards_seen[i]}) was already claimed - reward numbers stay fixed after a claim "
                         f"(unclaimed: {left})",
                         f"奖励 {i}（{rewards_seen[i]}）已经领取过了——领取后奖励编号不变（未领取：{left}）"))
        elif x is None:
            have = ", ".join(str(y.get("index")) for y in items if y.get("is_stocked") is not False) or t("none", "无")
            en, zh = _REF_NOUN.get(a, (what, what))
            out.append(t(f"there is no {en} {i} here (have: {have})", f"这里没有{zh} {i}（现有：{have}）"))
        else:
            label = reward_text(x) if key == "rewards" else (x.get("title") or x.get("name") or rest_title(x) or "?")
            s = t(f"{what} {i} is {label}", f"{what} {i} 是 {label}")
            if x.get("is_stocked") is False:
                s = t(f"{what} {i} is sold out", f"{what} {i} 已售出")
            elif a.startswith("buy_") and x.get("price", x.get("cost")) is not None:
                s += t(f" ({x.get('price', x.get('cost'))}g; you have {(st.get('player') or {}).get('gold')}g)",
                       f"（{x.get('price', x.get('cost'))} 金币；你有 {(st.get('player') or {}).get('gold')} 金币）")
            if x.get("is_locked"):
                s += t(" [locked]", " [锁定]")
            if x.get("is_enabled") is False:
                s += t(" [disabled]", " [不可用]")
            out.append(s)
    if a == "select_cards" and str(args.get("indices", "")) != "":
        cards = {c.get("index"): c for c in st.get("cards") or [] if isinstance(c, dict)}
        for x in str(args["indices"]).split(","):
            x = x.strip()
            c = cards.get(int(x)) if x.isdigit() else None
            out.append(t(f"card {x} is {c.get('name')}", f"卡牌 {x} 是 {c.get('name')}") if c else
                       t(f"there is no card {x} (have: {index_range(cards)})", f"没有卡牌 {x}（现有：{index_range(cards)}）"))
    if a == "select_map_node":
        c, r = args.get("col"), args.get("row")
        if map_bounds and isinstance(c, int) and isinstance(r, int) and not (
                0 <= c <= map_bounds[0] and 0 <= r <= map_bounds[1]):
            out.append(t(f"({c},{r}) is outside the map (columns 0-{map_bounds[0]}, rows 0-{map_bounds[1]}; sts2 map)",
                         f"({c},{r}) 在地图之外（列 0-{map_bounds[0]}，行 0-{map_bounds[1]}；sts2 map）"))
        else:
            out.append(t(f"({c},{r}) is not a move you can make now (sts2 map shows COL/ROW)",
                         f"({c},{r}) 现在不能前往（sts2 map 显示列/行号）"))
    # One line per referent: "select 0,0" names card 0 once.
    return list(dict.fromkeys(out))


def map_bounds(m):
    """(last column, last row) of an act map (get_map), or None."""
    if not isinstance(m, dict) or m.get("type") == "error":
        return None
    nodes = [n for r in m.get("rows") or [] if r for n in r if isinstance(n, dict)]
    nodes += [n for n in (m.get("start"), m.get("boss")) if isinstance(n, dict) and n.get("row") is not None]
    cols = [n.get("col") for n in nodes if isinstance(n.get("col"), int)]
    rows = [n.get("row") for n in nodes if isinstance(n.get("row"), int)]
    if not cols or not rows:
        return None
    return max(max(cols), 6), max(rows)


# Every engine error message (RunSimulator / StrictActions / AgentProtocol / CombatParity /
# Program: tests/test_sts2_errors.py checks them all) -> one explanation in the output
# language. (pattern, en, zh, kind), matched case-insensitively against the whole message
# (or the reason inside "'x' is not allowed now: ..."). en / zh are str.format templates over
# the pattern's groups plus: {v} the step word of the group 'a' (play, potion, select...),
# {kh} / {kh_zh} what the arg 'k' is, {w_zh} the zh of the place 'w'. kind:
#   "index"   says an index doesn't exist: describe_refs already says it (dropped then)
#   "count"   a card selection's count
#   "target"  / "notarget": a target is missing / given to something that takes none
_WHERE_ZH = {"the map": "地图", "your combat turn": "你的战斗回合", "the card reward screen": "卡牌奖励界面",
             "the card selection screen": "选牌界面", "the pack selection screen": "卡包选择界面", "this event": "此事件",
             "the rest site": "休息处", "the shop": "商店", "the rewards screen": "奖励界面", "the treasure chest": "宝箱",
             "the Crystal Sphere minigame": "水晶球小游戏"}
# Engine verb -> the step that sends it (what the player types).
_STEP_OF = {"play_card": "play", "use_potion": "potion", "discard_potion": "discard", "choose_option": "choose",
            "select_card_reward": "pick", "select_cards": "select", "claim_reward": "claim", "buy_card": "buy card",
            "buy_relic": "buy relic", "buy_potion": "buy potion", "remove_card": "remove", "select_map_node": "go",
            "select_bundle": "bundle", "select_treasure_relic": "relic", "skip_card_reward": "skip",
            "skip_select": "skip", "skip_treasure_relic": "skip", "skip_rewards": "proceed", "proceed": "proceed",
            "leave_room": "leave", "end_turn": "end", "select_card_alternative": "alt", "crystal_sphere_click": "click"}
_ARG_TEXT = {"card_index": ("a card (H#)", "一张牌（H#）"), "target_index": ("a target enemy (E#)", "一个目标敌人（E#）"),
             "target_id": ("a target enemy", "一个目标敌人"), "potion_index": ("a potion (P#)", "一瓶药水（P#）"),
             "option_index": ("an option number", "选项编号"), "reward_index": ("a reward number", "奖励编号"),
             "relic_index": ("a relic number", "遗物编号"), "bundle_index": ("a pack number", "卡包编号"),
             "alternative_index": ("an alternative number", "其他选项编号"), "col": ("a column", "列号"),
             "row": ("a row", "行号"), "indices": ("card numbers", "卡牌编号"), "x": ("x", "x"), "y": ("y", "y"),
             "tool": ("a tool (big|small)", "工具（big|small）"), "deck_index": ("a deck index", "牌组编号"),
             "hand_index": ("a hand index", "手牌编号")}
_I = r"(?P<i>-?\w+)"
_REASONS = [
    # ── StrictActions.CheckLegal / ReasonFor (inside "'x' is not allowed now: ...") ──
    (r"cannot play (?:card )?(?P<name>.*?): (?P<msg>.*?)(?: \((?P<code>[A-Za-z, ]+)\))?",
     "not playable: {msg}", "不能打出：{msg}", None),
    (r"invalid card index '?" + _I + r"'?, hand has (?P<n>\d+) cards", "there is no H{i} (the hand has {n} cards)",
     "没有 H{i}（手牌只有 {n} 张）", "index"),
    (r"'target_index' does not apply: it does not target a single enemy", "", "", "notarget"),
    (r"target_index \S+: no targetable enemies", "there is no enemy to target", "没有可以选为目标的敌人", None),
    (r"target_index (?P<i>\S+) out of range \((?P<r>.*)\)", "there is no E{i} (targets: E{r})", "没有 E{i}（目标：E{r}）", "index"),
    (r"invalid target_index (?P<i>.*)", "the target must be an E number, got {i}", "目标必须是 E 编号，收到的是 {i}", None),
    (r"invalid target_id (?P<i>.*)", "the target id must be a number, got {i}", "目标 id 必须是数字，收到的是 {i}", None),
    (r"target_id (?P<i>\S+) is not a targetable enemy \(targetable ids: (?P<ids>.*)\)",
     "enemy id {i} can't be targeted (targetable: {ids})", "敌人 id {i} 不能被选为目标（可选：{ids}）", None),
    (r"target_id (?P<i>\S+) and target_index (?P<j>\S+) name different enemies",
     "target id {i} and E{j} are different enemies", "目标 id {i} 和 E{j} 不是同一个敌人", None),
    (r"no potion at index " + _I, "there is no P{i}", "没有 P{i}", "index"),
    (r"no reward at index " + _I, "there is no reward {i}", "没有奖励 {i}", "index"),
    (r"no item at index " + _I, "there is no item {i} here", "这里没有商品 {i}", "index"),
    (r"no option at index " + _I, "there is no option {i}", "没有选项 {i}", "index"),
    (r"node \((?P<c>.*),(?P<r>.*)\) is not reachable from here", "({c},{r}) is not a move you can make now",
     "({c},{r}) 现在不能前往", "index"),
    (r"sold out", "it is sold out", "已售出", "index"),
    (r"combat potions can't be used during a card selection", "combat potions can't be used during a card selection",
     "选牌时不能使用战斗药水", None),
    (r"this potion can only be used in combat", "this potion can only be used in combat", "这瓶药水只能在战斗中使用", None),
    (r"this potion is not used by hand \(usage (?P<u>\w+)\)", "this potion is not used by hand ({u_h})",
     "这瓶药水不能手动使用（{u_h}）", None),
    (r"this potion is already being used \(queued\)", "this potion is already being used", "这瓶药水正在使用中", None),
    (r"this potion can't be used now", "this potion can't be used now", "这瓶药水现在不能使用", None),
    (r"potions are locked here", "potions can't be used or discarded here", "这里不能使用或丢弃药水", None),
    (r"this potion needs an enemy target: use it in combat", "this potion needs an enemy target: use it in combat",
     "这瓶药水需要敌人作为目标：请在战斗中使用", None),
    (r"potion slots are full: discard(?:_potion| a potion) first, or proceed to leave this potion",
     "your potion slots are full: discard a potion first (or proceed and leave this potion)",
     "药水栏已满：请先丢弃一瓶药水（或继续前进，放弃这瓶药水）", None),
    (r"potion slots are full: discard(?:_potion| a potion) first", "your potion slots are full: discard a potion first",
     "药水栏已满：请先丢弃一瓶药水", None),
    (r"potion slots are full", "your potion slots are full", "药水栏已满", None),
    (r"not enough gold", "not enough gold", "金币不足", None),
    (r"this option is locked", "this option is locked", "该选项已锁定", None),
    (r"it is not available on (?P<w>.*)", "it is not available on {w}", "在{w_zh}不能执行此操作", None),
    (r"the run is over", "the run is over", "本局已结束", None),
    (r"this screen can't be confirmed with the cards it shows; leave it with skip_select",
     "this screen can't be confirmed with the cards it shows; leave it with skip",
     "此界面无法用现有的牌确认；请用 skip 离开", None),
    (r"this screen can't be confirmed with the cards it shows and has no close button \(softlock\)",
     "this screen can't be confirmed with the cards it shows and has no close button (softlock)",
     "此界面无法用现有的牌确认，也没有关闭按钮（软锁）", None),
    (r"the screen has no cancel button; confirm with nothing selected.*",
     "the screen has no cancel button; confirm with nothing selected: select none",
     "此界面没有取消按钮；不选任何牌直接确认：select none", None),
    (r"this selection requires a choice \(it is not cancelable\)", "this selection requires a choice (it can't be cancelled)",
     "此处必须做出选择（不可取消）", None),
    (r"a choice is still required here", "a choice is still required here", "这里还需要做出选择", None),
    (r"'target_index' does not apply to (?P<a>\w+) here", "", "", "notarget"),
    (r"'(?P<k>\w+)' does not apply to (?P<a>\w+) here", "'{v}' takes no {kh} here", "{v} 在这里不接受{kh_zh}", None),
    (r"(?P<a>\w+) needs target_index(?: \(several are legal\))?", "", "", "target"),
    (r"(?P<a>\w+) needs (?P<k>.*?)(?: \(several are legal\))?", "'{v}' needs {kh}", "{v} 需要{kh_zh}", None),
    (r"it is not one of the legal actions", "it is not one of the legal actions", "这不是合法操作", None),
    # ── AgentProtocol.Act ──
    (r"unknown action '(?P<x>.*?)'\.(?: did you mean '(?P<s>\w+)'\?)?(?: legal now: .*)?",
     "unknown engine action '{x}'{s_hint}", "未知的引擎操作“{x}”{s_hint}", None),
    (r"'?(?P<k>[\w, ']+?)'? (?:is not an argument|are not arguments) of (?P<a>\w+) \(it takes (?P<takes>.*)\)",
     "{k} is not an argument of {a} (it takes: {takes})", "{k} 不是 {a} 的参数（它接受：{takes}）", None),
    (r"args\.(?P<k>\w+) must be an integer, got (?P<x>.*)", "{kh} must be a number, got {x}", "{kh_zh}必须是数字，收到的是 {x}", None),
    (r"'(?P<a>\w+)' rejected: the current decision could not be determined",
     "the game's current decision could not be read; nothing was done", "无法读取游戏当前的决策；没有执行任何操作", None),
    (r"'(?P<a>\w+)' rejected: no decision is waiting \((?P<x>.*)\)", "the game is not waiting for a decision ({x}); nothing was done",
     "游戏没有在等待决策（{x}）；没有执行任何操作", None),
    # ── RunSimulator / RoomsParity / EventFlow handlers ──
    (r"unknown action: (?P<x>.*)", "unknown engine action '{x}'", "未知的引擎操作“{x}”", None),
    (r"(?P<a>\w+) requires (?P<k>'\w+'(?: and '\w+')?)(?: and optionally .*| \(.*\))?", "'{v}' needs {kh}", "{v} 需要{kh_zh}", None),
    (r"select_map_node is only available at map_select", "moves are made on the map screen", "只能在地图界面上移动", None),
    (r"select_map_node: 'col' and 'row' must be integers", "go needs a column and a row (numbers): go COL ROW",
     "go 需要列号和行号（数字）：go COL ROW", None),
    (r"no map available", "there is no map now", "现在没有地图", None),
    (r"map node \((?P<c>.*),(?P<r>.*)\) is not reachable; choices: .*", "({c},{r}) is not a move you can make now",
     "({c},{r}) 现在不能前往", "index"),
    (r"not in combat", "not in combat", "不在战斗中", None),
    (r"cannot play cards now: player phase is (?P<x>\w+), not play", "cards can't be played now (the turn is changing: {x})",
     "现在不能打出卡牌（回合正在切换：{x}）", None),
    (r"(?:card|potion) (?P<x>\S+) targets a single enemy \(anyenemy\); 'target_index' is required when multiple enemies are alive.*",
     "", "", "target"),
    (r"card could not be played \(still in hand after action\): .*", "the game did not play the card (it is still in your hand)",
     "游戏没有打出这张牌（它仍在手牌中）", None),
    (r"invalid card index '?" + _I + r"'? \(valid: (?P<r>.*)\)", "there is no card {i} (valid: {r})", "没有卡牌 {i}（有效编号：{r}）", "index"),
    (r"invalid card index " + _I + r", (?P<n>\d+) cards available", "there is no card {i} ({n} offered)",
     "没有卡牌 {i}（共 {n} 张可选）", "index"),
    (r"invalid card index '?" + _I + r"'?", "there is no card {i}", "没有卡牌 {i}", "index"),
    (r"card index (?P<i>\d+) given twice", "card {i} is listed twice: each card can be selected once",
     "卡牌 {i} 列了两次：每张牌只能选一次", None),
    (r"select (?P<c>.+?) cards \(got (?P<g>\d+)\)", "this screen takes {c_h} {cw}, not {g}", "此界面需要选择 {c_h} 张牌，而不是 {g} 张", "count"),
    (r"this screen can't be confirmed with nothing selected: choose (?P<c>.+?) card\(s\)(?P<sk> \(or leave with skip_select\))?",
     "this screen can't be confirmed with nothing selected: choose {c_h} {cw}{sk_en}",
     "此界面不能在不选牌时确认：请选择 {c_h} 张牌{sk_zh}", "count"),
    (r"this screen can't be confirmed: it asks for (?P<c>\S+) card\(s\) but shows only (?P<n>\d+)(?P<sk>; leave with skip_select)?.*",
     "this screen can't be confirmed: it asks for {c_h} {cw} but shows only {n}{sk_en2}",
     "此界面无法确认：需要 {c_h} 张牌，但只显示了 {n} 张{sk_zh2}", "count"),
    (r"this screen has no cancel button: confirm with nothing selected.*",
     "this screen has no cancel button: confirm with nothing selected (select none)",
     "此界面没有取消按钮：不选任何牌直接确认（select none）", None),
    (r"this selection can't be skipped: choose at least (?P<n>\d+) card\(s\).*",
     "this selection can't be skipped: choose at least {n} card(s) (select i,j,...)",
     "此选择不能跳过：至少选择 {n} 张牌（select i,j,...）", None),
    (r"no pending card reward", "there is no card reward open now", "现在没有打开的卡牌奖励", None),
    (r"this card reward can't be skipped", "this card reward can't be skipped: pick a card", "这个卡牌奖励不能跳过：请选择一张牌", None),
    (r"invalid alternative index " + _I + r", (?P<n>\d+) alternatives available", "there is no alternative {i} ({n} available)",
     "没有其他选项 {i}（共 {n} 个）", "index"),
    (r"not in a shop", "not in a shop", "不在商店中", None),
    (r"(?P<x>card|relic|potion) already purchased", "that {x_h} was already bought", "这件{x_zh}已经买过了", None),
    (r"buy (?:card|relic) failed: (?P<x>.*)", "buying failed: {x}", "购买失败：{x}", None),
    (r"invalid (?P<x>relic|potion|bundle|reward|option) index " + _I + r"(?:, (?P<n>\d+) (?:relics offered|bundles available|options? available))?(?: \(.*\))?",
     "there is no {x_h} {i}{n_en}", "没有{x_zh} {i}{n_zh}", "index"),
    (r"no card removal available", "this shop offers no card removal", "这个商店不提供移除卡牌", None),
    (r"card removal was already used at this shop", "card removal was already used at this shop", "这个商店的移除卡牌服务已经用过了", None),
    (r"remove card failed: (?P<x>.*)", "removing a card failed: {x}", "移除卡牌失败：{x}", None),
    (r"no pending bundle selection", "there is no pack to choose now", "现在没有可选的卡包", None),
    (r"no pending card selection", "there is no card selection open now", "现在没有打开的选牌界面", None),
    (r"select_cards requires 'indices'.*", "select needs card numbers: select i,j (or select none)",
     "select 需要卡牌编号：select i,j（或 select none）", None),
    (r"potion (?P<x>\S+) can't be used manually \(usage: (?P<u>\w+)\)", "this potion is not used by hand ({u_h})",
     "这瓶药水不能手动使用（{u_h}）", None),
    (r"potion (?P<x>\S+) can't be used now: (?P<why>.*)", "this potion can't be used now: {why}", "这瓶药水现在不能使用：{why}", None),
    (r"invalid target for potion (?P<x>\S+) \((?P<tt>\w+)\)", "this potion can't be used on that target ({tt_h})",
     "这瓶药水不能用于该目标（{tt_h}）", None),
    (r"potion (?P<x>\S+) use was cancelled; the potion was kept", "the game cancelled the potion; you still have it",
     "游戏取消了这次使用；药水仍在", None),
    (r"potion (?P<x>\S+) use failed \((?P<why>.*)\); the potion was kept", "using the potion failed ({why}); you still have it",
     "使用药水失败（{why}）；药水仍在", None),
    (r"potions can't be used or discarded right now", "potions can't be used or discarded right now", "现在不能使用或丢弃药水", None),
    (r"potion (?P<x>\S+) is being used", "this potion is already being used", "这瓶药水正在使用中", None),
    (r"rest site option (?P<i>\S+) is not available", "rest site option {i} is not available", "休息处选项 {i} 不可用", None),
    (r"the fake merchant has no options: .*", "the Fake Merchant has no options: buy a relic, use a potion or leave",
     "假商人没有选项：购买遗物、使用药水或离开", None),
    (r"option (?P<i>\S+) is locked", "option {i} is locked", "选项 {i} 已锁定", None),
    (r"the event isn't over: choose one of its options", "the event isn't over: choose one of its options",
     "事件尚未结束：请选择一个选项", None),
    (r"the event isn't over and the game shows no way out \(softlock\).*", "the event isn't over and the game shows no way out (softlock)",
     "事件尚未结束，游戏也没有提供出路（软锁）", None),
    (r"choose a rest site option first.*", "choose a rest site option first: the rest site can be left only after one is used",
     "请先选择一个休息处选项：用过一个之后才能离开", None),
    (r"every rest site option is disabled and the game shows no proceed \(softlock\).*",
     "every rest site option is disabled and the game shows no Proceed (softlock)", "所有休息处选项都不可用，游戏也没有显示继续（软锁）", None),
    (r"crystal_sphere_click requires 'x', 'y' and optionally 'tool' \(big\|small\)", "click needs X Y big|small",
     "click 需要 X Y big|small", None),
    (r"force_leave_room .*", "force_leave_room is not game behaviour (STS2_SOFTLOCK_ESCAPE=1 engines only)",
     "force_leave_room 不是游戏行为（仅限设置了 STS2_SOFTLOCK_ESCAPE=1 的引擎）", None),
    (r"no crystal sphere minigame in progress", "no Crystal Sphere minigame is in progress", "现在没有进行中的水晶球小游戏", None),
    (r"unknown tool '(?P<x>.*)' \(big\|small\)", "unknown tool '{x}': big or small", "未知工具“{x}”：big 或 small", None),
    (r"cell \((?P<x>\S+),(?P<y>\S+)\) is outside the (?P<w>\d+)x(?P<h>\d+) grid",
     "({x},{y}) is outside the {w}x{h} grid (x 0-{w1}, y 0-{h1})", "({x},{y}) 在 {w}x{h} 网格之外（x 0-{w1}，y 0-{h1}）", None),
    (r"cell \((?P<x>\S+),(?P<y>\S+)\) is already cleared", "({x},{y}) is already revealed", "({x},{y}) 已经揭开", None),
    (r"no rewards to claim", "there are no rewards to claim now", "现在没有可领取的奖励", None),
    (r"no treasure relic to choose", "there is no treasure relic to choose now", "现在没有可选的宝箱遗物", None),
    (r"these rewards can't be skipped: claim every reward to continue", "these rewards can't be skipped: claim every reward to go on",
     "这些奖励不能跳过：全部领取后才能继续", None),
    (r"'(?P<a>\w+)' is not available on the rewards screen: .*", "'{v}' is not possible on the rewards screen: claim a reward or proceed",
     "奖励界面上不能执行 {v}：请领取奖励或继续", None),
    (r"'(?P<a>\w+)' is not available during the crystal sphere minigame: .*",
     "'{v}' is not possible during the Crystal Sphere minigame: click X Y big|small",
     "水晶球小游戏中不能执行 {v}：请用 click X Y big|small", None),
    (r"'(?P<a>\w+)' is not available while choosing a treasure relic: .*",
     "'{v}' is not possible while choosing a treasure relic: take a relic or skip", "选择宝箱遗物时不能执行 {v}：请拿取遗物或跳过", None),
    (r"'(?P<a>\w+)' is not available yet: finish the current reward choice first",
     "'{v}' is not possible yet: finish the current reward choice first", "现在还不能执行 {v}：请先完成当前的奖励选择", None),
    # ── queries and start_run (AgentProtocol / Program / RunSimulator) ──
    (r"no run in progress", "no run in progress", "没有进行中的对局", None),
    (r"no decision available", "there is no decision now", "现在没有待做的决策", None),
    (r"not in combat: combat piles exist only during combat.*", "not in combat: the combat piles exist only during a fight (sts2 deck: the deck)",
     "不在战斗中：战斗牌堆只在战斗中存在（sts2 deck 查看牌组）", None),
    (r"unknown pile '(?P<x>.*)' \(.*\)", "unknown pile '{x}': draw, discard, exhaust, hand or all",
     "未知牌堆“{x}”：draw、discard、exhaust、hand 或 all", None),
    (r"unknown card id '(?P<x>.*)'", "unknown card id '{x}' (e.g. BASH, STRIKE_IRONCLAD)", "未知卡牌 id“{x}”（例如 BASH、STRIKE_IRONCLAD）", None),
    (r"not in combat: no hand", "not in combat: there is no hand", "不在战斗中：没有手牌", None),
    (r"unknown card location '(?P<x>.*)' \(.*\)", "unknown card location '{x}': deck or hand", "未知卡牌位置“{x}”：deck 或 hand", None),
    (r"(?P<x>deck|hand)_index must be 0-(?P<n>-?\d+)", "{x_h} index must be 0-{n}{empty_en}", "{x_zh}编号必须是 0-{n}{empty_zh}", None),
    (r"get_card needs deck_index, hand_index or id", "sts2 card needs N (deck index), hN (hand index) or a card id",
     "sts2 card 需要 N（牌组编号）、hN（手牌编号）或卡牌 id", None),
    (r"unknown character: (?P<x>.*?)(?: \(.*\))?", "unknown character '{x}': Ironclad, Silent, Defect, Regent or Necrobinder",
     "未知角色“{x}”：Ironclad、Silent、Defect、Regent 或 Necrobinder", None),
    (r"start_run requires character.*", "a character is required: Ironclad, Silent, Defect, Regent or Necrobinder",
     "需要指定角色：Ironclad、Silent、Defect、Regent 或 Necrobinder", None),
    (r"'seed' must be at most (?P<n>\d+) characters, got (?P<g>\d+)", "the seed can have at most {n} characters (got {g})",
     "种子最多 {n} 个字符（收到 {g} 个）", None),
    (r"unknown game_mode '(?P<x>.*)' \(.*\)", "unknown game mode '{x}': standard, custom or daily", "未知游戏模式“{x}”：standard、custom 或 daily", None),
    (r"modifier '(?P<x>.*)' needs a character: character_cards:<character>\. known: (?P<k>.*)",
     "modifier '{x}' needs a character: CHARACTER_CARDS:<character> (known: {k})", "修改器“{x}”需要指定角色：CHARACTER_CARDS:<角色>（已知：{k}）", None),
    (r"unknown modifier '(?P<x>.*)'\. known: (?P<k>.*)", "unknown modifier '{x}' (known: {k})", "未知修改器“{x}”（已知：{k}）", None),
    (r"cannot read profile '(?P<x>.*)': (?P<why>.*)", "can't read the profile '{x}': {why}", "无法读取存档文件“{x}”：{why}", None),
    (r"'ascension' must be an integer 0-10, got (?P<x>.*)", "the ascension must be 0-10, got {x}", "进阶等级必须是 0-10，收到的是 {x}", None),
    (r"'lang' must be \"en\" or \"zh\", got \"(?P<x>.*)\"", "the language must be en or zh, got '{x}'", "语言必须是 en 或 zh，收到的是“{x}”", None),
    (r"'(?P<k>\w+)' must be (?P<what>a string|true or false|an array of strings|an integer)(?:, got (?P<x>.*))?",
     "'{k}' must be {what}{got_en}", "“{k}”必须是{what_zh}{got_zh}", None),
    # ── softlock reasons (EventFlow.MarkSoftlock / MarkUnconfirmableSelect) ──
    (r"this event is not finished but shows no options", "this event is not finished but shows no options",
     "此事件尚未结束，却没有任何选项", None),
    (r"every option of this event is locked", "every option of this event is locked", "此事件的所有选项都已锁定", None),
    (r"every rest site option is disabled and proceed is not shown before one is used",
     "every rest site option is disabled, and Proceed only shows after one is used", "所有休息处选项都不可用，而继续按钮要在用过一个选项后才会出现", None),
    (r"this card selection asks for (?P<c>\S+) card\(s\) but shows only (?P<n>\d+), so its confirm button can never be used",
     "this card selection asks for {c_h} {cw} but shows only {n}, so it can never be confirmed",
     "此选牌界面需要 {c_h} 张牌，但只显示了 {n} 张，因此永远无法确认", None),
]

_POTION_USAGE_TEXT = {"Automatic": ("it triggers by itself", "自动触发"), "None": ("it can't be used", "不能使用")}


def _fmt_groups(g):
    """The derived template fields of one matched engine message (see _REASONS)."""
    g = {k: v for k, v in g.items() if v is not None}
    out = dict(g)
    a = g.get("a")
    out["v"] = _STEP_OF.get(a, a or "")
    k = g.get("k", "").strip("'")
    if " and " in k:
        parts = [p.strip("' ") for p in k.split(" and ")]
        out["kh"] = " and ".join(_ARG_TEXT.get(p, (p, p))[0] for p in parts)
        out["kh_zh"] = "和".join(_ARG_TEXT.get(p, (p, p))[1] for p in parts)
    else:
        out["kh"], out["kh_zh"] = _ARG_TEXT.get(k, (k, k))
    out["w_zh"] = _WHERE_ZH.get(g.get("w", ""), g.get("w", ""))
    if "c" in g:
        out["c_h"] = g["c"].replace(" or ", t(" or ", " 或 "))
        if "-" in g["c"] and g["c"].split("-")[0] == g["c"].split("-")[-1]:
            out["c_h"] = g["c"].split("-")[0]
    out["cw"] = "card" if out.get("c_h") == "1" else "cards"
    out["sk_en"] = " (or leave it: skip)" if g.get("sk") else ""
    out["sk_zh"] = "（或用 skip 离开）" if g.get("sk") else ""
    out["sk_en2"] = "; leave it with skip" if g.get("sk") else " (softlock)"
    out["sk_zh2"] = "；请用 skip 离开" if g.get("sk") else "（软锁）"
    out["s_hint"] = t(f" (did you mean '{g['s']}'?)", f"（是不是想用“{g['s']}”？）") if g.get("s") else ""
    x = (g.get("x") or "").lower()
    names = {"card": ("card", "卡牌"), "relic": ("relic", "遗物"), "potion": ("potion", "药水"), "bundle": ("pack", "卡包"),
             "reward": ("reward", "奖励"), "option": ("option", "选项"), "deck": ("deck", "牌组"), "hand": ("hand", "手牌")}
    if x in names:
        out["x_h"], out["x_zh"] = names[x]
    n = g.get("n")
    out["n_en"] = f" ({n} offered)" if n else ""
    out["n_zh"] = f"（共 {n} 个）" if n else ""
    out["empty_en"] = " (it is empty)" if n == "-1" else ""
    out["empty_zh"] = "（为空）" if n == "-1" else ""
    if "u" in g:
        us = _POTION_USAGE_TEXT.get(g["u"]) or _POTION_USAGE.get(g["u"])
        out["u_h"] = t(*us) if us else g["u"]
    if "tt" in g:
        tt = _TARGET_TEXT.get(g["tt"])
        out["tt_h"] = t(*tt) if tt else g["tt"]
    if "w" in g and g["w"].isdigit() and "h" in g:
        out["w1"], out["h1"] = int(g["w"]) - 1, int(g["h"]) - 1
    what = g.get("what", "")
    out["what_zh"] = {"a string": "字符串", "true or false": "true 或 false", "an array of strings": "字符串数组",
                      "an integer": "整数"}.get(what, what)
    out["got_en"] = f", got {g['x']}" if g.get("x") else ""
    out["got_zh"] = f"，收到的是 {g['x']}" if g.get("x") else ""
    return out


def engine_reason(reason):
    """(text in the output language, short code or None, kind) for one engine message (see
    _REASONS). An unknown message is passed through as it is."""
    import re
    reason = str(reason).strip()
    stripped = reason.rstrip(".")
    for pat, en, zh, kind in _REASONS:
        m = re.fullmatch(pat, stripped, re.S | re.I)
        if not m:
            continue
        g = _fmt_groups(m.groupdict())
        code = g.pop("code", None)
        if "msg" in g:
            g["msg"] = g["msg"].strip().rstrip("。.")
        return t(en, zh).format(**g), code, kind
    return reason, None, None


def engine_reason_of(msg):
    """The reason inside an engine error's envelope: "'x' is not allowed now: REASON. Current
    decision: d. Legal: ..." / "x(args) is not one of the legal actions (REASON)"."""
    import re
    msg = str(msg)
    m = re.match(r"'[^']+' is not allowed(?: now)?: (.*?)(?: Current decision: [a-z_]+\.)?(?: Legal: .*)?$", msg, re.S)
    reason = m.group(1) if m else msg
    inner = re.search(r"is not one of the legal actions(?: \((.*)\))?\.?$", reason)
    if inner:
        reason = inner.group(1) or "it is not one of the legal actions"
    return reason.strip().rstrip(".")


def engine_pattern(msg):
    """The _REASONS entry an engine message (its reason, out of the envelope) matches, or None."""
    import re
    reason = engine_reason_of(msg)
    return next((r for r in _REASONS if re.fullmatch(r[0], reason, re.S | re.I)), None)


def engine_text(msg):
    """One engine message as text in the output language (queries, start: no step context)."""
    return engine_reason(msg)[0]


HELP = """sts2 - play Slay the Spire 2 (headless) from the shell. One command per step.

  sts2 start --character Ironclad|Silent|Defect|Regent|Necrobinder [--seed S] [--ascension 0-10]
             [--lang en|zh] [--profile PATH] [--modifiers A,B] [--game-mode custom]
                             --character is required; no/blank seed = random; ascension 0 by default
  sts2 state                 the current decision, with numbered legal actions
  sts2 do N                  take legal action number N (from the last state shown)
  sts2 do "STEP; STEP; ..."  several steps (a chain; see "Chains" below)
  sts2 deck [--upgrades]     the deck with full card text     sts2 relics | potions
  sts2 piles [draw|discard|exhaust|hand]  combat piles (draw pile sorted, order hidden)
  sts2 card N | card hN | card ID [--upgraded]   one card in detail (deck index, hand index, id)
  sts2 map                   the act map                      sts2 log [N]  recent events
  sts2 --json <command>      raw engine JSON                  sts2 stop [--forget]  stop the daemon
                             (the run is replayed on the next call; --forget ends it)

Steps for `do`:
  N                 legal action number N         play H2 [E1] | play Body Slam [Jaw Worm]
  end               end turn                      | play Body Slam -> E1   potion P0 [E1]
  discard P0        throw a potion away           choose N   (event / rest site option)
  go COL ROW (sts2 map) | go N (Nth map choice)   pick N  (card reward / relic / reward / pack)
  select 0,2 | select none  card selection        skip  (card reward, cancelable selection, treasure;
  alt N             card reward alternative             rewards screen: = proceed, first step only)
  claim N   proceed   leave   remove (shop)
  buy card|relic|potion N                         click X Y big|small  (Crystal Sphere)
  raw {"action":"...","args":{...}}               any engine verb as verb key=value

Chains - PREFER CARD NAMES (play Strike): H#/E# renumber as soon as a card leaves the hand
or an enemy dies (after `play H0` the old H1 is H0). In one chain every index means the view
shown when you submitted it (the last state/do reply):
  H# / P#   that card / potion wherever it is now (by uid); gone (played, discarded,
            exhausted, transformed) -> the chain stops, never a different card.
            "play H0; play H1" = the cards shown as H0 and H1; "play H0; play H0" stops.
  E#        that enemy (by combat_id); dead -> stop.
  N         EVERY action number (select, skip, proceed, leave, remove, end, potion, claim,
            buy...) is the action numbered N in the list printed with that view, and runs
            only on that very screen instance (fights: the same turn, cards/potions remapped
            by uid; rewards / shop: the items left of it); on any other screen it stops.
  choose / pick / claim / buy / select / alt / go N   only on the very screen shown (same
            source, prompt and offered items: True Grit+'s exhaust screen after Burning
            Pact's is another screen) and while that item is unchanged; a screen you have
            not seen yet (the card reward `claim 2` opens, the removal screen `skip; remove`
            closed and opened again) stops the chain before such a step, saying what
            differs. raw / engine verbs: as written.
  No index: end, skip, proceed, leave, remove, select none, go COL ROW and card / enemy
            names are read against the game where it is now ("claim 2; skip" skips the card
            reward that claim opened). A later 'skip' never leaves a rewards screen: there
            it stops the chain ("claim 0; skip", "pick 0; skip"); write 'proceed' for that.
  An action number that was not in the list shown is an error (with the legal actions now).
The chain goes on while the next step is legal and unambiguous where the game is now, also
after the decision type changed ("pick 1; proceed", "select 1; end" after a mid-turn card
selection). It stops before the next step on a new room, a fight starting or ending, a new
turn, the end of the run, a step that does not apply to the new decision, a potion step on
any screen but the very one the chain started at (a fight: the same turn; not a screen
that opened since, even one that looks the same: "skip; remove; discard P0" stops), an
index that can't be resolved (above), a later 'skip' on a rewards screen, and at
the first error: one explanation in the output language of what each index refers to now,
by the index you typed ("H2 (Defend as shown when the chain started, now H1) is ..."), why
the game refuses it (the engine's flag in brackets: (EnergyCostTooHigh); --json has the raw
engine message) and the legal actions. A screen verb on a decision without that screen
(go N in a fight) says what the decision is and lists the legal actions. Reward numbers are
STABLE: claiming one does not renumber the rest ("claim 0; claim 1" is fine); claiming a
gone index says it was already claimed. A card reward's Skip is the skip action (alt N on
it = skip; on a reward with no alternatives alt N says so and points to skip). go COL ROW
off the map says so (with the map's columns and rows). With --lang zh the action labels and
messages are Chinese; the steps are typed as listed here (play, select, go, ...).

Card names: the longest leading words that name a card in hand are the card, the rest is
the target. Two cards with the same name that differ (upgrade, enchantment, cost...) are
ambiguous: use H<n>. A name matches every card that has it, playable or not: `play Def` with
an unplayable Defend and a playable Deflect is ambiguous (never the other card), and a card
that can't be played is an error saying why.

Rules: only legal actions are accepted (the error lists them); nothing is chosen for you.
Rewards screens and treasure chests are decisions (claim / pick / proceed); --manual-rewards
is accepted but changes nothing. Each reply starts with what happened since the previous
decision (cards played, damage, enemy moves, relic triggers...), once: `state` right after a
`do` does not repeat them (sts2 log does). The header shows ascension and seed; sts2 map
numbers columns (COL) and rows (ROW, floor f) for `go COL ROW`. If the engine or the
daemon dies, the run is replayed from its seed and commands (kept in $TMPDIR).
"""


# ───────────────────────────── daemon ─────────────────────────────

class Daemon:
    def __init__(self, session):
        self.session = session
        self.sock_path, self.log_path = paths(session)
        self.engine = None
        self.history = []           # state-changing commands of the current run (for crash recovery)
        self.last = None            # last decision from the engine
        self.numbered = []          # [(n, legal action)] as shown with self.last
        self.log = []               # rendered event lines
        self.idle = float(os.environ.get("STS2_IDLE_TIMEOUT", "1800"))
        self.restarts = 0
        self._notes = []            # one-time notes for the next reply (restarts, restores)
        self._events_shown = None   # the events of the decision whose events were last printed
        self._rewards_seen = {}     # reward index -> text, for the rewards screen of this room
        self._rewards_room = None

    # engine
    def _start_engine(self):
        sys.path.insert(0, HERE)
        from engine import Engine
        env = dict(os.environ)
        for k in ("STS2_DEBUG", "STS2_DEBUG_COMMANDS"):  # cheats stay off: parity with a human player
            env.pop(k, None)
        self.engine = Engine(stderr_sink=None, timeout=90.0, env=env)

    def _close_engine(self):
        if self.engine is not None:
            try:
                self.engine.close()
            except Exception:
                pass
        self.engine = None

    def send(self, cmd, record=False):
        """Send to the engine; on a dead/wedged engine restart it and replay this run."""
        if self.engine is None:
            self._start_engine()
        elif self.engine.proc.poll() is not None:
            # Died while idle: restart and restore the run first, then apply the command.
            self._notes.append(f"{t('note', '注意')}{t(': ', '：')}{t('the engine had stopped', '引擎已停止')}{t('; ', '；')}"
                               f"{self._recover('engine exited while idle')}")
        try:
            r = self.engine.send(cmd)
        except Exception as e:
            note = self._recover(str(e))
            if not record:
                # A query changes nothing: just ask the restarted engine.
                try:
                    return self.engine.send(cmd)
                except Exception as e2:
                    return {"type": "error", "code": "engine_failed",
                            "message": t(f"The engine failed again ({e2}); {note}.", f"引擎再次出错（{e2}）；{note}。")}
            return {"type": "error", "code": "engine_restarted",
                    "message": t(f"The engine failed on this command ({e}); {note}. The command was not applied; "
                                 "sts2 state shows where the run is.",
                                 f"引擎执行这条命令时出错（{e}）；{note}。这条命令没有生效；sts2 state 显示本局当前的位置。")}
        if record and (r.get("type") != "error" or r.get("code") == "flow_failed"):
            self.history.append(cmd)
            try:
                with open(self._run_file(), "a") as f:
                    f.write(json.dumps(cmd) + "\n")
            except OSError:
                pass
        return r

    # The run's commands are also kept on disk, so a daemon that was killed (or stopped)
    # restores the run on its next start instead of losing it.
    def _run_file(self):
        return self.sock_path[:-len(".sock")] + ".run.jsonl"

    def _save_run(self):
        try:
            with open(self._run_file(), "w") as f:
                for c in self.history:
                    f.write(json.dumps(c) + "\n")
        except OSError:
            pass

    def _restore_run(self):
        try:
            with open(self._run_file()) as f:
                hist = [json.loads(l) for l in f if l.strip()]
        except (OSError, ValueError):
            return
        if not hist or hist[0].get("cmd") != "start_run" or not hist[0].get("seed"):
            return
        self.history = hist
        LANG["lang"] = hist[0].get("lang", "en")
        note = self._recover("daemon restarted", count=False, restored=True)
        self._notes.append(f"{t('note', '注意')}{t(': ', '：')}{t('the sts2 daemon restarted', 'sts2 守护进程已重启')}{t('; ', '；')}{note}")

    @staticmethod
    def _fingerprint(st):
        if not st:
            return None
        p = st.get("player") or {}
        ctx = st.get("context") or {}
        return (st.get("decision"), ctx.get("seed"), ctx.get("act"), ctx.get("floor"), p.get("hp"),
                p.get("max_hp"), p.get("gold"), p.get("deck_size"), st.get("round"))

    def _recover(self, why, count=True, restored=False):
        """Restart the engine and replay this run (the start command carries the seed the
        engine chose, so the replay is the same run). Says so if the replay diverged."""
        if count:
            self.restarts += 1
        before = self._fingerprint(self.last)
        self._close_engine()
        try:
            self._start_engine()
        except Exception as e:
            return t(f"restarting it failed too ({e})", f"重启也失败了（{e}）")
        if not self.history:
            return t("it was restarted (no run to restore)", "已重启（没有需要恢复的对局）")
        replayed, last = 0, None
        for cmd in self.history:
            try:
                r = self.engine.send(cmd)
            except Exception as e:
                self.history = self.history[:replayed]
                self._save_run()
                return t(f"it was restarted but replaying the run failed at step {replayed} ({e})",
                         f"已重启，但重放本局时在第 {replayed} 步失败（{e}）")
            replayed += 1
            st = r.get("state") if r.get("type") == "error" and isinstance(r.get("state"), dict) else r
            if st.get("type") == "decision":
                last = st
        if last is not None:
            self._set_last(last)
        after = self._fingerprint(last)
        if before is not None and after != before:
            return t(f"it was restarted and the run was replayed ({replayed} commands), but the replay "
                     f"DIVERGED from the run you were playing ({before} -> {after}); sts2 state shows the replayed game",
                     f"已重启并重放本局（{replayed} 条命令），但重放结果与你正在玩的局面不一致（DIVERGED：{before} -> {after}）；"
                     "sts2 state 显示重放后的局面")
        if restored:
            return t(f"the run was replayed ({replayed} commands)", f"已重放本局（{replayed} 条命令）")
        return t(f"it was restarted and the run was replayed ({replayed} commands)", f"已重启并重放本局（{replayed} 条命令）")

    def _set_last(self, r):
        st = r.get("state") if r.get("type") == "error" and isinstance(r.get("state"), dict) else r
        if st.get("type") == "decision":
            self.last = st
            self.numbered = list(enumerate(st.get("legal_actions") or [], 1))
            if st.get("decision") == "rewards":
                # Remember every reward this screen listed: claim indices never renumber, so
                # an index that has disappeared was claimed.
                ctx = st.get("context") or {}
                room = (ctx.get("act"), ctx.get("floor"), ctx.get("total_floor"))
                if room != self._rewards_room:
                    self._rewards_room, self._rewards_seen = room, {}
                for w in st.get("rewards") or []:
                    if isinstance(w, dict) and w.get("index") is not None:
                        self._rewards_seen[w["index"]] = reward_text(w)

    # requests
    def handle(self, argv, as_json):
        cmd, args = argv[0], argv[1:]
        fn = getattr(self, "cmd_" + cmd.replace("-", "_"), None)
        if fn is None:
            return f"unknown command '{cmd[:64] + ('...' if len(cmd) > 64 else '')}'. " + "Commands: start state do deck relics potions piles card map log help stop", 1
        out, code = fn(args, as_json)
        if self._notes:
            notes, self._notes = self._notes, []
            if as_json:
                try:
                    obj = json.loads(out)
                    if isinstance(obj, dict):
                        obj["notes"] = notes
                        out = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
                except ValueError:
                    pass
            else:
                out = "\n".join(notes) + "\n" + out
        return out, code

    def _out_engine(self, r, as_json, render):
        if as_json:
            return json.dumps(r, ensure_ascii=False, separators=(",", ":")), 0 if r.get("type") != "error" else 1
        if r.get("type") == "error":
            return self._error_text(r), 1
        return render(r), 0

    _HINTS = (("get_deck", "sts2 deck"), ("get_piles", "sts2 piles"), ("get_relics", "sts2 relics"),
              ("get_potions", "sts2 potions"), ("get_map", "sts2 map"), ("get_card", "sts2 card"),
              ("get_state", "sts2 state"), ("start_run", "sts2 start"))

    def _error_text(self, r):
        m = str(r.get("message"))
        loc_m = engine_text(m)
        if loc_m != m or LANG["lang"] == "zh":
            m = loc_m  # (an unknown engine message stays as it is)
        for verb, cmd in self._HINTS:
            m = m.replace(f"use {verb}", f"use {cmd}").replace(f"'{verb}'", f"'{cmd}'")
        if r.get("code") == "no_run" or "No run in progress" in str(r.get("message")):
            m += " " + t("(sts2 start --character Ironclad|Silent|Defect|Regent|Necrobinder)", "（sts2 start --character Ironclad|Silent|Defect|Regent|Necrobinder）")
        msg = f"error: {m}"
        if r.get("code") == "flow_failed" and isinstance(r.get("state"), dict):
            self._set_last(r["state"])
            self._remember_events(r["state"].get("events"))
            self._events_shown = self._events_key(r["state"])
            return msg + "\n" + render_decision(r["state"], self.numbered)
        if r.get("legal_actions") is not None and self.last is not None:
            msg += f"\n({t('sts2 state shows the current decision', 'sts2 state 显示当前决策')})"
        return msg

    def _remember_events(self, events):
        for line in event_lines(events):
            self.log.append(line)
        del self.log[:-400]

    @staticmethod
    def _usage(args, usage, flags=(), max_positional=0):
        """A usage error (text, exit code 1) when `args` has an unknown flag or too many
        positional arguments; None when they fit."""
        pos = [a for a in args if not a.startswith("--")]
        bad = [a for a in args if a.startswith("--") and a not in flags]
        if bad or len(pos) > max_positional:
            extra = bad or pos[max_positional:]
            return f"{t('unexpected argument', '多余的参数')} '{extra[0]}'. usage: {usage}", 1
        return None

    def cmd_help(self, args, as_json):
        if as_json:
            # The engine's help reply, without starting an engine for it.
            with open(os.path.join(HERE, "engine_help.json"), encoding="utf-8") as f:
                return f.read(), 0
        return HELP, 0

    def cmd_start(self, args, as_json):
        import argparse
        p = argparse.ArgumentParser(prog="sts2 start", add_help=False)
        p.add_argument("--character", "-c")  # required: the run starts on the player's Embark, not on a preselection
        p.add_argument("--seed", "-s")
        p.add_argument("--ascension", "-a", type=int, default=0)
        p.add_argument("--lang", default="en", choices=["en", "zh"])
        p.add_argument("--profile")
        p.add_argument("--manual-rewards", action="store_true")
        p.add_argument("--modifiers")
        p.add_argument("--game-mode")
        try:
            o = p.parse_args(args)
        except SystemExit:
            return "usage: " + p.format_usage().replace("usage: ", ""), 1
        if not o.character:
            return ("error: sts2 start requires --character Ironclad|Silent|Defect|Regent|Necrobinder "
                    "(the character is your pick, as on the game's character select)"), 1
        cmd = {"cmd": "start_run", "character": o.character, "ascension": o.ascension, "lang": o.lang,
               "omit_deck": True}
        if o.seed:
            cmd["seed"] = o.seed
        if o.profile:
            cmd["profile"] = os.path.abspath(os.path.expanduser(o.profile))
        # Rewards screens and treasure chests are always decisions; --manual-rewards is
        # accepted for compatibility (older engines needed it).
        cmd["manual_rewards"] = True
        if o.modifiers:
            cmd["modifiers"] = [m for m in o.modifiers.split(",") if m]
        if o.game_mode:
            cmd["game_mode"] = o.game_mode
        r = self.send(cmd)
        if r.get("type") == "decision":
            LANG["lang"] = o.lang
            # Record the seed the engine used (it picks one when none was given, and
            # normalizes given ones), so a crash recovery replays this very run.
            seed = (r.get("context") or {}).get("seed")
            if seed:
                cmd["seed"] = seed
            self.history = [cmd]
            self._save_run()
            self.log = []
            self._set_last(r)
            self._remember_events(r.get("events"))
            self._events_shown = self._events_key(r)
        elif not as_json:
            prev, LANG["lang"] = LANG["lang"], o.lang  # the error in the language asked for
            try:
                return self._out_engine(r, as_json, lambda r: render_decision(r, self.numbered))
            finally:
                LANG["lang"] = prev
        return self._out_engine(r, as_json, lambda r: render_decision(r, self.numbered))

    def cmd_state(self, args, as_json):
        if (u := self._usage(args, "sts2 state")):
            return u
        r = self.send({"cmd": "get_state"})
        if r.get("type") != "decision":
            return self._out_engine(r, as_json, lambda r: render_decision(r, self.numbered))
        self._set_last(r)
        key = self._events_key(r)
        if as_json or key != self._events_shown:
            # (--json always carries the engine's events.) First time these events are shown.
            if not as_json:
                self._events_shown = key
            return self._out_engine(r, as_json, lambda r: render_decision(r, self.numbered))
        # The previous reply already printed what happened since the last decision: once is enough.
        note = [t("(events since the last decision: shown in the previous reply; sts2 log repeats them)",
                  "（上次决策以来的事件已在上一条回复中显示；sts2 log 可再次查看）")] if r.get("events") else []
        return render_decision(r, self.numbered, extra_events=[], prefix=note), 0

    @staticmethod
    def _where(st):
        """(act, floor, in combat, combat round) of a decision: a `do` chain stops when any of
        them changes (a new room, a fight starting or ending, a new turn)."""
        st = st or {}
        ctx = st.get("context") or {}
        cb = Refs.combat_of(st)
        return ctx.get("act"), ctx.get("floor"), cb is not None, (cb or {}).get("round")

    def _boundary(self, start, st):
        a0, f0, c0, r0 = self._where(start)
        a1, f1, c1, r1 = self._where(st)
        dec = (st or {}).get("decision")
        if dec == "game_over":
            return t("the run is over", "本局已结束")
        if (a0, f0) != (a1, f1):
            return t(f"the run moved on to a new room ({dec})", f"已进入新的房间（{dec_name(dec)}）")
        if c0 and not c1:
            return t(f"the fight is over (now at {dec})", f"战斗已结束（现在处于{dec_name(dec)}）")
        if c1 and not c0:
            return t("a fight started", "战斗已开始")
        if c0 and r0 != r1:
            return t("a new turn", "新的回合")
        return None

    def cmd_do(self, args, as_json):
        """Run the steps in order. Index references are resolved against the state shown when
        the chain was submitted (Refs). After a step, the chain goes on when the next step is
        legal and unambiguous on the decision the game is at now - also when its type changed
        (pick a card -> back to the rewards; a mid-turn card selection -> back to the fight) -
        and stops before it when the game entered a new room, a fight started or ended, a new
        turn began, or the run ended; when the step does not apply to the new decision; when a
        potion step would run on a different screen than the chain started at; or when its
        index reference can't be resolved safely. It also stops at the first error."""
        text = " ".join(args).replace("\n", ";")
        steps = [s.strip() for s in text.split(";") if s.strip()]
        if not steps:
            return "usage: sts2 do <N | step; step; ...>  (sts2 help)", 1
        if self.last is None:
            r = self.send({"cmd": "get_state"})
            if r.get("type") != "decision":
                return self._error_text(r) if not as_json else json.dumps(r), 1
            self._set_last(r)
        results, events, final, code = [], [], None, 0
        start = self.last
        refs = Refs(start, self.numbered)   # what the agent saw
        for i, step in enumerate(steps):
            st = self.last
            dec = st.get("decision") if st else None
            refs.first = i == 0
            refs.moved = []
            refs.typed = {}
            if i > 0:
                why = self._boundary(start, st)
                if why:
                    results.append({"step": step, "ok": False, "stopped": True,
                                    "error": t(f"not run: {why}", f"未执行：{why}")})
                    break
            try:
                action = parse_verb(step, st, self.numbered, refs)
            except StopStep as e:
                results.append({"step": step, "ok": False, "stopped": True, "error": t(f"not run: {e}", f"未执行：{e}")})
                break
            except VerbError as e:
                if i > 0 and dec != start.get("decision") and not isinstance(e, BadNumber):
                    # The step was written for another screen than the one the game is at now.
                    # (A message that names the decision already says where the game is.)
                    where = "" if isinstance(e, NoScreen) else t(f"the game is now at {dec}: ", f"游戏现在处于{dec_name(dec)}：")
                    results.append({"step": step, "ok": False, "stopped": True,
                                    "error": t(f"not run: {where}{e}", f"未执行：{where}{e}")})
                else:
                    results.append({"step": step, "ok": False, "error": str(e)})
                    code = 1
                break
            act = action.get("action")
            if i > 0 and dec in NATIVE and act not in NATIVE[dec] and act not in ("use_potion", "discard_potion"):
                results.append({"step": step, "ok": False, "stopped": True,
                                "error": t(f"not run: the game is now at {dec}, where '{step}' does not apply",
                                           f"未执行：游戏现在处于{dec_name(dec)}，'{step}' 不适用")})
                break
            if i > 0 and act in ("use_potion", "discard_potion"):
                # A potion step only runs on the very screen instance the chain started at
                # (a fight: the same turn), never on one that opened since.
                try:
                    refs.same_screen(step, st, ("run on", "执行"))
                except StopStep as e:
                    results.append({"step": step, "ok": False, "stopped": True,
                                    "error": t(f"not run: {e} (a potion step only runs on the screen the chain started at)",
                                               f"未执行：{e}（药水步骤只在指令链开始时的界面上执行）")})
                    break
            label = action_label(action, st) if st else act
            if refs.moved:
                label += "  (" + t("shown as ", "原为 ") + ", ".join(m.split(" -> ")[0] for m in refs.moved) + ")"
            r = self.send({"cmd": "action", **action}, record=True)
            if r.get("type") == "error":
                results.append({"step": step, "ok": False, "error": self._explain(action, st, r, refs.typed)})
                if r.get("code") == "flow_failed" and isinstance(r.get("state"), dict):
                    self._set_last(r["state"])
                    events.extend(r["state"].get("events") or [])
                    final = r["state"]
                code = 1
                break
            results.append({"step": step, "ok": True, "did": label})
            events.extend(r.get("events") or [])
            self._set_last(r)
            final = r
            if r.get("decision") == "game_over":
                break
        self._remember_events(events)
        if final is not None:
            self._events_shown = self._events_key(final)
        if as_json:
            return json.dumps({"results": results, "state": final}, ensure_ascii=False, separators=(",", ":")), code
        lines = []
        for res in results:
            if res["ok"]:
                lines.append(f"ok: {res['did']}")
            elif res.get("stopped"):
                lines.append(f"stopped before '{res['step']}': {res['error']}")
            else:
                lines.append(f"error in '{res['step']}': {res['error']}")
        shown = final or self.last
        if shown is not None:
            return render_decision(shown, self.numbered, extra_events=events, prefix=lines), code
        return clip("\n".join(lines)), code

    @staticmethod
    def _events_key(st):
        return json.dumps((st or {}).get("events") or [], sort_keys=True, ensure_ascii=False)

    def _explain(self, action, st, r, typed=None):
        """An engine error for one step, as one explanation in the output language: what each
        index in it refers to now, then the engine's reason (localized, see _REASONS; dropped
        when the first part already says it, keeping only its short code), then the legal
        actions. The raw engine message is in --json."""
        import re
        st = st or {}
        msg = str(r.get("message") or r.get("code"))
        sep = t(". ", "。")
        legal = t("Legal now: ", "现在可选：") + legal_now(st)
        reason = engine_reason_of(msg)
        text, code, kind = engine_reason(reason)
        if r.get("code") not in ("illegal_action", "unknown_action", "bad_request", None) and text == reason:
            # The engine failed (not a refusal): its message as it is.
            return msg if LANG["lang"] != "zh" or r.get("code") in ("engine_restarted", "engine_failed") \
                else f"引擎执行这一步时出错：{msg}"
        if r.get("code") == "unknown_action":
            return text + sep + legal
        a, args = action.get("action"), dict(action.get("args") or {})
        if kind == "notarget":
            args.pop("target_index", None)
        elif a == "select_cards" and text == t("it is not one of the legal actions", "这不是合法操作"):
            # A one-card screen lists each card: name the count it takes.
            n = len([x for x in str(args.get("indices", "")).split(",") if x.strip()])
            cards = {c.get("index") for c in st.get("cards") or [] if isinstance(c, dict)}
            c = select_count_text(st)
            if all(x.strip().isdigit() and int(x) in cards for x in str(args.get("indices", "")).split(",") if x.strip()):
                text, kind = t(f"this screen takes {c} {'card' if c == '1' else 'cards'}, not {n}",
                               f"此界面需要选择 {c} 张牌，而不是 {n} 张"), "count"
        bounds = None
        if a == "select_map_node":
            try:
                bounds = map_bounds(self.send({"cmd": "get_map"}))
            except Exception:
                bounds = None
        refs = describe_refs({"action": a, **({"args": args} if args else {})}, st, self._rewards_seen, typed, bounds)
        if a == "claim_reward" and args.get("reward_index") in self._rewards_seen and kind == "index" and refs:
            return refs[0] + sep + legal
        if kind == "notarget":
            i = args.get("card_index", args.get("potion_index"))
            step = f"play H{i}" if a == "play_card" else f"potion P{i}"
            text = t(f"it takes no target: {step} without an E#", f"它不需要目标：{step}，不要加 E#")
        elif kind == "target":
            cb = Refs.combat_of(st) or {}
            ens = [e for e in cb.get("enemies") or [] if isinstance(e, dict)]
            names = ", ".join(f"E{e.get('index')} {e.get('name')}" for e in ens) or t("none", "无")
            i = args.get("card_index", args.get("potion_index"))
            step = f"play H{i}" if a == "play_card" else f"potion P{i}"
            e0 = f"E{ens[0].get('index')}" if ens else "E0"
            text = t(f"with several enemies it needs a target: {names} (e.g. {step} {e0})",
                     f"敌人不止一个，需要指定目标：{names}（例如 {step} {e0}）")
        if not refs:
            head = t(f"'{action_label(action, st)}' is not allowed now", f"现在不能执行“{action_label(action, st)}”")
            return head + t(": ", "：") + text + (f" ({code})" if code else "") + sep + legal
        head = t("; ", "；").join(refs)
        missing = any(x.startswith(("there is no", "没有", "这里没有")) or "outside the map" in x or "在地图之外" in x for x in refs)
        if kind == "index" or text.rstrip("。.").lower() in head.lower() or (
                missing and text == t("it is not one of the legal actions", "这不是合法操作")):
            return head + (f" ({code})" if code else "") + sep + legal
        return head + t(" - ", "；") + text + (f" ({code})" if code else "") + sep + legal

    def _simple(self, cmd, as_json, render):
        r = self.send(cmd)
        return self._out_engine(r, as_json, render)

    def cmd_deck(self, args, as_json):
        if (u := self._usage(args, "sts2 deck [--upgrades]", flags=("--upgrades",))):
            return u
        def render(r):
            gl = Glossary()
            show_up = "--upgrades" in args
            lines = [f"{t('Deck', '牌组')} ({r.get('deck_size')}):"]
            for c in r.get("cards") or []:
                lines.append(card_line(c, gl, show_upgrade=show_up, prefix=f" {c.get('index')}) "))
            text = "\n".join(lines)
            g = gl.render()
            if g and len(text) < MAX_OUT - 1500:
                text += "\n" + "\n".join(g)
            return clip(text)
        return self._simple({"cmd": "get_deck", "upgrades": "--upgrades" in args}, as_json, render)

    def cmd_relics(self, args, as_json):
        if (u := self._usage(args, "sts2 relics")):
            return u
        def render(r):
            gl = Glossary()
            lines = [t("Relics:", "遗物：")]
            for x in r.get("relics") or []:
                cnt = f" ({x['counter']})" if x.get("counter") is not None else ""
                lines.append(f" {x.get('index')}) {x.get('name')}{cnt}{_desc(x.get('description'))}")
                gl.add(x.get("tips"))
            return clip("\n".join(lines + gl.render()))
        return self._simple({"cmd": "get_relics"}, as_json, render)

    def cmd_potions(self, args, as_json):
        if (u := self._usage(args, "sts2 potions")):
            return u
        def render(r):
            gl = Glossary()
            lines = [f"{t('Potions', '药水')} ({len(r.get('potions') or [])}/{r.get('potion_slots')}):"]
            for x in r.get("potions") or []:
                lines.append(f" P{x.get('index')} {x.get('name')} [{potion_tags(x)}]{_desc(x.get('description'))}")
                gl.add(x.get("tips"))
            return clip("\n".join(lines + gl.render()))
        return self._simple({"cmd": "get_potions"}, as_json, render)

    def cmd_piles(self, args, as_json):
        if (u := self._usage(args, "sts2 piles [draw|discard|exhaust|hand|all]", max_positional=1)):
            return u
        which = args[0] if args else "all"

        def render(r):
            gl = Glossary()
            names = {"draw": t("Draw pile (sorted; order hidden)", "抽牌堆（已排序，顺序不可见）"),
                     "discard": t("Discard pile", "弃牌堆"), "exhaust": t("Exhaust pile", "消耗堆"), "hand": t("Hand", "手牌")}
            lines = []
            for k in ("draw", "discard", "exhaust", "hand"):
                if k not in r:
                    continue
                cards = r[k] or []
                lines.append(f"{names[k]} ({sum(c.get('count', 1) for c in cards)}):")
                for c in cards:
                    n = f" x{c['count']}" if c.get("count", 1) > 1 else ""
                    lines.append(card_line(c, gl, prefix="  ") + n)
            return clip("\n".join(lines + gl.render()))
        return self._simple({"cmd": "get_piles", "pile": which}, as_json, render)

    def cmd_card(self, args, as_json):
        if not args:
            return "usage: sts2 card N (deck index) | card hN (hand index) | card CARD_ID [--upgraded]", 1
        if (u := self._usage(args, "sts2 card N | hN | CARD_ID [--upgraded]", flags=("--upgraded",), max_positional=1)):
            return u
        ref = args[0]
        cmd = {"cmd": "get_card", "upgraded": "--upgraded" in args}
        if ref.isdigit():
            cmd["deck_index"] = int(ref)
        elif ref[:1].lower() == "h" and ref[1:].isdigit():
            cmd["hand_index"] = int(ref[1:])
        else:
            cmd["id"] = ref

        def render(r):
            gl = Glossary()
            c = r.get("card") or {}
            lines = [card_line(c, gl, show_upgrade=True), f"  {zh_term('rarity', c.get('rarity'))}"
                     + (f" | {', '.join(c['keywords'])}" if c.get("keywords") else "")]
            return clip("\n".join(lines + gl.render()))
        return self._simple(cmd, as_json, render)

    def cmd_map(self, args, as_json):
        if (u := self._usage(args, "sts2 map")):
            return u
        r = self.send({"cmd": "get_map"})
        return self._out_engine(r, as_json, lambda m: clip(render_map(m, self.last)))

    def cmd_log(self, args, as_json):
        if len(args) > 1 or (args and not (args[0].isdigit() and int(args[0]) > 0)):
            return f"{t('invalid argument', '无效的参数')} '{args[-1]}'. usage: sts2 log [N] (N = number of lines, a positive integer; default 40)", 1
        n = int(args[0]) if args else 40
        lines = self.log[-n:]
        if as_json:
            return json.dumps(lines, ensure_ascii=False), 0
        return clip("\n".join(lines) if lines else t("(no events yet)", "（暂无事件）")), 0

    def cmd_status(self, args, as_json):
        if (u := self._usage(args, "sts2 status")):
            return u
        alive = self.engine is not None and self.engine.proc.poll() is None
        return (f"daemon pid {os.getpid()}, engine {'running' if alive else 'stopped'}, "
                f"restarts {self.restarts}, run steps {len(self.history)}, log {self.log_path}"), 0

    def cmd_stop(self, args, as_json):
        if (u := self._usage(args, "sts2 stop [--forget]", flags=("--forget",))):
            return u
        self._stop = True
        if "--forget" in args:
            # End the run too: the next call starts with no run instead of replaying it.
            self.history = []
            self._forget = True
        return "stopped", 0

    def _socket_alive(self):
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            c.connect(self.sock_path)
            return True
        except OSError:
            return False
        finally:
            c.close()

    def serve(self):
        import fcntl
        import select
        import time
        # Two clients that both found no daemon each spawn one. Only the first to get the
        # lock serves; without this the second unlinked the first one's socket, leaving
        # an orphaned daemon (and engine) running until its idle timeout.
        lock_path = self.sock_path[:-len(".sock")] + ".lock"
        # (A daemon that is shutting down still holds it for a moment: wait briefly.)
        # A stopping daemon unlinks the lock file; a lock taken on that unlinked file is
        # worthless (a third daemon would lock the new file), so retry on the current one.
        self._lock_file = None
        for attempt in range(150):
            f = open(lock_path, "a")
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                try:
                    same = os.fstat(f.fileno()).st_ino == os.stat(lock_path).st_ino
                except OSError:
                    same = False
                if same:
                    self._lock_file = f
                    break
                f.close()
                continue
            except OSError:
                f.close()
                if attempt == 149 or self._socket_alive():
                    return 0  # another daemon serves this session
                time.sleep(0.02)
        if self._lock_file is None:
            return 0
        try:
            os.unlink(self.sock_path)
        except OSError:
            pass
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old_umask = os.umask(0o077)  # never world-connectable, even for a moment (/tmp fallback)
        try:
            srv.bind(self.sock_path)
        finally:
            os.umask(old_umask)
        os.chmod(self.sock_path, 0o600)
        srv.listen(8)
        self._stop = False
        last_use = time.time()
        try:
            self._start_engine()
            self._restore_run()
        except Exception as e:
            print(f"engine start failed: {e}", file=sys.stderr)
        try:
            while not self._stop:
                rl, _, _ = select.select([srv], [], [], 30)
                if not rl:
                    if time.time() - last_use > self.idle:
                        break
                    continue
                conn, _ = srv.accept()
                last_use = time.time()
                with conn:
                    try:
                        conn.settimeout(10)
                        chunks = []
                        while True:
                            b = conn.recv(65536)
                            if not b:
                                break
                            chunks.append(b)
                        buf = b"".join(chunks)
                        as_json = buf[:1] == b"j"
                        argv = [a for a in buf[1:].decode("utf-8", "replace").split("\0") if a != ""] or ["help"]
                        out, code = self.handle(argv, as_json)
                    except Exception as e:
                        import traceback
                        traceback.print_exc()
                        out, code = f"error: {type(e).__name__}: {e}", 2
                    try:
                        conn.settimeout(None)
                        if not out.endswith("\n"):
                            out += "\n"
                        conn.sendall(str(min(max(code, 0), 9)).encode() + out.encode("utf-8"))
                    except OSError:
                        pass
        finally:
            try:
                os.unlink(self.sock_path)
            except OSError:
                pass
            self._close_engine()
            self._cleanup_files()
        return 0

    def _cleanup_files(self):
        """Leave nothing behind in $TMPDIR but the run file (kept so the next call replays
        the run, unless `stop --forget`): the lock (unlinked while still held, see serve) and
        the daemon log when it is empty."""
        base = self.sock_path[:-len(".sock")]
        paths = [base + ".lock"]
        if getattr(self, "_forget", False) or not self.history:
            paths.append(base + ".run.jsonl")
        try:
            if os.path.getsize(base + ".log") == 0:
                paths.append(base + ".log")
        except OSError:
            pass
        for p in paths:
            try:
                os.unlink(p)
            except OSError:
                pass



if __name__ == "__main__":
    sys.exit(Daemon(sys.argv[1] if len(sys.argv) > 1 else "default").serve())
