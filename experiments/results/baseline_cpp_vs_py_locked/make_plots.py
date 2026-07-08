#!/usr/bin/env python3
"""Locked-clocks re-run — per-run figures + locked-vs-unlocked comparison."""
import csv, json, os
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = "/home/darklord01/Documents/deepstream_batch/multicam_perception_rt"
D_UNL = os.path.join(ROOT, "experiments/results/baseline_cpp_vs_py")
D_LCK = os.path.join(ROOT, "experiments/results/baseline_cpp_vs_py_locked")
OUT = os.path.join(D_LCK, "plots")
os.makedirs(OUT, exist_ok=True)
WARMUP = 4.0

SURFACE = "#fcfcfb"; INK = "#0b0b0b"; SECONDARY = "#52514e"; MUTED = "#898781"
GRID = "#e1e0d9"; BASELINE = "#c3c2b7"
C_CPP = "#2a78d6"; C_PY = "#1baf7a"
C_CPP_LIGHT = "#9ec5f4"; C_PY_LIGHT = "#a7e6cd"

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
    for k in ("n_in_batch", "total_dets", "new_ids_cum"):
        out[k] = np.array([int(r[k]) for r in rows])
    m = out["t_mono"] >= WARMUP
    return {k: v[m] for k, v in out.items()}

runs = {}   # (cond, app, i)
for cond, d in (("unlocked", D_UNL), ("locked", D_LCK)):
    for app, tag in (("cpp", "cpp_baseline"), ("py", "py_baseline")):
        for i in (1, 2, 3):
            runs[(cond, app, i)] = load(os.path.join(d, f"{tag}_r{i}.csv"))

def pooled(cond, app, key):
    return np.concatenate([runs[(cond, app, i)][key] for i in (1, 2, 3)])

APPS = [("cpp", "C++ (new mux)", C_CPP, C_CPP_LIGHT),
        ("py", "Python (legacy mux)", C_PY, C_PY_LIGHT)]
DPI = 160

summary = {}
for cond in ("unlocked", "locked"):
    for app, label, _, _ in APPS:
        e2e = pooled(cond, app, "e2e_ms"); comp = pooled(cond, app, "compute_ms")
        per_rep = []
        for i in (1, 2, 3):
            r = runs[(cond, app, i)]
            dur = r["t_mono"][-1] - r["t_mono"][0]
            per_rep.append({
                "batches_s": len(r["t_mono"]) / dur,
                "frames_s": int(r["n_in_batch"].sum()) / dur,
                "fullness": float(r["n_in_batch"].mean()),
                "compute_mean": float(r["compute_ms"].mean()),
                "wait_mean": float((r["e2e_ms"] - r["compute_ms"]).mean()),
                "e2e_mean": float(r["e2e_ms"].mean()),
                "e2e_p99": float(np.percentile(r["e2e_ms"], 99)),
                "dets": int(r["total_dets"].sum()),
            })
        summary[f"{cond}/{app}"] = {
            "per_rep": per_rep,
            "e2e": {"mean": float(e2e.mean()), "p1": float(np.percentile(e2e, 1)),
                    "p50": float(np.percentile(e2e, 50)), "p99": float(np.percentile(e2e, 99)),
                    "max": float(e2e.max())},
            "compute": {"mean": float(comp.mean()), "p50": float(np.percentile(comp, 50))},
        }
json.dump(summary, open(os.path.join(OUT, "summary_locked_vs_unlocked.json"), "w"), indent=2)

# =====================================================================
# L1 — time series, locked runs (did the C++ regime flips disappear?)
# =====================================================================
fig, axes = plt.subplots(2, 1, figsize=(7.6, 5.4), dpi=DPI, sharex=True)
for ax, key, ttl in ((axes[0], "e2e_ms", "end-to-end latency (ms)"),
                     (axes[1], "compute_ms", "compute latency (ms) — mux → tracker")):
    for app, label, col, _ in APPS:
        for i in (1, 2, 3):
            r = runs[("locked", app, i)]
            x, y = r["t_mono"], r[key]
            k = 15
            med = np.array([np.median(y[max(0, j - k):j + 1]) for j in range(len(y))])
            ax.plot(x, y, color=col, lw=0.6, alpha=0.18, zorder=2)
            ax.plot(x, med, color=col, lw=1.6, alpha=0.95 if i == 1 else 0.55,
                    solid_capstyle="round", zorder=3)
    ax.set_ylabel(ttl, fontsize=9)
axes[0].set_title("LOCKED CLOCKS — per-batch latency over the run (3 repeats per app)")
axes[1].set_xlabel("time since pipeline start (s)")
axes[0].legend(handles=[plt.Line2D([], [], color=c, lw=2) for _, _, c, _ in APPS],
               labels=[l for _, l, _, _ in APPS], loc="upper right", ncol=2)
fig.tight_layout(); fig.savefig(os.path.join(OUT, "figL1_timeseries_locked.png")); plt.close(fig)

# =====================================================================
# L2 — CDF: locked (solid, full hue) vs unlocked (lighter shade)
# =====================================================================
fig, ax = plt.subplots(figsize=(7.2, 3.8), dpi=DPI)
for app, label, col, col_l in APPS:
    for cond, c, lw in (("unlocked", col_l, 1.6), ("locked", col, 2.2)):
        e2e = np.sort(pooled(cond, app, "e2e_ms"))
        p = np.arange(1, len(e2e) + 1) / len(e2e) * 100
        ax.plot(e2e, p, color=c, lw=lw, solid_capstyle="round", zorder=3,
                label=f"{'C++' if app == 'cpp' else 'Python'} {cond}")
