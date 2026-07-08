#!/usr/bin/env python3
"""Parameter sweep (locked clocks) — figures."""
import csv, os
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = "/home/darklord01/Documents/deepstream_batch/multicam_perception_rt"
D = os.path.join(ROOT, "experiments/results/param_sweep_locked")
DB = os.path.join(ROOT, "experiments/results/baseline_cpp_vs_py_locked")
OUT = os.path.join(D, "plots")
os.makedirs(OUT, exist_ok=True)
WARMUP = 4.0

SURFACE = "#fcfcfb"; INK = "#0b0b0b"; SECONDARY = "#52514e"; MUTED = "#898781"
GRID = "#e1e0d9"; BASELINE = "#c3c2b7"
C_CPP = "#2a78d6"; C_PY = "#1baf7a"; C_CPP_LIGHT = "#9ec5f4"

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
    t = np.array([float(r["t_mono"]) for r in rows])
    e = np.array([float(r["e2e_ms"]) for r in rows])
    c = np.array([float(r["compute_ms"]) for r in rows])
    m = t >= WARMUP
    return t[m], e[m], c[m]

DPI = 160

# ---- S1: e2e p50 + p99 per config (sweep, single runs) ----
configs = [
    ("Py baseline (33 ms)", os.path.join(DB, "py_baseline_r1.csv"), C_PY),
    ("Py timeout 5 ms", os.path.join(D, "py_t5000.csv"), C_PY),
    ("C++ INI min-fps 30 (baseline)", os.path.join(DB, "cpp_baseline_r1.csv"), C_CPP),
    ("C++ timeout 20 ms (INI 30)", os.path.join(D, "cpp_t20000.csv"), C_CPP),
    ("C++ timeout 10 ms (INI 30)", os.path.join(D, "cpp_t10000.csv"), C_CPP),
    ("C++ INI min-fps 60", os.path.join(D, "cpp_minfps60.csv"), C_CPP),
    ("C++ mux defaults (no INI)", os.path.join(D, "cpp_muxnone.csv"), C_CPP),
    ("C++ INI min-fps 120", os.path.join(D, "cpp_minfps120.csv"), C_CPP),
]
fig, ax = plt.subplots(figsize=(7.6, 4.2), dpi=DPI)
ys = np.arange(len(configs))[::-1]
for y, (label, path, col) in zip(ys, configs):
    _, e, _ = load(path)
    p50, p99 = np.percentile(e, 50), np.percentile(e, 99)
    ax.plot([p50, p99], [y, y], color=col, lw=2, alpha=0.35, zorder=2)
    ax.scatter([p50], [y], s=58, color=col, zorder=4, edgecolors=SURFACE, linewidths=1.5)
    ax.scatter([p99], [y], s=34, color=col, zorder=3, alpha=0.55,
               edgecolors=SURFACE, linewidths=1.2)
    ax.annotate(f"{p50:.0f}", (p50, y), textcoords="offset points", xytext=(0, 9),
                ha="center", fontsize=8, color=SECONDARY)
ax.set_yticks(ys); ax.set_yticklabels([c[0] for c in configs], fontsize=9)
ax.set_xlabel("end-to-end latency (ms) — large dot = p50 (labeled), small dot = p99")
ax.set_title("Parameter sweep, locked clocks — one 25 s run per config")
ax.grid(axis="y", visible=False)
fig.tight_layout(); fig.savefig(os.path.join(OUT, "figS1_sweep_p50_p99.png")); plt.close(fig)

# ---- S2: winner time series vs python ----
fig, ax = plt.subplots(figsize=(7.6, 3.8), dpi=DPI)
series = [
    ("C++ min-fps 120 r1", os.path.join(D, "cpp_minfps120.csv"), C_CPP, 0.95),
    ("C++ min-fps 120 r2", os.path.join(D, "cpp_minfps120_r2.csv"), C_CPP, 0.55),
    ("C++ min-fps 120 r3", os.path.join(D, "cpp_minfps120_r3.csv"), C_CPP, 0.3),
    ("Python baseline", os.path.join(DB, "py_baseline_r1.csv"), C_PY, 0.95),
]
for label, path, col, alpha in series:
    t, e, _ = load(path)
    ax.plot(t, e, color=col, lw=0.7, alpha=0.9 * alpha, zorder=3)
ax.set_xlabel("time since pipeline start (s)")
ax.set_ylabel("per-batch e2e latency (ms)")
ax.set_title("Winner (C++ INI min-fps 120, 3 repeats, raw) vs Python baseline — locked clocks")
ax.legend(handles=[plt.Line2D([], [], color=C_CPP, lw=2),
                   plt.Line2D([], [], color=C_PY, lw=2)],
          labels=["C++ min-fps 120 (3 repeats)", "Python baseline"], loc="upper left")
fig.tight_layout(); fig.savefig(os.path.join(OUT, "figS2_winner_timeseries.png")); plt.close(fig)

# digest
print(f"{'config':34s} {'p50':>6s} {'p99':>6s} {'mean':>6s} {'wait':>6s} {'comp':>6s}")
for label, path, _ in configs:
    _, e, c = load(path)
    w = e - c
    print(f"{label:34s} {np.percentile(e,50):6.1f} {np.percentile(e,99):6.1f} "
          f"{e.mean():6.1f} {w.mean():6.1f} {c.mean():6.1f}")
print("wrote", OUT)
