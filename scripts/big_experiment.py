#!/usr/bin/env python3
"""big_experiment.py — controlled, sequential A/B campaign vs the baseline.

Answers: "does any of the added machinery actually beat the original pipeline?"

Design:
  * ONE pipeline runs at a time (subprocess, blocking) — no GPU contention, so
    every run gets full hardware (fair, representative numbers).
  * Each variant is REPEATED (default 3x) so we can report mean +/- std and tell a
    real effect from run-to-run noise.
  * All variants replay the SAME clips (reproducible input) and change ONE thing
    vs baseline (controlled).
  * A short cooldown between runs lets the GPU/thermal settle.

Outputs (in --out): per-run CSVs, a summary table (printed + summary.txt),
summary_bars.png (mean+/-std per variant) and timelines.png.

Variants (vs baseline = original behaviour: all cameras, fixed timeout, fixed
batch, sync off):
  baseline           control
  adaptiveT_all      adaptive timeout, all cameras (isolates timeout adaptation)
  skip_fixedT        scheduled skip to 2 cams, fixed timeout (isolates skipping)
  skip_adaptiveT     scheduled skip + adaptive timeout (the recommended combo)
  skip_adaptTB       skip + adaptive timeout + adaptive batch-size (all machinery)
  sync_inputs        sync-inputs on, all cameras (isolates input synchronization)

Usage:
    python3 scripts/big_experiment.py --duration 25 --repeats 3
"""
from __future__ import annotations

import argparse
import os
import statistics
import subprocess
import sys
import time

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_HERE, "scripts"))
import analyze  # noqa: E402

BASE = "config/camera_params.yaml"
SKIP = "experiments/exp_skip_big.yaml"
SYNC = "experiments/exp_sync_big.yaml"

# (label, config, extra CLI args) — each changes ONE thing vs baseline.
VARIANTS = [
    ("baseline",       BASE, ["--context", "all", "--timeout-policy", "fixed", "--batch-policy", "fixed"]),
    ("adaptiveT_all",  BASE, ["--context", "all", "--timeout-policy", "adaptive", "--batch-policy", "fixed"]),
    ("skip_fixedT",    SKIP, ["--context", "scheduled", "--timeout-policy", "fixed", "--batch-policy", "fixed"]),
    ("skip_adaptiveT", SKIP, ["--context", "scheduled", "--timeout-policy", "adaptive", "--batch-policy", "fixed"]),
    ("skip_adaptTB",   SKIP, ["--context", "scheduled", "--timeout-policy", "adaptive", "--batch-policy", "adaptive"]),
    ("activity_skip",  BASE, ["--context", "activity", "--timeout-policy", "fixed", "--batch-policy", "fixed"]),
    ("sync_inputs",    SYNC, ["--context", "all", "--timeout-policy", "fixed", "--batch-policy", "fixed"]),
]

PALETTE = ["#0072B2", "#56B4E9", "#E69F00", "#009E73", "#D55E00", "#CC79A7", "#666666", "#882255"]
# metrics to aggregate/plot: (stat key, pretty label, "lower better"?)
METRICS = [
    ("e2e_mean", "e2e mean (ms)", True),
    ("e2e_p99", "e2e p99 (ms)", True),
    ("compute_mean", "compute mean (ms)", True),
    ("frames_s", "REAL throughput (frames/s)", False),  # source-matched, phantom-free
]


