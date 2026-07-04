# Experiments — dynamic timeout, camera skipping, evaluation

This folder is the experiment/evaluation harness for the RT (RT-BEV-inspired)
variant of the pipeline. It adds three things to the base app and a way to
measure them:

1. **Dynamic batching timeout** — `nvstreammux batched-push-timeout` adapted at
   runtime (`controllers.TimeoutController`, policies `fixed` | `adaptive`).
2. **Context-aware camera skipping** — a `valve` per camera toggled by a
   `ContextProvider` (`context.py`: `all` | `activity` | `scheduled`), applied by
   `controllers.CameraGateController`.
3. **A measurement harness** — `metrics.py` logs per-batch latency/throughput to
   CSV; `scripts/analyze.py` aggregates; `scripts/benchmark.py` sweeps configs;
   `scripts/record_replay_clips.py` captures reproducible input.

## The RT-BEV connection (what maps, what doesn't)

RT-BEV (RTSS'24) co-optimizes *communication* (which cameras to sync + the
allowable sync delay) and *computation* (ROI processing). Its `FlexibleTimeSync`
shrinks the allowable delay when it syncs a smaller, context-selected camera
group. Our pipeline runs **independent per-camera detection** (no fused BEV
model), so RT-BEV's ROI/feature-split machinery does **not** apply — but its
**adaptive-sync idea** and its **evaluation methodology** do, and that's what
this harness implements:

| RT-BEV | Here |
|---|---|
| `slop` (allowable sync delay), adapted by context/TTC | `batched-push-timeout`, adapted by active-camera count (`adaptive` policy) |
| Context filter selects relevant cameras | `ContextProvider` selects active cameras → valves |
| Metrics: e2e latency (mean + worst-case), processed frames, mAP, FES | `analyze.py`: compute+e2e mean/p50/p99/max, frames/s, fullness, stability proxy, FES |

## Metrics logged (per batch, by `metrics.py`)

`n_in_batch` (frames actually batched — the timeout/skip effect), `n_active`,
`timeout_us`, `compute_ms` (mux→tracker = inference+track), `e2e_ms` (source→output,
**includes the batch wait**, correlated by frame PTS), `total_dets`, per-camera
detections, and cumulative new track IDs (stability proxy).

> **e2e vs compute:** `nvstreammux` re-timestamps the batch, so a naive PTS-based
> e2e would miss the wait. We instead stamp each frame's arrival at its source
> (post-valve) and match by PTS, so `e2e_ms` = wait + compute. `e2e_ms − compute_ms`
> ≈ the batch wait.

## Reproducible input (do this first)

Live cameras aren't reproducible. Record identical clips once, then replay:

```bash
python3 scripts/record_replay_clips.py --duration 40 --out-dir experiments/clips
# then run anything with:  --source file --replay-dir experiments/clips
```

`--source file` also flips `nvstreammux live-source=0` so the sink paces playback
to real time (not rushed). Absolute latencies under replay differ from live
(extra H.264 decode), but **relative** A/B comparisons are valid — that's the point.

## The experiments

### E1 — timeout sweep (does the timeout value matter?)
```bash
python3 scripts/benchmark.py --experiment e1 --source file --duration 25 --warmup 4
```
Sweeps fixed `batched-push-timeout` ∈ {10, 20, 33, 50} ms with all cameras active.
**Finding on synced input:** batches fill before the timeout (fullness ≈ 4), so
the value barely moves e2e — a fixed 33 ms is fine when cameras are synced and all
active. The timeout only bites when batches *don't* fill (skipping / desync).

### E2 — engine shape (dynamic vs static batch) — *scaffold*
Not automated (needs a re-export). Build a static-batch-4 engine and compare
`compute_ms` mean/p99 to the shipped dynamic engine, all else equal. See the main
project's `scripts/build_engine.py` and `pgie_config.txt` (`model-engine-file`).
Expect a small gain since the dynamic engine was already built with `opt=4`.

### E3 / policy — camera skipping + adaptive timeout
```bash
python3 scripts/benchmark.py --experiment policy --config experiments/exp_scheduled.yaml \
        --source file --duration 25 --warmup 4
```
Compares **baseline** (fixed timeout, all active) vs **skip_fixed** (skip cameras,
fixed timeout) vs **skip_adaptive** (skip + adaptive timeout), using the scheduled
active-set in `exp_scheduled.yaml`.

**Key finding (measured):** skipping 2 of 4 cameras cuts `compute_ms` (~31→22 ms)
*but* the batch can never fill (batch-size 4, only 2 arrive), so the muxer pays the
full timeout every batch — the **wait grows** and net e2e can get *worse*. Skipping
is a **compute/power win, not a latency win** on the legacy mux. Shrinking the
timeout (`--timeout-policy adaptive`) is the lever that recovers latency.

**Batch-size adaptation — tested, does NOT help (negative result).** The obvious
next idea was to also shrink `nvstreammux batch-size` to the active count so a
short batch pushes immediately. Built as `--batch-policy adaptive` and measured:
it doesn't work, and is slightly worse. Isolated `videotestsrc` test, wait
(source→mux-push) when 2 of 4 are skipped: **~54 ms at batch-size 4, ~90 ms at
batch-size 2.** Reason: the **legacy nvstreammux pushes on "all connected sink
pads delivered OR timeout", not on batch-size** — so a smaller batch-size never
triggers an early push. `nvinfer` batch-size is irrelevant here (it's the engine
max; the dynamic engine runs any smaller batch natively, and it's NULL/READY-only
anyway). A genuine early-push-on-skip would need the **new nvstreammux**
(`USE_NEW_NVSTREAMMUX=yes`, deadline-based) or **dynamically releasing the skipped
cameras' request pads** — both larger changes, not yet done.
**This is exactly why you measure before optimizing** — the intuitive fix was a
regression.

## Interpreting the table (`analyze.py`)

Compare runs on **e2e p99/max** (tail latency — what a real-time system lives or
dies on), **frames/s** (throughput), **avg batch fullness** (the timeout/skip
effect), and **FES** (`processed_frames × accuracy / avg_latency`, RT-BEV Eq. 4).
`accuracy` defaults to a **track-stability proxy** (no webcam ground truth); pass
`--accuracy <mAP>` if you replay a labelled dataset.

Always drop a warmup window (`--warmup 4`): the first ~1–2 s per run includes
engine deserialization and pipeline fill, which otherwise dominates p99/max.

## Files
```
experiments/
├── exp_scheduled.yaml   # example config: scheduled camera-skip + (CLI) adaptive timeout
├── clips/               # recorded per-camera replay clips (cam0..3.mp4)
└── results/             # benchmark CSV outputs land here
```
