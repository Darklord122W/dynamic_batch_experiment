#!/usr/bin/env python3
"""analyze_timing.py — figures + statistics for a frame_timing_probe run.

Usage:
    python3 analyze_timing.py RUN_DIR [--compare OTHER_RUN_DIR] [--zoom-s N]

Reads RUN_DIR/{capture,premux,batches,batch_frames}.csv + meta.json (written by
./frame_timing_probe) and emits into RUN_DIR/figures/:

    fig01_arrival_raster.png    when each camera's frames arrive (world time)
    fig02_interframe_jitter.png per-camera frame-interval distribution + timeline
    fig03_phase_drift.png       free-running phase of each camera vs cam 0
    fig04_latency.png           capture -> pre-mux latency per camera
    fig05_rtbev.png             RT-BEV Fig.5 replica: max time difference among
                                cameras per sample (true capture clock vs the
                                synthetic PTS nvstreammux actually sees)
    fig06_pairwise_offsets.png  arrival offset of each camera vs camera 0
    fig07_batch_composition.png batch sizes + which cameras made each batch
    fig08_staleness.png         age of each camera's data at batch push
    fig09_spread_cdf.png        CDF of intra-batch capture spread
    fig10_clock_check.png       clock-bridge sanity (dequeue delay, offset drift)
    summary.md                  all headline numbers as tables

With --compare, additionally writes fig11_compare_<name>.png contrasting the two
runs (arrived vs batched per camera, spread CDFs) into RUN_DIR/figures/.

Timestamp semantics (see README.md for the full derivation):
    capture.csv pts_ns  = KERNEL capture stamp (uvcvideo, CLOCK_MONOTONIC,
                          converted by v4l2src to pipeline running time).
                          TRUE capture time:  mono = pts + base_time.
    premux.csv  pts_ns  = SYNTHETIC: jpegparse re-stamps frames onto an ideal
                          1/fps grid. This is what nvstreammux aligns on.
    *.csv mono_ns/real_ns = CLOCK_MONOTONIC / CLOCK_REALTIME sampled in the pad
                          probe = when the buffer physically passed that point.
    world time of a pipeline timestamp T:
                          real = T + base_time_ns + real_minus_mono_ns
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D

# ----------------------------------------------------------------------------
# Style: dataviz reference palette (validated set, fixed slot order).
# Aqua/yellow sit <3:1 on the light surface -> relief rule: every multi-series
# figure carries a legend and/or direct labels; text always in ink, never in
# series color.
# ----------------------------------------------------------------------------
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK2 = "#52514e"
GRID = "#e7e6e2"
CAM_COLORS = ["#2a78d6", "#1baf7a", "#eda100", "#008300"]  # slots 1..4
ACCENT_RED = "#e34948"   # status/serious — reserved for "dropped/violation"

plt.rcParams.update({
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE,
    "savefig.facecolor": SURFACE,
    "text.color": INK, "axes.labelcolor": INK2,
    "xtick.color": INK2, "ytick.color": INK2,
    "axes.edgecolor": GRID, "axes.grid": True,
    "grid.color": GRID, "grid.linewidth": 0.6,
    "axes.spines.top": False, "axes.spines.right": False,
    "font.size": 9, "axes.titlesize": 10, "figure.titlesize": 12,
    "legend.frameon": False,
})

NS = 1e9
MS = 1e6


def cam_label(c):
    return f"cam {c}"


def load_run(run_dir: Path):
    meta = json.loads((run_dir / "meta.json").read_text())
    cap = pd.read_csv(run_dir / "capture.csv")
    pre = pd.read_csv(run_dir / "premux.csv")
    bat = pd.read_csv(run_dir / "batches.csv")
    bfr = pd.read_csv(run_dir / "batch_frames.csv")

    base = meta["base_time_ns"]
    off = meta["real_minus_mono_ns_start"]
    t0 = min(cap.mono_ns.min(), pre.mono_ns.min())  # run-relative zero

    # True capture instant (kernel stamp) mapped to both clocks.
    cap["cap_mono_ns"] = cap.pts_ns + base
    cap["cap_real_ns"] = cap.cap_mono_ns + off
    for df in (cap, pre, bat):
        df["t_s"] = (df.mono_ns - t0) / NS          # probe passage, run-relative
    cap["cap_t_s"] = (cap.cap_mono_ns - t0) / NS    # capture, run-relative

    cams = sorted(cap.cam.unique())

    # --- pair capture (P0) with premux (P1) rows, per camera.
    # Three cases, detected from the data:
    #   1. PTS preserved P0->P1 (replay --no-restamp): exact PTS match.
    #   2. PTS re-stamped onto the ideal 33.33 ms grid (live jpegparse, or
    #      replay restamp emulation): grid pairing — output k of the restamper
    #      corresponds to input k, and its synthetic PTS is
    #      first_capture_pts + k * 33,333,333 ns. Robust even when frames are
    #      dropped BETWEEN the probes (replay --ring): survivors keep their k.
    GRID_NS = 33333333
    pairs = []
    for c in cams:
        a = cap[cap.cam == c].sort_values("mono_ns").reset_index(drop=True)
        b = pre[pre.cam == c].sort_values("mono_ns").reset_index(drop=True)
        exact = b.pts_ns.isin(set(a.pts_ns)).mean() if len(b) else 0.0
        if exact > 0.9:                      # case 1: PTS untouched
            k = a.reset_index().set_index("pts_ns")["index"]
            idx = b.pts_ns.map(k)
        else:                                # case 2: synthetic grid
            idx = ((b.pts_ns - a.pts_ns.iloc[0]) / GRID_NS).round()
        ok = idx.notna() & (idx >= 0) & (idx < len(a))
        if (~ok).sum() > 2:
            print(f"  note: cam{c}: {int((~ok).sum())} premux rows without a "
                  f"capture match (in-flight tail at EOS)")
        bb = b[ok].reset_index(drop=True)
        aa = a.iloc[idx[ok].astype(int).values].reset_index(drop=True)
        dropped = len(a) - len(bb)
        if dropped > 2:
            print(f"  note: cam{c}: {dropped} of {len(a)} paced/captured "
                  f"frames never reached the mux (ring/backpressure drops)")
        pairs.append(pd.DataFrame({
            "cam": c,
            "cap_pts_ns": aa.pts_ns, "syn_pts_ns": bb.pts_ns,
            "cap_mono_ns": aa.cap_mono_ns, "cap_real_ns": aa.cap_real_ns,
            "cap_t_s": aa.cap_t_s,
            "p0_mono_ns": aa.mono_ns, "p1_mono_ns": bb.mono_ns,
            "p1_t_s": bb.t_s, "seq": aa.seq,
        }))
    paired = pd.concat(pairs, ignore_index=True)

    # --- attach true capture times to batched frames via the synthetic PTS
    # (unique & preserved from jpegparse through the mux: buf_pts == P1 pts).
    bfr = bfr.merge(
        paired[["cam", "syn_pts_ns", "cap_mono_ns", "cap_real_ns", "cap_t_s",
                "p1_mono_ns"]],
        left_on=["source_id", "buf_pts_ns"], right_on=["cam", "syn_pts_ns"],
        how="left")
    bfr = bfr.merge(bat[["batch_idx", "mono_ns", "t_s"]], on="batch_idx",
                    suffixes=("", "_push"))
    bfr.rename(columns={"mono_ns": "push_mono_ns", "t_s": "push_t_s"},
               inplace=True)

    span = cap.t_s.max()
    # Steady-state mask: drop pipeline warm-up (decoder/jpegparse burst) and
    # the EOS tail.
    lo, hi = 2.0, span - 0.5
    return dict(meta=meta, cap=cap, pre=pre, bat=bat, bfr=bfr, paired=paired,
                cams=cams, span=span, steady=(lo, hi), t0=t0)


def steady(df, col, run):
    lo, hi = run["steady"]
    return df[(df[col] >= lo) & (df[col] <= hi)]


def legend_cams(ax, cams, **kw):
    handles = [Line2D([], [], color=CAM_COLORS[c], lw=3) for c in cams]
    ax.legend(handles, [cam_label(c) for c in cams], **kw)


# ----------------------------------------------------------------------------
# Figures
# ----------------------------------------------------------------------------
def fig01_arrival_raster(run, out, zoom_s):
    """When frames arrive at the mux door (P1, world clock), per camera."""
    pre, cams = run["pre"], run["cams"]
    lo, hi = run["steady"]
    z0 = lo + (hi - lo) / 2
    fig, axes = plt.subplots(2, 1, figsize=(10, 5.2),
                             gridspec_kw={"height_ratios": [1, 1.4]})

    ax = axes[0]
    ev = [pre[pre.cam == c].t_s.values for c in cams]
    ax.eventplot(ev, colors=[CAM_COLORS[c] for c in cams],
                 lineoffsets=cams, linelengths=0.8, linewidths=0.6)
    ax.set_yticks(cams)
    ax.set_yticklabels([cam_label(c) for c in cams])
    ax.set_xlabel("run time (s)")
    ax.set_title("Every frame arrival at the nvstreammux sink pads (P1) — full run")

    ax = axes[1]
    for c in cams:
        d = pre[(pre.cam == c) & (pre.t_s >= z0) & (pre.t_s <= z0 + zoom_s)]
        ax.eventplot([d.t_s.values], colors=[CAM_COLORS[c]], lineoffsets=[c],
                     linelengths=0.8, linewidths=1.6)
    ax.set_yticks(cams)
    ax.set_yticklabels([cam_label(c) for c in cams])
    ax.set_xlabel("run time (s)")
    ax.set_title(f"{zoom_s:.0f}-second zoom — the cameras are free-running: "
                 "ticks never line up in columns")
    fig.suptitle("Frame arrival timing right before nvstreammux (world clock)")
    fig.tight_layout()
    fig.savefig(out / "fig01_arrival_raster.png", dpi=150)
    plt.close(fig)


def fig02_interframe_jitter(run, out):
    """Capture-clock frame intervals: distribution + evolution."""
    cams = run["cams"]
    fig, axes = plt.subplots(2, 1, figsize=(10, 6),
                             gridspec_kw={"height_ratios": [1, 1]})
    ax = axes[0]
    stats = []
    for c in cams:
        g = steady(run["cap"][run["cap"].cam == c], "cap_t_s", run)
        dt = g.sort_values("cap_mono_ns").cap_mono_ns.diff().dropna() / MS
        ax.hist(dt, bins=np.arange(0, 140, 2), histtype="step", lw=2,
                color=CAM_COLORS[c], label=cam_label(c))
        stats.append((c, dt.median(), dt.quantile(0.99)))
    ax.set_xlabel("interval between consecutive captures (ms)")
    ax.set_ylabel("frames")
    ax.set_title("Inter-frame interval distribution (kernel capture stamps, steady state)")
    ax.legend()
    for c, med, _ in stats:
        ax.axvline(med, color=CAM_COLORS[c], lw=0.8, alpha=0.5)

    ax = axes[1]
    for c in cams:
        g = steady(run["cap"][run["cap"].cam == c], "cap_t_s", run) \
            .sort_values("cap_mono_ns")
        dt = g.cap_mono_ns.diff() / MS
        ax.plot(g.cap_t_s, dt, lw=0.8, color=CAM_COLORS[c],
                label=cam_label(c))
    ax.set_xlabel("run time (s)")
    ax.set_ylabel("interval (ms)")
    ax.set_title("Interval over time — steps = auto-exposure changing the true frame rate")
    ax.legend(ncol=4)
    fig.suptitle("Per-camera capture cadence and jitter")
    fig.tight_layout()
    fig.savefig(out / "fig02_interframe_jitter.png", dpi=150)
    plt.close(fig)


def fig03_phase_drift(run, out):
    """Capture phase of each camera measured against camera 0's median period."""
    cams = run["cams"]
    cap = run["cap"]
    g0 = steady(cap[cap.cam == cams[0]], "cap_t_s", run)
    period_ns = g0.sort_values("cap_mono_ns").cap_mono_ns.diff().median()
    fig, ax = plt.subplots(figsize=(10, 4))
    for c in cams:
        g = steady(cap[cap.cam == c], "cap_t_s", run)
        phase = (g.cap_mono_ns % period_ns) / MS
        ax.plot(g.cap_t_s, phase, ".", ms=2.2, color=CAM_COLORS[c],
                label=cam_label(c))
    ax.set_xlabel("run time (s)")
    ax.set_ylabel(f"capture time mod {period_ns/MS:.1f} ms  (ms)")
    ax.set_title("Capture phase (mod one frame period) per camera — free-running "
                 "cameras: phases drift, nothing anchors them to each other")
    ax.legend(ncol=4, markerscale=4)
    fig.tight_layout()
    fig.savefig(out / "fig03_phase_drift.png", dpi=150)
    plt.close(fig)
    return period_ns


