#!/usr/bin/env python3
"""timeout_sweep.py — sweep nvstreammux batched-push-timeout, sync-inputs OFF.

Runs the LIVE cameras in real time with the FIXED-batch (static b4) engine and
measures how many frames actually land in each batch that goes through nvinfer,
as a function of batched-push-timeout. With sync-inputs=0 the muxer pushes on
"all pads delivered OR timeout", so the timeout is the knob that decides whether
the batch waits for all 4 free-running C920s or ships part-full.

Per timeout value it reports/plots:
  * the DISTRIBUTION of n_in_batch (how many batches carried 1/2/3/4 frames),
  * mean frames per batch,
  * batches/s and real frames/s through the inference engine,
  * e2e latency (camera -> tracker out, worst frame in batch), mean and p99.

Usage:
  python3 scripts/timeout_sweep.py                          # default sweep
  python3 scripts/timeout_sweep.py --ms 1 5 10 33 66 100    # custom values (ms)
  python3 scripts/timeout_sweep.py --duration 25 --warmup 5
"""
import argparse
import csv
import os
import statistics
import subprocess
import sys
import tempfile
from collections import Counter

import yaml

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # repo root

# ---- palette (validated reference palette; light surface) ------------------ #
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
BASELINE = "#c3c2b7"
BLUE = "#2a78d6"          # series-1 / sequential 450
BLUE_DARK = "#104281"     # sequential 650 (p99 vs mean: same hue, darker step)
GREEN = "#009E73"
# ordinal ramp for k frames in batch (1..4): sequential blue, steps 250/350/450/600
RAMP4 = ["#86b6ef", "#5598e7", "#2a78d6", "#184f95"]


def make_config(base_cfg: str, tmpdir: str, pgie_cfg: str) -> str:
    """Write a temp config: sync_inputs=0, live v4l2, the given pgie config."""
    with open(base_cfg) as f:
        cfg = yaml.safe_load(f)
    mux = cfg.setdefault("streammux", {})
    mux["sync_inputs"] = 0                       # sync OFF: timeout is the knob
    cfg.setdefault("source", {})["type"] = "v4l2"  # live cameras, real time

    def _abs(p):
        return p if os.path.isabs(p) else os.path.join(_HERE, p)

    # Which engine: config/_pgie_static.txt (batch locked at 4) or
    # config/pgie_config.txt (dynamic-batch engine, min 1 / max 4).
    cfg.setdefault("pgie", {})["config_file"] = _abs(pgie_cfg)
    if "tracker" in cfg and "ll_config_file" in cfg["tracker"]:
        cfg["tracker"]["ll_config_file"] = _abs(cfg["tracker"]["ll_config_file"])

    path = os.path.join(tmpdir, "timeout_sweep.yaml")
    with open(path, "w") as f:
        yaml.safe_dump(cfg, f, sort_keys=False)
    return path


