"""Decision policies for the bots and test drivers (play_full_run.py, play.py --auto,
agent/auto_play.py, scripts/fuzz_legal.py, scripts/bench.py).

A policy never chooses on its own idea of what is possible: it picks one entry of the
engine's ``legal_actions`` list (the same choices a human has on that screen) and turns it
into a ready-to-send command. There are no "take the first card" / "[0]" fallbacks and no
assumptions about the order of the list; every choice between equivalent options is made
by a seeded ``random.Random``, so a run is reproducible from (seed, policy).

RandomPolicy (the default)
    1. Group the legal actions by verb ("play_card", "end_turn", "claim_reward", ...).
    2. Pick a verb with probability proportional to its weight (DEFAULT_WEIGHTS, override
       per verb); verbs with weight 0 are never picked unless every listed verb has
       weight 0, in which case all are equally likely.
    3. Pick one entry of that verb uniformly. Multi-card selections (``choose``) pick a
       uniformly random count in [min, max] and a uniformly random subset; Crystal Sphere
       (``choose_cell``) picks a uniformly random hidden cell and tool.
    Memory: a card reward that was opened from the rewards screen and skipped is not
    reopened on that same screen (the game keeps it claimable; reopening it forever would
    never progress).

HeuristicPolicy(RandomPolicy) adds a few stated rules, falling back to RandomPolicy:
    - combat: play a playable card (uniformly) while one is legal, else end the turn;
      an enemy-targeted card/potion goes to the alive enemy with the lowest HP (ties: the
      random pick); use a potion only when HP is below 30% of max.
    - rest site: Rest (HEAL) when HP < 70% of max, otherwise Smith (SMITH) when offered,
      otherwise random among the enabled options.
    - map: a RestSite when HP < 40% of max and one is reachable, otherwise random.
    - card removal / transform / any selection of purpose "remove" or "transform": a card
      named Strike* or Defend* when one is offered, otherwise random.
    - rewards screen: claim a random claimable reward (a skipped card reward is not
      reopened), proceed when nothing claimable is left; treasure: take a random relic.
    - never discards potions.
"""

import random

# Relative weight of each verb in RandomPolicy (unlisted verbs: 1.0).
DEFAULT_WEIGHTS = {
    "play_card": 10.0,       # usually play a card rather than end the turn early
    "end_turn": 1.0,
    "use_potion": 0.3,
    "discard_potion": 0.0,   # never throw away potions
    "leave_room": 0.3,       # shop / rest site: mostly use the room before leaving
    "skip_select": 0.2,      # cancel / skip a card selection now and then
    "skip_card_reward": 0.5,
    "select_card_alternative": 0.3,
    "skip_treasure_relic": 0.1,
    "proceed": 0.3,          # rewards screen: mostly claim before moving on
}


def to_args(entry, rng):
    """One legal_actions entry -> {"action": ..., "args": {...}} with any open choice made by rng."""
    if "choose" in entry:
        ch = entry["choose"]
        lo, hi = int(ch.get("min", 1)), int(ch.get("max", 1))
        # "counts": the only sizes the screen confirms when they have a gap (0 or max).
        k = rng.choice(list(ch["counts"])) if ch.get("counts") else rng.randint(lo, hi)
        picks = sorted(rng.sample(list(ch["from"]), k))
        return {"action": entry["action"], "args": {"indices": ",".join(map(str, picks))}}
    if "choose_cell" in entry:
        cells = entry["choose_cell"].get("cells") or []
        tools = entry["choose_cell"].get("tools") or []   # only the tools the engine offers
        if not cells or not tools:
            raise NoLegalAction(f"{entry['action']} lists no cell or no tool: {entry['choose_cell']}")
        x, y = rng.choice(cells)
        return {"action": entry["action"], "args": {"x": x, "y": y, "tool": rng.choice(tools)}}
    out = {"action": entry["action"]}
    if entry.get("args"):
        out["args"] = dict(entry["args"])
    return out


def to_command(entry, rng):
    return {"cmd": "action", **to_args(entry, rng)}


class NoLegalAction(Exception):
    """The decision lists no legal action (game_over, or an engine bug)."""