def fig04_latency(run, out):
    """True capture -> mux-door latency (decode+convert+queueing), per camera."""
    cams = run["cams"]
    p = steady(run["paired"], "cap_t_s", run)
    lat = (p.p1_mono_ns - p.cap_mono_ns) / MS
    fig, axes = plt.subplots(1, 2, figsize=(10, 4),
                             gridspec_kw={"width_ratios": [1, 1.6]})
    ax = axes[0]
    data = [lat[p.cam == c] for c in cams]
    bp = ax.boxplot(data, labels=[cam_label(c) for c in cams],
                    showfliers=False, widths=0.5, patch_artist=True)
    for i, c in enumerate(cams):
        bp["boxes"][i].set(facecolor=CAM_COLORS[c], alpha=0.35,
                           edgecolor=CAM_COLORS[c])
        bp["medians"][i].set(color=CAM_COLORS[c], lw=2)
        for part in ("whiskers", "caps"):
            for artist in bp[part][2 * i:2 * i + 2]:
                artist.set(color=CAM_COLORS[c])
        ax.annotate(f"{data[i].median():.0f} ms", (i + 1, data[i].median()),
                    textcoords="offset points", xytext=(0, 6),
                    ha="center", fontsize=8, color=INK)
    ax.set_ylabel("capture → mux-door latency (ms)")
    ax.set_title("Distribution (fliers hidden)")

    ax = axes[1]
    for c in cams:
        d = p[p.cam == c]
        ax.plot(d.cap_t_s, (d.p1_mono_ns - d.cap_mono_ns) / MS, lw=0.8,
                color=CAM_COLORS[c], label=cam_label(c))
    ax.set_xlabel("run time (s)")
    ax.set_ylabel("latency (ms)")
    ax.set_title("Over time")
    ax.legend(ncol=4)
    fig.suptitle("Pre-DeepStream latency: kernel capture stamp → nvstreammux sink pad\n"
                 "(USB dequeue + jpegparse + nvjpegdec + nvvideoconvert + queueing)")
    fig.tight_layout()
    fig.savefig(out / "fig04_latency.png", dpi=150)
    plt.close(fig)
    return lat, p


