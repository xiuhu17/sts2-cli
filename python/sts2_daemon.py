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


def card_line(c, gl, show_upgrade=False, prefix=""):
    name = c.get("name", "?")
    if c.get("upgraded") and not name.endswith("+"):
        name += "+"
    cost = c.get("cost")
    if c.get("cost_x"):
        cost = "X"
    star = f" {c['star_cost']}*" if c.get("star_cost") else ""
    extra = []
    if c.get("enchantment"):
        extra.append(f"{c['enchantment']}{' ' + str(c['enchantment_amount']) if c.get('enchantment_amount') else ''}")
    if c.get("affliction"):
        extra.append(f"{c['affliction']}{' ' + str(c['affliction_amount']) if c.get('affliction_amount') else ''}")
    ex = f" <{', '.join(extra)}>" if extra else ""
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
                use = x.get("usage")
                lines.append(f"  P{x.get('index')} {x.get('name')} [{x.get('target_type')}, {use}]{_desc(x.get('description'))}")
    return lines


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
    "speech": lambda e: f"{e.get('speaker')}: \"{e.get('text')}\"",
    "thought": lambda e: f"{e.get('speaker')} ({t('thinks', '心想')}): \"{e.get('text')}\"",
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


def action_label(a, st):
    """Short text for one legal action."""
    act = a.get("action")
    args = a.get("args") or {}
    hand = {c.get("index"): c for c in st.get("hand") or []}
    enemies = {e.get("index"): e for e in st.get("enemies") or []}
    pots = {p.get("index"): p for p in (st.get("player") or {}).get("potions") or [] if isinstance(p, dict)}
    tgt = ""
    if "target_index" in args:
        e = enemies.get(args["target_index"])
        tgt = f" -> E{args['target_index']} {e.get('name') if e else ''}".rstrip()
    if act == "play_card":
        c = hand.get(args.get("card_index"), {})
        return f"play H{args.get('card_index')} {c.get('name', '?')}{tgt}"
    if act == "end_turn":
        return t("end turn", "结束回合")
    if act == "use_potion":
        return f"potion P{args.get('potion_index')} {pots.get(args.get('potion_index'), {}).get('name', '?')}{tgt}"
    if act == "discard_potion":
        return f"discard P{args.get('potion_index')} {pots.get(args.get('potion_index'), {}).get('name', '?')}"
    if act == "select_map_node":
        ch = next((c for c in st.get("choices") or [] if c.get("col") == args.get("col") and c.get("row") == args.get("row")), {})
        return f"go {args.get('col')} {args.get('row')} {zh_term('room', ch.get('type', ''))}".rstrip()
    if act == "choose_option":
        o = next((o for o in st.get("options") or [] if o.get("index") == args.get("option_index")), {})
        title = o.get("title") or rest_title(o)
        return f"choose {args.get('option_index')} {title or ''}".rstrip()
    if act == "select_cards":
        if "choose" in a:
            ch = a["choose"]
            return f"select {ch.get('min')}-{ch.get('max')} of {ch.get('from')}  (sts2 do \"select 0,2\")"
        idx = str(args.get("indices"))
        if idx == "":
            return t("confirm with no card selected", "不选任何牌，直接确认") + '  (sts2 do "select none")'
        cards = {c.get("index"): c for c in st.get("cards") or []}
        c = cards.get(int(idx)) if idx.isdigit() else None
        return f"select {idx} {c.get('name') if c else ''}".rstrip()
    if act == "select_card_reward":
        c = next((c for c in st.get("cards") or [] if c.get("index") == args.get("card_index")), {})
        return f"pick {args.get('card_index')} {c.get('name', '')}".rstrip()
    if act in ("buy_card", "buy_relic", "buy_potion"):
        key = {"buy_card": ("cards", "card_index"), "buy_relic": ("relics", "relic_index"), "buy_potion": ("potions", "potion_index")}[act]
        it = next((x for x in st.get(key[0]) or [] if x.get("index") == args.get(key[1])), {})
        return f"buy {key[0][:-1]} {args.get(key[1])} {it.get('name', '')} ({it.get('price', it.get('cost'))}g)"
    if act == "remove_card":
        return f"{t('remove a card', '移除一张牌')} ({st.get('card_removal_cost')}g)"
    if act == "claim_reward":
        w = next((x for x in st.get("rewards") or [] if x.get("index") == args.get("reward_index")), {})
        return f"claim {args.get('reward_index')} {reward_text(w)}"
    if act == "select_treasure_relic":
        w = next((x for x in st.get("relics") or [] if x.get("index") == args.get("relic_index")), {})
        return f"take relic {args.get('relic_index')} {w.get('name', '')}"
    if act == "leave_room":
        return t("leave", "离开")
    if act == "proceed":
        return t("proceed", "继续")
    if act == "skip_card_reward":
        return t("skip the card reward", "跳过卡牌奖励")
    if act == "skip_select":
        return t("skip / cancel the selection", "跳过 / 取消选择")
    if act == "skip_treasure_relic":
        return t("skip the relic", "跳过遗物")
    if act == "select_bundle":
        return f"bundle {args.get('bundle_index')}"
    if act == "crystal_sphere_click":
        return "click X Y big|small  (sts2 do \"click 2 3 big\"; big = 3x3, small = 1 cell)"
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
    name = w.get("name") or typ
    return f"{name}{_desc(w.get('description'))}"


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
        flag = "" if c.get("can_play", True) else t(" [unplayable]", " [不可打出]")
        dmg = ""
        # Per-target damage (what hovering each enemy shows), even with one enemy: Vulnerable,
        # Weak and other modifiers change it from the card text.
        if c.get("damage_by_target"):
            dmg = "  {" + ", ".join(f"E{d.get('target_index')}:{d.get('total_damage', d.get('damage'))}" for d in c["damage_by_target"]) + "}"
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
        out.append(f" [{n}] {action_label(a, st)}")
    groups = {}
    for n, a in selects:
        groups.setdefault(a["action"], []).append((n, a))
    pos = 1
    for verb, items in groups.items():
        key = next(iter(items[0][1]["args"]))
        nums = [n for n, _ in items]
        vals = [items[i][1]["args"].get(key) for i in range(len(items))]
        ints = all(isinstance(v, int) or str(v).isdigit() for v in vals)
        if ints and nums == list(range(nums[0], nums[0] + len(nums))) \
                and [int(v) for v in vals] == list(range(int(vals[0]), int(vals[0]) + len(vals))):
            line = (f" [{nums[0]}-{nums[-1]}] {verb} {key} {vals[0]}-{vals[-1]} "
                    f"([{nums[0]}] = {vals[0]}, [{nums[0] + 1}] = {int(vals[0]) + 1}, ...)" if len(nums) > 1
                    else f" [{nums[0]}] {verb} {key} {vals[0]}")
        else:
            line = f" {verb} {key}: " + " ".join(f"[{n}]={v}" for n, v in zip(nums, vals))
        out.insert(pos, line)
        pos += 1
    if discards:
        out.append(f" {t('discard potion', '丢弃药水')}: {' '.join(discards)}")
    return out


