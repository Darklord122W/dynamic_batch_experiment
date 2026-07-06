#!/usr/bin/env python3
"""sync_grid.py — 2D sweep of max-latency x batched-push-timeout (sync-inputs ON).

The 1D sweep (scripts/sync_sweep.py) showed the two knobs are coupled: widening
max-latency does nothing unless the muxer also waits (push-timeout) long enough to
use that window. This script maps the full 2D space to answer: at what
(max-latency, push-timeout) do all 4 free-running C920s finally land in one batch,
and how do the two knobs trade off?

For every (max_latency, push_timeout) cell we run the LIVE cameras in real time and
record:
  * mean n_in_batch      — average frames that made the batch (of num_cams)
  * pct_full             — % of batches that hit all num_cams
  * e2e_mean             — the latency cost of waiting

Outputs a table + two heatmaps (mean fullness, and e2e cost).

Usage:
  python3 scripts/sync_grid.py                                  # default grid
  python3 scripts/sync_grid.py --max-lat 33 100 300 --push 33 100 300
  python3 scripts/sync_grid.py --duration 16 --warmup 4
"""
import argparse
import csv
import os
import statistics
import tempfile
from collections import Counter

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# reuse the exact config-gen / run / load helpers from the 1D sweep
from sync_sweep import make_config, run_one, load, _HERE


def summarize(csv_path, warmup, num_cams):
    rows = load(csv_path, warmup)
    if not rows:
        return None
    nin = [int(r["n_in_batch"]) for r in rows]
    e2e = [float(r["e2e_ms"]) for r in rows if float(r["e2e_ms"]) >= 0]
    dist = Counter(nin)
    return {
        "mean_nin": statistics.mean(nin),
        "pct_full": 100.0 * dist.get(num_cams, 0) / len(nin),
        "e2e_mean": statistics.mean(e2e) if e2e else float("nan"),
        "dist": dist,
        "n": len(nin),
    }


def heatmap(ax, grid, xs, ys, title, fmt, cmap):
    im = ax.imshow(grid, origin="lower", aspect="auto", cmap=cmap)
    ax.set_xticks(range(len(xs))); ax.set_xticklabels([f"{x:g}" for x in xs])
    ax.set_yticks(range(len(ys))); ax.set_yticklabels([f"{y:g}" for y in ys])
    ax.set_xlabel("batched-push-timeout (ms)")
    ax.set_ylabel("max-latency (ms)")
    ax.set_title(title)
    for i in range(len(ys)):
        for j in range(len(xs)):
            v = grid[i][j]
            if v == v:  # not NaN
                ax.text(j, i, fmt.format(v), ha="center", va="center", fontsize=8,
                        color="white" if _dark(im, v) else "black")
    plt.colorbar(im, ax=ax, fraction=0.046, pad=0.04)


def _dark(im, v):
    lo, hi = im.get_clim()
    return (v - lo) / (hi - lo + 1e-9) > 0.55


def main() -> int:
    ap = argparse.ArgumentParser(description="2D grid: max-latency x push-timeout, sync ON.")
    ap.add_argument("--config", default="config/camera_params.yaml")
    ap.add_argument("--max-lat", type=float, nargs="+", default=[33, 66, 100, 200, 300],
                    help="max-latency values (ms)")
    ap.add_argument("--push", type=float, nargs="+", default=[33, 66, 100, 200, 300],
                    help="batched-push-timeout values (ms)")
    ap.add_argument("--num-cams", type=int, default=4)
    ap.add_argument("--duration", type=float, default=16.0)
    ap.add_argument("--warmup", type=float, default=4.0)
    ap.add_argument("--out", default="experiments/results/sync_grid")
    args = ap.parse_args()

    outdir = os.path.join(_HERE, args.out)
    os.makedirs(outdir, exist_ok=True)
    base_cfg = os.path.join(_HERE, args.config)
    tmpdir = tempfile.mkdtemp(prefix="sync_grid_")

    ys, xs = args.max_lat, args.push  # rows = max-latency, cols = push
    n_runs = len(ys) * len(xs)
    print(f"2D grid: {len(ys)} max-latency x {len(xs)} push-timeout = {n_runs} runs, "
          f"{args.duration}s each (live cameras, sync-inputs ON)\n")

    mean_grid = [[float("nan")] * len(xs) for _ in ys]
    full_grid = [[float("nan")] * len(xs) for _ in ys]
    e2e_grid = [[float("nan")] * len(xs) for _ in ys]

    k = 0
    for i, ml in enumerate(ys):
        for j, pu in enumerate(xs):
            k += 1
            ns = int(round(ml * 1e6))
            push_us = int(round(pu * 1e3))
            cfg = make_config(base_cfg, ns, tmpdir)
            csv_path = os.path.join(outdir, f"ml{ml:g}_push{pu:g}.csv")
            print(f"  [{k:>2}/{n_runs}] max-latency={ml:g}ms  push={pu:g}ms ...", flush=True)
            run_one(cfg, csv_path, args.duration, push_us)
            s = summarize(csv_path, args.warmup, args.num_cams)
            if s:
                mean_grid[i][j] = s["mean_nin"]
                full_grid[i][j] = s["pct_full"]
                e2e_grid[i][j] = s["e2e_mean"]

    # ---- report ----
    corner = "ml \\ push"
    print(f"\nMean frames-in-batch (of {args.num_cams})   rows=max-latency(ms), cols=push(ms)")
    print(f"{corner:>10}" + "".join(f"{x:>8g}" for x in xs))
    for i, ml in enumerate(ys):
        print(f"{ml:>10g}" + "".join(f"{mean_grid[i][j]:>8.2f}" for j in range(len(xs))))
    print(f"\n% of batches FULL (all {args.num_cams})")
    print(f"{corner:>10}" + "".join(f"{x:>8g}" for x in xs))
    for i, ml in enumerate(ys):
        print(f"{ml:>10g}" + "".join(f"{full_grid[i][j]:>7.0f}%" for j in range(len(xs))))
    print(f"\ne2e latency mean (ms)")
    print(f"{corner:>10}" + "".join(f"{x:>8g}" for x in xs))
    for i, ml in enumerate(ys):
        print(f"{ml:>10g}" + "".join(f"{e2e_grid[i][j]:>8.0f}" for j in range(len(xs))))

    # ---- heatmaps ----
    fig, axes = plt.subplots(1, 3, figsize=(19, 5.5))
    fig.suptitle("sync-inputs ON: max-latency x push-timeout "
                 "(4 live C920 @30fps, one USB-2 bus)", fontsize=13, fontweight="bold")
    heatmap(axes[0], mean_grid, xs, ys,
            f"mean frames in batch (of {args.num_cams})", "{:.2f}", "viridis")
    heatmap(axes[1], full_grid, xs, ys,
            f"% batches FULL (all {args.num_cams})", "{:.0f}", "magma")
    heatmap(axes[2], e2e_grid, xs, ys, "e2e latency mean (ms)", "{:.0f}", "cividis")
    fig.tight_layout(rect=[0, 0, 1, 0.95])
    plot_path = os.path.join(outdir, "sync_grid.png")
    fig.savefig(plot_path, dpi=110)
    print(f"\nsaved heatmaps -> {plot_path}")
    print(f"CSVs in        -> {outdir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
