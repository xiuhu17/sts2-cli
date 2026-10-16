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
               "Status": "策略", "Stun": "击晕", "Summon": "召唤", "Unknown": "未知"},
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
    for l in (lang, "eng"):
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


def intent_text(it):
    typ = it.get("type", "?")
    s = zh_term("intent", typ)
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


EVENT_FMT = {
    "turn": lambda e: (t("-- your turn ", "-- 你的回合 ") if e.get("side") == "player" else t("-- enemy turn ", "-- 敌人回合 ")) + f"{e.get('round')} --",
    "card_played": lambda e: t("played ", "打出 ") + e.get("card", "?") + (f" -> {e['target']}" if e.get("target") else "") + (t(" (auto)", "（自动）") if e.get("auto") else ""),
    "damage": lambda e: (f"{e['source']} " if e.get("source") else "") + t("hit ", "伤害 ") + f"{e.get('target')} {e.get('amount')}"
              + (f" ({t('blocked', '格挡')} {e['blocked']})" if e.get("blocked") else "") + (f" [{e['card']}]" if e.get("card") else "")
              + (t(" KILLED", " 击杀") if e.get("killed") else ""),
    "block": lambda e: f"{e.get('target')} +{e.get('amount')} {t('Block', '格挡')}",
    "power": lambda e: f"{e.get('target')} {e.get('power')} {'+' if (e.get('amount') or 0) >= 0 else ''}{e.get('amount')}"
             + (f" ({t('from', '来自')} {e['source']})" if e.get("source") else ""),
    "enemy_move": lambda e: f"{e.get('enemy')} {t('acts', '行动')}" + (f" [{', '.join(zh_term('intent', i) for i in e['intents'])}]" if e.get("intents") else ""),
    "draw": lambda e: t("drew ", "抽到 ") + ", ".join(e.get("cards") or []),
    "discard": lambda e: t("discarded ", "丢弃 ") + str(e.get("card")),
    "exhaust": lambda e: t("exhausted ", "消耗 ") + str(e.get("card")),
    "card_added": lambda e: t("card added: ", "加入卡牌：") + str(e.get("card")) + (f" -> {e['pile']}" if e.get("pile") else ""),
    "afflicted": lambda e: f"{e.get('card')} <{e.get('affliction')}>",
    "orb_channeled": lambda e: t("channeled ", "生成 ") + str(e.get("orb")),
    "potion_used": lambda e: t("used ", "使用 ") + str(e.get("potion")) + (f" -> {e['target']}" if e.get("target") else ""),
    "stars": lambda e: f"{t('Stars', '辉星')} {'+' if e.get('amount', 0) >= 0 else ''}{e.get('amount')}",
    "summon": lambda e: t("summon ", "召唤 ") + f"{e.get('amount')}",
    "relic": lambda e: t("relic: ", "遗物：") + str(e.get("relic")),
    "enemy_died": lambda e: f"{e.get('enemy')} {t('died', '死亡')}",
    "gold": lambda e: f"{t('Gold', '金币')} {e['change']:+d} (= {e.get('gold')})",
    "max_hp": lambda e: f"{t('Max HP', '最大生命')} {e['change']:+d} (= {e.get('max_hp')})",
    "hp": lambda e: f"{t('HP', '生命')} {e['change']:+d} (= {e.get('hp')})",
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
        return f"go ({args.get('col')},{args.get('row')}) {zh_term('room', ch.get('type', ''))}"
    if act == "choose_option":
        o = next((o for o in st.get("options") or [] if o.get("index") == args.get("option_index")), {})
        title = o.get("title") or rest_title(o)
        return f"choose {args.get('option_index')} {title or ''}".rstrip()
    if act == "select_cards":
        if "choose" in a:
            ch = a["choose"]
            return f"select {ch.get('min')}-{ch.get('max')} of {ch.get('from')}  (sts2 do \"select 0,2\")"
        idx = str(args.get("indices"))
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
        return "click X Y big|small  (sts2 do \"click 2 3 big\")"
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


def render_actions(st, numbered):
    out = [t("Actions:", "可选操作：")]
    line = []
    discards = []
    for n, a in numbered:
        if a.get("action") == "discard_potion":
            discards.append(f"[{n}] P{(a.get('args') or {}).get('potion_index')}")
            continue
        out.append(f" [{n}] {action_label(a, st)}")
    if discards:
        out.append(f" {t('discard potion', '丢弃药水')}: {' '.join(discards)}")
    return out


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
        out.append(f"Osty: " + (f"{t('HP', '生命')} {osty.get('hp')}/{osty.get('max_hp')} {t('Block', '格挡')} {osty.get('block')}"
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
    out.append(hand_title or t("Hand:", "手牌："))
    for c in hand:
        flag = "" if c.get("can_play", True) else t(" [unplayable]", " [不可打出]")
        dmg = ""
        if c.get("damage_by_target") and len(c["damage_by_target"]) > 1:
            dmg = "  {" + ", ".join(f"E{d.get('target_index')}:{d.get('total_damage', d.get('damage'))}" for d in c["damage_by_target"]) + "}"
        out.append(card_line(c, gl, prefix=f" H{c.get('index')} ") + flag + dmg)
    return out


def render_decision(st, numbered, extra_events=None):
    gl = Glossary()
    d = st.get("decision")
    out = []
    ev = event_lines(extra_events if extra_events is not None else st.get("events"))
    if ev:
        out.append(t("Since last decision:", "上次决策以来："))
        out.extend(f"  {x}" for x in ev)
    out.extend(header(st))
    body = []
    if d == "combat_play":
        body = render_combat_core(st, gl)
    elif d == "card_select":
        purpose = st.get("purpose", "choose")
        src = (st.get("source") or {}).get("name")
        mn, mx = st.get("min_select"), st.get("max_select")
        body.append(f"{t('CARD SELECT', '选牌')} [{purpose}] {st.get('prompt') or ''}".rstrip())
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
                line += f" || {t('becomes one of', '变为其一')}: {', '.join(c['transform_options'])}"
            body.append(line)
        if st.get("combat"):
            body.append(t("-- battlefield --", "-- 战场 --"))
            body.extend(render_combat_core(st["combat"], gl, hand_title=t("Hand (now):", "当前手牌：")))
    elif d == "card_reward":
        body.append(t("CARD REWARD", "卡牌奖励") + (t(" (skippable)", "（可跳过）") if st.get("can_skip", True) else ""))
        for c in st.get("cards") or []:
            body.append(card_line(c, gl, prefix=f" {c.get('index')}) "))
    elif d == "bundle_select":
        body.append(t("CHOOSE A PACK", "选择卡包"))
        for b in st.get("bundles") or []:
            body.append(f" {t('Pack', '卡包')} {b.get('index')}:")
            for c in b.get("cards") or []:
                body.append(card_line(c, gl, prefix="    "))
    elif d == "event_choice":
        body.append(f"{t('EVENT', '事件')}: {st.get('event_name')}")
        if st.get("description"):
            body.append(f"  {_one_line(st['description'])}")
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
        body.append(t("SHOP", "商店") + f"  ({t('card removal', '移除卡牌')}: {st.get('card_removal_cost')}g)")
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
        body.append(t("REWARDS", "奖励"))
        for w in st.get("rewards") or []:
            body.append(f" {w.get('index')}) {reward_text(w)}")
    elif d == "treasure_relic":
        body.append(t("TREASURE", "宝箱"))
        for r in st.get("relics") or []:
            body.append(f" {r.get('index')}) {r.get('name')}{_desc(r.get('description'))}")
    elif d == "map_select":
        body.append(t("MAP: choose the next room (sts2 map: whole act)", "地图：选择下一个房间（sts2 map 查看整幕）"))
    elif d == "crystal_sphere":
        body.append(f"CRYSTAL SPHERE  {t('divinations left', '剩余占卜')}: {st.get('divinations_left')}")
        for row in st.get("rows") or []:
            body.append("  " + (row if isinstance(row, str) else json.dumps(row, ensure_ascii=False)))
        if st.get("items_seen"):
            body.append(f"  {t('seen', '已见')}: {json.dumps(st['items_seen'], ensure_ascii=False)}")
        body.append(f"  hidden cells: {json.dumps(st.get('hidden_cells'))}")
    elif d == "game_over":
        body.append((t("VICTORY", "胜利") if st.get("victory") else t("DEFEAT", "失败")) +
                    f"  {t('Floor', '楼层')} {st.get('floor')}  ({t('sts2 start ... for a new run', 'sts2 start ... 开始新一局')})")
    else:
        rest = {k: v for k, v in st.items() if k not in ("legal_actions", "player", "context", "events")}
        body.append(json.dumps(rest, ensure_ascii=False)[:3000])
    out.extend(body)
    if numbered:
        out.extend(render_actions(st, numbered))
    gloss = gl.render()
    text = "\n".join(out)
    if gloss and len(text) + sum(len(x) + 1 for x in gloss) < MAX_OUT:
        text += "\n" + "\n".join(gloss)
    return clip(text)


def clip(text):
    b = text.encode("utf-8")
    if len(b) <= MAX_OUT:
        return text
    cut = b[:MAX_OUT].decode("utf-8", "ignore")
    return cut + f"\n... ({t('truncated; use --json or the query commands for the rest', '已截断；用 --json 或查询命令查看其余部分')})"


def render_map(m, st):
    rows = m.get("rows") or []
    sym = {"Monster": "M", "Elite": "E", "RestSite": "R", "Shop": "$", "Treasure": "T", "Unknown": "?",
           "Event": "?", "Boss": "B", "Ancient": "A"}
    choices = {(c.get("col"), c.get("row")) for c in (st or {}).get("choices") or []} if st and st.get("decision") == "map_select" else set()
    width = 7
    for r in rows:
        for n in r:
            width = max(width, (n.get("col") or 0) + 1)
    lines = [t("Map (bottom = start; [x] visited, <x> you are here, *x* reachable now; "
               "M monster E elite R rest $ shop T treasure ? unknown B boss A ancient; "
               "a visited ? shows what it was in lowercase: m e $ t, ? = event)",
               "地图（底部为起点；[x] 已走过，<x> 当前位置，*x* 现在可去；M 敌人 E 精英 R 休息 $ 商店 T 宝箱 ? 未知 B Boss A 先古之民；"
               "去过的 ? 用小写显示实际房间：m e $ t，? 为事件）")]
    grid = []
    for r in rows:
        if not r:
            continue
        row_i = r[0].get("row")
        cells = ["   "] * width
        links = [" "] * (width * 4)
        for n in r:
            c = n.get("col") or 0
            s = sym.get(n.get("type"), "o")
            if n.get("revealed_type"):
                # A visited "?" shows what it turned out to be (lowercase), like the game's icon.
                s = sym.get(n["revealed_type"], "?").lower()
            if n.get("current"):
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
        grid.append((row_i, " ".join(cells), "".join(links).rstrip()))
    for row_i, cells, links in reversed(grid):
        if links.strip():
            lines.append(f"     {links}")
        lines.append(f"{row_i:>3}  {cells}")
    if m.get("boss"):
        b = m["boss"]
        lines.insert(1, f"Boss: {b.get('name') if isinstance(b, dict) else b}")
    return "\n".join(lines)


# ───────────────────────────── verbs ─────────────────────────────

NATIVE = {
    "map_select": {"select_map_node"},
    "combat_play": {"play_card", "end_turn", "use_potion"},
    "card_reward": {"select_card_reward", "skip_card_reward"},
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
    "claim_reward", "skip_rewards", "select_treasure_relic", "skip_treasure_relic",
}


class VerbError(Exception):
    pass


def _int(s, what):
    s = str(s).strip()
    if s[:1].upper() in ("H", "E", "P") and s[1:].isdigit():
        s = s[1:]
    try:
        return int(s)
    except ValueError:
        raise VerbError(f"{what} must be a number, got '{s}'") from None


def _find_by_name(items, ref, what, prefix):
    ref_s = str(ref).strip()
    if ref_s[:1].upper() == prefix and ref_s[1:].isdigit():
        return int(ref_s[1:])
    if ref_s.lstrip("-").isdigit():
        return int(ref_s)
    low = ref_s.lower()
    exact = [x for x in items if str(x.get("name", "")).lower() == low]
    pref = exact or [x for x in items if str(x.get("name", "")).lower().startswith(low)]
    if not pref:
        names = ", ".join(f"{prefix}{x.get('index')} {x.get('name')}" for x in items)
        raise VerbError(f"no {what} matching '{ref_s}' (have: {names})")
    if what == "card":
        playable = [x for x in pref if x.get("can_play", True)]
        pref = playable or pref
    return pref[0].get("index")


def parse_verb(text, st, numbered):
    """One step of `sts2 do` -> engine action dict {action, args}."""
    words = text.split()
    if not words:
        raise VerbError("empty step")
    v = words[0].lower()
    a = words[1:]
    dec = (st or {}).get("decision")
    if v.isdigit() and not a:
        n = int(v)
        for num, act in numbered:
            if num == n:
                if "choose" in act or "choose_cell" in act:
                    raise VerbError(f"action {n} needs arguments: {action_label(act, st)}")
                return {"action": act["action"], **({"args": act["args"]} if act.get("args") else {})}
        raise VerbError(f"no action number {n} (have 1-{len(numbered)}; run 'sts2 state')")
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
        hand = (st or {}).get("hand") or []
        args = {"card_index": _find_by_name(hand, a[0], "card", "H")}
        if len(a) > 1:
            args["target_index"] = _find_by_name((st or {}).get("enemies") or [], " ".join(a[1:]), "enemy", "E")
        return {"action": "play_card", "args": args}
    if v in ("end", "e", "endturn", "end_turn"):
        return {"action": "end_turn"}
    if v in ("potion", "use", "pot"):
        if not a:
            raise VerbError("potion <P0 | 0> [target]")
        args = {"potion_index": _int(a[0], "potion")}
        if len(a) > 1:
            args["target_index"] = _find_by_name((st or {}).get("enemies") or [], " ".join(a[1:]), "enemy", "E")
        return {"action": "use_potion", "args": args}
    if v in ("discard", "toss"):
        return {"action": "discard_potion", "args": {"potion_index": _int(a[0] if a else "", "potion")}}
    if v in ("choose", "opt", "option", "o", "c"):
        return {"action": "choose_option", "args": {"option_index": _int(a[0] if a else "", "option")}}
    if v in ("go", "node", "n", "path"):
        if len(a) >= 2:
            return {"action": "select_map_node", "args": {"col": _int(a[0], "col"), "row": _int(a[1], "row")}}
        choices = (st or {}).get("choices") or []
        i = _int(a[0] if a else "", "choice")
        if not 0 <= i < len(choices):
            raise VerbError(f"go <0-{len(choices) - 1}> picks a map choice (or go <col> <row>)")
        return {"action": "select_map_node", "args": {"col": choices[i]["col"], "row": choices[i]["row"]}}
    if v in ("pick", "take"):
        n = _int(a[0] if a else "", "index")
        if dec == "card_reward":
            return {"action": "select_card_reward", "args": {"card_index": n}}
        if dec == "treasure_relic":
            return {"action": "select_treasure_relic", "args": {"relic_index": n}}
        if dec == "rewards":
            return {"action": "claim_reward", "args": {"reward_index": n}}
        if dec == "card_select":
            return {"action": "select_cards", "args": {"indices": str(n)}}
        if dec == "bundle_select":
            return {"action": "select_bundle", "args": {"bundle_index": n}}
        raise VerbError(f"'pick' does not apply to {dec}")
    if v in ("select", "sel", "s"):
        idx = ",".join(x for part in a for x in part.split(",") if x.strip())
        if not idx:
            raise VerbError("select <i,j,...>")
        return {"action": "select_cards", "args": {"indices": idx}}
    if v == "skip":
        m = {"card_reward": "skip_card_reward", "card_select": "skip_select", "treasure_relic": "skip_treasure_relic",
             "rewards": "proceed"}
        if dec not in m:
            raise VerbError(f"'skip' does not apply to {dec}")
        return {"action": m[dec]}
    if v == "claim":
        return {"action": "claim_reward", "args": {"reward_index": _int(a[0] if a else "", "reward")}}
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
        return {"action": verb[0], "args": {verb[1]: _int(idx, "index")}}
    if v in ("remove", "removal"):
        return {"action": "remove_card"}
    if v in ("proceed", "continue", "next"):
        return {"action": "proceed"}
    if v in ("leave", "exit"):
        return {"action": "leave_room"}
    if v in ("bundle", "pack"):
        return {"action": "select_bundle", "args": {"bundle_index": _int(a[0] if a else "", "bundle")}}
    if v == "relic":
        return {"action": "select_treasure_relic", "args": {"relic_index": _int(a[0] if a else "", "relic")}}
    if v == "click":
        if len(a) < 2:
            raise VerbError("click <x> <y> [big|small]")
        return {"action": "crystal_sphere_click", "args": {"x": _int(a[0], "x"), "y": _int(a[1], "y"),
                                                           "tool": a[2] if len(a) > 2 else "big"}}
    raise VerbError(f"unknown step '{text}' (sts2 help lists the verbs)")


HELP = """sts2 - play Slay the Spire 2 (headless) from the shell. One command per step.

  sts2 start --character Ironclad|Silent|Defect|Regent|Necrobinder [--seed S] [--ascension 0-10]
             [--lang en|zh] [--profile PATH] [--manual-rewards] [--modifiers A,B] [--game-mode custom]
  sts2 state                 the current decision, with numbered legal actions
  sts2 do N                  take legal action number N (from the last state shown)
  sts2 do "STEP; STEP; ..."  several steps; stops at the first error or when a step does
                             not fit the decision the game is now in (e.g. a card selection)
  sts2 deck [--upgrades]     the deck with full card text     sts2 relics | potions
  sts2 piles [draw|discard|exhaust|hand]  combat piles (draw pile sorted, order hidden)
  sts2 card N | card hN | card ID [--upgraded]   one card in detail (deck index, hand index, id)
  sts2 map                   the act map                      sts2 log [N]  recent events
  sts2 --json <command>      raw engine JSON                  sts2 stop     stop the daemon

Steps for `do` (indices are read from the state at the moment each step runs):
  N                 legal action number N         play H2 [E1] | play Strike [Jaw Worm]
  end               end turn                      potion P0 [E1]   discard P0
  choose N          event / rest site option      go N (Nth map choice) | go COL ROW
  pick N            card reward / treasure relic / reward / pack
  select 0,2        card selection                skip  (card reward, cancelable selection, treasure)
  buy card|relic|potion N   remove   claim N   proceed   leave   click X Y big|small
  raw {"action":"...","args":{...}}               any engine verb as verb key=value

Rules: only legal actions are accepted (the error lists them). Each reply starts with what
happened since the previous decision (cards played, damage, enemy moves, relic triggers...).
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
        if record and r.get("type") != "error":
            self.history.append(cmd)
        elif record and r.get("type") == "error" and r.get("code") == "flow_failed":
            self.history.append(cmd)
        return r

    def _recover(self, why):
        self.restarts += 1
        self._close_engine()
        try:
            self._start_engine()
        except Exception as e:
            return f"restarting it failed too ({e})"
        replayed = 0
        for cmd in self.history:
            try:
                r = self.engine.send(cmd)
            except Exception as e:
                self.history = self.history[:replayed]
                return f"it was restarted but replaying the run failed at step {replayed} ({e})"
            replayed += 1
            if r.get("type") == "decision":
                self._set_last(r)
        return f"it was restarted and the run was replayed ({replayed} commands)" if self.history else "it was restarted (no run to restore)"

    def _set_last(self, r):
        st = r.get("state") if r.get("type") == "error" and isinstance(r.get("state"), dict) else r
        if st.get("type") == "decision":
            self.last = st
            self.numbered = list(enumerate(st.get("legal_actions") or [], 1))

    # requests
    def handle(self, argv, as_json):
        cmd, args = argv[0], argv[1:]
        fn = getattr(self, "cmd_" + cmd.replace("-", "_"), None)
        if fn is None:
            return f"unknown command '{cmd}'. " + "Commands: start state do deck relics potions piles card map log help stop", 1
        return fn(args, as_json)

    def _out_engine(self, r, as_json, render):
        if as_json:
            return json.dumps(r, ensure_ascii=False, separators=(",", ":")), 0 if r.get("type") != "error" else 1
        if r.get("type") == "error":
            return self._error_text(r), 1
        return render(r), 0

    def _error_text(self, r):
        msg = f"error: {r.get('message')}"
        if r.get("code") == "flow_failed" and isinstance(r.get("state"), dict):
            self._set_last(r["state"])
            self._remember_events(r["state"].get("events"))
            return msg + "\n" + render_decision(r["state"], self.numbered)
        if r.get("legal_actions") is not None and self.last is not None:
            msg += f"\n({t('sts2 state shows the current decision', 'sts2 state 显示当前决策')})"
        return msg

    def _remember_events(self, events):
        for line in event_lines(events):
            self.log.append(line)
        del self.log[:-400]

    def cmd_help(self, args, as_json):
        if as_json:
            r = self.send({"cmd": "help"})
            return json.dumps(r, ensure_ascii=False, separators=(",", ":")), 0
        return HELP, 0

    def cmd_start(self, args, as_json):
        import argparse
        p = argparse.ArgumentParser(prog="sts2 start", add_help=False)
        p.add_argument("--character", "-c", default="Ironclad")
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
        cmd = {"cmd": "start_run", "character": o.character, "ascension": o.ascension, "lang": o.lang,
               "omit_deck": True}
        if o.seed:
            cmd["seed"] = o.seed
        if o.profile:
            cmd["profile"] = os.path.abspath(os.path.expanduser(o.profile))
        if o.manual_rewards:
            cmd["manual_rewards"] = True
        if o.modifiers:
            cmd["modifiers"] = [m for m in o.modifiers.split(",") if m]
        if o.game_mode:
            cmd["game_mode"] = o.game_mode
        r = self.send(cmd)
        if r.get("type") == "decision":
            LANG["lang"] = o.lang
            self.history = [cmd]
            self.log = []
            self._set_last(r)
            self._remember_events(r.get("events"))
        return self._out_engine(r, as_json, lambda r: render_decision(r, self.numbered))

    def _need_run(self):
        if self.last is None:
            return t("No run in progress: sts2 start --character Ironclad", "没有进行中的对局：sts2 start --character Ironclad")
        return None

    def cmd_state(self, args, as_json):
        r = self.send({"cmd": "get_state"})
        if r.get("type") == "decision":
            self._set_last(r)
        return self._out_engine(r, as_json, lambda r: render_decision(r, self.numbered))

    def cmd_do(self, args, as_json):
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
        for i, step in enumerate(steps):
            st = self.last
            try:
                action = parse_verb(step, st, self.numbered)
            except VerbError as e:
                results.append({"step": step, "ok": False, "error": str(e)})
                code = 1
                break
            dec = st.get("decision") if st else None
            if i > 0 and dec in NATIVE and action.get("action") not in NATIVE[dec] \
                    and action.get("action") not in ("use_potion", "discard_potion"):
                results.append({"step": step, "ok": False, "stopped": True,
                                "error": f"not run: the game is now at {dec}"})
                break
            label = action_label(action, st) if st else action.get("action")
            r = self.send({"cmd": "action", **action}, record=True)
            if r.get("type") == "error":
                results.append({"step": step, "ok": False, "error": r.get("message")})
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
            lines.append(render_decision(shown, self.numbered, extra_events=events))
        return clip("\n".join(lines)), code

    def _simple(self, cmd, as_json, render):
        r = self.send(cmd)
        return self._out_engine(r, as_json, render)

    def cmd_deck(self, args, as_json):
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
        def render(r):
            gl = Glossary()
            lines = [f"{t('Potions', '药水')} ({len(r.get('potions') or [])}/{r.get('potion_slots')}):"]
            for x in r.get("potions") or []:
                lines.append(f" P{x.get('index')} {x.get('name')} [{x.get('target_type')}, {x.get('usage')}]{_desc(x.get('description'))}")
                gl.add(x.get("tips"))
            return clip("\n".join(lines + gl.render()))
        return self._simple({"cmd": "get_potions"}, as_json, render)

    def cmd_piles(self, args, as_json):
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
        r = self.send({"cmd": "get_map"})
        return self._out_engine(r, as_json, lambda m: clip(render_map(m, self.last)))

    def cmd_log(self, args, as_json):
        n = int(args[0]) if args and args[0].isdigit() else 40
        lines = self.log[-n:]
        if as_json:
            return json.dumps(lines, ensure_ascii=False), 0
        return clip("\n".join(lines) if lines else t("(no events yet)", "（暂无事件）")), 0

    def cmd_status(self, args, as_json):
        alive = self.engine is not None and self.engine.proc.poll() is None
        return (f"daemon pid {os.getpid()}, engine {'running' if alive else 'stopped'}, "
                f"restarts {self.restarts}, run steps {len(self.history)}, log {self.log_path}"), 0

    def cmd_stop(self, args, as_json):
        self._stop = True
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
        self._lock_file = open(self.sock_path[:-len(".sock")] + ".lock", "w")
        # (A daemon that is shutting down still holds it for a moment: wait briefly.)
        for attempt in range(150):
            try:
                fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if attempt == 149 or self._socket_alive():
                    return 0  # another daemon serves this session
                time.sleep(0.02)
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
        return 0



if __name__ == "__main__":
    sys.exit(Daemon(sys.argv[1] if len(sys.argv) > 1 else "default").serve())
