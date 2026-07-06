#!/usr/bin/env python3
"""sync_sweep.py — sweep nvstreammux max-latency with sync-inputs ON (live cams).

Runs the LIVE cameras in real time under sync-inputs=1 and measures how
n_in_batch (frames that made the batch "in time") responds to the alignment
window. Two series, because max-latency alone is not the whole story:

  A. push=33ms   — batched-push-timeout fixed at one frame period (33 ms) while
                   max-latency grows. The batch is pushed at 33 ms before a wider
                   window can gather a 2nd camera, so n_in_batch stays ~1.
  B. push=match  — batched-push-timeout grows WITH max-latency, so the muxer
                   actually waits the full window. Only then can more cameras
                   align — but on 4 free-running C920s sharing one USB-2 bus it
                   tops out around 2, never 4, and e2e latency climbs.

Usage:
  python3 scripts/sync_sweep.py                         # default sweep, both series
  python3 scripts/sync_sweep.py --ms 16 33 66 100 200   # custom values (ms)
  python3 scripts/sync_sweep.py --duration 18 --warmup 4
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


def make_config(base_cfg: str, max_latency_ns: int, tmpdir: str) -> str:
    """Write a temp config: sync_inputs=1, given max_latency_ns, live v4l2 source."""
    with open(base_cfg) as f:
        cfg = yaml.safe_load(f)
    mux = cfg.setdefault("streammux", {})
    mux["sync_inputs"] = 1
    mux["max_latency_ns"] = int(max_latency_ns)
    cfg.setdefault("source", {})["type"] = "v4l2"  # force live cameras

    # The temp config lives outside the repo, so any relative paths in it would
    # resolve wrong. Absolutize them (relative to repo root) so they still point
    # at the real files — nvinfer then resolves the engine/labels inside
    # pgie_config.txt relative to that real config dir, exactly as normal.
    def _abs(p):
        return p if os.path.isabs(p) else os.path.join(_HERE, p)
    if "pgie" in cfg and "config_file" in cfg["pgie"]:
        cfg["pgie"]["config_file"] = _abs(cfg["pgie"]["config_file"])
    if "tracker" in cfg and "ll_config_file" in cfg["tracker"]:
        cfg["tracker"]["ll_config_file"] = _abs(cfg["tracker"]["ll_config_file"])
    path = os.path.join(tmpdir, f"sync_ml_{max_latency_ns}.yaml")
    with open(path, "w") as f:
        yaml.safe_dump(cfg, f, sort_keys=False)
    return path


def run_one(cfg: str, csv_path: str, duration: float, push_us: int) -> None:
    """Run main.py live (blocking); --timeout-us fixes batched-push-timeout."""
    cmd = [
        sys.executable, "main.py",
        "--config", cfg, "--source", "v4l2",
        "--log", "none", "--metrics-csv", csv_path,
        "--timeout-policy", "fixed", "--timeout-us", str(int(push_us)),
        "--duration", str(duration), "--control-ms", "300",
    ]
    subprocess.run(cmd, cwd=_HERE, check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def load(csv_path: str, warmup: float):
    if not os.path.exists(csv_path):
        return []
    with open(csv_path) as f:
        return [r for r in csv.DictReader(f) if float(r["t_mono"]) >= warmup]


def summarize(csv_path, warmup, num_cams):
    rows = load(csv_path, warmup)
    if not rows:
        return None
    nin = [int(r["n_in_batch"]) for r in rows]
    t = [float(r["t_mono"]) - float(rows[0]["t_mono"]) for r in rows]
    e2e = [float(r["e2e_ms"]) for r in rows if float(r["e2e_ms"]) >= 0]
    dur = t[-1] or 1.0
    dist = Counter(nin)
    e2e_s = sorted(e2e)
    return {
        "mean_nin": statistics.mean(nin),
        "frac": 100.0 * statistics.mean(nin) / num_cams,
        "batches_s": len(nin) / dur,
        "in_time_fps": sum(nin) / dur,
        "e2e_mean": statistics.mean(e2e) if e2e else float("nan"),
        "e2e_p99": e2e_s[int(0.99 * (len(e2e) - 1))] if e2e else float("nan"),
        "dist": " ".join(f"{k}:{dist.get(k, 0)}" for k in range(num_cams + 1)),
    }


# (label, how to compute push-timeout in µs from max-latency µs, color)
SERIES = [
    ("push=33ms", lambda ml_us: 33333, "#0072B2"),
    ("push=match", lambda ml_us: ml_us, "#D55E00"),
]


def main() -> int:
    ap = argparse.ArgumentParser(description="Sweep max-latency with sync-inputs ON.")
    ap.add_argument("--config", default="config/camera_params.yaml")
    ap.add_argument("--ms", type=float, nargs="+",
                    default=[16, 33, 50, 66, 100, 150, 200],
                    help="max-latency values to test, in milliseconds")
    ap.add_argument("--num-cams", type=int, default=4)
    ap.add_argument("--duration", type=float, default=18.0, help="total run seconds (incl. warmup)")
    ap.add_argument("--warmup", type=float, default=4.0, help="seconds to drop from analysis")
    ap.add_argument("--out", default="experiments/results/sync_sweep")
    args = ap.parse_args()

    outdir = os.path.join(_HERE, args.out)
    os.makedirs(outdir, exist_ok=True)
    base_cfg = os.path.join(_HERE, args.config)
    tmpdir = tempfile.mkdtemp(prefix="sync_sweep_")

    n_runs = len(args.ms) * len(SERIES)
    print(f"Sweeping {len(args.ms)} max-latency x {len(SERIES)} series = {n_runs} runs, "
          f"{args.duration}s each (live cameras, sync-inputs ON)\n")

    data = {name: [] for name, _, _ in SERIES}  # name -> list of (ms, summary)
    for name, push_fn, _ in SERIES:
        for ms in args.ms:
            ns = int(round(ms * 1e6))
            push_us = push_fn(int(round(ms * 1e3)))
            cfg = make_config(base_cfg, ns, tmpdir)
            csv_path = os.path.join(outdir, f"{name.replace('=', '_')}_ml{ms:g}.csv")
            print(f"  {name:>10}  max-latency={ms:g}ms  push={push_us/1000:g}ms ...", flush=True)
            run_one(cfg, csv_path, args.duration, push_us)
            data[name].append((ms, summarize(csv_path, args.warmup, args.num_cams)))

    # ---- report ----
    hdr = (f"{'series':>10}{'max-lat':>9}{'push':>8}{'mean in-batch':>15}{'% cams':>8}"
           f"{'in-time fps':>13}{'e2e mean':>10}{'e2e p99':>10}   distribution")
    print("\n" + hdr)
    print("-" * len(hdr))
    for name, push_fn, _ in SERIES:
        for ms, s in data[name]:
            push_ms = push_fn(int(round(ms * 1e3))) / 1000
            if s is None:
                print(f"{name:>10}{ms:>7g}ms{push_ms:>6g}ms{'  FAILED':>15}")
                continue
            print(f"{name:>10}{ms:>7g}ms{push_ms:>6g}ms{s['mean_nin']:>15.2f}{s['frac']:>7.0f}%"
                  f"{s['in_time_fps']:>13.1f}{s['e2e_mean']:>10.1f}{s['e2e_p99']:>10.1f}   [{s['dist']}]")

    # ---- plot: mean n_in_batch vs max-latency, one line per series ----
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 5.5))
    fig.suptitle("sync-inputs ON: max-latency sweep (4 live C920 @30fps, one USB-2 bus)",
                 fontsize=13, fontweight="bold")
    for name, _, color in SERIES:
        pts = [(ms, s) for ms, s in data[name] if s]
        if not pts:
            continue
        xs = [ms for ms, _ in pts]
        ax1.plot(xs, [s["mean_nin"] for _, s in pts], "o-", color=color, label=name)
        ax2.plot(xs, [s["e2e_mean"] for _, s in pts], "o-", color=color, label=name)
    ax1.axhline(args.num_cams, ls="--", color="#888", lw=1, label=f"all {args.num_cams} cams (ideal)")
    ax1.axvline(33.3, ls=":", color="#009E73", lw=1, label="1 frame period (33.3 ms)")
    ax1.set_xlabel("max-latency (ms)"); ax1.set_ylabel(f"mean frames in batch (of {args.num_cams})")
    ax1.set_title("does a wider window align more cameras?")
    ax1.set_ylim(0.8, args.num_cams + 0.3); ax1.grid(alpha=0.3); ax1.legend(fontsize=8)
    ax2.axvline(33.3, ls=":", color="#009E73", lw=1)
    ax2.set_xlabel("max-latency (ms)"); ax2.set_ylabel("e2e latency mean (ms)")
    ax2.set_title("the latency cost of waiting"); ax2.grid(alpha=0.3); ax2.legend(fontsize=8)
    fig.tight_layout(rect=[0, 0, 1, 0.95])
    plot_path = os.path.join(outdir, "sync_sweep.png")
    fig.savefig(plot_path, dpi=110)
    print(f"\nsaved plot -> {plot_path}")
    print(f"CSVs in     -> {outdir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
