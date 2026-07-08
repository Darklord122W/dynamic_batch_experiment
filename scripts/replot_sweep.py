#!/usr/bin/env python3
"""replot_sweep.py — regenerate a timeout_sweep_cpp.py output directory's
figures from its persisted per-run CSV/JSONL files (no re-running of the
pipeline). Usage: python3 scripts/replot_sweep.py <outdir> [<outdir> ...]"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from timeout_sweep_cpp import (summarize, det_agreement, plot_batching,
                               plot_detection)


def replot(outdir):
    meta = json.load(open(os.path.join(outdir, "run_meta.json")))
    ms_list, num_cams = meta["ms"], meta["num_cams"]
    warmup, ref_ms = meta["warmup"], meta.get("ref_ms", 100)
    tag = meta.get("tag") or ("dynamic batch" if "_pgie_static" not in meta["pgie"]
                              else f"fixed batch-{num_cams}")
    sync = meta.get("sync", False)
    subtitle = (f"{num_cams}-clip skewed replay, sync-inputs "
                f"{'ON (ml ' + format(meta.get('max_latency_ms', 33.333), 'g') + ' ms)' if sync else 'OFF'} "
                f"(stagger {meta['skew_ms']} ms, rate {meta['rate'].split(',')[0]}, "
                f"gaps 1/{meta['gap_every']}, ring {meta['ring']}, "
                f"restamp {'ON' if meta.get('restamp') else 'off'})")
    results = []
    for ms in ms_list:
        base = os.path.join(outdir, f"push_{ms:g}ms")
        results.append((ms, summarize(base + ".csv", base + "_dets.jsonl",
                                      warmup, num_cams)))
    ref = next((s for ms, s in results if s and abs(ms - ref_ms) < 1e-9), None)
    agreements = [det_agreement(s, ref) for _, s in results]
    plot_batching(results, num_cams, os.path.join(outdir, "timeout_sweep.png"),
                  tag, subtitle)
    plot_detection(results, agreements, num_cams,
                   os.path.join(outdir, "detection_perf.png"), tag, subtitle,
                   ref_ms)


if __name__ == "__main__":
    for d in sys.argv[1:]:
        print(f"[replot] {d}")
        replot(d)
