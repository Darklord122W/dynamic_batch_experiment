# Step 2 — Re-doing baseline_pinned live (4 cameras, 120 s, fix off/on)

**Date:** 2026-07-07 · **Instrument:** `cpp/experiments/frame_timing/frame_timing_probe`
(three probes: P0 = v4l2src src pad / true kernel stamps, P1 = mux sink pads,
P2 = mux src pad / batch composition) · **Cameras:** 4× Logitech C920,
`/dev/video0,2,4,6`, 640×480@30 MJPG, one shared USB-2 bus.

## 1. Design

Two 120 s legs, sync-inputs OFF, both with the **corrected** frame-rate
pinning control (`exposure_dynamic_framerate=0`, see STEP1 §4). Camera
sessions need 15 s settle between opens and a retry (the v4l2 `-5`
"Internal data stream error" on a random camera when reopened too soon — hit
once, absorbed by the retry):

```bash
cd cpp/experiments/frame_timing

# leg 1 — original pipeline conditions (PTS fix OFF): fresh ground truth,
# and the source of the replay-skew injection parameters
./frame_timing_probe --devices /dev/video0,/dev/video2,/dev/video4,/dev/video6 \
    --duration 120 --extra-controls "exposure_dynamic_framerate=0" \
    --out-dir results/baseline_pinned_rerun

# leg 2 — identical, PTS fix ON (the fixed production pipeline)
./frame_timing_probe --devices /dev/video0,/dev/video2,/dev/video4,/dev/video6 \
    --duration 120 --extra-controls "exposure_dynamic_framerate=0" --pts-fix \
    --out-dir results/baseline_pinned_fixed

# analysis (11 figures + summary.md each, plus A/B comparison figures)
python3 analyze_timing.py results/baseline_pinned_rerun --compare results/baseline_pinned
python3 analyze_timing.py results/baseline_pinned_fixed --compare results/baseline_pinned_rerun
```

Clock state: NOT locked during these legs (`jetson_clocks` was applied later,
before Step 5). The probe pipeline ends in `fakesink` (no GPU inference), so
this affects only the decode-latency figure margins, not timing structure.

## 2. Results — leg 1 (`baseline_pinned_rerun`, fix OFF) vs the 2026-07-06 original

| metric | original `baseline_pinned` | `baseline_pinned_rerun` |
|---|---|---|
| frames / eff. fps per cam | 3375–3383 / 29.0–29.1 | 3482–3483 / 29.8 |
| capture Δt p50 / p99 | 32.0 / 100–108 ms | 32.0 / **36.0** ms |
| kernel seq gaps per cam | 51–55 | **9–10** (corrected pinning) |
| batches (steady) / full | 3379 / 100 % | 3482 / 100 % |
| batch cadence p50 | 32.5 ms | 32.2 ms |
| nearest-frame true spread p50/p99 | 8.9 / 45.0 ms | 1.6 / 31.3 ms (per-launch phase luck) |
| **as-batched true spread p50** | **208.9 ms** | **198.4 ms** |
| **synthetic-PTS in-batch spread** | **constant 1050.1 ms** | **constant 1468.8 ms** |
| capture→mux-door p50 ladder | 27.2/39.5/108.6/110.4 ms | 101.5/35.6/32.2/33.5 ms |
| startup stagger (first kernel stamp) | 0/568/1137/1217 ms | 0/567.2/1134.8/1702.1 ms (cam order differs per launch) |

Everything structural reproduces: 100 % full batches whose members are
~200 ms apart in true capture time (the standing-queue skew), a bit-constant
synthetic-PTS fiction equal to this launch's stagger, and a staleness ladder
ordered by startup order. The constant differs (1469 vs 1050 ms) exactly as
documented — it is frozen per-launch startup history, not a universal number.

New mechanism detail measured this time: the synthetic clocks also carry a
**common-mode drift of +0.65 %/s** (ages grow ~677 ms per 90 s on all four
cameras equally) because the cameras deliver 29.8 fps against the grid's
30.0 fps. Cross-camera offsets stay constant (the stagger), but under
sync-inputs every frame's *absolute* age drifts toward the LATE cut — the
broken world is even worse for sync than the constant-offset model implied
(see STEP4).

## 3. Results — leg 2 (`baseline_pinned_fixed`, fix ON)

| metric | value |
|---|---|
| frames / eff. fps per cam | 3452–3454 / 29.5 |
| capture Δt p50/p99 | 32.0 / 36.1 ms; 25–27 seq gaps per cam |
| batches / full / cadence | 3453 / **100 %** / 32.2 ms |
| as-batched TRUE spread p50/p90/p99 | 200.1 / 204.2 / 276.1 ms |
| as-batched **synthetic** spread p50/p90/p99 | **200.1 / 204.2 / 276.1 ms — identical** |
| PTS integrity | premux == capture 100.00 % ×4 cams; buf_pts true 13 940/13 940 |

The headline: the "what the mux believes" row now **equals** the "what is
physically true" row at every percentile. The 1.0–1.5 s fiction is gone from
the metadata while sync-off batching behaviour is bit-for-bit the same
(100 % full, same cadence, same ~200 ms standing-queue skew — the fix changes
what downstream *knows*, not what the sync-off mux *does*).

## 4. Replay injection parameters derived from leg 1 (REPLAY_SKEW.md §8)

```text
--skew-ms  0,1134.8,1702.1,567.2          # first kernel stamp per camera
--rate     0.96063,0.96099,0.96087,0.96128 # per-camera modal step / 33.333 ms
--gap-every 44                             # matches DELIVERED mean rate 29.8 fps
--ring 4
```

`gap-every 44` (not the naive §8 output of ~275) is deliberate: the sweep in
STEP4 exposed that restamp-world sync fidelity requires the *delivered* frame
rate (mean cadence 33.57 ms), not just the modal step, to match live —
otherwise the emulated grids drift the wrong way and sync trivially succeeds.
Per-camera rates differ (crystal drift), de-quantizing replay phases.

## 5. Evaluation

* The re-run is a faithful, better-instrumented reproduction of the original
  baseline_pinned: same batching structure, tighter capture behaviour thanks
  to the corrected pinning control.
* The fixed leg proves the fix end-to-end live at 120 s scale with zero PTS
  mismatches and zero behavioural regression under sync-off.
* The +0.65 %/s common-mode synthetic drift is a new, previously undocumented
  property of the broken world that materially changes sync-on predictions.
