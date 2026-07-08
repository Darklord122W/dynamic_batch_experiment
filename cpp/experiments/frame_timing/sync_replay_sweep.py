#!/usr/bin/env python3
"""sync_replay_sweep.py — CAN sync-inputs=1 fill the batch once jpegparse's
timestamp destruction is fixed?

Reproduces the project's sync_sweep (1-D max-latency scan) and sync_grid
(max-latency x push-timeout) on the SKEWED REPLAY instead of live cameras,
in both timestamp worlds:

  restamp ON  (broken)  the mux sees jpegparse's synthetic per-camera grids,
                        offset by the startup stagger (~0.6-1.8 s). The live
                        sweeps' conclusion: no affordable window can bridge
                        that, sync erases 90+% of frames, fill caps at ~2.
  restamp OFF (fixed)   the mux sees TRUE capture-timeline timestamps — what
                        the production app now delivers with the jpegparse
                        PTS-restore fix. Required window = the real physical
                        skew (p50 ~9 ms, p99 ~45 ms), three orders of
                        magnitude smaller.

Each cell = one frame_timing_probe replay run (fakesink, no inference — this
isolates mux behaviour). Outputs summary.csv, summary.md and two figures into
--out.

Usage (skew/rate/gap measured from the live baseline run being simulated):
  python3 sync_replay_sweep.py --skew-ms 568,0,1217,1137 \
      --rate 0.9608,0.9608,0.9608,0.9608 --gap-every 70 --ring 4 \
      --out results/sync_replay_sweep
  # add --grid for the 2-D max-latency x push-timeout map (fixed world only)
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

HERE = Path(__file__).resolve().parent

SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
BLUE = "#2a78d6"
RED = "#e34948"
GREEN = "#009E73"

plt.rcParams.update({
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE,
    "savefig.facecolor": SURFACE, "text.color": INK,
    "axes.labelcolor": INK_2, "xtick.color": INK_2, "ytick.color": INK_2,
    "axes.edgecolor": GRID, "axes.grid": True, "grid.color": GRID,
    "grid.linewidth": 0.6, "axes.spines.top": False,
    "axes.spines.right": False, "font.size": 9, "axes.titlesize": 10,
    "figure.titlesize": 12, "legend.frameon": False,
})


def run_cell(args, cell_dir: Path, ml_ms: float, timeout_us: int,
             restamp: bool) -> bool:
    if (cell_dir / "batches.csv").exists() and not args.force:
        return True  # cached from a previous invocation
    cmd = [
        str(HERE / "frame_timing_probe"),
        "--replay-dir", args.replay_dir, "--num-cams", str(args.num_cams),
        "--duration", str(args.duration),
        "--skew-ms", args.skew_ms, "--rate", args.rate,
        "--gap-every", str(args.gap_every), "--ring", str(args.ring),
        "--sync", "--max-latency-ms", f"{ml_ms}",
        "--timeout-us", str(timeout_us),
        "--out-dir", str(cell_dir),
    ]
    if not restamp:
        cmd.append("--no-restamp")
    r = subprocess.run(cmd, cwd=HERE, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=False)
    return r.returncode == 0 and (cell_dir / "batches.csv").exists()


def analyze_cell(cell_dir: Path, num_cams: int):
    """Steady-state fill / discard / cadence for one run directory."""
    try:
        bat = pd.read_csv(cell_dir / "batches.csv")
        pre = pd.read_csv(cell_dir / "premux.csv")
        bfr = pd.read_csv(cell_dir / "batch_frames.csv")
        cap = pd.read_csv(cell_dir / "capture.csv")
    except Exception:
        return None
    if len(cap) == 0:
        return None
    t0 = min(cap.mono_ns.min(), pre.mono_ns.min()) if len(pre) else cap.mono_ns.min()
    span = (cap.mono_ns.max() - t0) / 1e9
    lo, hi = 2.0, span - 0.5
    bat_w = bat[((bat.mono_ns - t0) / 1e9 >= lo) &
                ((bat.mono_ns - t0) / 1e9 <= hi) & (bat.n_frames >= 0)]
    pre_w = pre[((pre.mono_ns - t0) / 1e9 >= lo) &
                ((pre.mono_ns - t0) / 1e9 <= hi)]
    bfr_w = bfr[bfr.batch_idx.isin(set(bat_w.batch_idx))]
    if len(bat_w) == 0:
        return dict(n_batches=0, mean_fill=0.0, frac_full=0.0, arrivals=len(pre_w),
                    batched=0, discard=1.0, cadence_ms=float("nan"),
                    per_cam_batched=[0] * num_cams)
    per_cam = [int((bfr_w.source_id == c).sum()) for c in range(num_cams)]
    return dict(
        n_batches=int(len(bat_w)),
        mean_fill=float(bat_w.n_frames.mean()),
        frac_full=float((bat_w.n_frames == num_cams).mean()),
        arrivals=int(len(pre_w)),
        batched=int(len(bfr_w)),
        discard=float(1.0 - len(bfr_w) / max(len(pre_w), 1)),
        cadence_ms=float(bat_w.sort_values("mono_ns").mono_ns.diff().median() / 1e6),
        per_cam_batched=per_cam,
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skew-ms", required=True)
    ap.add_argument("--rate", required=True)
    ap.add_argument("--gap-every", type=int, default=70)
    ap.add_argument("--ring", type=int, default=4)
    ap.add_argument("--num-cams", type=int, default=4)
    ap.add_argument("--replay-dir", default="clips")
    ap.add_argument("--duration", type=float, default=32.0)
    ap.add_argument("--ml", type=float, nargs="+",
                    default=[2, 5, 8.33, 16, 33.333, 66.7, 133, 2000],
                    help="max-latency values (ms) for the 1-D scan")
    ap.add_argument("--timeout-us", type=int, default=33333,
                    help="push timeout for the 1-D scan (new-mux EARLY gate)")
    ap.add_argument("--grid", action="store_true",
                    help="also run the 2-D max-latency x push-timeout grid "
                         "(fixed world only)")
    ap.add_argument("--grid-ml", type=float, nargs="+",
                    default=[16, 33.333, 66.7])
    ap.add_argument("--grid-timeout-us", type=int, nargs="+",
                    default=[8333, 16667, 33333, 66667])
    ap.add_argument("--out", default="results/sync_replay_sweep")
    ap.add_argument("--force", action="store_true",
                    help="re-run cells even if their CSVs already exist")
    args = ap.parse_args()

    out = HERE / args.out
    out.mkdir(parents=True, exist_ok=True)
    (out / "params.json").write_text(json.dumps(vars(args), indent=2))

    rows = []
    # ---- 1-D scan, both worlds -------------------------------------------
    for restamp in (True, False):
        world = "broken" if restamp else "fixed"
        for ml in args.ml:
            cell = out / f"scan_{world}_ml{ml:g}"
            print(f"[cell] {world} world, max-latency {ml:g} ms, "
                  f"timeout {args.timeout_us} us ...", flush=True)
            ok = run_cell(args, cell, ml, args.timeout_us, restamp)
            a = analyze_cell(cell, args.num_cams) if ok else None
            if a is None:
                print("        FAILED")
                continue
            print(f"        fill {a['mean_fill']:.2f}  full {100*a['frac_full']:.1f}%  "
                  f"discard {100*a['discard']:.1f}%  batches {a['n_batches']}")
            rows.append(dict(kind="scan", world=world, ml_ms=ml,
                             timeout_us=args.timeout_us, **a))
    # ---- 2-D grid, fixed world -------------------------------------------
    if args.grid:
        for ml in args.grid_ml:
            for to in args.grid_timeout_us:
                cell = out / f"grid_fixed_ml{ml:g}_to{to}"
                print(f"[cell] grid fixed, ml {ml:g} ms x timeout {to} us ...",
                      flush=True)
                ok = run_cell(args, cell, ml, to, restamp=False)
                a = analyze_cell(cell, args.num_cams) if ok else None
                if a is None:
                    print("        FAILED")
                    continue
                print(f"        fill {a['mean_fill']:.2f}  "
                      f"full {100*a['frac_full']:.1f}%  "
                      f"discard {100*a['discard']:.1f}%")
                rows.append(dict(kind="grid", world="fixed", ml_ms=ml,
                                 timeout_us=to, **a))

    df = pd.DataFrame(rows)
    df["per_cam_batched"] = df["per_cam_batched"].apply(
        lambda v: " ".join(map(str, v)))
    df.to_csv(out / "summary.csv", index=False)

    # ---- figure 1: fill + discard vs window, broken vs fixed --------------
    scan = df[df.kind == "scan"]
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.2))
    ax = axes[0]
    for world, color, label in (("broken", RED, "restamp ON — synthetic grids "
                                 "(unfixed jpegparse)"),
                                ("fixed", BLUE, "restamp OFF — true timestamps "
                                 "(PTS fix)")):
        g = scan[scan.world == world].sort_values("ml_ms")
        ax.plot(g.ml_ms, g.mean_fill, "o-", color=color, lw=2, label=label)
    ax.axhline(args.num_cams, ls="--", color=MUTED, lw=1)
    ax.annotate("full batch", (0.02, args.num_cams),
                xycoords=("axes fraction", "data"), xytext=(0, -12),
                textcoords="offset points", fontsize=8, color=INK_2)
    ax.set_xscale("log")
    ax.set_xlabel("nvstreammux max-latency (ms, log)")
    ax.set_ylabel(f"mean frames per batch (of {args.num_cams})")
    ax.set_ylim(0, args.num_cams + 0.4)
    ax.set_title("sync-on batch fill vs alignment window")
    ax.legend(fontsize=8, loc="lower right")

    ax = axes[1]
    for world, color in (("broken", RED), ("fixed", BLUE)):
        g = scan[scan.world == world].sort_values("ml_ms")
        ax.plot(g.ml_ms, 100 * g.discard, "o-", color=color, lw=2)
    ax.set_xscale("log")
    ax.set_xlabel("nvstreammux max-latency (ms, log)")
    ax.set_ylabel("% of arrived frames silently discarded")
    ax.set_ylim(-2, 102)
    ax.set_title("the price: sync-inputs discards")
    fig.suptitle("sync-inputs=1 on skewed replay — before vs after the "
                 "jpegparse PTS fix "
                 f"(stagger {args.skew_ms} ms, push-timeout "
                 f"{args.timeout_us} µs)")
    fig.tight_layout()
    fig.savefig(out / "fig_sync_fill.png", dpi=150)
    plt.close(fig)

    # ---- figure 2: the 2-D grid heat map (fixed world) --------------------
    # Mean fill saturates at 4.00 in every fixed-world cell, so a fill heat
    # map carries no information. The informative variable is the DISCARD
    # rate: it forms a gradient along max-latency and is flat along the
    # push-timeout axis — that flatness is itself the finding (the
    # batched-push-timeout property is inert on the new mux).
    grid = df[df.kind == "grid"]
    if len(grid):
        piv = grid.pivot_table(index="ml_ms", columns="timeout_us",
                               values="discard").sort_index(ascending=False)
        fill = grid.pivot_table(index="ml_ms", columns="timeout_us",
                                values="mean_fill").sort_index(ascending=False)
        fig, ax = plt.subplots(figsize=(7.2, 4.6))
        im = ax.imshow(100 * piv.values, cmap="Reds", vmin=0,
                       vmax=max(1.0, 100 * piv.values.max()), aspect="auto")
        ax.set_xticks(range(len(piv.columns)))
        ax.set_xticklabels([f"{c/1000:g}" for c in piv.columns])
        ax.set_yticks(range(len(piv.index)))
        ax.set_yticklabels([f"{i:g}" for i in piv.index])
        ax.set_xlabel("batched-push-timeout property (ms) — INERT on the new "
                      "mux:\nevery column is identical")
        ax.set_ylabel("max-latency (ms) — the knob that acts")
        for yi, ml in enumerate(piv.index):
            for xi, to in enumerate(piv.columns):
                v = 100 * piv.values[yi, xi]
                f = fill.values[yi, xi]
                if np.isfinite(v):
                    ax.text(xi, yi,
                            f"{v:.1f}% lost\nfill {f:.2f}",
                            ha="center", va="center", fontsize=9,
                            color=SURFACE if v > 0.6 * 100 * piv.values.max()
                            else INK)
        ax.set_title("sync-on with TRUE timestamps: % of frames discarded\n"
                     "(mean fill = 4.00 in every cell; rows differ, columns "
                     "don't — max-latency\nis the only live knob; cf. the "
                     "pre-fix live sync_grid: best cell 2.00)")
        fig.colorbar(im, ax=ax, label="% of arrived frames discarded")
        fig.tight_layout()
        fig.savefig(out / "fig_sync_grid.png", dpi=150)
        plt.close(fig)

    # ---- summary.md --------------------------------------------------------
    lines = ["# sync_replay_sweep — sync-inputs on skewed replay, broken vs "
             "fixed timestamps", "",
             f"- injected stagger (ms): {args.skew_ms}; rate {args.rate}; "
             f"gaps 1 per {args.gap_every}; ring {args.ring}",
             f"- {args.duration:.0f} s per cell, steady state = 2 s .. end-0.5 s",
             f"- 1-D scan push-timeout: {args.timeout_us} µs", "",
             "| world | max-latency (ms) | timeout (µs) | batches | mean fill "
             "| % full | % discarded | cadence (ms) | per-cam batched |",
             "|---|---|---|---|---|---|---|---|---|"]
    for _, r in df.sort_values(["kind", "world", "ml_ms", "timeout_us"]).iterrows():
        lines.append(
            f"| {r.world} | {r.ml_ms:g} | {r.timeout_us} | {r.n_batches} | "
            f"{r.mean_fill:.2f} | {100*r.frac_full:.1f} | {100*r.discard:.1f} "
            f"| {r.cadence_ms:.1f} | {r.per_cam_batched} |")
    (out / "summary.md").write_text("\n".join(lines) + "\n")
    print(f"\nwrote {out}/summary.csv, summary.md, fig_sync_fill.png"
          + (", fig_sync_grid.png" if len(grid) else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