# Top-level fields each decision's renderer already shows; anything else the engine sends
# is printed generically (so new fields reach the agent without a client change).
_COMMON_KEYS = {"type", "decision", "legal_actions", "player", "context", "events"}
_KNOWN_KEYS = {
    "combat_play": {"round", "energy", "max_energy", "stars", "draw_pile", "discard_pile", "exhaust_pile",
                    "draw_pile_count", "discard_pile_count", "exhaust_pile_count", "player_powers", "orbs",
                    "orb_slots", "osty", "enemies", "hand", "inactive_enemies"},
    "card_select": {"purpose", "source", "min_select", "max_select", "prompt", "screen", "cancelable",
                    "enchantment", "cards", "combat"},
    "card_reward": {"cards", "can_skip", "alternatives", "from_event", "gold_earned"},
    "bundle_select": {"bundles"},
    "event_choice": {"event_name", "event_id", "description", "options", "dialogue", "finished", "popup",
                     "dialogue_pool", "dialogue_random", "softlocked", "softlock_reason", "message"},
    "rest_site": {"options", "can_leave", "softlocked", "softlock_reason", "message"},
    "shop": {"cards", "relics", "potions", "card_removal_cost", "card_removal_available"},
    "rewards": {"rewards", "potion_slots_full", "gold_earned", "source"},
    "treasure_relic": {"relics", "can_skip"},
    "map_select": {"act", "act_name", "choices", "floor"},
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
              "unplayable_reasons", "blocked_by", "costs_x", "x_value", "colorless"}


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
        body.append(f"{t('CARD SELECT', '选牌')} [{purpose}] {_one_line(st.get('prompt')) or ''}".rstrip())
        body.append(f"  {t('select', '选择')} {mn}-{mx}" + (f" | {t('from', '来自')} {src}" if src else "")
                    + (f" | {t('screen', '界面')} {st['screen']}" if st.get("screen") else "")
                    + (f" | {t('cancelable', '可取消')}" if st.get("cancelable") else ""))
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
            if isinstance(a, dict):
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
        for w in st.get("rewards") or []:
            body.append(f" {w.get('index')}) {reward_text(w)}")
    elif d == "treasure_relic":
        body.append(t("TREASURE", "宝箱"))
        for r in st.get("relics") or []:
            body.append(f" {r.get('index')}) {r.get('name')}{_desc(r.get('description'))}")
            gl.add(r.get("tips"))
    elif d == "map_select":
        body.append(t("MAP: choose the next room (sts2 map: whole act)", "地图：选择下一个房间（sts2 map 查看整幕）"))
    elif d == "crystal_sphere":
        body.extend(crystal_sphere_lines(st))
    elif d == "game_over":
        body.extend(game_over_lines(st))
    else:
        body.append(f"{t('DECISION', '决策')}: {d}")
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
        note = t(f"No way on: softlock ({st.get('softlock_reason')}); the game shows no button that moves on here"
                 + ("; only the potion belt works." if numbered else "."),
                 f"软锁（{st.get('softlock_reason')}）：游戏在这里没有能继续的按钮"
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
      only on the same kind of screen as shown and only while the item at that index is
      still the same; otherwise stop. A claimed reward / bought item is left to the engine,
      whose error then says so.
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
                                 f"{prefix}{i}：这条指令链开始时（{self.snap.get('decision')}）没有显示这些内容；请先查看新状态"))
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
        sd, cd = self.snap.get("decision"), (st or {}).get("decision")
        if sd != cd:
            raise StopStep(t(f"'{what} {i}' would pick from a {cd} screen this chain has not shown you "
                             f"(it started at {sd}); look at it first",
                             f"'{what} {i}' 会在指令链未显示过的 {cd} 界面上选择（链开始于 {sd}）；请先查看"))
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

    def numbered_action(self, n, st, act):
        """The action numbered n in the shown list, its references resolved against `st`."""
        a = act.get("action")
        args = dict(act.get("args") or {})
        if self.first:
            return {"action": a, **({"args": args} if args else {})}
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
        if a == "select_map_node" and (st or {}).get("decision") != self.snap.get("decision"):
            raise StopStep(t(f"action {n} was a map move; the game is now at {(st or {}).get('decision')}",
                             f"操作 {n} 是地图移动；游戏现在处于 {(st or {}).get('decision')}"))
        if a in self._SCREEN:
            key, arg, what = self._SCREEN[a]
            self.screen(key, args.get(arg), st, what)
        return {"action": a, **({"args": args} if args else {})}


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


