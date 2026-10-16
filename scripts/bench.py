#!/usr/bin/env python3
"""
Benchmark + regression harness for the headless simulator.

Runs the play_full_run.py bot (seeded policy over legal_actions, python/policy.py;
--policy random|heuristic) over fixed seeds and reports wall time, per-command latency, outcome, and a trajectory
fingerprint (hash of every state the engine returned). Two builds that behave
identically produce identical fingerprints, so this doubles as a behaviour
regression check for performance work.

Usage:
  python3 scripts/bench.py [--runs N] [--characters Ironclad,Silent] [--jobs J]
                           [--reuse] [--out results.json] [--compare baseline.json]
"""

import argparse
import hashlib
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "python"))

import play_full_run  # noqa: E402

# Fields that legitimately differ between otherwise identical runs.
VOLATILE_KEYS = {"stack_trace", "ts"}


def _strip(obj):
    if isinstance(obj, dict):
        # None-valued keys are dropped so a build that omits null fields hashes the
        # same as one that emits them ("key": null and a missing key read the same).
        return {k: _strip(v) for k, v in obj.items() if k not in VOLATILE_KEYS and v is not None}
    if isinstance(obj, list):
        return [_strip(v) for v in obj]
    return obj


class RecordingLogger:
    """Drop-in for GameLogger that keeps timings and a state hash in memory."""

    def __init__(self, character, seed, enabled=True):
        self.path = None
        self._hash = hashlib.sha256()
        self._sent_at = None
        self.latencies = []
        self.per_cmd = {}
        self.trace = []

    def log_action(self, action):
        self._sent_at = time.perf_counter()
        self._cmd = action.get("action") or action.get("cmd")
        self._hash.update(json.dumps(action, sort_keys=True).encode())
        self.trace.append({"action": action})

    def log_state(self, state):
        if self._sent_at is not None:
            dt = time.perf_counter() - self._sent_at
            self.latencies.append(dt)
            self.per_cmd.setdefault(self._cmd, []).append(dt)
            self._sent_at = None
        self._hash.update(json.dumps(_strip(state), sort_keys=True).encode())
        self.trace.append({"state": _strip(state)})

    def close(self):
        pass

    @property
    def fingerprint(self):
        return self._hash.hexdigest()[:16]


# Per-worker engine when --reuse is on (one process plays many runs).
_ENGINE = None


