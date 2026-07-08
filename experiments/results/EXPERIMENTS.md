# Experiment catalog — every kept result, its purpose, setup, and how to read it

Last updated **2026-07-08**. On this date all pre-PTS-fix experiment results
were **deleted as unreliable** (the jpegparse re-stamping bug corrupted every
PTS-derived measurement made on live cameras before 2026-07-07). Everything
below either post-dates the fix, deliberately reproduces the broken world with
the fix switched OFF (so the "before" is documented under controlled
conditions), or never involved jpegparse at all (replay-clip A/Bs). Deleted
runs are listed at the bottom with the recovery command.

**Shared context.** Rig: Jetson AGX Orin (MODE_30W), 4× Logitech C920 (MJPEG
640×480@30). The bug: `jpegparse` discarded true kernel capture timestamps and
re-stamped frames onto a synthetic grid, so the mux "believed" a bit-constant
~1.4 s cross-camera spread that didn't exist. The fix (`pts_fix`, default ON in
`cpp/multicam_rt` and available as `--pts-fix` in `frame_timing_probe`)
restores the true stamps via a pad-probe pair around each jpegparse.
Replay injection parameters (derived from `baseline_pinned_rerun`, used by
every replay experiment): `--skew-ms 0,1134.8,1702.1,567.2
--rate 0.96063,0.96099,0.96087,0.96128 --gap-every 44 --ring 4`.

---

## 1. Live-camera frame-timing runs
`cpp/experiments/frame_timing/results/<run>/` · instrument: `frame_timing_probe`, 120 s per run

**File schema (all frame_timing runs, live and replay):**

| file | probe point | contents |
|---|---|---|
| `capture.csv` | P0, v4l2src pad | kernel capture stamp of every frame — ground truth |
| `premux.csv` | P1, mux sink pad | PTS of what reaches the mux door (post-jpegparse) — where the bug lived |
| `batches.csv` | P2, mux src pad | every pushed batch: size, push time, cadence |
| `batch_frames.csv` | P2 | exact membership of every batch (which frame from which camera) |
| `meta.json` | — | run parameters + monotonic↔realtime clock bridge |
| `figures/` + `summary.md` | — | 11 figures from `analyze_timing.py` |

**How to read the key metrics:** *fill / % full batches* = batches containing
all 4 cameras. *Nearest-frame spread* = max capture-time gap between the batch
members' closest-in-time frames (how simultaneous the batch *could* be).
*As-batched TRUE spread* = the gap using the frames actually batched, measured
in kernel time (reality). *As-batched SYNTHETIC spread* = the same gap in the
PTS the mux sees (its belief). Pre-fix these two disagree wildly — that
disagreement IS the bug. *% kept* (sync runs) = frames surviving the mux LATE
cut; drops are silent, so coverage must be computed from capture vs batch
membership.

