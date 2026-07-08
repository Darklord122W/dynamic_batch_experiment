#!/usr/bin/env python3
"""Baseline C++ (new mux) vs Python (legacy mux) — comparison plots.

Reads the 6 metrics CSVs in experiments/results/baseline_cpp_vs_py/ and writes
PNG figures + a summary JSON. Styling follows the dataviz reference palette.
"""
import csv, json, math, os
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

ROOT = "/home/darklord01/Documents/deepstream_batch/multicam_perception_rt"
DIR = os.path.join(ROOT, "experiments/results/baseline_cpp_vs_py")
OUT = os.path.join(DIR, "plots")
os.makedirs(OUT, exist_ok=True)
WARMUP = 4.0

# ---- dataviz reference palette (light mode) ----
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
SECONDARY = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
BASELINE = "#c3c2b7"
C_CPP = "#2a78d6"   # categorical slot 1 (blue)
C_PY = "#1baf7a"    # categorical slot 2 (aqua)
# lighter steps of each hue for the stacked "wait" segment (same-ramp, lighter)
C_CPP_LIGHT = "#9ec5f4"   # blue 200
C_PY_LIGHT = "#a7e6cd"    # aqua light step

plt.rcParams.update({
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE,
    "savefig.facecolor": SURFACE, "font.family": "sans-serif",
    "font.sans-serif": ["DejaVu Sans"], "text.color": INK,
    "axes.edgecolor": BASELINE, "axes.labelcolor": SECONDARY,
    "xtick.color": MUTED, "ytick.color": MUTED,
    "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.8,
    "grid.linestyle": "-", "axes.linewidth": 0.8,
    "axes.spines.top": False, "axes.spines.right": False,
    "axes.titlesize": 11, "axes.titlecolor": INK,
    "axes.labelsize": 9, "xtick.labelsize": 8.5, "ytick.labelsize": 8.5,
    "legend.frameon": False, "legend.fontsize": 9,
})

def load(path):
    with open(path) as f:
        rows = list(csv.DictReader(f))
    out = {}
    for k in ("t_mono", "compute_ms", "e2e_ms"):
        out[k] = np.array([float(r[k]) for r in rows])
    for k in ("n_in_batch", "n_active", "total_dets", "new_ids_cum",
              "dets_cam0", "dets_cam1", "dets_cam2", "dets_cam3"):
        out[k] = np.array([int(r[k]) for r in rows])
    m = out["t_mono"] >= WARMUP
    return {k: v[m] for k, v in out.items()}

runs = {}
for app, tag in (("cpp", "cpp_baseline"), ("py", "py_baseline")):
    for i in (1, 2, 3):
        runs[(app, i)] = load(os.path.join(DIR, f"{tag}_r{i}.csv"))

APPS = [("cpp", "C++ (new mux)", C_CPP, C_CPP_LIGHT),
        ("py", "Python (legacy mux)", C_PY, C_PY_LIGHT)]

def pooled(app, key):
    return np.concatenate([runs[(app, i)][key] for i in (1, 2, 3)])

def pct(x, p):
    return float(np.percentile(x, p))

# ---- summary stats ----
summary = {}
for app, label, _, _ in APPS:
    e2e = pooled(app, "e2e_ms"); comp = pooled(app, "compute_ms")
    wait = e2e - comp
    per_rep = []
    for i in (1, 2, 3):
        r = runs[(app, i)]
        dur = r["t_mono"][-1] - r["t_mono"][0]
        per_rep.append({
            "batches": len(r["t_mono"]), "dur": dur,
            "batches_s": len(r["t_mono"]) / dur,
            "frames_s": int(r["n_in_batch"].sum()) / dur,
            "fullness": float(r["n_in_batch"].mean()),
            "compute_mean": float(r["compute_ms"].mean()),
            "wait_mean": float((r["e2e_ms"] - r["compute_ms"]).mean()),
            "e2e_mean": float(r["e2e_ms"].mean()),
            "e2e_p99": pct(r["e2e_ms"], 99),
            "dets": int(r["total_dets"].sum()),
            "new_ids": int(r["new_ids_cum"][-1] - r["new_ids_cum"][0]),
        })
    summary[app] = {
        "label": label, "per_rep": per_rep,
        "pooled": {
            "compute": {"mean": float(comp.mean()), "p50": pct(comp, 50), "p99": pct(comp, 99), "max": float(comp.max())},
            "wait": {"mean": float(wait.mean()), "p50": pct(wait, 50), "p99": pct(wait, 99), "max": float(wait.max())},
            "e2e": {"mean": float(e2e.mean()), "p50": pct(e2e, 50), "p99": pct(e2e, 99), "max": float(e2e.max())},
        },
    }