def _int(s, what):
    s = str(s).strip()
    if s[:1].upper() in ("H", "E", "P") and s[1:].isdigit():
        s = s[1:]
    try:
        return int(s)
    except ValueError:
        raise VerbError(f"{what} must be a number, got '{s}'") from None


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
        raise VerbError(f"no {what} matching '{ref_s}' (have: {names or t('none', '无')})")
    # Every match counts, playable or not: picking the playable one would play a card the
    # player did not name. Enemies with the same name are different targets; identical cards
    # (same text, cost and playability) are the same play.
    if len(pref) > 1 and (what != "card" or any(not _same_card(pref[0], x) for x in pref[1:])):
        names = ", ".join(f"{prefix}{x.get('index')} {x.get('name')}"
                          + ("" if what != "card" or x.get("can_play", True) else t(" (not playable)", "（不能打出）"))
                          for x in pref)
        raise VerbError(f"'{ref_s}' matches several {what}s ({names}); use the {prefix} number")
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
        raise VerbError("empty step")
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
                            if (st or {}).get("softlocked") else f"no legal actions at {dec}")
        shown = refs.numbered if not refs.first else numbered
        for num, act in shown:
            if num == n:
                if "choose" in act or "choose_cell" in act:
                    raise VerbError(f"action {n} needs arguments: {action_label(act, refs.snap if not refs.first else st)}")
                return refs.numbered_action(n, st, act)
        raise VerbError(f"no action number {n} (have 1-{len(shown)}; run 'sts2 state')")
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
                raise VerbError(f"engine verb arguments are key=value, got '{kv}'")
            k, val = kv.split("=", 1)
            args[k] = int(val) if val.lstrip("-").isdigit() else val
        return {"action": v, **({"args": args} if args else {})}
    if v in ("play", "p"):
        if not a:
            raise VerbError("play <card: H0 | 0 | name> [target: E1 | 1 | name]")
        if dec != "combat_play":
            raise VerbError(f"not in combat (decision: {dec}); 'play' plays a card from your hand")
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
        return {"action": "choose_option", "args": {"option_index": refs.screen("options", _int(a[0] if a else "", "option"), st, "choose")}}
    if v in ("go", "node", "n", "path"):
        a = " ".join(a).replace("(", " ").replace(")", " ").replace(",", " ").split()  # go (3,5) / go 3,5
        if len(a) >= 2:
            return {"action": "select_map_node", "args": {"col": _int(a[0], "col"), "row": _int(a[1], "row")}}
        choices = (st or {}).get("choices") or []
        i = _int(a[0] if a else "", "choice")
        if not refs.first:
            # go N = the Nth listed move: only on the map screen the chain started from.
            if dec != refs.snap.get("decision") or [(c.get("col"), c.get("row")) for c in choices] != \
                    [(c.get("col"), c.get("row")) for c in refs.snap.get("choices") or []]:
                raise StopStep(t(f"'go {i}' means the {i}th move on the map you saw; the game is now at {dec} - use go COL ROW",
                                 f"'go {i}' 指你看到的地图上的第 {i} 个选项；游戏现在处于 {dec}——请用 go COL ROW"))
        if not 0 <= i < len(choices):
            raise VerbError(f"go <0-{len(choices) - 1}> picks a map choice (or go <col> <row>)")
        return {"action": "select_map_node", "args": {"col": choices[i]["col"], "row": choices[i]["row"]}}
    if v in ("pick", "take"):
        n = _int(a[0] if a else "", "index")
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
        raise VerbError(f"'pick' does not apply to {dec}")
    if v in ("select", "sel", "s"):
        if [x.lower() for x in a] in (["none"], ["-"]):
            return {"action": "select_cards", "args": {"indices": ""}}  # confirm nothing (min_select 0)
        idx = ",".join(x for part in a for x in part.split(",") if x.strip())
        if not idx:
            raise VerbError("select <i,j,...> (or select none: confirm with no card when min_select is 0)")
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
        if dec not in m:
            raise VerbError(f"'skip' does not apply to {dec}")
        return {"action": m[dec]}
    if v in ("alt", "alternative", "reroll"):
        return {"action": "select_card_alternative",
                "args": {"alternative_index": refs.screen("alternatives", _int(a[0] if a else "", "alternative"), st, "alt")}}
    if v == "claim":
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
        return {"action": verb[0], "args": {verb[1]: refs.screen(key, _int(idx, "index"), st, f"buy {key[:-1]}")}}
    if v in ("remove", "removal"):
        return {"action": "remove_card"}
    if v in ("proceed", "continue", "next"):
        return {"action": "proceed"}
    if v in ("leave", "exit"):
        return {"action": "leave_room"}
    if v in ("bundle", "pack"):
        return {"action": "select_bundle", "args": {"bundle_index": refs.screen("bundles", _int(a[0] if a else "", "bundle"), st, "bundle")}}
    if v == "relic":
        return {"action": "select_treasure_relic", "args": {"relic_index": refs.screen("relics", _int(a[0] if a else "", "relic"), st, "relic")}}
    if v == "click":
        if len(a) < 2:
            raise VerbError("click <x> <y> big|small")
        if len(a) < 3 or a[2].lower() not in ("big", "small"):
            raise VerbError("click <x> <y> big|small  (the tool is part of the choice: big = 3x3, small = 1 cell)")
        return {"action": "crystal_sphere_click", "args": {"x": _int(a[0], "x"), "y": _int(a[1], "y"),
                                                           "tool": a[2].lower()}}
    raise VerbError(f"unknown step '{text}' (sts2 help lists the verbs)")


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


