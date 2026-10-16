#!/usr/bin/env python3
"""Auto-play full STS2 Necrobinder runs through the bridge (agent/sts2_bridge.py).

Usage: python3 agent/auto_play.py [PORT]

Every decision is one entry of the engine's legal_actions, chosen by NecrobinderPolicy:
the stated rules below, and for anything they don't cover the seeded HeuristicPolicy /
RandomPolicy of python/policy.py. Nothing is "the first option" by default.

NecrobinderPolicy rules
  combat       agent/combat_helper.best_play (legal cards only, scored; see that file);
               emergency potions when HP < 20 or the hit is lethal; else end the turn.
  card reward  take the highest-scoring card from PRIORITY (score >= 15 and deck < 18),
               else skip if skipping is legal, else the highest-scoring card anyway.
  shop         buy a stocked, affordable card from PRIORITY; remove a card when affordable
               (the removal selection then picks a Strike, else a Defend, else random);
               then leave.
  rest site    Rest (HEAL) below 75% HP or from floor 15 on, otherwise Smith (SMITH);
               the Smith selection upgrades the best card from UPGRADE_PRIORITY, else random.
  map          room type priority (Treasure > Boss > RestSite (when hurt) > Shop > ...).
  event        a random unlocked option that does not cost HP when HP < 50%; otherwise
               a random unlocked option.
  rewards      claim everything claimable, then proceed (via HeuristicPolicy / random).
"""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "python"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from policy import HeuristicPolicy, to_args, _name  # noqa: E402
import combat_helper  # noqa: E402

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 19234
URL = f"http://localhost:{PORT}"

PRIORITY = {
    "Calcify": 100, "Flatten": 90, "Sic 'Em": 85, "Fetch": 80,
    "Bodyguard": 75, "Unleash": 70,
    "Reave": 65, "Grave Warden": 60, "Wisp": 55, "Borrowed Time": 50,
    "Enfeebling Touch": 45, "Drain Power": 40, "Defy": 35, "Haunt": 30,
    "Capture Spirit": 28, "Melancholy": 25, "Deathbringer": 20,
}
UPGRADE_PRIORITY = {"Calcify": 10, "Bodyguard": 9, "Unleash": 8, "Flatten": 7, "Drain Power": 6,
                    "Enfeebling Touch": 5}
SHOP_BUY = {"Calcify", "Flatten", "Sic 'Em", "Fetch", "Wisp", "Borrowed Time", "Drain Power",
            "Enfeebling Touch", "Bodyguard", "Unleash", "Reave"}


def cmd(data):
    try:
        req = urllib.request.Request(URL, json.dumps(data).encode(), {'Content-Type': 'application/json'})
        return json.loads(urllib.request.urlopen(req, timeout=60).read())
    except Exception as e:
        print(f"  ERR: {e}")
        return {"type": "error", "message": str(e)}


def _by(acts):
    out = {}
    for a in acts:
        out.setdefault(a["action"], []).append(a)
    return out


