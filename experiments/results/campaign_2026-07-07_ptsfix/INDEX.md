# Campaign index — every new experiment, file, and where it lives

Campaign: **jpegparse PTS fix + sync rehabilitation + engine sweeps**, run
2026-07-07/08 on the Jetson AGX Orin rig (4× C920). This file is the map;
the narrative with commands and evaluations is [REPORT.md](REPORT.md) and
STEP1–STEP5 in this directory. All paths below are repo-relative.

---

## 1. New and modified code

| path | status | what it does |
|---|---|---|
| `cpp/src/pipeline_builder.cpp` / `.hpp` | modified | **The jpegparse PTS-restore fix**: pad-probe pair (sink FIFO → src restore) around each `jpegparse` so true kernel capture stamps survive downstream; **replay-skew front-end** ported into the production app (skew probe, pacing, leaky ring, optional restamp); **mux INI ordering fix** (INI loaded before the timeout property) |
| `cpp/src/app_config.cpp` / `.hpp` | modified | Config/CLI plumbing for the above: `pts_fix` (default ON), `ReplayCfg` (skew/rate/gap/ring/restamp), `--pgie-config` engine override |
| `cpp/src/main.cpp` | modified | New CLI flags: `--pts-fix/--no-pts-fix`, `--pgie-config`, `--skew-ms`, `--rate`, `--gap-every`, `--ring`, `--restamp/--no-restamp`; banner reports all of it |
| `cpp/src/output_writer.cpp` | modified | Adds `buf_pts` to every JSON detection record → deterministic frame identity for cross-run detection matching |
| `cpp/experiments/frame_timing/frame_timing_probe.cpp` | modified | Instrument gains `--pts-fix` (live-mode fix, mirrors the app) + the same INI-ordering fix; `meta.json` records `pts_fix` |
| `cpp/experiments/frame_timing/analyze_timing.py` | modified | Replay-aware summary header, `n/a (replay)` for kernel seq gaps, reports actual captured span |
| `cpp/experiments/frame_timing/sync_replay_sweep.py` | **new** | Reproduces the historic `sync_sweep` (1-D window scan) + `sync_grid` (window × timeout) on the skewed replay, in BOTH timestamp worlds (restamp = broken jpegparse, no-restamp = fixed pipeline); emits `summary.csv/.md`, `fig_sync_fill.png`, `fig_sync_grid.png` |
| `cpp/experiments/frame_timing/run_replay.sh` | modified | Injection parameters re-derived from the 2026-07-07 live run (`SKEW="0,1134.8,1702.1,567.2"`, per-camera `RATE`, `GAP=44`) |
| `scripts/timeout_sweep_cpp.py` | **new** | The C++-app push-deadline sweep driver: runs `cpp/multicam_rt` on skewed replay per timeout value, **generating a per-run mux INI** (`overall-min-fps = 1e6/push_µs`) because the `batched-push-timeout` property is inert on the new mux; collects per-batch metrics CSV + per-frame detection JSONL; plots the 4-panel batching figure and the detection-performance figure |
| `scripts/replot_sweep.py` | **new** | Regenerates a sweep directory's figures from its persisted CSV/JSONL (no pipeline re-runs) |

Docs updated in the same campaign (30+ verified corrections): `TERMINOLOGY.md`,
`PIPELINE.md`, `README.md`, `config/mux_config.txt`, `cpp/README.md`,
`cpp/ARCHITECTURE.md`, `cpp/TUTORIAL.md`, `cpp/experiments/frame_timing/README.md`
(§6 = this campaign), `REPLAY_SKEW.md` (§9 drift/anchor fidelity limits, §10),
`experiments/README.md`, `experiments/TUTORIAL.md`,
`experiments/results/BASELINE_COMMANDS.md` (Campaign 4 section),
`experiments/results/{param_sweep_locked,baseline_cpp_vs_py*}/README.md`,
`experiments/static_engine_test/README.md`.

---

## 2. Live-camera experiments (frame_timing instrument, 120 s each)

Location: `cpp/experiments/frame_timing/results/<run>/`. Each run directory
contains the raw probe tables — `capture.csv` (P0: kernel capture stamps at
the v4l2src pad), `premux.csv` (P1: what reaches the mux door), `batches.csv`
+ `batch_frames.csv` (P2: every pushed batch and its exact membership),
`meta.json` (run parameters + clock bridge) — and `figures/` (11 figures +
`summary.md` from `analyze_timing.py`).