with open(os.path.join(OUT, "summary.json"), "w") as f:
    json.dump(summary, f, indent=2)

DPI = 160

# =====================================================================
# Fig 1 — e2e latency percentiles (pooled), grouped horizontal bars
# =====================================================================
fig, ax = plt.subplots(figsize=(7.2, 3.4), dpi=DPI)
metrics = [("mean", "mean"), ("p50", "p50"), ("p99", "p99"), ("max", "max")]
ys = np.arange(len(metrics))[::-1]
h = 0.32
for j, (app, label, col, _) in enumerate(APPS):
    vals = [summary[app]["pooled"]["e2e"][k] for k, _ in metrics]
    off = (j - 0.5) * (h + 0.06)
    bars = ax.barh(ys - off, vals, height=h, color=col, zorder=3)
    for y, v in zip(ys - off, vals):
        ax.text(v + 4, y, f"{v:.0f}", va="center", ha="left",
                fontsize=8.5, color=SECONDARY)
ax.set_yticks(ys); ax.set_yticklabels([lbl for _, lbl in metrics])
ax.set_xlabel("end-to-end latency (ms) — source arrival → tracker output")
ax.set_title("End-to-end latency, pooled over 3×25 s replay runs (warmup 4 s dropped)")
ax.legend(handles=[plt.Rectangle((0, 0), 1, 1, color=c) for _, _, c, _ in APPS],
          labels=[l for _, l, _, _ in APPS], loc="upper right")
ax.set_xlim(0, max(summary[a]["pooled"]["e2e"]["max"] for a, *_ in APPS) * 1.14)
ax.grid(axis="y", visible=False)
fig.tight_layout(); fig.savefig(os.path.join(OUT, "fig1_e2e_percentiles.png")); plt.close(fig)

# =====================================================================
# Fig 2 — e2e CDF, pooled
# =====================================================================
fig, ax = plt.subplots(figsize=(7.2, 3.6), dpi=DPI)
for app, label, col, _ in APPS:
    e2e = np.sort(pooled(app, "e2e_ms"))
    p = np.arange(1, len(e2e) + 1) / len(e2e)
    ax.plot(e2e, p * 100, color=col, lw=2, solid_capstyle="round", label=label, zorder=3)
    ax.annotate(label, xy=(e2e[int(0.55 * len(e2e))], 57 if app == "cpp" else 45),
                color=INK, fontsize=9,
                xytext=(8, 12) if app == "cpp" else (14, -22), textcoords="offset points")
for q in (50, 99):
    ax.axhline(q, color=GRID, lw=0.8, zorder=1)
    ax.text(ax.get_xlim()[0], q + 1, f"p{q}", color=MUTED, fontsize=8)
ax.set_xlabel("end-to-end latency (ms)"); ax.set_ylabel("% of batches ≤ x")
ax.set_ylim(0, 102)
ax.set_title("Latency CDF — all post-warmup batches, 3 repeats pooled")
fig.tight_layout(); fig.savefig(os.path.join(OUT, "fig2_e2e_cdf.png")); plt.close(fig)