def batch_spreads(run):
    """Per batch: spread (max-min) of member frames' TRUE capture times, and of
    the synthetic PTS the mux saw. Batches with >=2 cameras only."""
    bfr = run["bfr"].dropna(subset=["cap_mono_ns"])
    rows = []
    for bi, g in bfr.groupby("batch_idx"):
        if g.source_id.nunique() < 2:
            continue
        rows.append({
            "batch_idx": bi,
            "push_t_s": g.push_t_s.iloc[0],
            "n_cams": g.source_id.nunique(),
            "true_spread_ms": (g.cap_mono_ns.max() - g.cap_mono_ns.min()) / MS,
            "syn_spread_ms": (g.buf_pts_ns.max() - g.buf_pts_ns.min()) / MS,
        })
    return pd.DataFrame(rows)


def approx_sync_spreads(run, period_ns):
    """RT-BEV-style 'synchronized sample' grouping, independent of the mux:
    for every reference frame of cam0, take each other camera's nearest capture;
    the sample's time difference is max-min of those true capture stamps."""
    cams = run["cams"]
    cap = run["cap"]
    ref = steady(cap[cap.cam == cams[0]], "cap_t_s", run) \
        .sort_values("cap_mono_ns")
    others = {c: cap[cap.cam == c].sort_values("cap_mono_ns")
              .cap_mono_ns.values for c in cams[1:]}
    diffs = []
    for t in ref.cap_mono_ns.values:
        member = [t]
        ok = True
        for c, arr in others.items():
            i = np.searchsorted(arr, t)
            cand = [arr[j] for j in (i - 1, i) if 0 <= j < len(arr)]
            if not cand:
                ok = False
                break
            member.append(min(cand, key=lambda x: abs(x - t)))
        if ok:
            diffs.append((max(member) - min(member)) / MS)
    return np.array(diffs)


