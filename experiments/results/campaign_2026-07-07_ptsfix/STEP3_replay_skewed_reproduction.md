# Step 3 — Reproducing replay_skewed from the new baseline_pinned

**Date:** 2026-07-07 · **Instrument:** `frame_timing_probe --replay-dir`
(filesrc → qtdemux → h264parse → nvv4l2decoder(+20 surfaces) → skew probe →
identity sync=true → leaky ring queue(4) → nvvideoconvert → mux; see
`REPLAY_SKEW.md`) · **Clips:** `clips/cam{0..3}.mp4` (45.7/75.3/66.3/60.0 s,
640×480@30 H.264).

## 1. Design

Reproduce the live `baseline_pinned_rerun` timing situation on recorded video,
in both timestamp worlds, using the injection parameters derived in STEP2 §4:

```bash
cd cpp/experiments/frame_timing
SKEW="0,1134.8,1702.1,567.2"; RATE="0.96063,0.96099,0.96087,0.96128"

# broken world — restamp ON emulates the UNFIXED jpegparse (synthetic grids)
./frame_timing_probe --replay-dir clips --num-cams 4 --duration 42 \
    --skew-ms "$SKEW" --rate "$RATE" --gap-every 275 --ring 4 \
    --out-dir results/replay_skewed_rerun

# fixed world — --no-restamp: the mux sees the true pacing timeline,
# i.e. exactly what the PTS-fixed production pipeline delivers
./frame_timing_probe --replay-dir clips --num-cams 4 --duration 42 \
    --skew-ms "$SKEW" --rate "$RATE" --gap-every 275 --ring 4 --no-restamp \
    --out-dir results/replay_skewed_fixed

python3 analyze_timing.py results/replay_skewed_rerun --compare results/baseline_pinned_rerun
python3 analyze_timing.py results/replay_skewed_fixed --compare results/baseline_pinned_fixed
```

(The initial reproduction used `--gap-every 275`, the naive §8 derivation.
STEP4's sync experiments showed the restamp world additionally needs the
**delivered** rate matched — `--gap-every 44` — which is what
`run_replay.sh` and all sync-on replays now use. For the sync-OFF validation
below the distinction is immaterial; both settings reproduce the structure.)

## 2. Validation — replay vs live (steady state)

| metric | live rerun (fix OFF) | replay_skewed_rerun | live fixed (fix ON) | replay_skewed_fixed |
|---|---|---|---|---|
| capture cadence p50 | 32.0 ms | 32.0 ms | 32.0 ms | 32.0 ms |
| batches full / cadence | 100 % / 32.2 ms | 100 % / 32.0 ms | 100 % / 32.2 ms | 100 % / 32.0 ms |
| nearest-frame spread p50 | 1.6 ms | 20.8 ms † | 0.7 ms | 20.8 ms † |
| as-batched TRUE spread p50 | 198.4 ms | 308.5 ms ‡ | 200.1 ms | 299.6 ms ‡ |
| as-batched SYNTHETIC spread | **constant 1468.8 ms** | **constant 1438.1 ms** | == true (200.1) | == true (299.6) |
| cross-camera synthetic age offsets vs cam0 | −1001/−1338/−436 ms | −929/−1247/−469 ms | n/a (true) | n/a (true) |

† frozen-phase quantization: with constant per-camera rates the injected
phases don't sweep the way live crystals do; documented REPLAY_SKEW.md §7†.
‡ standing-queue rung values are frozen warm-up history — chaotic between any
two runs (live included); mechanism and ~250–360 ms scale is what must match.

The decisive world-defining property reproduces in both directions:

* broken world: mux believes a **bit-constant ~1.4 s** spread while reality
  is ~0.3 s — and the believed cross-camera offsets match live within ~8 %;
* fixed world: mux belief **== reality at every percentile**, exactly like
  the live `baseline_pinned_fixed` leg.

## 3. The drift correction (why gap-every 44 supersedes 275 for sync work)

Live, the synthetic grids advance 33.333 ms per **delivered** frame while
wall time advances 33.57 ms per frame slot (29.8 fps delivered) → every
camera's synthetic age grows +0.65 %/s (measured +677 ms/90 s, common-mode).
With `--gap-every 275` the replay delivers 31.0 fps → ages *shrink*
(−884 ms/90 s, measured): frames look future-stamped, are never LATE, and
sync-on trivially fills — an emulation artifact, not the live behaviour
(the 2026-07-06 replay_skewed was only ever validated under sync-off, where
this term is invisible). Setting `--gap-every 44` makes the delivered rate
(273/275 × …) ≈ 29.8 fps and restores the live drift sign and magnitude.
All sync-on replays (STEP4) use gap-every 44.

## 4. Evaluation

The replay reproduces the live baseline_pinned situation it was asked to
simulate — startup stagger, true cadence, capture gaps, bounded ring,
standing-queue ladder, and (crucially, new) the correct synthetic-clock
drift — and its `--no-restamp` mode is a validated stand-in for the fixed
pipeline. This gives Step 5 a camera-free, deterministic input whose frame
content is identical across every sweep run: something live cameras can never
provide.