# =====================================================================
# Fig 3 — time series: e2e + compute per repeat (the stability story)
# =====================================================================
fig, axes = plt.subplots(2, 1, figsize=(7.6, 5.4), dpi=DPI, sharex=True)
for ax, key, ttl in ((axes[0], "e2e_ms", "end-to-end latency (ms)"),
                     (axes[1], "compute_ms", "compute latency (ms) — mux → tracker")):
    for app, label, col, _ in APPS:
        for i in (1, 2, 3):
            r = runs[(app, i)]
            x, y = r["t_mono"], r[key]
            k = 15  # rolling median
            med = np.array([np.median(y[max(0, j - k):j + 1]) for j in range(len(y))])
            ax.plot(x, y, color=col, lw=0.6, alpha=0.18, zorder=2)
            ax.plot(x, med, color=col, lw=1.6, alpha=0.95 if i == 1 else 0.55,
                    solid_capstyle="round", zorder=3)
    ax.set_ylabel(ttl, fontsize=9)
axes[0].set_title("Per-batch latency over the run — 3 repeats per app (rolling median over 15 batches)")
axes[1].set_xlabel("time since pipeline start (s)")
axes[0].legend(handles=[plt.Line2D([], [], color=c, lw=2) for _, _, c, _ in APPS],
               labels=[l for _, l, _, _ in APPS], loc="upper right", ncol=2)
fig.tight_layout(); fig.savefig(os.path.join(OUT, "fig3_timeseries.png")); plt.close(fig)

# =====================================================================
# Fig 4 — e2e decomposition per repeat: wait (light) + compute (full hue)
# =====================================================================
fig, ax = plt.subplots(figsize=(7.2, 3.6), dpi=DPI)
labels, waits, comps, cols_w, cols_c = [], [], [], [], []
for app, label, col, col_l in APPS:
    for i in (1, 2, 3):
        pr = summary[app]["per_rep"][i - 1]
        labels.append(f"{'C++' if app == 'cpp' else 'Py'} r{i}")
        waits.append(pr["wait_mean"]); comps.append(pr["compute_mean"])
        cols_w.append(col_l); cols_c.append(col)
ys = np.arange(len(labels))[::-1]
gap = 1.2  # ms → visual 2px-ish surface gap between segments
ax.barh(ys, waits, height=0.55, color=cols_w, zorder=3)
ax.barh(ys, comps, left=[w + gap for w in waits], height=0.55, color=cols_c, zorder=3)
for y, w, c in zip(ys, waits, comps):
    ax.text(w + c + gap + 3, y, f"{w + c:.0f} ms", va="center", fontsize=8.5, color=SECONDARY)
    if w > 14:
        ax.text(w / 2, y, f"{w:.0f}", va="center", ha="center", fontsize=8, color=INK)
    ax.text(w + gap + c / 2, y, f"{c:.0f}", va="center", ha="center", fontsize=8, color="#ffffff")
ax.set_yticks(ys); ax.set_yticklabels(labels)
ax.set_xlabel("mean per-batch latency (ms)")
ax.set_title("Where the time goes — batch wait (light) + compute (solid), per repeat")
ax.legend(handles=[plt.Rectangle((0, 0), 1, 1, color=C_CPP_LIGHT),
                   plt.Rectangle((0, 0), 1, 1, color=C_CPP),
                   plt.Rectangle((0, 0), 1, 1, color=C_PY_LIGHT),
                   plt.Rectangle((0, 0), 1, 1, color=C_PY)],
          labels=["C++ wait", "C++ compute", "Py wait", "Py compute"],
          loc="upper center", bbox_to_anchor=(0.5, -0.18), ncol=4, fontsize=8)
ax.grid(axis="y", visible=False)
fig.tight_layout(); fig.savefig(os.path.join(OUT, "fig4_decomposition.png"),
                                bbox_inches="tight"); plt.close(fig)