def fig05_rtbev(run, out, period_ns):
    """The RT-BEV Fig. 5 replica + what the mux believes."""
    sp = batch_spreads(run)
    sp = sp[(sp.push_t_s >= run["steady"][0]) & (sp.push_t_s <= run["steady"][1])]
    approx = approx_sync_spreads(run, period_ns)

    # Independent y scales: panel (c) sits orders of magnitude above (a)/(b) —
    # that gap IS the finding, so each panel gets its own readable range.
    fig, axes = plt.subplots(1, 3, figsize=(12, 3.8))
    ax = axes[0]
    ax.plot(np.arange(len(approx)), approx, ".", ms=3, color=CAM_COLORS[0])
    ax.set_xlabel("Sample Index")
    ax.set_ylabel("Time Difference (ms)")
    axes[1].set_ylabel("Time Difference (ms)")
    axes[2].set_ylabel("Time Difference (ms)")
    ax.set_title("(a) RT-BEV Fig. 5 equivalent\nnearest-frame sets, true capture stamps")

    ax = axes[1]
    full = sp[sp.n_cams == len(run["cams"])]
    part = sp[sp.n_cams < len(run["cams"])]
    ax.plot(full.batch_idx, full.true_spread_ms, ".", ms=3,
            color=CAM_COLORS[0], label="full batch")
    if len(part):
        ax.plot(part.batch_idx, part.true_spread_ms, ".", ms=3,
                color=ACCENT_RED, label="partial batch")
    ax.set_xlabel("Batch Index")
    ax.set_title("(b) as actually batched by nvstreammux\ntrue capture stamps")
    ax.legend(markerscale=3, loc="upper right")

    ax = axes[2]
    ax.plot(sp.batch_idx, sp.syn_spread_ms, ".", ms=3, color=CAM_COLORS[1])
    ax.set_xlabel("Batch Index")
    ax.set_title("(c) what the mux believes\nsame batches, jpegparse's synthetic PTS")

    for ax in axes:
        ax.axhline(period_ns / MS, color=INK2, lw=0.8, ls="--")
    axes[0].annotate("one frame period", (0.02, period_ns / MS),
                     xycoords=("axes fraction", "data"),
                     textcoords="offset points", xytext=(0, 4),
                     fontsize=8, color=INK2)
    fig.suptitle("Time Differences Among Cameras — 4× free-running Logitech C920 "
                 "(cf. RT-BEV Fig. 5, synced nuScenes: 39–46 ms)")
    fig.tight_layout()
    fig.savefig(out / "fig05_rtbev.png", dpi=150)
    plt.close(fig)
    return sp, approx


