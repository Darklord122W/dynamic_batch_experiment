# Step 4 — Can sync-inputs=1 fill the batch after the fix? (Yes.)

**Date:** 2026-07-07 · **Question:** the historic sweeps never produced one
full 4-camera batch under `sync-inputs=1` (best mean fill 2.00; 93.5 %
discard live). Does the jpegparse PTS-restore fix change that?

## 1. The decisive live A/B (ground truth)

Two 120 s live runs, `--sync --max-latency-ms 33.333 --timeout-us 33333`,
corrected pinning control, identical except the fix:

```bash
cd cpp/experiments/frame_timing
./frame_timing_probe --devices /dev/video0,/dev/video2,/dev/video4,/dev/video6 \
    --duration 120 --extra-controls "exposure_dynamic_framerate=0" \
    --pts-fix --sync --max-latency-ms 33.333 --timeout-us 33333 \
    --out-dir results/sync_fixed_ml33        # fix ON
# ... same command without --pts-fix -> results/sync_rerun_ml33   # fix OFF
python3 analyze_timing.py results/sync_fixed_ml33 --compare results/baseline_pinned_fixed
python3 analyze_timing.py results/sync_rerun_ml33
```

Steady-state results:

| | fix OFF (`sync_rerun_ml33`) | **fix ON (`sync_fixed_ml33`)** |
|---|---|---|
| arrivals → batched | 13 985 → 2 059 (**14.7 % kept**) | 9 587 → 9 580 (**99.9 % kept**) |
| batches / mean fill / % full | 639 / 3.22 / 40.4 % | **2 395 / 4.00 / 100.0 %** |
| batch size distribution | {1: 27, 2: 62, 3: 292, 4: 258} | **{4: 2395}** |
| per-camera survival | 635/258/616/550 of ~3 495 each | 2 395 of ~2 396 each (fair) |
| true capture spread inside batches | (survivors only) | **p50 2.1 ms, p90 32.7, p99 70.1** |
| staleness at push, vs true capture (per cam median) | — | 308/309/310/311 ms — **uniform** (fig08; the sync-off ladder was 32/172/239/241 ms) |
| batch cadence | 200.3 ms | bursts: p50 gap 0.3 ms, every ~200 ms |

Three headlines:

1. **Full batches under sync-on are now routine** — 100.0 % of steady-state
   batches carry all 4 cameras, with 99.9 % of arrived frames kept. Pre-fix,
   at the same settings, 85.3 % of frames were silently erased and only
   40.4 % of the (far rarer: 639 vs 2 395) batches were full.
2. **The batches are genuinely simultaneous**: true capture spread p50
   2.1 ms — versus ~200 ms in the sync-off baseline (standing-queue skew) and
   39–46 ms for RT-BEV's hardware-synchronized nuScenes reference. Sync-inputs
   + true timestamps = a real software genlock, and it also *flushes the
   standing-queue skew* (staleness becomes uniform across cameras instead of
   the 32/172/239/241 ms ladder).
3. **The cost at these settings**: capture throughput drops to ~20.5 fps/cam
   (kernel drops the excess; seq gaps ~550/cam) and frames leave ~308–311 ms
   old (vs true capture), because batches are pushed in bursts every ~200 ms.
   That 200 ms is the new mux's **default `overall-min-fps=5`** floor (the
   probe loads no INI) — a configuration artifact, not a property of
   sync+fix (see §3): with the shipped INI's min-fps=30 the service cycle is
   33 ms.