for q in (50, 99):
    ax.axhline(q, color=GRID, lw=0.8, zorder=1)
    ax.text(2, q + 1.5, f"p{q}", color=MUTED, fontsize=8)
ax.set_xlabel("end-to-end latency (ms)"); ax.set_ylabel("% of batches ≤ x")
ax.set_ylim(0, 103)
ax.set_title("Latency CDF — locked clocks (solid) vs unlocked (light), 3 repeats pooled")
ax.legend(loc="lower right", fontsize=8.5)
fig.tight_layout(); fig.savefig(os.path.join(OUT, "figL2_cdf_locked_vs_unlocked.png")); plt.close(fig)

# =====================================================================
# L3 — dumbbells: e2e percentiles, unlocked → locked per app
# =====================================================================
fig, ax = plt.subplots(figsize=(7.2, 3.6), dpi=DPI)
metrics = ["mean", "p50", "p99", "max"]
ys = np.arange(len(metrics))[::-1]
for j, (app, label, col, col_l) in enumerate(APPS):
    off = (j - 0.5) * -0.22
    for yi, m in zip(ys, metrics):
        u = summary[f"unlocked/{app}"]["e2e"][m]
        l = summary[f"locked/{app}"]["e2e"][m]
        y = yi + off
        ax.plot([u, l], [y, y], color=col_l, lw=2, zorder=2)
        ax.scatter([u], [y], s=42, color=col_l, zorder=3, edgecolors=SURFACE, linewidths=1.5)
        ax.scatter([l], [y], s=58, color=col, zorder=4, edgecolors=SURFACE, linewidths=1.5)
        ax.annotate(f"{l:.0f}", (l, y), textcoords="offset points",
                    xytext=(0, 8), ha="center", fontsize=8, color=SECONDARY)
ax.set_yticks(ys); ax.set_yticklabels(metrics)
ax.set_xlabel("end-to-end latency (ms) — small light dot = unlocked, large solid dot = locked")
ax.set_title("Effect of locking clocks on e2e latency (pooled percentiles)")
ax.legend(handles=[plt.Line2D([], [], color=C_CPP, marker="o", lw=0),
                   plt.Line2D([], [], color=C_PY, marker="o", lw=0)],
          labels=["C++ (new mux)", "Python (legacy mux)"], loc="lower right")
ax.grid(axis="y", visible=False)
fig.tight_layout(); fig.savefig(os.path.join(OUT, "figL3_dumbbell.png")); plt.close(fig)

# =====================================================================
# L4 — decomposition per repeat, locked
# =====================================================================
fig, ax = plt.subplots(figsize=(7.2, 3.6), dpi=DPI)
labels, waits, comps, cols_w, cols_c = [], [], [], [], []
for app, label, col, col_l in APPS:
    for i in (1, 2, 3):
        pr = summary[f"locked/{app}"]["per_rep"][i - 1]
        labels.append(f"{'C++' if app == 'cpp' else 'Py'} r{i}")
        waits.append(pr["wait_mean"]); comps.append(pr["compute_mean"])
        cols_w.append(col_l); cols_c.append(col)
ys = np.arange(len(labels))[::-1]
gap = 1.2
ax.barh(ys, waits, height=0.55, color=cols_w, zorder=3)
ax.barh(ys, comps, left=[w + gap for w in waits], height=0.55, color=cols_c, zorder=3)
for y, w, c in zip(ys, waits, comps):
    ax.text(w + c + gap + 3, y, f"{w + c:.0f} ms", va="center", fontsize=8.5, color=SECONDARY)
    if w > 14:
        ax.text(w / 2, y, f"{w:.0f}", va="center", ha="center", fontsize=8, color=INK)
    if c > 14:
        ax.text(w + gap + c / 2, y, f"{c:.0f}", va="center", ha="center", fontsize=8, color="#ffffff")
ax.set_yticks(ys); ax.set_yticklabels(labels)
ax.set_xlabel("mean per-batch latency (ms)")
ax.set_title("LOCKED CLOCKS — batch wait (light) + compute (solid), per repeat")
ax.legend(handles=[plt.Rectangle((0, 0), 1, 1, color=C_CPP_LIGHT),
                   plt.Rectangle((0, 0), 1, 1, color=C_CPP),
                   plt.Rectangle((0, 0), 1, 1, color=C_PY_LIGHT),
                   plt.Rectangle((0, 0), 1, 1, color=C_PY)],
          labels=["C++ wait", "C++ compute", "Py wait", "Py compute"],
          loc="upper center", bbox_to_anchor=(0.5, -0.18), ncol=4, fontsize=8)
ax.grid(axis="y", visible=False)
fig.tight_layout(); fig.savefig(os.path.join(OUT, "figL4_decomposition_locked.png"),
                                bbox_inches="tight"); plt.close(fig)

# quick text digest
for k in ("unlocked/cpp", "locked/cpp", "unlocked/py", "locked/py"):
    s = summary[k]
    print(f"{k:14s} e2e mean {s['e2e']['mean']:6.1f}  p50 {s['e2e']['p50']:6.1f}  "
          f"p99 {s['e2e']['p99']:6.1f}  max {s['e2e']['max']:6.1f}  "
          f"compute mean {s['compute']['mean']:6.1f}")
print("wrote", OUT)
