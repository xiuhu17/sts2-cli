#!/usr/bin/env python3
"""
Fuzz legal_actions: play runs choosing at random among legal_actions and fail on any error
response, or when the same decision repeats 40 times without progress.

Policy (seeded per run): never discard a potion unless nothing else is legal; in combat, with
probability 0.85 exclude end_turn when something else is legal; then pick uniformly among the
remaining entries (selections / Crystal Sphere cells: uniform count, subset, cell and tool).
Checks that everything listed as legal is actually accepted and actually does something.

Usage:
  python3 scripts/fuzz_legal.py [--runs N] [--steps S] [--manual-rewards] [--immortal] [--seed-prefix P]

--immortal gives the player huge HP (debug set_player) so runs reach later acts and rare screens.
"""

import argparse
import json
import os
import random
import sys
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "python"))
from engine import Engine  # noqa: E402
from policy import to_command  # noqa: E402

CHARACTERS = ["Ironclad", "Silent", "Defect", "Regent", "Necrobinder"]


def progress_key(state):
    """Identity of a decision ignoring legal_actions; repeating it means no progress."""
    p = state.get("player") or {}
    return json.dumps([state.get("decision"), state.get("event_name"), state.get("description"),
                       [o.get("title") for o in state.get("options") or []],
                       p.get("hp"), p.get("gold"), p.get("deck_size"),
                       state.get("round"), len(state.get("hand") or []), state.get("energy")])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=20)
    ap.add_argument("--steps", type=int, default=1500)
    ap.add_argument("--manual-rewards", action="store_true")
    ap.add_argument("--immortal", action="store_true")
    ap.add_argument("--seed-prefix", default="fuzz")
    ap.add_argument("--only", type=int, help="run only this run index (to reproduce a failure)")
    ap.add_argument("--fresh", action="store_true", help="new engine process for every run")
    ap.add_argument("--stderr", help="append engine stderr (logs) to this file")
    ap.add_argument("--trace", help="write the action/response trace (JSONL) to this file")
    args = ap.parse_args()

    failures, used, decisions = [], Counter(), Counter()
    total_steps = 0
    log = open(args.stderr, "a") if args.stderr else None
    trace = open(args.trace, "w") if args.trace else None
    sink = (lambda l: (log.write(l + "\n"), log.flush())) if log else None
    engine = Engine(stderr_sink=sink)
    for i in range(args.runs):
        if args.only is not None and i != args.only:
            continue
        if args.fresh and i > 0 and args.only is None:
            engine.close()
            engine = Engine(stderr_sink=sink)
        seed = f"{args.seed_prefix}_{i}"
        character = CHARACTERS[i % len(CHARACTERS)]
        rng = random.Random(seed)
        # Rewards screens and treasure chests are always decisions (--manual-rewards is kept
        # for compatibility; older engine builds needed the flag).
        start = {"cmd": "start_run", "character": character, "seed": seed, "manual_rewards": True}
        if log:
            log.write(f"=== start {json.dumps(start)}\n")
        state = engine.send(start)
        if args.immortal:
            engine.send({"cmd": "set_player", "hp": 99999, "max_hp": 99999})
        last_key, repeats = None, 0
        for step in range(args.steps):
            if state.get("type") == "decision":
                key = progress_key(state)
                repeats = repeats + 1 if key == last_key else 0
                last_key = key
                if repeats >= 40 and state["decision"] != "combat_play":
                    failures.append((seed, step, "stalled", f"{state['decision']} {state.get('event_name') or ''}"))
                    break
            if state.get("type") != "decision":
                failures.append((seed, step, last, state.get("message")))
                break
            decisions[state["decision"]] += 1
            if state["decision"] == "game_over":
                break
            legal = state.get("legal_actions") or []
            if not legal:
                failures.append((seed, step, "no legal actions", state["decision"]))
                break
            # Prefer progress over endless potion discards so runs go deep.
            pool = [a for a in legal if a["action"] != "discard_potion"] or legal
            # Mostly play cards rather than ending the turn immediately.
            if state["decision"] == "combat_play" and rng.random() < 0.85:
                pool = [a for a in pool if a["action"] != "end_turn"] or pool
            entry = rng.choice(pool)
            last = to_command(entry, rng)
            used[entry["action"]] += 1
            if log:
                log.write(f"=== step {step} {json.dumps(last)}\n")
            state = engine.send(last)
            if trace:
                trace.write(json.dumps({"step": step, "action": last, "response": state}) + "\n")
            total_steps += 1
        print(f"{character:<12} {seed:<10} steps={step + 1:<5} end={state.get('decision') or state.get('type')} "
              f"floor={(state.get('context') or {}).get('floor')}")
    engine.close()

    print(f"\n{args.runs} runs, {total_steps} actions")
    print("decisions:", dict(decisions))
    print("actions used:", dict(used))
    if failures:
        print(f"\n{len(failures)} FAILURES:")
        for f in failures:
            print("  ", json.dumps(f)[:400])
        sys.exit(1)
    print("\nOK: every legal action was accepted")


if __name__ == "__main__":
    main()