Bonus mechanism finding (fix OFF leg): the synthetic clocks don't just carry
the constant ~0.6–1.7 s grid offsets — they also drift **+0.65 %/s
common-mode** (delivered 29.8 fps vs the grid's 30.0), so under sync every
frame's age creeps toward the LATE cut. The broken world degrades
*progressively* at any frame-scale window; only a whole-stagger window
(ml ≈ 2000 ms, the historic workaround) holds, at the price of accepting
seconds of apparent skew.

## 2. The parameter sweep (sync_sweep + sync_grid reproduced on skewed replay)

`sync_replay_sweep.py` re-runs the historic 1-D window scan and 2-D
window×timeout grid on the STEP3 skewed replay, in both timestamp worlds
(28 cells × 32 s, `fakesink` — mux behaviour isolated from inference):

```bash
cd cpp/experiments/frame_timing
python3 sync_replay_sweep.py --skew-ms "0,1134.8,1702.1,567.2" \
    --rate "0.96063,0.96099,0.96087,0.96128" --gap-every 44 --ring 4 \
    --grid --out results/sync_replay_sweep
```

1-D scan (push-timeout 33 333 µs), mean fill / % discarded:

| max-latency (ms) | broken world (restamp) | fixed world (true PTS) |
|---|---|---|
| 2 | 3.79 / 43.0 % | **4.00 / 19.1 %** |
| 8.33 | 3.76 / 43.8 % | **3.99 / 18.5 %** |
| 33.333 | 3.72 / 47.5 % | **4.00 / 4.5 %** |
| 66.7 | 3.79 / 43.6 % | **4.00 / 1.0 %** |
| 133 | 3.85 / 33.1 % | **4.00 / 0.1 %** |
| 2000 | 4.00 / ~0 % | 4.00 / 0.2 % |

* Fixed world: **fill is ≥ 3.99 at every window** (4.00 everywhere except
  ml 5–8.33) — with true timestamps the co-batching condition is trivially
  satisfied; the only tradeoff left is discard vs window width, and it
  closes by ml ≈ 66–133 ms.
* Broken world: ~41–47 % erasure at every frame-scale window (ml 2–66.7),
  drift-dominated, matching the live mechanism; recovery starts only at
  whole-stagger windows (33.1 % at ml 133, ~0 % at ml 2000 over a 32 s
  cell — the historic `--max-latency-ms 2000` escape hatch, at seconds of
  latency exposure). (Replay under-erases vs the
  live 85 % because live grids also carry a 0.3–1.3 s per-camera startup
  anchor lag the emulation does not inject — fidelity limit documented in
  REPLAY_SKEW.md §9; the live A/B above is the authoritative broken-world
  number.)

2-D grid (fixed world, ml ∈ {16, 33.3, 66.7} × timeout ∈ {8.3, 16.7, 33.3,
66.7} ms): discard depends **only on ml** (12.0/11.8/12.1/12.9 % across the
whole timeout row at ml=16; 1.4–2.4 % at ml=66.7). **The batched-push-timeout
property is inert under sync-on on the new mux** — the historic sync_grid's
second axis never did anything on this code path; under sync, cadence is set
by the INI's `overall-min-fps` and the alignment window by `max-latency`.

## 3. Recommended sync-on configuration (post-fix)

```
./cpp/multicam_rt --config config/camera_params.yaml \
    --sync --max-latency-ms 66.7            # window: <=1 % discard in replay
    # INI: config/mux_config.txt overall-min-fps governs the push cadence —
    # min-fps=30 gives a 33 ms service cycle (~30 full batches/s);
    # the shipped file already sets 30. --timeout-us does nothing under sync.
```

This replaces the pre-fix recommendation (`--max-latency-ms 2000`), which is
obsolete on a fixed pipeline: the window can shrink 30–60× because it now has
to cover only real physical skew (p99 ≈ 45 ms), not the synthetic grid
offsets. Step 5 measures this configuration under the full detector/tracker
load for both engines.

## 4. Evaluation

The fix converts sync-inputs from "erases 85–94 % of the data to align a
fiction" into "aligns reality at ~0–5 % loss with full batches". What remains
is an engineering tradeoff (window vs discard vs INI-governed cadence), not a
structural impossibility. For the project's own per-camera-detection workload
sync-off remains simpler and lossless — but sync-on is now a *viable* option
for any future fusion stage, delivering better-than-hardware-sync median
alignment (2.1 ms) from free-running USB cameras.