def describe_refs(action, st, rewards_seen=None):
    """What each index in an engine action refers to in `st` (for error messages)."""
    a = action.get("action")
    args = action.get("args") or {}
    out = []
    cb = Refs.combat_of(st) or {}

    def listing(items, prefix):
        return ", ".join(f"{prefix}{x.get('index')} {x.get('name') or x.get('title')}" for x in items if isinstance(x, dict)) \
            or t("none", "无")

    if "card_index" in args and a == "play_card":
        i, hand = args["card_index"], cb.get("hand") or []
        c = next((x for x in hand if x.get("index") == i), None)
        out.append(t(f"H{i} is now {card_ref_text(c)}", f"H{i} 现在是 {card_ref_text(c)}") if c else
                   t(f"there is no H{i} (hand: {listing(hand, 'H')})", f"没有 H{i}（手牌：{listing(hand, 'H')}）"))
    if "potion_index" in args and a in ("use_potion", "discard_potion"):
        i = args["potion_index"]
        pots = [x for x in (st.get("player") or {}).get("potions") or [] if isinstance(x, dict)]
        p = next((x for x in pots if x.get("index") == i), None)
        if p:
            tt = _TARGET_TEXT.get(p.get("target_type"))
            out.append(t(f"P{i} is now {p.get('name')} ({tt[0] if tt else p.get('target_type')}, {p.get('usage')}"
                         + ("" if p.get("can_use", True) else ", can't be used now") + ")",
                         f"P{i} 现在是 {p.get('name')}（{tt[1] if tt else p.get('target_type')}，{p.get('usage')}"
                         + ("" if p.get("can_use", True) else "，现在不能使用") + "）"))
        else:
            out.append(t(f"there is no P{i} (potions: {listing(pots, 'P')})", f"没有 P{i}（药水：{listing(pots, 'P')}）"))
    if "target_index" in args:
        j, ens = args["target_index"], cb.get("enemies") or []
        e = next((x for x in ens if x.get("index") == j), None)
        out.append(t(f"E{j} is {e.get('name')} (HP {e.get('hp')}/{e.get('max_hp')})", f"E{j} 是 {e.get('name')}（生命 {e.get('hp')}/{e.get('max_hp')}）")
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
            out.append(t(f"there is no {what} {i} here (have: {have})", f"这里没有 {what} {i}（现有：{have}）"))
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
                       t(f"there is no card {x} (0-{len(cards) - 1})", f"没有卡牌 {x}（0-{len(cards) - 1}）"))
        if st.get("min_select") is not None:
            out.append(t(f"this screen takes {st.get('min_select')}-{st.get('max_select')} cards",
                         f"此界面需选择 {st.get('min_select')}-{st.get('max_select')} 张牌"))
    if a == "select_map_node":
        ch = st.get("choices") or []
        legal = ", ".join(f"go {c.get('col')} {c.get('row')} {zh_term('room', c.get('type'))}" for c in ch) or t("none", "无")
        out.append(t(f"({args.get('col')},{args.get('row')}) is not a move you can make now (legal: {legal}; sts2 map shows COL/ROW)",
                     f"({args.get('col')},{args.get('row')}) 现在不能前往（可走：{legal}；sts2 map 显示列/行号）"))
    return out


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
  select 0,2        card selection                skip  (card reward, cancelable selection, treasure)
  alt N             card reward alternative       claim N   proceed   leave   remove
  buy card|relic|potion N                         click X Y big|small  (Crystal Sphere)
  raw {"action":"...","args":{...}}               any engine verb as verb key=value

