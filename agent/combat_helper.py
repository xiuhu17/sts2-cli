#!/usr/bin/env python3
"""Combat helper: plays the card-playing part of one combat through the bridge.

Usage: python3 agent/combat_helper.py [PORT]

Policy (stated, nothing else is decided here):
  - Only the engine's legal_actions are used: a card is played only if a legal play_card
    lists it, a target only if that play_card entry lists it.
  - Each step plays the best-scoring legal card (score: free cards first; block when the
    incoming damage would get through; powers when safe; attacks otherwise); targets are
    the legal target with the highest threat (one-shottable attackers first). When no legal
    card scores as worth playing, it ends the turn.
  - A potion is used only when HP < 20 or the incoming damage is lethal, and only potions
    the engine lists as usable (untargeted ones first).
  - Ties (equal card score, equal threat, several usable potions) are broken by a seeded
    random.Random (the caller's, or one seeded from the run seed), never by list order.
  - It STOPS and returns the state at every other decision (card selections from a card's
    effect, rewards, card rewards, map, ...): those are the caller's choices.
"""
import json
import random
import sys
import urllib.request

PORT = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 19044
URL = f"http://localhost:{PORT}"


def cmd(data):
    try:
        req = urllib.request.Request(URL, json.dumps(data).encode(), {'Content-Type': 'application/json'})
        return json.loads(urllib.request.urlopen(req, timeout=30).read())
    except Exception as e:
        return {"type": "error", "message": str(e)}


def send_action(act):
    return cmd({"cmd": "action", **act})


def _name(x):
    n = x.get("name") if isinstance(x, dict) else x
    if isinstance(n, dict):
        n = n.get("en") or next(iter(n.values()), "")
    return str(n or "")


def calc_incoming(enemies):
    """Total incoming damage from all enemies, accounting for multi-hit."""
    total = 0
    for en in enemies:
        for it in (en.get("intents") or []):
            if it.get("type") in ("Attack", "DeathBlow"):
                dmg = it.get("total_damage")
                if dmg is None:
                    dmg = it.get("damage", 0) * max(it.get("hits") or 1, 1)
                total += dmg
    return total


def enemy_threat(en):
    atk = calc_incoming([en])
    return atk * 100 + (200 - en.get("hp", 0))


def _rng(d, rng):
    return rng if rng is not None else random.Random(str((d.get("context") or {}).get("seed")))


def pick_target(enemies, targets, card_dmg=6, rng=None):
    """Best of the legal targets: a one-shottable attacker, else the highest threat
    (ties: rng). A target the engine does not list is never picked."""
    by = {e.get("index"): e for e in enemies}
    cands = [by[t] for t in sorted(targets) if t in by]
    if not cands:
        # Legal targets the state does not describe: any of them, chosen by rng.
        return (rng or random.Random()).choice(sorted(targets)) if targets else None
    oneshot = [e for e in cands if e.get("hp", 0) <= card_dmg and calc_incoming([e]) > 0]
    pool = oneshot or cands
    top = max(enemy_threat(e) for e in pool)
    return (rng or random.Random()).choice([e for e in pool if enemy_threat(e) == top]).get("index")


def card_priority(c, inc, blk, hp):
    """Lower = play first; None = not worth playing now."""
    block_gap = inc - blk
    lethal = block_gap >= hp
    can_tank = hp > inc * 2
    need_block = block_gap > 0 and (lethal or not can_tank or block_gap > 15)
    cost, ctype, tt = c.get("cost") or 0, c.get("type"), c.get("target_type", "")
    if ctype in ("Status", "Curse"):
        return None
    is_block = ctype == "Skill" and tt != "AnyEnemy"
    is_attack = tt == "AnyEnemy"
    if cost == 0 and is_block and need_block: return 1
    if cost == 0 and ctype == "Power": return 2
    if cost == 0 and is_attack: return 3
    if cost == 0: return 4
    if ctype == "Power": return (8 + cost) if (not lethal and inc <= 15) else ((15 + cost) if not lethal else 50)
    if is_block and lethal: return 2 + cost
    if is_block and need_block: return 10 + cost
    if is_block and inc == 0: return 45
    if is_block and block_gap <= 0: return 42
    if is_block: return 25 + cost
    if is_attack: return (12 + cost) if not lethal else 40
    return 20 + cost


def best_play(d, rng=None):
    """The legal play_card entry to send, or None."""
    rng = _rng(d, rng)
    hand = {c.get("index"): c for c in d.get("hand") or []}
    enemies = d.get("enemies") or []
    p = d.get("player") or {}
    inc, blk, hp = calc_incoming(enemies), p.get("block", 0), p.get("hp", 99)
    plays = {}
    for a in d.get("legal_actions") or []:
        if a.get("action") == "play_card":
            plays.setdefault(a["args"]["card_index"], []).append(a)
    scored = []
    for ci, entries in sorted(plays.items()):
        c = hand.get(ci, {})
        pr = card_priority(c, inc, blk, hp)
        if pr is not None:
            scored.append((pr, ci, c, entries))
    if not scored:
        return None
    best_p = min(pr for pr, *_ in scored)
    _, ci, c, entries = rng.choice([x for x in scored if x[0] == best_p])
    targets = [a["args"]["target_index"] for a in entries if "target_index" in a["args"]]
    if targets:
        ti = pick_target(enemies, targets, (c.get("stats") or {}).get("damage", 6), rng)
        return next(a for a in entries if a["args"].get("target_index") == ti)
    return rng.choice(entries)  # normally the card's only legal play


def emergency_potion(d, rng=None):
    rng = _rng(d, rng)
    p = d.get("player") or {}
    hp, blk = p.get("hp", 99), p.get("block", 0)
    inc = calc_incoming(d.get("enemies") or [])
    if not (hp < 20 or (inc - blk >= hp)):
        return None
    uses = [a for a in d.get("legal_actions") or [] if a.get("action") == "use_potion"]
    if not uses:
        return None
    targeted = [a for a in uses if "target_index" in a["args"]]
    plain = [a for a in uses if "target_index" not in a["args"]]
    if plain:
        return rng.choice(plain)
    pi = rng.choice(sorted({a["args"]["potion_index"] for a in targeted}))
    entries = [a for a in targeted if a["args"]["potion_index"] == pi]
    ti = pick_target(d.get("enemies") or [], [a["args"]["target_index"] for a in entries], rng=rng)
    return next(a for a in entries if a["args"]["target_index"] == ti)


def fight(d=None, max_steps=300, rng=None):
    """Play cards until the state is no longer a combat turn; returns that state."""
    if d is None:
        d = cmd({"cmd": "get_state"})
    for _ in range(max_steps):
        if d.get("type") != "decision" or d.get("decision") != "combat_play":
            return d  # any other decision (or an error) is the caller's
        rng = _rng(d, rng)
        act = emergency_potion(d, rng) or best_play(d, rng)
        if act is None:
            act = next((a for a in d.get("legal_actions") or [] if a.get("action") == "end_turn"), None)
            if act is None:
                return d
        d = send_action({k: v for k, v in act.items() if k in ("action", "args")})
    return d


if __name__ == "__main__":
    result = fight()
    dec = result.get("decision", result.get("type", ""))
    p = result.get("player", {})
    print(f"STATE: {dec} HP={p.get('hp', '?')}/{p.get('max_hp', '?')} gold={p.get('gold', '?')}")
    if result.get("type") == "error":
        print(f"  error: {result.get('message')}")
    for a in result.get("legal_actions") or []:
        print(f"  {json.dumps(a, ensure_ascii=False)}")
