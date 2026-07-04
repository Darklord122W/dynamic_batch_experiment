#!/usr/bin/env python3
"""analyze.py — aggregate one or more metrics CSVs into a comparison table.

Reads the per-batch CSVs written by MetricsCollector (metrics.py) and reports, per
run, the numbers you actually compare in an evaluation (RT-BEV's metric families):

  * latency   — compute (inference+track) and e2e (source->output) mean / p50 /
                p99 / max. Tails (p99/max) matter for a real-time system.
  * batch     — average batch fullness (the timeout / skip effect).
  * throughput— batches/s and processed frames/s.
  * accuracy  — a tracking-stability PROXY (fewer brand-new track IDs per frame =
                steadier). No webcam ground truth exists; pass --accuracy to plug
                in a real mAP if you replayed a labelled dataset.
  * FES       — RT-BEV's Frame Efficiency Score = processed_frames * accuracy /
                avg_latency. One number combining speed + throughput + accuracy.

A warmup window (default 3s: engine build spillover + pipeline fill) is dropped.

Usage:
    python3 scripts/analyze.py run_a.csv run_b.csv [...] [--warmup 3] [--accuracy 0.37]
"""
from __future__ import annotations

import argparse
import csv
import os
import statistics
from typing import Dict, List, Optional


def _pct(xs: List[float], p: float) -> float:
    """Linear-interpolated p-th percentile of xs (0..100)."""
    if not xs:
        return float("nan")
    xs = sorted(xs)
    k = (len(xs) - 1) * p / 100.0
    lo = int(k)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def analyze(path: str, warmup_s: float = 3.0, accuracy: Optional[float] = None) -> Optional[Dict]:
    """Aggregate one metrics CSV. Returns a dict of stats, or None if empty."""
    with open(path) as f:
        rows = list(csv.DictReader(f))
    rows = [r for r in rows if float(r["t_mono"]) >= warmup_s]
    if not rows:
        return None

    t0, t1 = float(rows[0]["t_mono"]), float(rows[-1]["t_mono"])
    dur = max(t1 - t0, 1e-6)

    compute = [float(r["compute_ms"]) for r in rows if float(r["compute_ms"]) >= 0]
    e2e = [float(r["e2e_ms"]) for r in rows if float(r["e2e_ms"]) >= 0]
    n_in = [int(r["n_in_batch"]) for r in rows]
    # REAL frames processed = the gate's ACTIVE-camera count. This is the reliable
    # measure: it is what the controller commanded and the valves were verified to
    # obey (a dropped camera pushes no frames). num_frames_in_batch is NOT reliable
    # here — the legacy mux repeats a skipped camera's stale frame to keep the batch
    # "full", so it overcounts (those phantom frames produce no detections).
    n_act = [int(r["n_active"]) for r in rows]
    real_frames = sum(n_act)
    frames = sum(n_in)
    dets = sum(int(r["total_dets"]) for r in rows)

    # Per-batch wait = e2e - compute (both from the same batch) -> the batch-wait.
    # Low-noise: the compute noise is in both terms and cancels. This is the metric
    # the timeout directly controls.
    wait = [float(r["e2e_ms"]) - float(r["compute_ms"]) for r in rows
            if float(r["e2e_ms"]) >= 0 and float(r["compute_ms"]) >= 0]

    new_ids = int(rows[-1]["new_ids_cum"]) - int(rows[0]["new_ids_cum"])
    # ROUGH stability proxy in (0, 1]: fewer brand-new track IDs per REAL frame ->
    # closer to 1. CAVEAT: it conflates tracking churn with legitimately-new objects
    # entering the scene, and the x100 scale is arbitrary. It is a relative
    # placeholder for accuracy, NOT a correctness measure — use --accuracy <mAP>
    # with a labelled replay for real accuracy.
    stability = 1.0 / (1.0 + (new_ids / max(1, real_frames)) * 100.0)
    acc = accuracy if accuracy is not None else stability

    real_frames_s = real_frames / dur
    e2e_mean = statistics.mean(e2e) if e2e else float("nan")
    # FES (RT-BEV Eq. 4) adapted: REAL frames/SECOND so runs of different duration
    # are comparable and phantom frames don't inflate it.
    fes = (real_frames_s * acc) / e2e_mean if e2e and e2e_mean > 0 else float("nan")

    return {
        "run": os.path.basename(path),
        "batches": len(rows),
        "dur_s": dur,
        "batches_s": len(rows) / dur,
        "frames_s": real_frames_s,                 # REAL throughput (active cams x rate)
        "reported_frames_s": frames / dur,          # what num_frames_in_batch claims
        "fullness": statistics.mean(n_act),         # REAL active cameras per batch
        "reported_fullness": statistics.mean(n_in),
        "compute_mean": statistics.mean(compute) if compute else float("nan"),
        "compute_p99": _pct(compute, 99),
        "wait_mean": statistics.mean(wait) if wait else float("nan"),
        "wait_p99": _pct(wait, 99),
        "e2e_mean": e2e_mean,
        "e2e_p50": _pct(e2e, 50),
        "e2e_p99": _pct(e2e, 99),
        "e2e_max": max(e2e) if e2e else float("nan"),
        "dets": dets,
        "new_ids": new_ids,
        "stability": stability,
        "accuracy": acc,
        "fes": fes,
    }


def print_table(results: List[Dict]) -> None:
    """Print an aligned comparison table (one column per run)."""
    if not results:
        print("No data.")
        return
    rows = [
        ("batches", "{batches}"),
        ("duration (s)", "{dur_s:.1f}"),
        ("REAL frames/s", "{frames_s:.1f}"),
        ("reported frames/s", "{reported_frames_s:.1f}"),
        ("REAL frames/batch", "{fullness:.2f}"),
        ("reported frames/batch", "{reported_fullness:.2f}"),
        ("compute mean (ms)", "{compute_mean:.2f}"),
        ("wait mean (ms)", "{wait_mean:.2f}"),
        ("e2e mean (ms)", "{e2e_mean:.2f}"),
        ("e2e p50 (ms)", "{e2e_p50:.2f}"),
        ("e2e p99 (ms)", "{e2e_p99:.2f}"),
        ("e2e max (ms)", "{e2e_max:.2f}"),
        ("total detections", "{dets}"),
        ("stability proxy", "{stability:.3f}"),
        ("FES (fps*acc/e2e)", "{fes:.4f}"),
    ]
    label_w = max(len(lbl) for lbl, _ in rows) + 2
    col_w = max(12, max(len(r["run"]) for r in results) + 2)

    header = " " * label_w + "".join(r["run"].rjust(col_w) for r in results)
    print(header)
    print("-" * len(header))
    for lbl, fmt in rows:
        line = lbl.ljust(label_w)
        for r in results:
            line += fmt.format(**r).rjust(col_w)
        print(line)


def main() -> int:
    ap = argparse.ArgumentParser(description="Aggregate/compare metrics CSVs.")
    ap.add_argument("csvs", nargs="+", help="one or more metrics CSV files")
    ap.add_argument("--warmup", type=float, default=3.0, help="seconds to skip at start")
    ap.add_argument(
        "--accuracy", type=float, default=None,
        help="override accuracy for FES (e.g. a measured mAP); default = stability proxy",
    )
    args = ap.parse_args()

    results = []
    for path in args.csvs:
        r = analyze(path, warmup_s=args.warmup, accuracy=args.accuracy)
        if r is None:
            print(f"[analyze] {path}: no rows after warmup — skipped.")
        else:
            results.append(r)
    print_table(results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