class NecrobinderPolicy(HeuristicPolicy):
    name = "necrobinder"

    def decide(self, state, acts):
        dec = state.get("decision")
        by = _by(acts)
        p = state.get("player") or {}
        hp, mhp = p.get("hp", 1), max(p.get("max_hp", 1), 1)
        if dec == "combat_play":
            act = combat_helper.emergency_potion(state, self.rng) or combat_helper.best_play(state, self.rng)
            if act is not None:
                return to_args(act, self.rng)
            if by.get("end_turn"):
                return to_args(self.rng.choice(by["end_turn"]), self.rng)
        if dec == "card_reward":
            cards = {c.get("index"): c for c in state.get("cards") or []}
            deck_size = p.get("deck_size", 10)
            scored = [(self._card_score(cards.get(a["args"]["card_index"], {}), deck_size), a)
                      for a in by.get("select_card_reward", [])]
            best = max((sc for sc, _ in scored), default=None)
            if scored and (best >= 15 and deck_size < 18 or not by.get("skip_card_reward")):
                return to_args(self.rng.choice([a for sc, a in scored if sc == best]), self.rng)
            if by.get("skip_card_reward"):
                return to_args(self.rng.choice(by["skip_card_reward"]), self.rng)
        if dec == "shop":
            items = {c.get("index"): c for c in state.get("cards") or []}
            buys = [a for a in by.get("buy_card", []) if _name(items.get(a["args"]["card_index"], {})) in SHOP_BUY]
            if buys:
                return to_args(self.rng.choice(buys), self.rng)
            if by.get("remove_card") and p.get("deck_size", 0) > 8:
                return to_args(self.rng.choice(by["remove_card"]), self.rng)
            if by.get("leave_room"):
                return to_args(self.rng.choice(by["leave_room"]), self.rng)
        if dec == "rest_site" and by.get("choose_option"):
            ids = {o.get("index"): o.get("option_id") for o in state.get("options") or []}
            floor = (state.get("context") or {}).get("floor", 0)
            want = "HEAL" if (hp / mhp < 0.75 or floor >= 15) else "SMITH"
            pref = [a for a in by["choose_option"] if ids.get(a["args"]["option_index"]) == want]
            if pref:
                return to_args(self.rng.choice(pref), self.rng)
        if dec == "card_select" and state.get("purpose") == "upgrade":
            cards = {c.get("index"): c for c in state.get("cards") or []}
            singles = [a for a in by.get("select_cards", []) if "args" in a]
            scored = [(UPGRADE_PRIORITY.get(_name(cards.get(int(a["args"]["indices"]), {})), 0), a) for a in singles]
            best = max((s for s, _ in scored), default=0)
            if best > 0:
                return to_args(self.rng.choice([a for s, a in scored if s == best]), self.rng)
        if dec == "card_select" and state.get("purpose") == "remove":
            cards = {c.get("index"): c for c in state.get("cards") or []}
            singles = [a for a in by.get("select_cards", []) if "args" in a]
            for prefix in ("Strike", "Defend"):
                hit = [a for a in singles if _name(cards.get(int(a["args"]["indices"]), {})).startswith(prefix)]
                if hit:
                    return to_args(self.rng.choice(hit), self.rng)
        if dec == "map_select" and by.get("select_map_node"):
            return to_args(self._map_pick(state, by["select_map_node"]), self.rng)
        if dec == "event_choice" and by.get("choose_option"):
            opts = {o.get("index"): o for o in state.get("options") or []}
            safe = [a for a in by["choose_option"]
                    if not ((opts.get(a["args"]["option_index"], {}).get("vars") or {}).get("HpLoss", 0) > 0
                            and hp < mhp * 0.5)]
            return to_args(self.rng.choice(safe or by["choose_option"]), self.rng)
        return super().decide(state, acts)

    @staticmethod
    def _card_score(c, deck_size):
        name = _name(c)
        score = PRIORITY.get(name, 0)
        stats = c.get("stats") or {}
        if deck_size < 12:
            if c.get("type") == "Attack" and stats.get("damage", 0) >= 8:
                score = max(score, 15)
            if c.get("type") == "Power":
                score = max(score, 18)
        if deck_size > 15 and score < 50:
            score = 0
        return score

    def _map_pick(self, state, entries):
        p = state.get("player") or {}
        hp_pct = p.get("hp", 1) / max(p.get("max_hp", 1), 1)
        floor = (state.get("context") or {}).get("floor", 0)
        prio = {"Treasure": 100, "Boss": 90, "RestSite": 95 if floor >= 15 else (80 if hp_pct < 0.7 else 30),
                "Shop": 70, "Unknown": 40, "Monster": (3 if hp_pct < 0.3 else 35 if hp_pct > 0.4 else 10),
                "Elite": 50 if hp_pct > 0.85 and floor >= 12 else 1}
        types = {(c.get("col"), c.get("row")): c.get("type") for c in state.get("choices") or []}
        score = lambda a: prio.get(types.get((a["args"]["col"], a["args"]["row"])), 0)
        best = max(score(a) for a in entries)
        return self.rng.choice([a for a in entries if score(a) == best])


def play_game(seed=None):
    seed = seed or uuid.uuid4().hex[:12]
    policy = NecrobinderPolicy(seed)
    print(f"Starting Necrobinder run (seed={seed})...")
    d = cmd({"cmd": "start_run", "character": "Necrobinder", "seed": seed})
    for _ in range(3000):
        if d.get("type") != "decision":
            print(f"  STOP: {d.get('type')} {d.get('code', '')} {str(d.get('message', ''))[:200]}")
            return False  # an error means the bot or the engine has a bug: never guess past it
        if d.get("decision") == "game_over":
            p, ctx = d.get("player") or {}, d.get("context") or {}
            print(f"\n{'VICTORY!' if d.get('victory') else 'DEFEAT'} Act{ctx.get('act')} F{ctx.get('floor')} "
                  f"HP={p.get('hp')}/{p.get('max_hp')} score={d.get('score')}")
            return bool(d.get("victory"))
        if d.get("softlocked"):
            # The game shows no button here: the run can't go on (not a bot or engine error).
            print(f"  SOFTLOCK: {d.get('softlock_reason')}")
            return False
        act = policy.choose(d)
        if d.get("decision") != "combat_play":
            print(f"  {d.get('decision')}: {act['action']} {act.get('args', '')}")
        d = cmd({"cmd": "action", **act})
    print("Max steps reached!")
    return False


def restart_bridge():
    """Kill old bridge and start fresh one."""
    import signal
    try:
        result = subprocess.run(["lsof", "-ti", f":{PORT}"], capture_output=True, text=True)
        for pid in result.stdout.strip().split("\n"):
            if pid:
                os.kill(int(pid), signal.SIGKILL)
    except Exception:
        pass
    deadline = time.time() + 2
    while time.time() < deadline:
        r = subprocess.run(["lsof", "-ti", f":{PORT}"], capture_output=True, text=True)
        if not r.stdout.strip():
            break
        time.sleep(0.05)
    log = f"/tmp/sts2_game_{PORT}.jsonl"
    bridge = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sts2_bridge.py")
    proc = subprocess.Popen(
        [sys.executable, bridge, str(PORT), "--compact", "--log", log],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    deadline = time.time() + 30
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"bridge exited with code {proc.returncode}")
        try:
            socket.create_connection(("127.0.0.1", PORT), 0.2).close()
            return proc
        except OSError:
            time.sleep(0.05)
    raise RuntimeError("bridge did not start listening within 30s")


if __name__ == "__main__":
    max_attempts = 10
    for attempt in range(1, max_attempts + 1):
        print(f"\n{'=' * 40}\nATTEMPT {attempt}/{max_attempts}\n{'=' * 40}")
        if attempt > 1:
            restart_bridge()
        if play_game():
            print(f"\nWON on attempt {attempt}!")
            break
    else:
        print(f"\nFailed all {max_attempts} attempts.")