def run_one(cfg: str, csv_path: str, duration: float, push_us: int) -> None:
    """Run main.py live (blocking) with a fixed batched-push-timeout."""
    cmd = [
        sys.executable, "main.py",
        "--config", cfg, "--source", "v4l2",
        "--log", "none", "--metrics-csv", csv_path,
        "--timeout-policy", "fixed", "--timeout-us", str(int(push_us)),
        "--batch-policy", "fixed", "--context", "all",
        "--duration", str(duration), "--control-ms", "300",
    ]
    subprocess.run(cmd, cwd=_HERE, check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def summarize(csv_path: str, warmup: float, num_cams: int):
    """Aggregate one run's per-batch CSV into the numbers we plot."""
    if not os.path.exists(csv_path):
        return None
    with open(csv_path) as f:
        rows = [r for r in csv.DictReader(f) if float(r["t_mono"]) >= warmup]
    if not rows:
        return None
    nin = [int(r["n_in_batch"]) for r in rows]
    nreal = [int(r["n_real"]) for r in rows]
    t0, t1 = float(rows[0]["t_mono"]), float(rows[-1]["t_mono"])
    dur = max(t1 - t0, 1e-6)
    e2e = sorted(float(r["e2e_ms"]) for r in rows if float(r["e2e_ms"]) >= 0)
    comp = [float(r["compute_ms"]) for r in rows if float(r["compute_ms"]) >= 0]
    dist = Counter(nin)
    return {
        "n_batches": len(nin),
        "mean_nin": statistics.mean(nin),
        "mean_nreal": statistics.mean(nreal),
        "batches_s": len(nin) / dur,
        "frames_s": sum(nin) / dur,
        "e2e_mean": statistics.mean(e2e) if e2e else float("nan"),
        "e2e_p99": e2e[int(0.99 * (len(e2e) - 1))] if e2e else float("nan"),
        "compute_mean": statistics.mean(comp) if comp else float("nan"),
        # fraction of batches that carried exactly k frames, k = 0..num_cams
        "dist_frac": [dist.get(k, 0) / len(nin) for k in range(num_cams + 1)],
        "dist_str": " ".join(f"{k}:{dist.get(k, 0)}" for k in range(num_cams + 1)),
    }


def _style_axis(ax):
    ax.set_facecolor(SURFACE)
    ax.grid(alpha=0.6, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    for spine in ("left", "bottom"):
        ax.spines[spine].set_color(BASELINE)
    ax.tick_params(colors=MUTED, labelsize=9)
    ax.xaxis.label.set_color(INK_2)
    ax.yaxis.label.set_color(INK_2)
    ax.title.set_color(INK)


def plot(results, num_cams: int, out_png: str, engine_label: str = "fixed batch-4") -> None:
    """4-panel figure: batch-fill distribution, mean fill, throughput, latency."""
    pts = [(ms, s) for ms, s in results if s]
    if not pts:
        print("nothing to plot (all runs failed)", file=sys.stderr)
        return
    ms_vals = [ms for ms, _ in pts]
    xs = list(range(len(pts)))  # equal spacing for the bar panel
    labels = [f"{ms:g}" for ms in ms_vals]

    fig, axes = plt.subplots(2, 2, figsize=(14, 9.5))
    fig.patch.set_facecolor(SURFACE)
    fig.suptitle(
        f"batched-push-timeout sweep — sync-inputs OFF, {engine_label} engine, "
        f"{num_cams} live C920 @30fps",
        fontsize=13, fontweight="bold", color=INK,
    )
    (ax_dist, ax_mean), (ax_thr, ax_lat) = axes

    # (a) stacked distribution: % of inference batches carrying k frames
    bottom = [0.0] * len(pts)
    for k in range(1, num_cams + 1):
        vals = [100.0 * s["dist_frac"][k] for _, s in pts]
        ax_dist.bar(xs, vals, bottom=bottom, width=0.72, color=RAMP4[k - 1],
                    edgecolor=SURFACE, linewidth=2,
                    label=f"{k} frame{'s' if k > 1 else ''}")
        for x, v, b in zip(xs, vals, bottom):
            if v >= 8:  # selective direct labels: only segments big enough to read
                ax_dist.text(x, b + v / 2, f"{v:.0f}%", ha="center", va="center",
                             fontsize=8, color=SURFACE if k >= 3 else INK)
        bottom = [b + v for b, v in zip(bottom, vals)]
    ax_dist.set_xticks(xs)
    ax_dist.set_xticklabels(labels)
    ax_dist.set_ylim(0, 100)
    ax_dist.set_xlabel("batched-push-timeout (ms)")
    ax_dist.set_ylabel("% of inference batches")
    ax_dist.set_title("how full is the batch entering nvinfer?", pad=30)
    ax_dist.legend(fontsize=8, ncol=num_cams, loc="lower center",
                   bbox_to_anchor=(0.5, 1.0), frameon=False)

    # (b) mean frames per batch vs timeout
    ax_mean.plot(ms_vals, [s["mean_nin"] for _, s in pts], "o-", color=BLUE,
                 linewidth=2, markersize=6, label="mean frames/batch")
    ax_mean.axhline(num_cams, ls="--", color=MUTED, lw=1,
                    label=f"all {num_cams} cams (ideal)")
    ax_mean.axvline(33.3, ls=":", color=GREEN, lw=1, label="1 frame period (33.3 ms)")
    ax_mean.set_ylim(0.8, num_cams + 0.3)
    ax_mean.set_xlabel("batched-push-timeout (ms)")
    ax_mean.set_ylabel(f"mean frames in batch (of {num_cams})")
    ax_mean.set_title("longer wait → fuller batch?")
    ax_mean.legend(fontsize=8)

    # (c) throughput: real frames/s through the engine + batches/s
    ax_thr.plot(ms_vals, [s["frames_s"] for _, s in pts], "o-", color=BLUE,
                linewidth=2, markersize=6, label="frames/s through nvinfer")
    ax_thr.plot(ms_vals, [s["batches_s"] for _, s in pts], "o-", color=BLUE_DARK,
                linewidth=2, markersize=6, label="batches/s (engine invocations)")
    ax_thr.axhline(num_cams * 30, ls="--", color=MUTED, lw=1,
                   label=f"capture rate ({num_cams}x30 fps)")
    ax_thr.axvline(33.3, ls=":", color=GREEN, lw=1)
    ax_thr.set_ylim(bottom=0)
    ax_thr.set_xlabel("batched-push-timeout (ms)")
    ax_thr.set_ylabel("per second")
    ax_thr.set_title("throughput: fewer, fuller batches vs many part-full ones")
    ax_thr.legend(fontsize=8)

    # (d) e2e latency (mean + p99) vs timeout — same hue, darker step for p99
    ax_lat.plot(ms_vals, [s["e2e_mean"] for _, s in pts], "o-", color=BLUE,
                linewidth=2, markersize=6, label="e2e mean")
    ax_lat.plot(ms_vals, [s["e2e_p99"] for _, s in pts], "o-", color=BLUE_DARK,
                linewidth=2, markersize=6, label="e2e p99")
    ax_lat.axvline(33.3, ls=":", color=GREEN, lw=1, label="1 frame period (33.3 ms)")
    ax_lat.set_ylim(bottom=0)
    ax_lat.set_xlabel("batched-push-timeout (ms)")
    ax_lat.set_ylabel("camera → tracker-out latency (ms)")
    ax_lat.set_title("the latency price of waiting for a fuller batch")
    ax_lat.legend(fontsize=8)

    for ax in (ax_dist, ax_mean, ax_thr, ax_lat):
        _style_axis(ax)

    fig.tight_layout(rect=[0, 0, 1, 0.94])
    fig.savefig(out_png, dpi=110, facecolor=SURFACE)
    print(f"\nsaved plot -> {out_png}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Sweep batched-push-timeout (sync OFF, fixed-batch engine, live cams).")
    ap.add_argument("--config", default="config/camera_params.yaml")
    ap.add_argument("--pgie", default="config/_pgie_static.txt",
                    help="nvinfer config: config/_pgie_static.txt (fixed batch-4 "
                         "engine, default) or config/pgie_config.txt (dynamic batch)")
    ap.add_argument("--tag", default=None,
                    help="label used in the plot title (default: static/dynamic "
                         "inferred from --pgie)")
    ap.add_argument("--ms", type=float, nargs="+",
                    default=[1, 5, 10, 20, 33.3, 50, 66.7, 100],
                    help="batched-push-timeout values to test, in milliseconds")
    ap.add_argument("--num-cams", type=int, default=4)
    ap.add_argument("--duration", type=float, default=20.0,
                    help="seconds per run (incl. warmup)")
    ap.add_argument("--warmup", type=float, default=4.0,
                    help="seconds dropped from the front of each run's analysis")
    ap.add_argument("--out", default="experiments/results/timeout_sweep")
    args = ap.parse_args()

    outdir = os.path.join(_HERE, args.out)
    os.makedirs(outdir, exist_ok=True)
    base_cfg = os.path.join(_HERE, args.config)
    tmpdir = tempfile.mkdtemp(prefix="timeout_sweep_")
    cfg = make_config(base_cfg, tmpdir, args.pgie)
    tag = args.tag or ("dynamic batch" if "_pgie_static" not in args.pgie
                       else f"fixed batch-{args.num_cams}")

    print(f"Sweeping {len(args.ms)} timeout values, {args.duration}s each "
          f"(live cameras, sync-inputs OFF, {tag} engine [{args.pgie}])\n")

    results = []
    for ms in args.ms:
        push_us = int(round(ms * 1e3))
        csv_path = os.path.join(outdir, f"push_{ms:g}ms.csv")
        print(f"  push-timeout={ms:g}ms ...", flush=True)
        run_one(cfg, csv_path, args.duration, push_us)
        s = summarize(csv_path, args.warmup, args.num_cams)
        results.append((ms, s))
        if s:
            print(f"    mean in-batch {s['mean_nin']:.2f}  "
                  f"batches/s {s['batches_s']:.1f}  frames/s {s['frames_s']:.1f}  "
                  f"e2e {s['e2e_mean']:.1f}ms  dist [{s['dist_str']}]")
        else:
            print("    FAILED (no metrics rows)")

    # ---- summary table + CSV ----
    hdr = (f"{'push (ms)':>10}{'mean in-batch':>15}{'% full':>8}{'batches/s':>11}"
           f"{'frames/s':>10}{'e2e mean':>10}{'e2e p99':>10}{'compute':>9}   distribution 0..4")
    print("\n" + hdr)
    print("-" * len(hdr))
    summary_path = os.path.join(outdir, "summary.csv")
    with open(summary_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["push_ms", "mean_n_in_batch", "frac_full", "batches_s", "frames_s",
                    "e2e_mean_ms", "e2e_p99_ms", "compute_mean_ms"]
                   + [f"frac_{k}" for k in range(args.num_cams + 1)])
        for ms, s in results:
            if s is None:
                print(f"{ms:>10g}{'FAILED':>15}")
                continue
            full = 100.0 * s["dist_frac"][args.num_cams]
            print(f"{ms:>10g}{s['mean_nin']:>15.2f}{full:>7.0f}%{s['batches_s']:>11.1f}"
                  f"{s['frames_s']:>10.1f}{s['e2e_mean']:>10.1f}{s['e2e_p99']:>10.1f}"
                  f"{s['compute_mean']:>9.1f}   [{s['dist_str']}]")
            w.writerow([ms, f"{s['mean_nin']:.3f}", f"{s['dist_frac'][args.num_cams]:.3f}",
                        f"{s['batches_s']:.2f}", f"{s['frames_s']:.2f}",
                        f"{s['e2e_mean']:.2f}", f"{s['e2e_p99']:.2f}",
                        f"{s['compute_mean']:.2f}"]
                       + [f"{fr:.4f}" for fr in s["dist_frac"]])
    print(f"\nsummary CSV -> {summary_path}")

    plot(results, args.num_cams, os.path.join(outdir, "timeout_sweep.png"), tag)
    print(f"per-run CSVs in -> {outdir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
