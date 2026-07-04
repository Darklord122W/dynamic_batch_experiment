#!/usr/bin/env python3
"""benchmark.py — run a sweep of configurations and compare them.

Runs main.py once per variant (each with --metrics-csv + --duration), then
aggregates every run's CSV with analyze.py into one side-by-side table. Vary ONE
factor at a time (RT-BEV's ablation style). For fair numbers, use reproducible
input (``--source file`` with recorded clips: scripts/record_replay_clips.py).

Experiments:
  e1     — timeout sweep: fixed batched-push-timeout at several values (context all).
  policy — skip study: baseline (fixed+all) vs skip (fixed+scheduled) vs
           skip+adaptive-timeout (adaptive+scheduled). Needs a config whose
           context.schedule is set (e.g. experiments/_exp_scheduled.yaml).

Usage:
    # reproducible (record clips first):
    python3 scripts/record_replay_clips.py --duration 40
    python3 scripts/benchmark.py --experiment e1 --source file --duration 25
    python3 scripts/benchmark.py --experiment policy --config experiments/_exp_scheduled.yaml \
            --source file --duration 25
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_HERE, "scripts"))
import analyze  # noqa: E402


# label -> extra CLI args for main.py
EXPERIMENTS = {
    "e1": [
        ("t10ms", ["--timeout-policy", "fixed", "--timeout-us", "10000", "--context", "all"]),
        ("t20ms", ["--timeout-policy", "fixed", "--timeout-us", "20000", "--context", "all"]),
        ("t33ms", ["--timeout-policy", "fixed", "--timeout-us", "33333", "--context", "all"]),
        ("t50ms", ["--timeout-policy", "fixed", "--timeout-us", "50000", "--context", "all"]),
    ],
    "policy": [
        ("baseline", ["--timeout-policy", "fixed", "--context", "all"]),
        ("skip_fixed", ["--timeout-policy", "fixed", "--context", "scheduled"]),
        ("skip_adaptive", ["--timeout-policy", "adaptive", "--context", "scheduled"]),
    ],
}


def run_variant(label: str, extra: list, args) -> str:
    """Run main.py for one variant; return the CSV path."""
    csv_path = os.path.join(args.out_dir, f"{args.experiment}_{label}.csv")
    cmd = [
        sys.executable, os.path.join(_HERE, "main.py"),
        "--config", args.config,
        "--source", args.source,
        "--log", "none",
        "--metrics-csv", csv_path,
        "--duration", str(args.duration),
        "--control-ms", str(args.control_ms),
    ]
    if args.replay_dir:
        cmd += ["--replay-dir", args.replay_dir]
    cmd += extra
    print(f"\n[benchmark] === {label} ===\n  {' '.join(cmd)}", flush=True)
    subprocess.run(cmd, cwd=_HERE, check=False)
    return csv_path


def main() -> int:
    ap = argparse.ArgumentParser(description="Sweep configs and compare.")
    ap.add_argument("--experiment", choices=sorted(EXPERIMENTS), default="e1")
    ap.add_argument("--config", default="config/camera_params.yaml")
    ap.add_argument("--source", choices=["v4l2", "file"], default="v4l2",
                    help="'file' (reproducible) recommended; record clips first.")
    ap.add_argument("--replay-dir", default=None)
    ap.add_argument("--duration", type=float, default=25.0, help="seconds per variant")
    ap.add_argument("--control-ms", type=int, default=300)
    ap.add_argument("--warmup", type=float, default=4.0, help="warmup seconds dropped in analysis")
    ap.add_argument("--out-dir", default="experiments/results")
    args = ap.parse_args()

    args.out_dir = os.path.abspath(args.out_dir)
    os.makedirs(args.out_dir, exist_ok=True)
    if args.source == "v4l2":
        print("[benchmark] WARNING: live cameras are not reproducible; for rigorous "
              "numbers record clips and use --source file.", file=sys.stderr)

    variants = EXPERIMENTS[args.experiment]
    csvs = [run_variant(label, extra, args) for label, extra in variants]

    print("\n[benchmark] ================ RESULTS ================")
    results = []
    for path in csvs:
        r = analyze.analyze(path, warmup_s=args.warmup)
        if r is not None:
            # shorten the run label for the table
            r["run"] = os.path.basename(path).replace(f"{args.experiment}_", "").replace(".csv", "")
            results.append(r)
    analyze.print_table(results)
    print(f"\n[benchmark] CSVs in {args.out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