def run_one(label: str, cfg: str, extra: list, rep: int, args) -> str:
    """Run main.py once (blocking) and return its metrics CSV path."""
    csv = os.path.join(args.out, f"{label}_r{rep}.csv")
    cmd = [
        sys.executable, "main.py",
        "--config", cfg, "--source", "file", "--replay-dir", args.clips,
        "--log", "none", "--metrics-csv", csv,
        "--duration", str(args.duration), "--control-ms", "300",
    ] + extra
    subprocess.run(cmd, cwd=_HERE, check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return csv


def aggregate(reps: list) -> dict:
    """mean/std across repeat stat-dicts for each metric key."""
    out = {}
    reps = [r for r in reps if r is not None]
    for key in [m[0] for m in METRICS] + ["fullness", "reported_fullness",
                                          "wait_mean", "fes", "stability", "e2e_max"]:
        vals = [r[key] for r in reps if r.get(key) == r.get(key)]  # drop NaN
        if vals:
            out[key] = (statistics.mean(vals),
                        statistics.pstdev(vals) if len(vals) > 1 else 0.0)
        else:
            out[key] = (float("nan"), 0.0)
    out["n"] = len(reps)
    return out


def plot_summary(labels, agg, out_path):
    """Bar chart per metric with mean+/-std error bars across repeats."""
    fig, axes = plt.subplots(2, 2, figsize=(13, 9))
    fig.suptitle("Big experiment — mean ± std across repeats (vs baseline)",
                 fontsize=14, fontweight="bold")
    base = labels[0]
    for ax, (key, lab, lower_better) in zip(axes.flat, METRICS):
        means = [agg[l][key][0] for l in labels]
        stds = [agg[l][key][1] for l in labels]
        colors = [PALETTE[i % len(PALETTE)] for i in range(len(labels))]
        ax.bar(range(len(labels)), means, yerr=stds, capsize=4, color=colors, alpha=0.9)
        ax.set_xticks(range(len(labels)))
        ax.set_xticklabels(labels, rotation=25, ha="right", fontsize=8)
        ax.set_ylabel(lab)
        arrow = "lower=better" if lower_better else "higher=better"
        ax.set_title(f"{lab}  ({arrow})")
        ax.grid(axis="y", alpha=0.3)
        # annotate % change vs baseline
        b = agg[base][key][0]
        for i, l in enumerate(labels):
            if i == 0 or b == 0 or b != b:
                continue
            pct = (agg[l][key][0] - b) / b * 100.0
            ax.annotate(f"{pct:+.0f}%", (i, means[i]), ha="center",
                        va="bottom", fontsize=7, color="#333")
    fig.tight_layout(rect=[0, 0, 1, 0.96])
    fig.savefig(out_path, dpi=110)
    plt.close(fig)


def plot_timelines(labels, csv_by_label, warmup, out_path):
    """e2e and batch-fullness over time (representative repeat) per variant."""
    fig, (a, b) = plt.subplots(2, 1, figsize=(13, 9))
    fig.suptitle("Big experiment — representative run over time", fontsize=14, fontweight="bold")
    import csv as csvmod
    for i, l in enumerate(labels):
        t, e2e, nin = [], [], []
        with open(csv_by_label[l]) as f:
            for r in csvmod.DictReader(f):
                tm = float(r["t_mono"])
                if tm < warmup:
                    continue
                t.append(tm)
                e2e.append(float(r["e2e_ms"]))
                nin.append(int(r["n_in_batch"]))
        if not t:
            continue
        t0 = t[0]
        xs = [tt - t0 for tt in t]
        c = PALETTE[i % len(PALETTE)]
        a.plot([x for x, e in zip(xs, e2e) if e >= 0], [e for e in e2e if e >= 0],
               color=c, alpha=0.7, lw=0.9, label=l)
        b.plot(xs, nin, color=c, alpha=0.7, lw=0.9, label=l)
    a.set_ylabel("e2e latency (ms)"); a.set_title("e2e latency over time"); a.grid(alpha=0.3); a.legend(fontsize=8)
    b.set_ylabel("frames in batch"); b.set_xlabel("time since warmup (s)")
    b.set_title("batch fullness over time (skip shows as 4 -> 2)"); b.grid(alpha=0.3); b.legend(fontsize=8)
    fig.tight_layout(rect=[0, 0, 1, 0.96])
    fig.savefig(out_path, dpi=110)
    plt.close(fig)


def plot_tradeoff(labels, agg, out_path):
    """Scatter: REAL throughput vs cost (compute and e2e). Bottom-right = best."""
    fig, (a, b) = plt.subplots(1, 2, figsize=(14, 6))
    fig.suptitle("Cost vs throughput tradeoff  (bottom-right = best: more work per less cost)",
                 fontsize=13, fontweight="bold")
    for i, l in enumerate(labels):
        c = PALETTE[i % len(PALETTE)]
        x, xe = agg[l]["frames_s"]
        for ax, key in ((a, "compute_mean"), (b, "e2e_mean")):
            y, ye = agg[l][key]
            ax.errorbar(x, y, xerr=xe, yerr=ye, fmt="o", ms=11, color=c, capsize=3, zorder=3)
            ax.annotate(l, (x, y), fontsize=8, xytext=(7, 4), textcoords="offset points")
    a.set_xlabel("REAL throughput (frames/s)  ->  more cameras covered")
    a.set_ylabel("compute mean (ms)  ->  less GPU work (down)")
    a.set_title("throughput vs compute (GPU/power cost)")
    a.grid(alpha=0.3)
    b.set_xlabel("REAL throughput (frames/s)  ->  more cameras covered")
    b.set_ylabel("e2e mean (ms)  ->  lower latency (down)")
    b.set_title("throughput vs latency")
    b.grid(alpha=0.3)
    fig.tight_layout(rect=[0, 0, 1, 0.95])
    fig.savefig(out_path, dpi=110)
    plt.close(fig)


def plot_active_strip(csv_path, warmup, out_path, title):
    """Heatmap of per-camera active state over time (green=active, white=skipped)."""
    import csv as csvmod
    import numpy as np
    t, masks = [], []
    with open(csv_path) as f:
        for r in csvmod.DictReader(f):
            if "active_mask" not in r or not r["active_mask"] or float(r["t_mono"]) < warmup:
                continue
            t.append(float(r["t_mono"]))
            masks.append(r["active_mask"])
    if not t:
        return False
    ncam = len(masks[0])
    mat = np.array([[int(m[c]) for m in masks] for c in range(ncam)])
    fig, ax = plt.subplots(figsize=(13, 3.6))
    ax.imshow(mat, aspect="auto", cmap="Greens", vmin=0, vmax=1, interpolation="nearest",
              extent=[0, t[-1] - t[0], ncam - 0.5, -0.5])
    ax.set_yticks(range(ncam))
    ax.set_yticklabels([f"cam{i}" for i in range(ncam)])
    ax.set_xlabel("time since warmup (s)")
    ax.set_title(title)
    frac = [f"{100 * mat[c].mean():.0f}% active" for c in range(ncam)]
    for c in range(ncam):
        ax.text(1.005, c, frac[c], transform=ax.get_yaxis_transform(), va="center", fontsize=8)
    fig.tight_layout()
    fig.savefig(out_path, dpi=110)
    plt.close(fig)
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="Sequential repeated A/B campaign vs baseline.")
    ap.add_argument("--clips", default="experiments/clips")
    ap.add_argument("--duration", type=float, default=25.0)
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--warmup", type=float, default=6.0)
    ap.add_argument("--cooldown", type=float, default=2.0)
    ap.add_argument("--out", default="experiments/results/big")
    ap.add_argument("--reanalyze", action="store_true",
                    help="skip the runs; re-aggregate + re-plot the existing CSVs in --out")
    args = ap.parse_args()
    args.out = os.path.join(_HERE, args.out) if not os.path.isabs(args.out) else args.out
    os.makedirs(args.out, exist_ok=True)

    labels = [v[0] for v in VARIANTS]
    per_variant_reps = {l: [] for l in labels}
    rep0_csv = {}
    total = len(VARIANTS) * args.repeats
    done = 0
    t_start = time.monotonic()

    for label, cfg, extra in VARIANTS:
        for rep in range(args.repeats):
            done += 1
            csv = os.path.join(args.out, f"{label}_r{rep}.csv")
            if not args.reanalyze:
                print(f"[big] ({done}/{total}) {label} repeat {rep + 1}/{args.repeats} "
                      f"[{time.monotonic() - t_start:.0f}s elapsed] ...", flush=True)
                csv = run_one(label, cfg, extra, rep, args)
            if rep == 0:
                rep0_csv[label] = csv
            if os.path.exists(csv):
                per_variant_reps[label].append(analyze.analyze(csv, warmup_s=args.warmup))
            if not args.reanalyze:
                time.sleep(args.cooldown)

    agg = {l: aggregate(per_variant_reps[l]) for l in labels}

    # ---- summary table ----
    lines = []
    lines.append(f"BIG EXPERIMENT — {args.repeats} repeats, duration {args.duration}s, "
                 f"warmup {args.warmup}s, replay input (sequential, one GPU at a time)")
    lines.append("")
    hdr = (f"{'variant':16}" + "".join(f"{m[1].split(' (')[0]:>16}" for m in METRICS)
           + f"{'wait':>8}{'real/bt':>9}{'rep/bt':>8}")
    lines.append(hdr)
    lines.append("-" * len(hdr))
    base = labels[0]
    for l in labels:
        row = f"{l:16}"
        for key, _lab, _lb in METRICS:
            m, s = agg[l][key]
            row += f"{m:8.1f}±{s:<5.1f}"[:16].rjust(16)
        row += f"{agg[l]['wait_mean'][0]:>8.1f}{agg[l]['fullness'][0]:>9.2f}{agg[l]['reported_fullness'][0]:>8.2f}"
        lines.append(row)
    lines.append("")
    lines.append("Deltas vs baseline (negative e2e/compute = better; positive frames/s = better):")
    for l in labels[1:]:
        parts = []
        for key, lab, _lb in METRICS:
            b = agg[base][key][0]
            if b and b == b:
                parts.append(f"{lab.split(' (')[0]} {(agg[l][key][0]-b)/b*100:+.0f}%")
        lines.append(f"  {l:16} " + " | ".join(parts))
    summary = "\n".join(lines)
    print("\n" + summary)
    with open(os.path.join(args.out, "summary.txt"), "w") as f:
        f.write(summary + "\n")

    # ---- plots ----
    plot_summary(labels, agg, os.path.join(args.out, "summary_bars.png"))
    plot_timelines(labels, rep0_csv, args.warmup, os.path.join(args.out, "timelines.png"))
    plot_tradeoff(labels, agg, os.path.join(args.out, "tradeoff.png"))
    made_strip = False
    if "activity_skip" in rep0_csv and os.path.exists(rep0_csv["activity_skip"]):
        made_strip = plot_active_strip(
            rep0_csv["activity_skip"], args.warmup,
            os.path.join(args.out, "activity_cameras.png"),
            "Adaptive (activity) skip — which cameras are active over time (green = active)")
    outs = "summary.txt, summary_bars.png, timelines.png, tradeoff.png" + (
        ", activity_cameras.png" if made_strip else "")
    print(f"\n[big] wrote {outs} to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