def run_one(job):
    global _ENGINE
    seed, character, dump_dir, reuse, policy = job
    extra = {"policy": policy}
    holder = {}

    def make_logger(*a, **kw):
        holder["logger"] = RecordingLogger(*a, **kw)
        return holder["logger"]

    play_full_run.GameLogger = make_logger
    t0 = time.perf_counter()
    if reuse:
        from engine import Engine
        if _ENGINE is None:
            _ENGINE = Engine()
        result = play_full_run.play_run(seed, character, verbose=False, log=True, engine=_ENGINE, **extra)
        if result.get("engine_broken") or result.get("error") == "engine_stuck":
            _ENGINE.close()
            _ENGINE = None
    else:
        result = play_full_run.play_run(seed, character, verbose=False, log=True, **extra)
    wall = time.perf_counter() - t0
    lg = holder["logger"]
    lat = sorted(lg.latencies)
    if dump_dir:
        with open(os.path.join(dump_dir, f"{character}_{seed}.jsonl"), "w") as f:
            for e in lg.trace:
                f.write(json.dumps(e, sort_keys=True) + "\n")
    return {
        "seed": seed,
        "character": character,
        "wall_s": round(wall, 3),
        "steps": result.get("steps"),
        "victory": result.get("victory"),
        "completed": result.get("completed", False),
        "act": result.get("act"),
        "floor": result.get("floor"),
        "error": result.get("error"),
        "fingerprint": lg.fingerprint,
        "engine_s": round(sum(lat), 3),
        "p50_ms": round(1000 * lat[len(lat) // 2], 2) if lat else None,
        "max_ms": round(1000 * lat[-1], 1) if lat else None,
        "per_cmd_ms": {k: round(1000 * sum(v) / len(v), 2) for k, v in lg.per_cmd.items()},
        "per_cmd_total_s": {k: round(sum(v), 3) for k, v in lg.per_cmd.items()},
        "start_run_ms": [round(1000 * v, 2) for v in lg.per_cmd.get("start_run", [])],
        "step_ms": [round(1000 * v, 3) for k, vs in lg.per_cmd.items() if k != "start_run" for v in vs],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3, help="seeds per character")
    ap.add_argument("--characters", default="Ironclad,Silent,Defect,Regent,Necrobinder")
    ap.add_argument("--seed-prefix", default="bench")
    ap.add_argument("--jobs", type=int, default=1, help="parallel engine processes")
    ap.add_argument("--out")
    ap.add_argument("--compare", help="previous results JSON to diff fingerprints against")
    ap.add_argument("--dump", help="directory to write per-run trajectories (JSONL)")
    ap.add_argument("--manual-rewards", action="store_true",
                    help="accepted for compatibility: rewards / treasure are always decisions")
    ap.add_argument("--policy", default="random", choices=["random", "heuristic"],
                    help="bot policy (python/policy.py)")
    ap.add_argument("--reuse", action="store_true",
                    help="reuse one engine process per worker across runs")
    args = ap.parse_args()

    if args.dump:
        os.makedirs(args.dump, exist_ok=True)
    jobs = [(f"{args.seed_prefix}_{i}", c, args.dump, args.reuse, args.policy)
            for c in args.characters.split(",") for i in range(args.runs)]
    t0 = time.perf_counter()
    if args.jobs > 1:
        with ProcessPoolExecutor(args.jobs) as ex:
            results = list(ex.map(run_one, jobs))
    else:
        results = [run_one(j) for j in jobs]
    total = time.perf_counter() - t0

    for r in results:
        status = "WIN " if r["victory"] else ("done" if r["completed"] else "FAIL")
        print(f"{r['character']:<12} {r['seed']:<10} {status} act={r['act']} floor={r['floor']} "
              f"steps={r['steps']} wall={r['wall_s']:.1f}s engine={r['engine_s']:.1f}s "
              f"p50={r['p50_ms']}ms max={r['max_ms']}ms fp={r['fingerprint']}"
              + (f" err={r['error']}" if r["error"] else ""))

    agg = {}
    for r in results:
        for k, v in r["per_cmd_total_s"].items():
            agg[k] = agg.get(k, 0) + v
    print("\nEngine time by command (s):")
    for k, v in sorted(agg.items(), key=lambda kv: -kv[1]):
        print(f"  {k:<22} {v:8.2f}")
    steps = sum(r["steps"] or 0 for r in results)
    print(f"\n{len(results)} runs, {steps} steps, total wall {total:.1f}s, "
          f"{steps / total:.1f} steps/s overall")

    def pct(xs, q):
        return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else None

    sr = [v for r in results for v in r["start_run_ms"]]
    first, warm = sr[:1], sorted(sr[1:])
    st = sorted(v for r in results for v in r["step_ms"])
    print(f"start_run: first {first[0] if first else None}ms, warm p50 {pct(warm, .5)}ms "
          f"max {warm[-1] if warm else None}ms | steps (excl. start_run): p50 {pct(st, .5)}ms "
          f"p90 {pct(st, .9)}ms p99 {pct(st, .99)}ms max {st[-1] if st else None}ms "
          f"mean {round(sum(st) / len(st), 3) if st else None}ms")

    summary = {"total_wall_s": round(total, 2), "jobs": args.jobs, "reuse": args.reuse,
               "results": results}
    if args.out:
        with open(args.out, "w") as f:
            json.dump(summary, f, indent=2)

    if args.compare:
        with open(args.compare) as f:
            base = {(r["seed"], r["character"]): r for r in json.load(f)["results"]}
        same = diff = 0
        for r in results:
            b = base.get((r["seed"], r["character"]))
            if not b:
                continue
            if b["fingerprint"] == r["fingerprint"]:
                same += 1
            else:
                diff += 1
                print(f"  DIFF {r['character']} {r['seed']}: steps {b['steps']}->{r['steps']} "
                      f"floor {b['floor']}->{r['floor']} err {b['error']}->{r['error']}")
        print(f"Fingerprints: {same} identical, {diff} different")


if __name__ == "__main__":
    main()
