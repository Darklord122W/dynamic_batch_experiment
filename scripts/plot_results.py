#!/usr/bin/env python3
"""plot_results.py — visualize experiment metrics as a PNG chart.

Reads one or more metrics CSVs (written by --metrics-csv) and renders a single
figure with four panels:

  A. e2e latency per run       — mean / p50 / p99 / max grouped bars (the tail is
                                 what matters for real-time; see p99/max).
  B. throughput & compute      — frames/s (bars) with compute-mean overlaid.
  C. e2e latency over time     — one line per run (shows warmup + dynamics).
  D. batch fullness over time  — one line per run (shows skipping: 4 -> 2 -> 4).

Reuses scripts/analyze.py for the summary stats (same warmup handling).

Usage:
    python3 scripts/plot_results.py runA.csv runB.csv [...] \
            [--warmup 4] [--out experiments/results/plot.png]
"""
from __future__ import annotations

import argparse
import csv
import os
import sys

import matplotlib
matplotlib.use("Agg")  # headless: render straight to a file
import matplotlib.pyplot as plt  # noqa: E402

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_HERE, "scripts"))
import analyze  # noqa: E402

# Colorblind-safe qualitative palette (Okabe-Ito).
PALETTE = ["#0072B2", "#E69F00", "#009E73", "#D55E00", "#CC79A7", "#56B4E9"]


def _load_timeline(path: str, warmup: float):
    """Return (t, e2e, n_in) lists for one CSV, after the warmup window."""
    t, e2e, n_in = [], [], []
    with open(path) as f:
        for r in csv.DictReader(f):
            tm = float(r["t_mono"])
            if tm < warmup:
                continue
            t.append(tm)
            e2e.append(float(r["e2e_ms"]) if float(r["e2e_ms"]) >= 0 else None)
            n_in.append(int(r["n_in_batch"]))
    return t, e2e, n_in


def main() -> int:
    ap = argparse.ArgumentParser(description="Plot experiment metrics CSVs.")
    ap.add_argument("csvs", nargs="+", help="metrics CSV files")
    ap.add_argument("--warmup", type=float, default=4.0)
    ap.add_argument("--out", default="experiments/results/plot.png")
    ap.add_argument("--labels", default=None,
                    help="comma-separated run labels (default: file names)")
    args = ap.parse_args()

    labels = (args.labels.split(",") if args.labels
              else [os.path.splitext(os.path.basename(c))[0] for c in args.csvs])
    stats = [analyze.analyze(c, warmup_s=args.warmup) for c in args.csvs]
    keep = [(lb, c, s) for lb, c, s in zip(labels, args.csvs, stats) if s is not None]
    if not keep:
        print("[plot] no data after warmup.")
        return 1
    labels = [k[0] for k in keep]
    colors = [PALETTE[i % len(PALETTE)] for i in range(len(keep))]

    fig, ax = plt.subplots(2, 2, figsize=(13, 9))
    fig.suptitle("multicam_perception_rt — experiment metrics", fontsize=14, fontweight="bold")

    # ---- A. e2e latency percentiles (grouped bars) ----
    a = ax[0, 0]
    metrics = [("mean", "e2e_mean"), ("p50", "e2e_p50"), ("p99", "e2e_p99"), ("max", "e2e_max")]
    x = range(len(metrics))
    w = 0.8 / max(1, len(keep))
    for i, (lb, _c, s) in enumerate(keep):
        vals = [s[key] for _, key in metrics]
        a.bar([xx + i * w for xx in x], vals, w, label=lb, color=colors[i])
    a.set_xticks([xx + w * (len(keep) - 1) / 2 for xx in x])
    a.set_xticklabels([m[0] for m in metrics])
    a.set_ylabel("e2e latency (ms)")
    a.set_title("A. End-to-end latency (lower is better; watch p99/max)")
    a.legend(fontsize=8)
    a.grid(axis="y", alpha=0.3)

    # ---- B. throughput (frames/s) with compute-mean overlay ----
    b = ax[0, 1]
    xi = range(len(keep))
    b.bar(xi, [s["frames_s"] for _, _c, s in keep], 0.6, color=colors, alpha=0.85)
    b.set_xticks(list(xi))
    b.set_xticklabels(labels, rotation=20, ha="right", fontsize=8)
    b.set_ylabel("throughput (frames/s)")
    b.set_title("B. Throughput (bars) & compute mean (line)")
    b.grid(axis="y", alpha=0.3)
    b2 = b.twinx()
    b2.plot(list(xi), [s["compute_mean"] for _, _c, s in keep], "o-", color="#444", label="compute mean")
    b2.set_ylabel("compute mean (ms)")

    # ---- C. e2e over time ----
    c = ax[1, 0]
    for i, (lb, path, _s) in enumerate(keep):
        t, e2e, _ = _load_timeline(path, args.warmup)
        t0 = t[0] if t else 0
        xs = [tt - t0 for tt, e in zip(t, e2e) if e is not None]
        ys = [e for e in e2e if e is not None]
        c.plot(xs, ys, color=colors[i], alpha=0.7, linewidth=0.9, label=lb)
    c.set_xlabel("time since warmup (s)")
    c.set_ylabel("e2e latency (ms)")
    c.set_title("C. e2e latency over time")
    c.legend(fontsize=8)
    c.grid(alpha=0.3)

    # ---- D. batch fullness over time ----
    d = ax[1, 1]
    for i, (lb, path, _s) in enumerate(keep):
        t, _, n_in = _load_timeline(path, args.warmup)
        t0 = t[0] if t else 0
        d.plot([tt - t0 for tt in t], n_in, color=colors[i], alpha=0.7, linewidth=0.9, label=lb)
    d.set_xlabel("time since warmup (s)")
    d.set_ylabel("frames in batch")
    d.set_title("D. Batch fullness (skipping shows as 4 -> fewer)")
    d.legend(fontsize=8)
    d.grid(alpha=0.3)

    fig.tight_layout(rect=[0, 0, 1, 0.97])
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    fig.savefig(args.out, dpi=110)
    print(f"[plot] wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