| run dir | pipeline | what the experiment does / shows |
|---|---|---|
| `results/baseline_pinned_rerun` | sync OFF, **fix OFF**, corrected pinning (`exposure_dynamic_framerate=0`) | Re-does the canonical 2026-07-06 `baseline_pinned` under original (unfixed) conditions: reproduces 100 % full batches, the ~198 ms standing-queue skew, and the **bit-constant 1468.8 ms synthetic-PTS fiction**; source of the fresh replay injection parameters; also first measurement of the **+0.65 %/s common-mode synthetic drift** |
| `results/baseline_pinned_fixed` | sync OFF, **fix ON** | Proves the fix live at scale: pre-mux PTS == kernel stamp **100.00 %** on all 4 cameras, `buf_pts` true **13 940/13 940**, mux belief ≡ reality at every percentile, sync-off batching bit-identical to the unfixed leg |
| `results/sync_rerun_ml33` | **sync ON** (ml 33.3 ms), fix OFF | The disaster, fresh: **14.7 % of frames kept**, 40.4 % full batches, 200 ms cadence — and `figures/fig07` shows the *progressive death* (cameras erased one by one, batching halts by t≈90 s) |
| `results/sync_fixed_ml33` | **sync ON** (ml 33.3 ms), **fix ON** | **The campaign headline**: 99.9 % kept, **100.0 % full batches**, members truly simultaneous (spread p50 **2.1 ms** — beats RT-BEV's hardware-synced 39–46 ms), standing-queue ladder flushed; cadence/staleness set by the mux INI service cycle |

Historic runs `baseline`, `baseline_pinned`, `sync_pinned`, `replay_ideal`,
`replay_skewed` (2026-07-06) were **deleted 2026-07-08** as unreliable pre-fix
data — their role as pre-fix reference is covered by the fix-OFF reruns above
(`baseline_pinned_rerun`, `sync_rerun_ml33`, `replay_skewed_rerun`). Recover
with `git checkout 265570b -- cpp/experiments/frame_timing/results/<name>`.

## 3. Replay experiments (no cameras, deterministic input)

Same location and file schema as §2.

| run dir | what it does |
|---|---|
| `results/replay_skewed_rerun` (42 s) | Reproduces the live `baseline_pinned_rerun` situation on recorded clips with the fresh parameters, **restamp ON** (emulated broken jpegparse). Validated: synthetic constant 1438 vs live 1469 ms, believed cross-camera offsets within ~8 % of live |
| `results/replay_skewed_fixed` (42 s) | Same injection, **`--no-restamp`** = the fixed pipeline's world. Validated: mux belief ≡ reality, mirroring `baseline_pinned_fixed` |
| `results/sync_replay_sweep` (28 cells × 32 s) | `sync_sweep` + `sync_grid` re-run on the skewed replay in both worlds. Findings: fixed world **fills (≈4.00) at every window ≥ 2 ms** with discard 19 % → 0.1 % from ml 2 → 133 ms; broken world loses 41–47 % at every frame-scale window; the 2-D grid proves the **timeout property is inert under sync** (columns identical). Cell dirs `scan_*`/`grid_*` inside hold raw tables; top level has `summary.csv`, `summary.md`, `fig_sync_fill.png`, `fig_sync_grid.png` (discard heat map), `params.json` |

Injection parameters (derived from `baseline_pinned_rerun` per REPLAY_SKEW §8,
with the delivered-rate correction): `--skew-ms 0,1134.8,1702.1,567.2
--rate 0.96063,0.96099,0.96087,0.96128 --gap-every 44 --ring 4`.

## 4. Engine × sync push-deadline sweeps (full inference pipeline)

Location: `experiments/results/timeout_sweep_cpp_*/`. App: `cpp/multicam_rt`
(PTS fix ON, clocks locked at 612 MHz), input: identical skewed replay for
every run, 45 s per timeout value, deadlines {1, 5, 10, 20, 33.3, 50, 66.7,
100} ms. Per run: `push_<ms>ms.csv` (per-batch metrics: fill, e2e, compute,
detections, arrivals), `push_<ms>ms_dets.jsonl` (one JSON per camera-frame,
keyed by `buf_pts`), `push_<ms>ms_stderr.log`, `mux_push_<µs>us.txt` (the
generated per-run INI — the knob that actually acts). Per sweep: `summary.csv`,
`run_meta.json`, `timeout_sweep.png` (the 4-panel figure reproducing the
original live-sweep layout), `detection_perf.png`.

| sweep dir | engine | sync | headline |
|---|---|---|---|
| `timeout_sweep_cpp_static` | static batch-4 (`_pgie_static.txt` → `model_static_b4_gpu0_fp16.engine`) | off | Pays full batch-4 cost per invocation → GPU saturates below one frame period (compute 78–311 ms, e2e to 375 ms mean / 1.4 s p99) |
| `timeout_sweep_cpp_dynamic` | dynamic 1–4 (`pgie_config.txt` → `model_b4_gpu0_fp16.engine`) | off | Rides the tradeoff cleanly: **e2e 15.3 ms at 117 single-frame invocations/s** (1 ms deadline) → 30/s full batches at 33.3 ms; identical to static at ≥ 33.3 ms |
| `timeout_sweep_cpp_static_sync` | static batch-4 | on (ml 33.3) | Engine-speed → sync feedback: inference queueing pushes frames past the LATE cut, **12–20 % of input erased** at sub-frame cycles (coverage 80–88 %) |
| `timeout_sweep_cpp_dynamic_sync` | dynamic 1–4 | on (ml 33.3) | Sync-on as a low-latency aligned mode: e2e tracks the service cycle (26–102 ms), ≥ 97.9 % kept everywhere |
| `timeout_sweep_cpp_dynamic_sync_ml{16,66.7,133}` | dynamic | on, window mini-sweep at push 33.3 | Window→coverage curve inside the full pipeline: 98.9 / 99.6 / 99.7 % kept, e2e flat (~99–102 ms). Single-cell dirs: `summary.csv` + raw files + README (single-point plots removed as uninformative) |

Detection result across all of it: output is **invariant to batching policy**
(~100 % frame-matched agreement vs the 100 ms reference, dets/frame flat,
track churn flat) — coverage is the only detection-relevant variable, and
only the static×sync corner loses any.

## 5. Campaign documents and deliverables (this directory + repo root)

| file | contents |
|---|---|
| `REPORT.md` | The full campaign report: context, per-step links, **complete command log with every parameter**, campaign-level verdicts |
| `STEP1_jpegparse_pts_fix.md` | The fix: mechanism, verification table, the two companion bugs (dead UVC pinning control `exposure_auto_priority` → `exposure_dynamic_framerate`; INI-vs-property ordering) |
| `STEP2_baseline_pinned_redo.md` | The two 120 s baseline legs, original-vs-rerun-vs-fixed comparison table, replay parameter derivation, the +0.65 %/s drift finding |
| `STEP3_replay_skewed_reproduction.md` | Replay reproduction in both worlds, validation table, the gap-every-44 delivered-rate correction and its rationale |
| `STEP4_sync_after_fix.md` | The sync question answered: live A/B table, window/grid sweep results, recommended post-fix sync config (`--sync --max-latency-ms 66.7`) |
| `STEP5_timeout_sweeps_engines.md` | The four sweeps + mini-sweep: design (per-run INI mechanism), results tables, detection analysis, capacity-boundary caveats, operating-point recommendations |
| `sweep_comparison.png` | All four sweeps overlaid (fill, invocations/s, e2e, coverage) |
| `INDEX.md` | this file |
| `../../../pts_fix_campaign_overleaf.zip` (repo root) | Overleaf-ready LaTeX report ("Fixing the Clock"): `main.tex` + 19 figures + `tools/` (the scripts that regenerate every figure from the raw CSVs). Compiles with stock pdfLaTeX |

## 6. How to re-run any of it

```bash
# live baselines + sync A/B (needs the 4 cameras idle; ~15 s settle between runs)
cd cpp/experiments/frame_timing && make
./frame_timing_probe --devices /dev/video0,/dev/video2,/dev/video4,/dev/video6 \
    --duration 120 --extra-controls "exposure_dynamic_framerate=0" [--pts-fix] \
    [--sync --max-latency-ms 33.333 --timeout-us 33333] --out-dir results/<name>
python3 analyze_timing.py results/<name> [--compare results/<other>]

# replay reproduction (no cameras)
./run_replay.sh          # or frame_timing_probe --replay-dir clips ... (STEP3)

# sync window/grid sweep on replay
python3 sync_replay_sweep.py --skew-ms "0,1134.8,1702.1,567.2" \
    --rate "0.96063,0.96099,0.96087,0.96128" --gap-every 44 --ring 4 --grid \
    --out results/sync_replay_sweep

# engine x sync push-deadline sweeps (lock clocks first: sudo jetson_clocks)
python3 scripts/timeout_sweep_cpp.py --skew-ms 0,1134.8,1702.1,567.2 \
    --rate 0.96063,0.96099,0.96087,0.96128 --gap-every 44 --ring 4 \
    --duration 45 --warmup 5 --pgie config/_pgie_static.txt|config/pgie_config.txt \
    [--sync --max-latency-ms 33.333] --out experiments/results/<dir>
python3 scripts/replot_sweep.py experiments/results/<dir>   # figures only
```