def fig06_pairwise_offsets(run, out, period_ns):
    """Arrival offset of every camera against cam 0, over time + distribution."""
    cams = run["cams"]
    cap = run["cap"]
    ref = steady(cap[cap.cam == cams[0]], "cap_t_s", run) \
        .sort_values("cap_mono_ns")
    fig, axes = plt.subplots(1, 2, figsize=(10, 4),
                             gridspec_kw={"width_ratios": [1.8, 1]})
    ax, axh = axes
    half = period_ns / 2 / MS
    for c in cams[1:]:
        arr = cap[cap.cam == c].sort_values("cap_mono_ns").cap_mono_ns.values
        offs, ts = [], []
        for t, trel in zip(ref.cap_mono_ns.values, ref.cap_t_s.values):
            i = np.searchsorted(arr, t)
            cand = [arr[j] for j in (i - 1, i) if 0 <= j < len(arr)]
            if cand:
                nearest = min(cand, key=lambda x: abs(x - t))
                offs.append((nearest - t) / MS)
                ts.append(trel)
        ax.plot(ts, offs, ".", ms=2, color=CAM_COLORS[c],
                label=f"{cam_label(c)} − cam 0")
        axh.hist(offs, bins=np.linspace(-half, half, 45), histtype="step",
                 lw=2, color=CAM_COLORS[c])
    ax.set_xlabel("run time (s)")
    ax.set_ylabel("nearest-frame capture offset vs cam 0 (ms)")
    ax.set_title("Nearest-frame capture offset vs cam 0 over time\n"
                 "(a slope = clock-rate mismatch; wraps at ±half a period)")
    ax.legend(ncol=3, markerscale=4)
    axh.set_xlabel("offset (ms)")
    axh.set_ylabel("samples")
    axh.set_title("Offset distribution")
    fig.suptitle("Pairwise capture-time offsets between free-running cameras")
    fig.tight_layout()
    fig.savefig(out / "fig06_pairwise_offsets.png", dpi=150)
    plt.close(fig)