Chains - PREFER CARD NAMES (play Strike): H#/E# renumber as soon as a card leaves the hand
or an enemy dies (after `play H0` the old H1 is H0). In one chain every index means the view
shown when you submitted it (the last state/do reply):
  H# / P#   that card / potion wherever it is now (by uid); gone (played, discarded,
            exhausted, transformed) -> the chain stops, never a different card.
            "play H0; play H1" = the cards shown as H0 and H1; "play H0; play H0" stops.
  E#        that enemy (by combat_id); dead -> stop.     N   that numbered action, remapped.
  choose / pick / claim / buy / select / alt / go N   only on the same kind of screen and
            while that item is unchanged; a screen you have not seen yet (the card reward
            `claim 2` opens) stops the chain before such a step. raw / engine verbs: as written.
The chain goes on while the next step is legal and unambiguous where the game is now, also
after the decision type changed ("pick 1; proceed", "select 1; end" after a mid-turn card
selection). It stops before the next step on a new room, a fight starting or ending, a new
turn, the end of the run, a step that does not apply to the new decision, a potion step on
another screen than the chain started at, an index that can't be resolved (above), and at
the first error (which says what each index refers to now).
Reward numbers are STABLE: claiming one does not renumber the rest ("claim 0; claim 1" is
fine); claiming a gone index says it was already claimed.

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
            self._notes.append(f"{t('note', '注意')}: {t('the engine had stopped', '引擎已停止')}; {self._recover('engine exited while idle')}")
        try:
            r = self.engine.send(cmd)
        except Exception as e:
            note = self._recover(str(e))
            if not record:
                # A query changes nothing: just ask the restarted engine.
                try:
                    return self.engine.send(cmd)
                except Exception as e2:
                    return {"type": "error", "code": "engine_failed", "message": f"The engine failed again ({e2}); {note}."}
            return {"type": "error", "code": "engine_restarted",
                    "message": f"The engine failed on this command ({e}); {note}. The command was not applied; "
                               "sts2 state shows where the run is."}
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
        note = self._recover("daemon restarted", count=False).replace("it was restarted and ", "")
        self._notes.append(f"{t('note', '注意')}: {t('the sts2 daemon restarted', 'sts2 守护进程已重启')}; {note}")

    @staticmethod
    def _fingerprint(st):
        if not st:
            return None
        p = st.get("player") or {}
        ctx = st.get("context") or {}
        return (st.get("decision"), ctx.get("seed"), ctx.get("act"), ctx.get("floor"), p.get("hp"),
                p.get("max_hp"), p.get("gold"), p.get("deck_size"), st.get("round"))

    def _recover(self, why, count=True):
        """Restart the engine and replay this run (the start command carries the seed the
        engine chose, so the replay is the same run). Says so if the replay diverged."""
        if count:
            self.restarts += 1
        before = self._fingerprint(self.last)
        self._close_engine()
        try:
            self._start_engine()
        except Exception as e:
            return f"restarting it failed too ({e})"
        if not self.history:
            return "it was restarted (no run to restore)"
        replayed, last = 0, None
        for cmd in self.history:
            try:
                r = self.engine.send(cmd)
            except Exception as e:
                self.history = self.history[:replayed]
                self._save_run()
                return f"it was restarted but replaying the run failed at step {replayed} ({e})"
            replayed += 1
            st = r.get("state") if r.get("type") == "error" and isinstance(r.get("state"), dict) else r
            if st.get("type") == "decision":
                last = st
        if last is not None:
            self._set_last(last)
        after = self._fingerprint(last)
        if before is not None and after != before:
            return (f"it was restarted and the run was replayed ({replayed} commands), but the replay "
                    f"DIVERGED from the run you were playing ({before} -> {after}); sts2 state shows the replayed game")
        return f"it was restarted and the run was replayed ({replayed} commands)"

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
        for verb, cmd in self._HINTS:
            m = m.replace(f"use {verb}", f"use {cmd}").replace(f"'{verb}'", f"'{cmd}'")
        if r.get("code") == "no_run" or "No run in progress" in m:
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
            return t(f"the run moved on to a new room ({dec})", f"已进入新的房间（{dec}）")
        if c0 and not c1:
            return t(f"the fight is over (now at {dec})", f"战斗已结束（现在处于 {dec}）")
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
                if i > 0 and dec != start.get("decision"):
                    # The step was written for another screen than the one the game is at now.
                    results.append({"step": step, "ok": False, "stopped": True,
                                    "error": t(f"not run: the game is now at {dec}: {e}", f"未执行：游戏现在处于 {dec}：{e}")})
                else:
                    results.append({"step": step, "ok": False, "error": str(e)})
                    code = 1
                break
            act = action.get("action")
            if i > 0 and dec in NATIVE and act not in NATIVE[dec] and act not in ("use_potion", "discard_potion"):
                results.append({"step": step, "ok": False, "stopped": True,
                                "error": t(f"not run: the game is now at {dec}, where '{step}' does not apply",
                                           f"未执行：游戏现在处于 {dec}，'{step}' 不适用")})
                break
            if i > 0 and act in ("use_potion", "discard_potion") and dec != start.get("decision"):
                results.append({"step": step, "ok": False, "stopped": True,
                                "error": t(f"not run: the game is now at {dec} (the chain started at {start.get('decision')}); "
                                           "a potion step only runs on the screen the chain started at",
                                           f"未执行：游戏现在处于 {dec}（链开始于 {start.get('decision')}）；药水步骤只在链开始时的界面上执行")})
                break
            label = action_label(action, st) if st else act
            if refs.moved:
                label += "  (" + t("shown as ", "原为 ") + ", ".join(m.split(" -> ")[0] for m in refs.moved) + ")"
            r = self.send({"cmd": "action", **action}, record=True)
            if r.get("type") == "error":
                results.append({"step": step, "ok": False, "error": self._explain(action, st, r)})
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

    def _explain(self, action, st, r):
        """An engine error for one step, led by what each index in it refers to now."""
        import re
        msg = str(r.get("message") or r.get("code"))
        if r.get("code") != "illegal_action":
            return msg
        m = re.match(r"'[a-z_]+' is not allowed now: (.*?)(?: Current decision: [a-z_]+\.)?(?: (Legal: .*))?$", msg, re.S)
        reason, legal = (m.group(1), m.group(2)) if m else (msg, None)
        inner = re.search(r"is not one of the legal actions \((.*)\)\.?$", reason)
        if inner:
            reason = inner.group(1)
        refs = describe_refs(action, st or {}, self._rewards_seen)
        if not refs:
            return msg
        if action.get("action") == "claim_reward" and (action.get("args") or {}).get("reward_index") in self._rewards_seen \
                and "no reward at index" in reason:
            return refs[0] + (f". {legal}" if legal else "")
        head = "; ".join(refs)
        if reason.rstrip(".").lower() in head.lower():
            return head + (f". {legal}" if legal else "")
        return head + " - " + reason.rstrip(".") + (f". {legal}" if legal else "")

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
                lines.append(f" P{x.get('index')} {x.get('name')} [{x.get('target_type')}, {x.get('usage')}]{_desc(x.get('description'))}")
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