class RandomPolicy:
    name = "random"

    def __init__(self, seed=None, weights=None):
        self.rng = random.Random(seed)
        self.weights = dict(DEFAULT_WEIGHTS)
        if weights:
            self.weights.update(weights)
        self._declined = set()   # card rewards skipped on the current rewards screen

    # -- helpers ------------------------------------------------------------------
    def legal(self, state):
        acts = state.get("legal_actions") or []
        if not acts:
            if state.get("softlocked"):
                raise NoLegalAction(f"softlocked at {state.get('decision')!r}: {state.get('softlock_reason')}")
            raise NoLegalAction(f"no legal actions at decision {state.get('decision')!r}")
        return acts

    def pick_weighted(self, acts):
        groups = {}
        for a in acts:
            groups.setdefault(a["action"], []).append(a)
        verbs = list(groups)
        w = [max(0.0, float(self.weights.get(v, 1.0))) for v in verbs]
        if sum(w) <= 0:
            w = [1.0] * len(verbs)
        verb = self.rng.choices(verbs, weights=w)[0]
        return self.rng.choice(groups[verb])

    def _rewards_filter(self, state, acts):
        """Drop claim_reward entries for card rewards this policy already declined here."""
        key = self._screen_key(state)
        declined = {i for (k, i) in self._declined if k == key}
        return [a for a in acts if not (a["action"] == "claim_reward"
                                        and (a.get("args") or {}).get("reward_index") in declined)]

    @staticmethod
    def _screen_key(state):
        ctx = state.get("context") or {}
        return (ctx.get("act"), ctx.get("floor"), ctx.get("total_floor"))

    # -- API ----------------------------------------------------------------------
    def choose(self, state):
        """The action to send at this decision: {"action": ..., "args": {...}}."""
        acts = self.legal(state)
        dec = state.get("decision")
        if dec == "rewards":
            acts = self._rewards_filter(state, acts) or acts
        a = self.decide(state, acts)
        self._note(state, a)
        return a

    def decide(self, state, acts):
        return to_args(self.pick_weighted(acts), self.rng)

    def _note(self, state, a):
        # Remember the card reward we opened, so that skipping it marks it declined.
        if state.get("decision") == "rewards":
            if a["action"] == "claim_reward":
                idx = a["args"]["reward_index"]
                w = next((r for r in state.get("rewards") or [] if r.get("index") == idx), {})
                self._opened = (self._screen_key(state), idx) if w.get("type") == "card" else None
            else:
                self._opened = None
                self._declined.clear()
        elif state.get("decision") == "card_reward" and a["action"] == "skip_card_reward":
            opened = getattr(self, "_opened", None)
            if opened:
                self._declined.add(opened)
        elif state.get("decision") not in ("card_reward", "rewards"):
            self._declined.clear()
            self._opened = None


def _hp_ratio(state):
    p = state.get("player") or {}
    return (p.get("hp") or 0) / max(p.get("max_hp") or 1, 1)


def _name(x):
    n = x.get("name") if isinstance(x, dict) else None
    if isinstance(n, dict):
        n = n.get("en") or next(iter(n.values()), "")
    return str(n or "")


class HeuristicPolicy(RandomPolicy):
    name = "heuristic"

    def decide(self, state, acts):
        dec = state.get("decision")
        by = {}
        for a in acts:
            by.setdefault(a["action"], []).append(a)
        if dec == "combat_play":
            if _hp_ratio(state) < 0.3 and by.get("use_potion"):
                return self._targeted(state, by["use_potion"], "potion_index")
            if by.get("play_card"):
                return self._targeted(state, by["play_card"], "card_index")
            if by.get("end_turn"):
                return to_args(self.rng.choice(by["end_turn"]), self.rng)
        if dec == "rest_site" and by.get("choose_option"):
            ids = {o.get("index"): o.get("option_id") for o in state.get("options") or []}
            want = "HEAL" if _hp_ratio(state) < 0.7 else "SMITH"
            pref = [a for a in by["choose_option"] if ids.get(a["args"]["option_index"]) == want]
            if pref:
                return to_args(self.rng.choice(pref), self.rng)
            return to_args(self.rng.choice(by["choose_option"]), self.rng)
        if dec == "map_select" and _hp_ratio(state) < 0.4 and by.get("select_map_node"):
            types = {(c.get("col"), c.get("row")): c.get("type") for c in state.get("choices") or []}
            rest = [a for a in by["select_map_node"]
                    if types.get((a["args"]["col"], a["args"]["row"])) == "RestSite"]
            if rest:
                return to_args(self.rng.choice(rest), self.rng)
        if dec == "rewards":
            claims = by.get("claim_reward") or []
            if claims:
                return to_args(self.rng.choice(claims), self.rng)
            if by.get("proceed"):
                return to_args(self.rng.choice(by["proceed"]), self.rng)
        if dec == "treasure_relic" and by.get("select_treasure_relic"):
            return to_args(self.rng.choice(by["select_treasure_relic"]), self.rng)
        if dec == "card_select" and state.get("purpose") in ("remove", "transform"):
            cards = {c.get("index"): c for c in state.get("cards") or []}
            singles = [a for a in by.get("select_cards", []) if str((a.get("args") or {}).get("indices", "")).isdigit()]
            basic = [a for a in singles
                     if _name(cards.get(int(a["args"]["indices"]), {})).startswith(("Strike", "Defend"))]
            if basic:
                return to_args(self.rng.choice(basic), self.rng)
        acts = [a for a in acts if a["action"] != "discard_potion"] or acts
        return to_args(self.pick_weighted(acts), self.rng)

    def _targeted(self, state, entries, key):
        """Pick a card/potion uniformly; for enemy-targeted ones aim at the lowest-HP enemy."""
        item = self.rng.choice(sorted({a["args"][key] for a in entries}))
        options = [a for a in entries if a["args"][key] == item]
        targeted = [a for a in options if "target_index" in a["args"]]
        if not targeted:
            return to_args(self.rng.choice(options), self.rng)
        hp = {e.get("index"): e.get("hp", 0) for e in state.get("enemies") or []}
        low = min(hp.get(a["args"]["target_index"], 0) for a in targeted)
        return to_args(self.rng.choice([a for a in targeted if hp.get(a["args"]["target_index"], 0) == low]), self.rng)


POLICIES = {"random": RandomPolicy, "heuristic": HeuristicPolicy}


def make_policy(name, seed):
    try:
        return POLICIES[name](seed)
    except KeyError:
        raise ValueError(f"unknown policy {name!r} (have: {', '.join(POLICIES)})") from None
