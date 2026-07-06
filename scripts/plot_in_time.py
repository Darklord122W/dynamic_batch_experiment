#!/usr/bin/env python3
"""plot_in_time.py — under sync, how many frames actually made it "in time"?

The metrics CSV (from metrics.py) has a column **n_in_batch** = the number of frames
the muxer put in each batch. With `sync-inputs=1`, late frames are dropped, so
n_in_batch is how many cameras were "in time" that cycle (< num_cams when some missed
the max-latency window). With sync off it's normally = num_cams (all in time).

Produces two panels:
  1. histogram — how many batches had 0,1,2,...,N frames in time
  2. timeline  — frames-in-time per batch over the run
and prints, per run: mean in-time/batch, the fraction of cameras in time
(mean/num_cams — no frame-rate assumption needed), the batch rate, and the measured
in-time throughput (sum of n_in_batch / duration).

Usage:
  python3 scripts/plot_in_time.py sync_on.csv [sync_off.csv ...] \
      --num-cams 4 --warmup 3 --out in_time.png
"""
import argparse
import csv
import statistics
from collections import Counter

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


def load(path, warmup):
    with open(path) as f:
        return [r for r in csv.DictReader(f) if float(r["t_mono"]) >= warmup]


def main():
    ap = argparse.ArgumentParser(description="Plot 'frames in time' (n_in_batch) under sync.")
    ap.add_argument("csvs", nargs="+", help="one or more metrics CSVs")
    ap.add_argument("--warmup", type=float, default=3.0)
    ap.add_argument("--num-cams", type=int, default=4)
    ap.add_argument("--out", default="in_time.png")
    args = ap.parse_args()

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(13, 5))
    print(f"{'run':<26}{'mean in-time/batch':>19}{'% cams in time':>16}{'batches/s':>11}{'in-time fps':>13}")
    print("-" * 85)
    for path in args.csvs:
        rows = load(path, args.warmup)
        if not rows:
            print(f"{path}: no rows after warmup")
            continue
        nin = [int(r["n_in_batch"]) for r in rows]
        t = [float(r["t_mono"]) - float(rows[0]["t_mono"]) for r in rows]
        dur = t[-1] or 1.0
        mean_nin = statistics.mean(nin)
        frac = 100.0 * mean_nin / args.num_cams        # fraction of cameras in time — no fps needed
        batches_s = len(nin) / dur
        in_time_fps = sum(nin) / dur                    # measured, not assumed
        label = path.split("/")[-1]
        dist = Counter(nin)
        xs = list(range(args.num_cams + 1))
        ax1.bar([x + 0.0 for x in xs], [dist.get(x, 0) for x in xs], width=0.8, alpha=0.55, label=label)
        ax2.plot(t, nin, ".", ms=3, alpha=0.5, label=label)
        print(f"{label:<26}{mean_nin:>19.2f}{frac:>15.0f}%{batches_s:>11.1f}{in_time_fps:>13.1f}")

    ax1.set_xlabel(f"frames in time per batch (of {args.num_cams})")
    ax1.set_ylabel("# batches")
    ax1.set_title("distribution — how many made the batch")
    ax1.set_xticks(list(range(args.num_cams + 1)))
    ax1.legend(fontsize=8)
    ax2.set_ylabel("frames in time")
    ax2.set_xlabel("time (s)")
    ax2.set_title("frames in time over the run")
    ax2.set_ylim(-0.2, args.num_cams + 0.5)
    ax2.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(args.out, dpi=110)
    print(f"\nsaved -> {args.out}")


if __name__ == "__main__":
    raise SystemExit(main())
