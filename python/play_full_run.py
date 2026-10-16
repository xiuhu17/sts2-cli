#!/usr/bin/env python3
"""
Play full STS2 runs using the headless simulator with a bot.

The bot decides through an explicit policy over the engine's legal_actions (python/policy.py):
"random" (default: seeded, weighted by verb) or "heuristic". It never picks "the first" option.
Rewards screens and treasure chests are decisions like any other.

Usage:
  python3 play_full_run.py <num_runs> <character> [--policy random|heuristic]

Arguments:
  num_runs    Positive integer: number of runs to play
  character   One of: Ironclad, Silent, Defect, Regent, Necrobinder (required)

Examples:
  python3 play_full_run.py 5 Ironclad
  python3 play_full_run.py 3 Silent
"""

import argparse
import json
import sys
from engine import Engine, EngineError
from game_log import GameLogger
from policy import NoLegalAction, make_policy

VALID_CHARACTERS = ["Ironclad", "Silent", "Defect", "Regent", "Necrobinder"]


def play_run(seed: str, character: str, verbose: bool = True, log: bool = True,
             engine: Engine = None, manual_rewards: bool = True, policy: str = "random"):
    """Play a complete run and return the result.

    Pass an existing Engine to reuse one process across runs (much faster for batches);
    if the result has "engine_broken": True, that engine must be discarded.
    """
    # manual_rewards is accepted for compatibility: rewards screens are always decisions.
    policy = make_policy(policy, seed)
    logger = GameLogger(character, seed, enabled=log)
    owns_engine = engine is None

    def send(cmd: dict) -> dict:
        line = json.dumps(cmd)
        if verbose:
            print(f"  > {line[:200]}")
        logger.log_action(cmd)
        resp = engine.send(cmd)
        logger.log_state(resp)
        if verbose:
            rtype = resp.get("type", "?")
            decision = resp.get("decision", "")
            if rtype == "decision":
                player = resp.get("player", {})
                hp = player.get("hp", "?")
                max_hp = player.get("max_hp", "?")
                gold = player.get("gold", "?")
                act = resp.get("act", "?")
                floor = resp.get("floor", "?")
                print(f"  < {rtype}/{decision} act={act} floor={floor} hp={hp}/{max_hp} gold={gold}")
            else:
                print(f"  < {json.dumps(resp)[:200]}")
        return resp

    step = 0
    try:
        if owns_engine:
            engine = Engine(stderr_sink=(lambda l: print(l, file=sys.stderr)) if verbose else None)
            if verbose:
                print(f"Connected: {engine.ready}")

        # Start run
        # manual_rewards: rewards/treasure are always decisions (the engine ignores false;
        # sent so older engine builds behave the same).
        start = {"cmd": "start_run", "character": character, "seed": seed, "manual_rewards": True}
        state = send(start)

        step = 0
        max_steps = 2000  # Safety limit
        stuck_count = 0
        last_state_key = None

        while step < max_steps:
            step += 1

            if state.get("type") == "error":
                print(f"  ERROR: {state.get('message', 'unknown')}")
                if state.get("code") == "engine_stuck":
                    return {"victory": False, "seed": seed, "steps": step, "error": "engine_stuck",
                            "act": (state.get("context") or {}).get("act"),
                            "floor": (state.get("context") or {}).get("floor")}
                if state.get("code") == "restart_required":
                    return {"victory": False, "seed": seed, "steps": step,
                            "error": "restart_required", "engine_broken": True}
                raise RuntimeError(state.get("message", "Simulator error"))

            decision = state.get("decision", "")

            # Stuck detection — use comprehensive state key
            hand_len = len(state.get("hand", []))
            enemy_hp = sum(e.get("hp", 0) for e in state.get("enemies", []))
            energy = state.get("energy", 0)
            pl = state.get("player") or {}
            state_key = (f"{decision}:{state.get('round')}:{pl.get('hp')}:{hand_len}:{enemy_hp}:{energy}:"
                         f"{pl.get('gold')}:{pl.get('deck_size')}:{len(pl.get('potions') or [])}:"
                         f"{len(state.get('legal_actions') or [])}")
            if state_key == last_state_key:
                stuck_count += 1
                if stuck_count > 20:
                    print(f"  STUCK after {step} steps, forcing quit")
                    return {"victory": False, "seed": seed, "steps": step, "error": "stuck",
                            "act": state.get("act"), "floor": state.get("floor"),
                            "hp": state.get("player", {}).get("hp"),
                            "max_hp": state.get("player", {}).get("max_hp")}
            else:
                stuck_count = 0
                last_state_key = state_key

            if decision == "game_over":
                victory = state.get("victory", False)
                player = state.get("player", {})
                print(f"\n{'VICTORY' if victory else 'DEFEAT'} at act {state.get('act')}, "
                      f"floor {state.get('floor')} "
                      f"(HP: {player.get('hp')}/{player.get('max_hp')}, "
                      f"Gold: {player.get('gold')}, "
                      f"Deck: {player.get('deck_size')} cards)")
                return {
                    "victory": victory,
                    "completed": True,
                    "seed": seed,
                    "steps": step,
                    "act": state.get("act"),
                    "floor": state.get("floor"),
                    "hp": player.get("hp"),
                    "max_hp": player.get("max_hp"),
                }

            else:
                # Every other decision: the policy picks one of the engine's legal_actions.
                try:
                    act = policy.choose(state)
                except NoLegalAction as e:
                    raise RuntimeError(str(e)) from None
                state = send({"cmd": "action", **act})

        print(f"  Reached max steps ({max_steps})")
        return {"victory": False, "seed": seed, "steps": step, "timeout": True}

    except (TimeoutError, EngineError) as e:
        print(f"  ENGINE FAILURE: {e}")
        return {"victory": False, "seed": seed, "steps": step, "error": str(e), "engine_broken": True}

    except Exception as e:
        print(f"  EXCEPTION: {e}")
        return {"victory": False, "seed": seed, "steps": step, "error": str(e)}

    finally:
        logger.close()
        if logger.path:
            print(f"  [log] Saved to {logger.path}")
        if owns_engine and engine is not None:
            engine.close()