| run | fix | sync | purpose | headline |
|---|---|---|---|---|
| `baseline_pinned_rerun` | OFF | off | Controlled reproduction of the pre-fix world (replaces the deleted 07-06 `baseline_pinned`); also the source of the replay injection parameters | 100 % full batches, ~198 ms true standing-queue skew, **bit-constant 1468.8 ms synthetic spread**, +0.65 %/s common-mode synthetic drift |
| `baseline_pinned_fixed` | ON | off | Prove the fix live at scale | pre-mux PTS == kernel stamp **100.00 %** (13 940/13 940 on all 4 cams); mux belief ≡ reality at every percentile; sync-off batching otherwise identical to fix-OFF |
| `sync_rerun_ml33` | OFF | on, ml 33.3 ms | Document the sync disaster under the bug, fresh | **14.7 % of frames kept**, 40.4 % full batches, progressive camera death (batching halts by t≈90 s) |
| `sync_fixed_ml33` | ON | on, ml 33.3 ms | The campaign headline: does the fix rehabilitate sync? | **99.9 % kept, 100.0 % full batches, spread p50 2.1 ms** (beats RT-BEV's hardware-synced 39–46 ms); standing-queue ladder flushed |

Setup (all four): `./frame_timing_probe --devices
/dev/video0,/dev/video2,/dev/video4,/dev/video6 --duration 120
--extra-controls "exposure_dynamic_framerate=0"` plus `[--pts-fix]` and
`[--sync --max-latency-ms 33.333 --timeout-us 33333]` per the table.
Pinning note: `exposure_dynamic_framerate=0` is the control that actually pins
C920 frame rate (the old `exposure_auto_priority` name was a silent no-op).

Read `sync_rerun_ml33` vs `sync_fixed_ml33` as the single most important A/B in
the repo: same window, same cameras, only the PTS world differs.

## 2. Replay frame-timing runs (deterministic, no cameras)
Same location and schema. Input: `experiments/clips/cam{0..3}.mp4` via
`--replay-dir`; `--restamp` emulates the broken jpegparse, `--no-restamp` is
the fixed pipeline's world.

| run | world | purpose | how to read it |
|---|---|---|---|
| `replay_skewed_rerun` (42 s) | broken (restamp ON) | Validate that the replay harness reproduces the live pre-fix situation | Compare against `baseline_pinned_rerun`: synthetic constant 1438 vs live 1469 ms; believed cross-camera offsets within ~8 % of live. Two known artifacts: frozen-phase spread quantization (~20.8 ms plateaus) and chaotic standing-queue rung values — mechanism and scale must match, exact rungs won't (REPLAY_SKEW.md §7) |
| `replay_skewed_fixed` (42 s) | fixed (no-restamp) | Same injection, fixed world | Mux belief ≡ reality, mirroring `baseline_pinned_fixed` |
| `sync_replay_sweep` (28 cells × 32 s) | both | Re-run the deleted Python-era `sync_sweep` (1-D window scan) + `sync_grid` (window × timeout) on trustworthy replay, in both worlds | `summary.csv/.md` + `fig_sync_fill.png` + `fig_sync_grid.png`. Findings: fixed world fills (≈4.00) at **every** window ≥ 2 ms, discard 19 % → 0.1 % from ml 2 → 133 ms; broken world loses 41–47 % at every frame-scale window; identical columns in the grid prove the timeout property is **inert under sync** |

Setup: `sync_replay_sweep.py --grid` + the shared injection parameters above;
individual cell dirs `scan_*`/`grid_*` hold raw tables in the standard schema.

## 3. C++ vs Python full-pipeline A/B (replay clips — never affected by the bug)
`experiments/results/{baseline_cpp_vs_py, baseline_cpp_vs_py_locked, param_sweep_locked}` ·
3 × 25 s per app, YOLO11n FP16 batch-4, warmup 4 s dropped, `--source file`.
These use filesrc → h264parse → nvv4l2decoder — no jpegparse in the path, so
the PTS bug never touched them.

**Metrics:** *e2e* = capture-to-push latency per batch; *wait* = time the mux
held the batch open; *compute* = GPU inference. `plots/` + `summary.json`
regenerate via `python3 make_plots.py`. Commands: `BASELINE_COMMANDS.md`.

| dir | variable | purpose | headline |
|---|---|---|---|
| `baseline_cpp_vs_py` | app (clocks UNLOCKED) | Is the C++ new-mux app faster than the Python legacy-mux app, defaults vs defaults? | Python is the stability reference (e2e 133 ± 0.1 ms every repeat); C++ 144 ms mean with a worse tail — because DVFS and the shipped INI hide its real advantage |
| `baseline_cpp_vs_py_locked` | + `jetson_clocks` | Remove DVFS noise (CPU 1.73 GHz / GPU 612 MHz / EMC 3.2 GHz pinned) | Compute drops 89 → 43 ms but C++ e2e *rises* to 158 ms — the gap is ~115 ms of **new-mux hold**, not compute; motivates the sweep below |
| `param_sweep_locked` | mux knobs | Find the hold's source: `--timeout-us` values vs INI `overall-min-fps` variants | The INI is the knob that acts: `overall-min-fps=120` (or 60) → **e2e p50 28.3 ms, wait 1.5 ms** — C++ beats Python ~4.7× once the INI stops adding a ~115 ms service-cycle hold. The `batched-push-timeout` property alone barely moves it |

Read the three as one arc: (1) naive A/B says Python wins → (2) locking clocks
says the loss isn't compute → (3) the sweep finds the single INI line
responsible. The 28 ms p50 config (`min-fps=120` INI) is the shipped
recommendation.

## 4. Engine × sync push-deadline sweeps (full inference pipeline)
`experiments/results/timeout_sweep_cpp_*/` · driver: `scripts/timeout_sweep_cpp.py` ·
app: `cpp/multicam_rt` (PTS fix ON, clocks locked), identical skewed replay
every run, 45 s per deadline, deadlines {1, 5, 10, 20, 33.3, 50, 66.7, 100} ms.

**Key mechanism:** `batched-push-timeout` is inert on the new mux, so the
driver generates a per-run INI with `overall-min-fps = 1e6 / push_µs` — that
INI (`mux_push_<µs>us.txt`, kept in each dir) is what actually sets the
deadline.

**Per-run files:** `push_<ms>ms.csv` (per-batch fill, e2e, compute,
detections, arrivals), `push_<ms>ms_dets.jsonl` (one JSON per camera-frame,
keyed by `buf_pts` for deterministic cross-run frame matching),
`push_<ms>ms_stderr.log`. **Per-sweep:** `summary.csv`, `run_meta.json`,
`timeout_sweep.png` (4-panel batching figure), `detection_perf.png`.
Regenerate figures without re-running: `python3 scripts/replot_sweep.py <dir>`.

| sweep | engine | sync | what it shows |
|---|---|---|---|
| `timeout_sweep_cpp_static` | static batch-4 | off | Static engine pays full batch-4 cost per invocation → GPU saturates below one frame period (compute 78–311 ms, e2e up to 375 ms mean / 1.4 s p99). Don't run static with sub-frame deadlines |
| `timeout_sweep_cpp_dynamic` | dynamic 1–4 | off | The clean latency/throughput dial: **e2e 15.3 ms at 117 single-frame invocations/s** (1 ms deadline) → 30/s full batches at 33.3 ms; identical to static at ≥ 33.3 ms |
| `timeout_sweep_cpp_static_sync` | static batch-4 | on (ml 33.3) | Engine speed feeds back into sync: inference queueing pushes frames past the LATE cut, **12–20 % of input silently erased** at sub-frame cycles (coverage 80–88 %) |
| `timeout_sweep_cpp_dynamic_sync` | dynamic 1–4 | on (ml 33.3) | Sync-on as a low-latency aligned mode: e2e tracks the service cycle (26–102 ms), ≥ 97.9 % kept everywhere |
| `timeout_sweep_cpp_dynamic_sync_ml{16,66.7,133}` | dynamic | on, push fixed 33.3 | Window→coverage curve inside the full pipeline: 98.9 / 99.6 / 99.7 % kept, e2e flat ~99–102 ms → **ml 66.7 is the recommended window** (99.6 % kept, nothing gained beyond it) |

**How to read the detection panels:** detection output is **invariant to
batching policy** — ~100 % frame-matched agreement vs the 100 ms reference,
dets/frame flat, track churn flat. Coverage (frames kept) is the *only*
detection-relevant variable, and only the static×sync corner loses any. So
pick operating points on latency/coverage alone.

`sweep_comparison.png` (in the campaign dir) overlays all four sweeps.

## 5. Campaign documentation
`experiments/results/campaign_2026-07-07_ptsfix/` — not an experiment but the
map: `REPORT.md` (full command log + verdicts), `STEP1`–`STEP5` (fix →
baseline redo → replay reproduction → sync after fix → engine sweeps),
`INDEX.md` (where every file lives). The Overleaf-ready LaTeX report is
`pts_fix_campaign_overleaf.zip` at repo root (19 figures + `tools/` that
regenerate every figure from the CSVs above).

---

## Deleted pre-fix runs (2026-07-08)

All PTS-derived numbers in these were corrupted by the jpegparse re-stamping
bug; each one's question was re-answered post-fix by the run in the last
column. Data remains in git history — recover with
`git checkout 265570b -- <path>`.

| deleted path | was | superseded by |
|---|---|---|
| `experiments/results/big/` | 2026-07-04 Python-app strategy campaign (skip/sync/adaptive) | strategy conclusions unaffected in *relative* terms but absolutes untrustworthy; no direct redo (Python app retired) |
| `experiments/results/myowntest/` | 2026-07-06 scratch run | — |
| `experiments/results/sync_sweep/` | 1-D sync window scan (found "window must be ≥ 1.05–1.47 s" — an artifact of the bug) | `sync_replay_sweep` (fixed world fills at every window ≥ 2 ms) |
| `experiments/results/sync_grid/` | window × timeout grid | `sync_replay_sweep --grid` |
| `experiments/results/timeout_sweep{,_smoke,_dynamic}/`, `timeout_sweep_compare.png` | Python-app push-timeout sweeps | `timeout_sweep_cpp_*` (per-run INI mechanism, detection-matched) |
| `cpp/…/results/baseline` | first frame-timing baseline (unpinned) | `baseline_pinned_rerun` |
| `cpp/…/results/baseline_pinned` | 2026-07-06 canonical pre-fix baseline | `baseline_pinned_rerun` (fix OFF, controlled) |
| `cpp/…/results/sync_pinned` | pre-fix sync run | `sync_rerun_ml33` |
| `cpp/…/results/replay_ideal` | replay with no skew injection | superseded by the validated skewed-replay harness |
| `cpp/…/results/replay_skewed` | first replay reproduction (`--gap-every 275`, naive derivation) | `replay_skewed_rerun` (`--gap-every 44`, delivered-rate corrected) |

The historical *reports* built on the deleted Python-era data
(`why_the_batch_never_filled_overleaf.zip`, memory notes) keep their rendered
figures; regenerating those figures from CSVs now requires the git checkout
above.