def fig07_batch_composition(run, out):
    """What nvstreammux actually assembled."""
    bat, bfr, cams = run["bat"], run["bfr"], run["cams"]
    bs = steady(bat[bat.n_frames >= 0], "t_s", run)
    fig, axes = plt.subplots(2, 1, figsize=(10, 5.6),
                             gridspec_kw={"height_ratios": [1, 1.2]})
    ax = axes[0]
    counts = bs.n_frames.value_counts().sort_index()
    colors = [ACCENT_RED if int(n) < len(cams) else CAM_COLORS[0]
              for n in counts.index]
    bars = ax.bar(counts.index.astype(int), counts.values, color=colors,
                  width=0.6)
    for b, v in zip(bars, counts.values):
        ax.annotate(f"{v}", (b.get_x() + b.get_width() / 2, v),
                    textcoords="offset points", xytext=(0, 3), ha="center",
                    fontsize=8, color=INK)
    ax.set_xticks(range(1, len(cams) + 1))
    ax.set_xlim(0.4, len(cams) + 0.6)
    ax.set_xlabel("frames in batch")
    ax.set_ylabel("batches")
    full = counts.get(len(cams), 0)
    ax.set_title(f"Batch size distribution (steady state) — "
                 f"{100*full/max(counts.sum(),1):.1f}% full batches; "
                 "red = partial (a camera missed the push window)")

    ax = axes[1]
    lo, hi = run["steady"]
    b = bfr[(bfr.push_t_s >= lo) & (bfr.push_t_s <= hi)]
    present = b.groupby(["batch_idx", "source_id"]).size().unstack(
        fill_value=0).reindex(columns=cams, fill_value=0)
    misses = {c: [] for c in cams}
    push_t = b.groupby("batch_idx").push_t_s.first()
    for c in cams:
        miss_idx = present.index[present[c] == 0]
        ax.plot(push_t.loc[present.index[present[c] > 0]],
                [c] * int((present[c] > 0).sum()), "|", ms=6,
                color=CAM_COLORS[c], alpha=0.7)
        ax.plot(push_t.loc[miss_idx], [c] * len(miss_idx), "x", ms=5,
                color=ACCENT_RED)
        misses[c] = len(miss_idx)
    ax.set_yticks(cams)
    ax.set_yticklabels([f"{cam_label(c)}  ({misses[c]} missed)" for c in cams])
    ax.set_xlabel("run time (s)")
    ax.set_title("Batch membership per camera — ✕ marks a batch pushed WITHOUT "
                 "that camera")
    fig.suptitle("Effect of arrival discrepancies on nvstreammux batching")
    fig.tight_layout()
    fig.savefig(out / "fig07_batch_composition.png", dpi=150)
    plt.close(fig)


def fig08_staleness(run, out):
    """Age of each camera's frame at the moment its batch is pushed."""
    cams = run["cams"]
    bfr = run["bfr"].dropna(subset=["cap_mono_ns"])
    lo, hi = run["steady"]
    b = bfr[(bfr.push_t_s >= lo) & (bfr.push_t_s <= hi)]
    fig, ax = plt.subplots(figsize=(7, 4.2))
    meds = []
    for c in cams:
        age = (b[b.source_id == c].push_mono_ns -
               b[b.source_id == c].cap_mono_ns) / MS
        xs = np.sort(age.values)
        ax.plot(xs, np.linspace(0, 1, len(xs)), lw=2, color=CAM_COLORS[c],
                label=f"{cam_label(c)} (median {np.median(xs):.0f} ms)")
        meds.append(np.median(xs))
    ax.set_xlabel("frame age at batch push:  push time − true capture time (ms)")
    ax.set_ylabel("fraction of frames ≤ x")
    ax.set_title("Data staleness entering DeepStream — every ms here is motion "
                 "the detector never sees")
    ax.legend()
    fig.tight_layout()
    fig.savefig(out / "fig08_staleness.png", dpi=150)
    plt.close(fig)


def fig09_spread_cdf(run, out, sp, approx, period_ns):
    fig, ax = plt.subplots(figsize=(7, 4.2))
    for arr, color, label in (
            (approx, CAM_COLORS[0], "nearest-frame sets (Fig.5a)"),
            (sp.true_spread_ms.values, CAM_COLORS[2],
             "as batched by the mux (Fig.5b)"),
            (sp.syn_spread_ms.values, CAM_COLORS[1],
             "synthetic PTS, as the mux believes (Fig.5c)")):
        xs = np.sort(arr)
        if len(xs):
            ax.plot(xs, np.linspace(0, 1, len(xs)), lw=2, color=color,
                    label=f"{label} — p50 {np.median(xs):.1f} ms")
    ax.axvline(period_ns / MS, color=INK2, lw=0.8, ls="--")
    ax.annotate("one frame period", (period_ns / MS, 0.03),
                textcoords="offset points", xytext=(4, 0), fontsize=8,
                color=INK2)
    ax.set_xlabel("max capture-time difference among cameras in a sample (ms)")
    ax.set_ylabel("fraction of samples ≤ x")
    ax.set_title("Intra-batch time spread — cumulative view")
    ax.legend()
    fig.tight_layout()
    fig.savefig(out / "fig09_spread_cdf.png", dpi=150)
    plt.close(fig)