# =====================================================================
# Fig 5 — throughput / fullness / detections: small multiples
# =====================================================================
fig, axes = plt.subplots(1, 4, figsize=(9.6, 2.9), dpi=DPI)
panels = [("batches_s", "batches / s", "{:.1f}"),
          ("frames_s", "frames / s", "{:.1f}"),
          ("fullness", "frames / batch", "{:.2f}"),
          ("dets", "detections (25 s)", "{:.0f}")]
for ax, (key, ttl, fmt) in zip(axes, panels):
    for j, (app, label, col, _) in enumerate(APPS):
        vals = [summary[app]["per_rep"][i][key] for i in range(3)]
        m = np.mean(vals)
        ax.bar([j], [m], width=0.55, color=col, zorder=3)
        ax.scatter([j] * 3, vals, s=14, color=INK, zorder=4,
                   edgecolors=SURFACE, linewidths=1.2)
        ax.text(j, max(vals) * 1.045, fmt.format(m), ha="center", va="bottom",
                fontsize=8.5, color=SECONDARY)
    ax.set_xticks([0, 1]); ax.set_xticklabels(["C++", "Python"], fontsize=9)
    ax.set_title(ttl, fontsize=9.5, pad=10)
    top = max(max(summary[a]["per_rep"][i][key] for i in range(3)) for a, *_ in APPS)
    ax.set_ylim(0, top * 1.22)
    ax.grid(axis="x", visible=False)
fig.suptitle("Throughput & output volume — bar = 3-repeat mean, dots = repeats", fontsize=10.5, y=1.02)
fig.tight_layout(); fig.savefig(os.path.join(OUT, "fig5_throughput.png"), bbox_inches="tight"); plt.close(fig)

# =====================================================================
# Fig 6 — detections per camera (agreement check)
# =====================================================================
fig, ax = plt.subplots(figsize=(7.2, 3.2), dpi=DPI)
w = 0.32
xs = np.arange(4)
for j, (app, label, col, _) in enumerate(APPS):
    vals = [float(np.mean([runs[(app, i)][f"dets_cam{c}"].mean() for i in (1, 2, 3)]))
            for c in range(4)]
    off = (j - 0.5) * (w + 0.04)
    ax.bar(xs + off, vals, width=w, color=col, zorder=3, label=label)
    for x, v in zip(xs + off, vals):
        ax.text(x, v + 0.02, f"{v:.2f}", ha="center", fontsize=8, color=SECONDARY)
ax.set_xticks(xs); ax.set_xticklabels([f"cam{c}" for c in range(4)])
ax.set_ylabel("mean detections per batch")
ax.set_title("Per-camera detections agree across both apps (same clips, same engine)")
ax.legend(loc="upper right"); ax.margins(y=0.2)
ax.grid(axis="x", visible=False)
fig.tight_layout(); fig.savefig(os.path.join(OUT, "fig6_dets_per_cam.png")); plt.close(fig)

# =====================================================================
# Fig 7 — track stability: cumulative new IDs over time
# =====================================================================
fig, ax = plt.subplots(figsize=(7.2, 3.4), dpi=DPI)
for app, label, col, _ in APPS:
    for i in (1, 2, 3):
        r = runs[(app, i)]
        y = r["new_ids_cum"] - r["new_ids_cum"][0]
        ax.plot(r["t_mono"], y, color=col, lw=1.6, alpha=0.95 if i == 1 else 0.5,
                solid_capstyle="round", zorder=3)
ax.set_xlabel("time since pipeline start (s)")
ax.set_ylabel("new track IDs since warmup (cumulative)")
ax.set_title("Track churn — fewer new IDs = steadier tracking (3 repeats per app)")
ax.legend(handles=[plt.Line2D([], [], color=c, lw=2) for _, _, c, _ in APPS],
          labels=[l for _, l, _, _ in APPS], loc="upper left")
fig.tight_layout(); fig.savefig(os.path.join(OUT, "fig7_track_churn.png")); plt.close(fig)

print("wrote plots to", OUT)
for f in sorted(os.listdir(OUT)):
    print(" ", f)
