"""
Roadmap item 8 profiling harness. Runs a 25-parcel Pasco scan with the
timing sink enabled, prints a step-by-step breakdown, and saves the
full ScanResultRow list to JSON so before/after correctness can be
compared.
"""
import sys, os, json, time, argparse, collections
sys.path.insert(0, "app")

import scan_orchestrator
from dataclasses import asdict


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--county", default="pasco")
    ap.add_argument("--n", type=int, default=25)
    args = ap.parse_args()

    scan_orchestrator._PROFILE_SINK = []
    t0 = time.perf_counter()
    rows = scan_orchestrator.run_county_scan(args.county, max_candidates=args.n)
    wall = time.perf_counter() - t0

    # Aggregate timings by step
    per_step = collections.defaultdict(float)
    per_step_count = collections.defaultdict(int)
    for (_pid, step, dt) in scan_orchestrator._PROFILE_SINK:
        per_step[step] += dt
        per_step_count[step] += 1

    result = {
        "county": args.county,
        "n_requested": args.n,
        "n_returned": len(rows),
        "wall_clock_sec": round(wall, 3),
        "wall_clock_per_parcel_sec": round(wall / max(1, len(rows)), 3),
        "step_timings_total_sec": {k: round(v, 3) for k, v in sorted(per_step.items(), key=lambda kv: -kv[1])},
        "step_timings_count": dict(per_step_count),
        "rows": [asdict(r) for r in rows],
    }
    with open(args.out, "w") as f:
        json.dump(result, f, indent=2, sort_keys=True)

    print(f"wall clock: {wall:.2f}s across {len(rows)} parcels "
          f"({wall/max(1,len(rows)):.2f}s/parcel)")
    print()
    print(f"{'step':<32} {'total_sec':>10} {'count':>7}")
    print("-" * 55)
    for step, total in sorted(per_step.items(), key=lambda kv: -kv[1]):
        print(f"{step:<32} {total:>10.2f} {per_step_count[step]:>7}")


if __name__ == "__main__":
    main()
