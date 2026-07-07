# REPLAY_SKEW — simulating the live multi-camera timing situation from recorded video

How to replay the recorded clips (`clips/cam{0..3}.mp4`, copied from
`../../../experiments/clips/`) through the same NEW-nvstreammux pipeline and
**inject the timing imperfections of the live rig**, so that a recorded-video
experiment reproduces the sync-off `baseline_pinned` real-time situation —
startup stagger, standing-queue staleness, synthetic PTS fiction and all.

Everything here was validated against the live run: the numbers in
[§7 Validation](#7-validation--replay-vs-live) come from
`results/replay_skewed` (simulation) vs `results/baseline_pinned` (live
ground truth, 2026-07-06).

```bash
# the whole thing, one command (no cameras needed):
./run_replay.sh            # -> results/replay_skewed + results/replay_ideal
```

---

## 1. Why a naive replay is useless for timing experiments

Replaying four clips through `filesrc → qtdemux → h264parse → nvv4l2decoder →
nvstreammux` gives you a rig that never existed:

- all four streams start at PTS 0 at the same instant → **zero startup
  stagger**, so no standing queues, no staleness ladder;
- the container PTS grid is a perfect 33.33 ms for every camera with a
  **shared origin** → cameras look hardware-genlocked;
- nothing drops, nothing gaps, decode is faster than real time.

Measured (the `replay_ideal` ablation, produced by `run_replay.sh`): max time
difference among cameras = **0.0 ms in every batch**; staleness identical
across cameras. Compare the live rig: 209 ms median intra-batch spread and a
32→241 ms per-camera staleness ladder. Every conclusion drawn from a naive
replay would be wrong.

So the replay must *inject* the live imperfections. The C++ instrument
(`frame_timing_probe --replay-dir …`) does that with four mechanisms, each
mapped to a measured live phenomenon.

## 2. What "the live situation" actually is (measured, to be reproduced)

From the live `baseline_pinned` run (120 s, 4× C920, sync-inputs=0,
`results/baseline_pinned/figures/summary.md`):

| # | live phenomenon | measured value |
|---|---|---|
| P1 | true capture cadence is 32.026 ms, not 33.33 ms | Δt p50 32.0 ms all cams |
| P2 | sequential USB/UVC startup staggers the cameras | first frame at 0 / 568 / 1137 / 1217 ms (cam1 → cam0 → cam3 → cam2) |
| P3 | ~2-frame capture gaps, ~once per 2 s per camera | 51–55 kernel seq gaps / 117 s ≈ one per 70 frames; Δt p99 ≈ 100 ms |
| P4 | the v4l2 kernel ring bounds any backlog by DROPPING new frames | ring ≈ 4 buffers; live staleness capped ≈ 240 ms |
| P5 | jpegparse re-stamps PTS onto an ideal 33.33 ms grid anchored at each camera's own first frame, counting only delivered frames | constant cross-camera PTS offset 1050 ms |
| P6 | standing-queue staleness ladder (the consequence of P2+P4) | 32 / 172 / 239 / 241 ms per camera |
| P7 | free-running phases drift (crystals differ ~ppm) | fig03/fig06 |

P6 is **emergent** — it is not injected, it must *come out* of the simulation
when P1–P5 are injected correctly. That's the fidelity test.

## 3. The key idea: one camera = two timelines

A live camera branch carries two different notions of time and a faithful
replay must keep them separate:

- the **true timeline** — when frames really exist and arrive (32.026 ms
  cadence, staggered start, gaps, ring drops). This is what paces buffers
  into the mux and what `capture.csv` records;
- the **synthetic timeline** — the PTS jpegparse writes (ideal grid,
  per-camera origin, gaps erased). This is what the mux *sees* and what
  `premux.csv` / `NvDsFrameMeta.buf_pts` record.

Replay branch (compare the live branch above it):

```
LIVE:    v4l2src ──────────────► jpegparse ─ nvjpegdec ─ nvvideoconvert ─► mux sink_i
         [kernel ring drops]  ▲  [re-stamps PTS]                       ▲
                              P0 (true stamps)                         P1 (synthetic)

REPLAY:  filesrc ─ qtdemux ─ h264parse ─ nvv4l2decoder(+20 surfaces)
             ─ [skew probe: gaps, PTS·rate+skew] ─ identity sync=true
             ─ [ring queue: leaky, N=4] ─ nvvideoconvert ─► mux sink_i
                                        ▲ P0 probe (true/pace stamps)
                                        ▲ restamp probe (jpegparse emulation)
                                        ▲ then P1 on the mux pad (synthetic)
```

Stage by stage:

1. **skew probe** (decoder src pad) — builds the true timeline:
   - drops 2 consecutive frames every `--gap-every` N frames (P3), with a
     per-camera phase so cameras don't gap together;
   - rewrites `PTS' = PTS × rate + skew`: `rate` turns the clip's 33.33 ms
     grid into the camera's true 32.026 ms cadence (P1) and can differ per
     camera by ~ppm to model crystal drift (P7); `skew` is the startup
     stagger (P2);
   - lifts the segment stop (qtdemux bounds it at clip duration; skewed PTS
     would land outside and break pacing near EOS).
2. **`identity sync=true`** — the "camera": releases each buffer when
   pipeline running-time reaches its (skewed) PTS. Measured pacing error:
   0.09 ms p50, 0.23 ms p99.
3. **ring queue** (`leaky=upstream`, `--ring` N buffers) — the v4l2 kernel
   ring stand-in (P4): when the mux side backs up, it fills to N and then
   drops the NEWEST arrivals, so the pacer never blocks — exactly the
   drop-new semantics of `uvcvideo` when userspace can't dequeue.
4. **P0 probe + restamp probe** (nvvideoconvert src pad, in that order) —
   sits AFTER the ring, exactly where live P0 (v4l2src src pad) sits relative
   to the kernel ring. The P0 probe records the buffer's still-true PTS +
   arrival clocks; the restamp probe then rewrites PTS onto
   `first_pts + n × 33,333,333 ns`, counting only surviving frames —
   byte-faithful jpegparse emulation (P5), gaps and drops erased.
5. From the mux onward everything is identical to the live experiment,
   including probes P1/P2 and the CSV/analysis tooling.

### 3.1 Two hard-won correctness details (read before modifying)

- **The decoder pool must be bigger than the congestion backlog.**
  `nvv4l2decoder`'s default output pool (~5 surfaces) is smaller than what a
  congested mux holds downstream; without headroom the *pool*, not the ring,
  becomes the throttle — the pacer starves, and lateness accumulated during
  the stagger window freezes in permanently (measured: a constant 938 ms
  pacing error on the earliest camera, zero ring drops). We set
  `num-extra-surfaces=20`. If you enlarge `--ring` or the stagger a lot,
  scale it accordingly.
- **Probe order on a pad = attach order.** The P0 (capture) probe must be
  attached before the restamp probe on the same pad, or `capture.csv` records
  synthetic instead of true timestamps. `attach_probes()` runs before
  `attach_replay_probes()` for exactly this reason.

### 3.2 How the analysis pairs the two timelines

`analyze_timing.py` pairs each pre-mux (synthetic) row back to its capture
(true) row per camera by **grid index**: survivor k's synthetic PTS is
`first_capture_pts + k × 33,333,333 ns`, so
`k = round((syn_pts − first_capture_pts) / 33,333,333)` — robust even when
the ring dropped frames between the probes. (With `--no-restamp` PTS is
unchanged and exact-PTS matching is used; the script auto-detects.)

## 4. The knobs, and where each number comes from

| flag | phenomenon | value for `baseline_pinned` | how it was measured |
|---|---|---|---|
| `--skew-ms a,b,c,d` | P2 startup stagger | `568,0,1217,1137` | first kernel capture stamp per camera, `results/baseline_pinned/capture.csv` |
| `--rate r0..r3` | P1 true cadence (and P7 drift) | `0.9608` ×4 (= 32.026/33.333) | median capture Δt per camera |
| `--gap-every N` | P3 capture gaps | `70` | ~52 seq-gap events / 3650 frames per camera |
| `--ring N` | P4 kernel ring | `4` (default) | order of the v4l2 mmap ring; tunes the staleness cap ≈ (ring + mux queue) × period |
| `--no-restamp` | disable P5 (ablation) | off | — |
| `--sync`, `--timeout-us`, `--mux-config` | same as live mode | defaults | — |

To simulate a *different* live run, re-measure the first two columns from its
`capture.csv` (the two python one-liners are in `run_replay.sh`'s comments and
§8).

## 5. Exact commands

```bash
cd cpp/experiments/frame_timing
make

# clips were copied from ../../../experiments/clips (45.7–75.3 s, 640x480@30 H264);
# replay length is bounded by the shortest clip -> 42 s is safe.

# closest simulation of live sync-off baseline_pinned:
./frame_timing_probe --replay-dir clips --num-cams 4 --duration 42 \
    --skew-ms "568,0,1217,1137" --rate "0.9608,0.9608,0.9608,0.9608" \
    --gap-every 70 --ring 4 \
    --out-dir results/replay_skewed

# ablation — what a naive replay pretends the world looks like:
./frame_timing_probe --replay-dir clips --num-cams 4 --duration 42 \
    --out-dir results/replay_ideal

# figures + summary (same tooling as the live experiment), with an
# A/B figure against the live run:
python3 analyze_timing.py results/replay_skewed --compare results/baseline_pinned
python3 analyze_timing.py results/replay_ideal
```

Or all of the above: `./run_replay.sh [duration] [results_dir]`.

## 6. What comes out

The same artifacts as a live run — `capture.csv`, `premux.csv`,
`batches.csv`, `batch_frames.csv`, `meta.json` (now also recording
`skew_ms/rate/gap_every/ring/restamp`), and the same 10 figures + summary,
plus `fig11_compare_baseline_pinned.png` contrasting simulation with ground
truth.

## 7. Validation — replay vs live

Steady-state numbers, `replay_skewed` (42 s) vs `baseline_pinned` (live,
120 s) vs `replay_ideal`:

| metric | live (ground truth) | replay_skewed | replay_ideal (naive) |
|---|---|---|---|
| capture cadence p50 | 32.0 ms | 32.0 ms | 33.3 ms |
| batch cadence / full batches | 32.5 ms / 100 % | 32.0 ms / 100 % | 33.3 ms / 100 % |
| nearest-frame spread p50 (fig05a) | 8.9 ms | 16.0 ms † | **0.0 ms** |
| intra-batch TRUE spread p50 (fig05b) | 208.9 ms | 288.2 ms | **0.0 ms** |
| synthetic-PTS spread (fig05c) | constant 1050 ms | constant 1117 ms | **0.0 ms** |
| staleness ladder (fig08) | 32 / 172 / 239 / 241 ms | 9 / 89 / 241 / 297 ms | flat |
| capture→mux-door latency ladder | 27 / 39 / 109 / 110 ms | 0.1 / 0.1 / 49 / 105 ms | flat |
| frames dropped pre-mux | kernel-side (seq gaps) | ring drops: 8 + 26 on the two early cams | 0 |
| pacer accuracy | (is a real camera) | 0.09 ms p50 / 0.23 ms p99 | 0.09 ms |

The structure matches: a staleness *ladder ordered by startup order* with the
last-started camera fresh and ~250–300 ms ceiling, ~9× worse as-batched
spread than physics requires, and a constant synthetic-PTS fiction of the
right magnitude. The exact rung values differ from the live run **and would
differ between two live runs too** — they are frozen warm-up history (which
partial batches fired while cameras were still joining), i.e. chaotic in both
worlds. Match the *mechanism and scale*, don't chase the third digit.

† 16.0 vs 8.9: with identical `--rate` values the injected phases are
*constant* (their value set by `skew mod 32 ms`), while live phases drift
through all values. Give the cameras slightly different rates (e.g.
`--rate "0.96081,0.96079,0.96082,0.96080"`) to reproduce the drifting-phase
behaviour of fig03/fig06 and de-quantize fig05a.

## 8. Re-deriving injection parameters from any live run

```python
import pandas as pd
cap = pd.read_csv("results/<live_run>/capture.csv")
first = cap.groupby("cam").pts_ns.min() / 1e6
print("skew-ms:", (first - first.min()).round(1).to_dict())
per = cap.sort_values("pts_ns").groupby("cam").pts_ns.diff().median() / 1e6
print("rate:", round(per / (1000/30), 4))     # true period / clip period
g = cap.sort_values(["cam","pts_ns"]).groupby("cam").pts_ns.diff()
print("gap-every ~", int(len(cap) / max((g > 60e6).sum(), 1)))
```

## 9. Fidelity limits — what this simulation does NOT reproduce

Be honest about these when drawing conclusions:

- **Decode latency profile.** Live: MJPEG → nvjpegdec (~20–25 ms floor).
  Replay: H.264 → nvv4l2decoder ahead of the pacer, so capture→mux latency
  for uncongested cameras is ~0.1 ms instead of ~27 ms. A constant per-camera
  latency doesn't change inter-camera *differences*, but absolute
  end-to-end-latency numbers from replay are optimistic by one decode.
- **USB bus contention** (four cameras sharing one USB-2 bus) and its
  correlated arrival jitter; the ~150 ms all-camera stalls in live fig01.
- **Auto-exposure frame-rate changes** (the live `baseline` run's 15→30 fps
  scene dependence). You can approximate a constant dim-light case with
  `--rate 2.08` (66 ms cadence) but not the dynamics.
- **Kernel-timestamp noise**: live capture stamps carry USB-transfer and
  IRQ-scheduling noise (~ms); replay pace stamps are clean (0.1 ms).
- **Content-time mismatch**: the clips' pixel content was recorded unskewed —
  we shift frame *timing*, not the scene. For detection-accuracy experiments
  the injected 1.2 s stagger means camera i's pixels genuinely lag, which is
  the point, but cross-camera *content* correspondence differs from a rig
  that was born skewed.

## 10. Where to take it next

- **Sync-on replay**: add `--sync --max-latency-ms 33.333` to the skewed
  replay to reproduce the live sync-inputs disaster deterministically — the
  synthetic PTS origins (= your `--skew-ms` values, mod alignment window)
  decide which cameras can ever co-batch. With `568,0,1217,1137`, cam3/cam2
  origins differ by 80 ms and everything else by ≥500 ms. Now you can *design*
  the stagger to make sync succeed or fail on purpose.
- **Startup-flush fix prototyping**: the standing-queue skew reproduces here
  without cameras, so a fix (e.g. flushing mux input queues once all sources
  are live) can be developed and measured entirely in replay, then confirmed
  live.
- **`--no-restamp` ablation**: hand the mux the TRUE capture timeline as PTS
  and measure what sync-inputs *could* do if jpegparse didn't destroy the
  timestamps.