def fig10_clock_check(run, out):
    """Sanity of the measurement itself."""
    cams, meta = run["cams"], run["meta"]
    cap = run["cap"]
    fig, axes = plt.subplots(1, 2, figsize=(10, 4),
                             gridspec_kw={"width_ratios": [1.8, 1]})
    ax = axes[0]
    for c in cams:
        g = steady(cap[cap.cam == c], "cap_t_s", run)
        d = (g.mono_ns - g.cap_mono_ns) / MS
        ax.plot(g.cap_t_s, d, lw=0.8, color=CAM_COLORS[c], label=cam_label(c))
    ax.set_xlabel("run time (s)")
    ax.set_ylabel("P0 probe time − kernel capture stamp (ms)")
    ax.set_title("v4l2 dequeue delay (kernel stamp → userspace probe).\n"
                 "Flat & small = kernel timestamps are trustworthy")
    ax.legend(ncol=4)

    ax = axes[1]
    drift = (meta["real_minus_mono_ns_end"] -
             meta["real_minus_mono_ns_start"])
    ax.bar([0, 1],
           [meta["real_minus_mono_bracket_ns_start"] / 1e3,
            meta["real_minus_mono_bracket_ns_end"] / 1e3],
           color=[CAM_COLORS[0]] * 2, width=0.5)
    ax.set_xticks([0, 1])
    ax.set_xticklabels(["start", "end"])
    ax.set_ylabel("offset measurement bracket (µs)")
    ax.set_title(f"REALTIME−MONOTONIC bridge\ndrift over run: {drift/1e3:.1f} µs")
    fig.suptitle("Measurement validity checks")
    fig.tight_layout()
    fig.savefig(out / "fig10_clock_check.png", dpi=150)
    plt.close(fig)


def fig11_compare(run_a, name_a, run_b, name_b, out, period_ns):
    """A/B: arrived vs batched per camera + spread CDFs."""
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.2))
    ax = axes[0]
    cams = run_a["cams"]
    width = 0.2
    xs = np.arange(len(cams))
    for k, (run, name, hatch) in enumerate(
            ((run_a, name_a, None), (run_b, name_b, "//"))):
        arrived, batched = [], []
        lo, hi = run["steady"]
        for c in cams:
            arrived.append(len(steady(run["pre"][run["pre"].cam == c],
                                      "t_s", run)))
            b = run["bfr"]
            b = b[(b.push_t_s >= lo) & (b.push_t_s <= hi)]
            batched.append((b.source_id == c).sum())
        span = hi - lo
        ax.bar(xs + (2 * k - 1) * width - width / 2, np.array(arrived) / span,
               width, color=[CAM_COLORS[c] for c in cams], alpha=0.35,
               hatch=hatch, edgecolor=INK2, lw=0.4)
        ax.bar(xs + (2 * k - 1) * width + width / 2, np.array(batched) / span,
               width, color=[CAM_COLORS[c] for c in cams], hatch=hatch,
               edgecolor=INK2, lw=0.4)
    ax.set_xticks(xs)
    ax.set_xticklabels([cam_label(c) for c in cams])
    ax.set_ylabel("frames / s")
    ax.set_title("pale = arrived at mux, solid = made it into a batch\n"
                 f"plain = {name_a}, hatched = {name_b}")

    ax = axes[1]
    for run, name, color in ((run_a, name_a, CAM_COLORS[0]),
                             (run_b, name_b, CAM_COLORS[3])):
        sp = batch_spreads(run)
        sp = sp[(sp.push_t_s >= run["steady"][0]) &
                (sp.push_t_s <= run["steady"][1])]
        xs2 = np.sort(sp.true_spread_ms.values)
        if len(xs2):
            ax.plot(xs2, np.linspace(0, 1, len(xs2)), lw=2, color=color,
                    label=f"{name} — p50 {np.median(xs2):.1f} ms, "
                          f"n={len(xs2)}")
    ax.axvline(period_ns / MS, color=INK2, lw=0.8, ls="--")
    ax.set_xlabel("true capture spread inside a batch (ms)")
    ax.set_ylabel("fraction ≤ x")
    ax.set_title("True capture spread inside a batch")
    ax.legend()
    fig.suptitle(f"Run comparison: {name_a} vs {name_b}")
    fig.tight_layout()
    fig.savefig(out / f"fig11_compare_{name_b}.png", dpi=150)
    plt.close(fig)