def main():
    parser = argparse.ArgumentParser(
        description="Play full STS2 runs using the headless simulator with a bot (policy over legal_actions).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Valid characters: " + ", ".join(VALID_CHARACTERS),
    )
    parser.add_argument("num_runs", type=int, help="Number of runs to play (must be positive)")
    parser.add_argument("character", choices=VALID_CHARACTERS, metavar="character",
                        help=f"Character to play as (required: the run is started on an explicit pick). Choices: {', '.join(VALID_CHARACTERS)}")
    parser.add_argument("--policy", default="random", choices=["random", "heuristic"],
                        help="Decision policy (python/policy.py): seeded weighted random (default) or heuristic")
    parser.add_argument("--fresh-process", action="store_true",
                        help="Start a new engine process for every run instead of reusing one")
    args = parser.parse_args()

    if args.num_runs <= 0:
        parser.error(f"num_runs must be a positive integer, got {args.num_runs}")

    num_runs = args.num_runs
    character = args.character

    print(f"Playing {num_runs} runs as {character}")
    print("=" * 60)

    results = []
    engine = None
    for i in range(num_runs):
        seed = f"run_{i+1}"
        print(f"\n--- Run {i+1}/{num_runs} (seed: {seed}) ---")
        if not args.fresh_process and engine is None:
            engine = Engine(stderr_sink=lambda l: print(l, file=sys.stderr))
        result = play_run(seed, character, verbose=True, engine=engine, policy=args.policy)
        if engine is not None and (result.get("engine_broken") or result.get("error") == "engine_stuck"):
            engine.close()
            engine = None
        results.append(result)
        print()
    if engine is not None:
        engine.close()

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    wins = sum(1 for r in results if r and r.get("victory"))
    completed = sum(1 for r in results if r and r.get("completed"))
    for i, r in enumerate(results):
        if r:
            status = "WIN" if r.get("victory") else ("LOSS" if r.get("completed") else "ERROR")
            print(f"  Run {i+1}: {status} | seed={r.get('seed')} steps={r.get('steps')} "
                  f"act={r.get('act')} floor={r.get('floor')}")
    print(f"\nWins: {wins}/{num_runs}, Completed: {completed}/{num_runs}")
    return 0 if completed == num_runs else 1


if __name__ == "__main__":
    sys.exit(main())