# ----------------------------------------------------------------------------
# summary.md
# ----------------------------------------------------------------------------
def write_summary(run, out, period_ns, lat, paired_steady, sp, approx):
    cams, meta = run["cams"], run["meta"]
    lines = ["# frame_timing_probe — run summary", "",
             f"- devices: {meta['devices']}",
             f"- mode: {meta['width']}x{meta['height']}@{meta['fps']} MJPG → "
             f"{meta['decoder']}; sync-inputs="
             f"{'ON' if meta['sync_inputs'] else 'OFF'}; "
             f"batched-push-timeout={meta['batched_push_timeout_us']} µs"
             + (f"; max-latency={meta['max_latency_ns']/1e6:.0f} ms"
                if meta['sync_inputs'] else ""),
             f"- extra v4l2 controls: "
             f"{meta.get('extra_controls','') or '(none)'}",
             f"- duration: {meta['duration_s']} s "
             f"(steady-state window {run['steady'][0]:.1f}–"
             f"{run['steady'][1]:.1f} s used for all stats)",
             f"- pipeline clock: {meta['pipeline_clock_type']} "
             f"(monotonic); base_time={meta['base_time_ns']} ns",
             f"- REALTIME−MONOTONIC offset drift over the run: "
             f"{(meta['real_minus_mono_ns_end']-meta['real_minus_mono_ns_start'])/1e3:.1f} µs",
             "", "## Per-camera capture behaviour (kernel stamps)", "",
             "| cam | frames | eff. fps | Δt p50 (ms) | Δt p99 (ms) | "
             "kernel seq gaps | capture→mux p50 (ms) | p99 (ms) |",
             "|---|---|---|---|---|---|---|---|"]
    for c in cams:
        g = steady(run["cap"][run["cap"].cam == c], "cap_t_s", run) \
            .sort_values("cap_mono_ns")
        dt = g.cap_mono_ns.diff().dropna() / MS
        gaps = (g.seq.diff().dropna() > 1).sum()
        span = (g.cap_mono_ns.max() - g.cap_mono_ns.min()) / NS
        pl = lat[paired_steady.cam == c]
        lines.append(f"| {c} | {len(g)} | {len(g)/span:.1f} | "
                     f"{dt.median():.1f} | {dt.quantile(.99):.1f} | {gaps} | "
                     f"{pl.median():.1f} | {pl.quantile(.99):.1f} |")

    bs = steady(run["bat"][run["bat"].n_frames >= 0], "t_s", run)
    full = (bs.n_frames == len(cams)).sum()
    lines += ["", "## Batching", "",
              f"- batches (steady state): {len(bs)}; full: {full} "
              f"({100*full/max(len(bs),1):.1f} %); "
              f"partial: {len(bs)-full}",
              f"- batch cadence: median "
              f"{bs.sort_values('mono_ns').mono_ns.diff().median()/MS:.1f} ms",
              "", "## Time differences among cameras (the RT-BEV Fig. 5 numbers)", "",
              "| sample definition | n | p50 (ms) | p90 (ms) | p99 (ms) | max (ms) |",
              "|---|---|---|---|---|---|"]
    for name, arr in (("nearest-frame sets (true capture)", approx),
                      ("mux batches (true capture)", sp.true_spread_ms.values),
                      ("mux batches (synthetic PTS)", sp.syn_spread_ms.values)):
        if len(arr):
            lines.append(f"| {name} | {len(arr)} | {np.median(arr):.1f} | "
                         f"{np.percentile(arr,90):.1f} | "
                         f"{np.percentile(arr,99):.1f} | {arr.max():.1f} |")
    lines += ["", f"(one frame period = {period_ns/MS:.1f} ms; RT-BEV reports "
              "39–46 ms on hardware-synced nuScenes cameras)", ""]
    (out / "summary.md").write_text("\n".join(lines))
    print("\n".join(lines))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir", type=Path)
    ap.add_argument("--compare", type=Path, default=None,
                    help="second run dir (e.g. sync-on) to contrast")
    ap.add_argument("--zoom-s", type=float, default=3.0)
    args = ap.parse_args()

    run = load_run(args.run_dir)
    out = args.run_dir / "figures"
    out.mkdir(exist_ok=True)

    print(f"[analyze] {args.run_dir}: {len(run['cap'])} captures, "
          f"{len(run['bat'])} batches, span {run['span']:.1f}s")
    fig01_arrival_raster(run, out, args.zoom_s)
    fig02_interframe_jitter(run, out)
    period_ns = fig03_phase_drift(run, out)
    lat, paired_steady = fig04_latency(run, out)
    sp, approx = fig05_rtbev(run, out, period_ns)
    fig06_pairwise_offsets(run, out, period_ns)
    fig07_batch_composition(run, out)
    fig08_staleness(run, out)
    fig09_spread_cdf(run, out, sp, approx, period_ns)
    fig10_clock_check(run, out)
    write_summary(run, out, period_ns, lat, paired_steady, sp, approx)

    if args.compare is not None:
        run_b = load_run(args.compare)
        fig11_compare(run, args.run_dir.name, run_b, args.compare.name, out,
                      period_ns)
        print(f"[analyze] comparison figure vs {args.compare.name} written.")
    print(f"[analyze] figures -> {out}")


if __name__ == "__main__":
    sys.exit(main())
