# frame_timing — when do camera frames *really* arrive, and what does nvstreammux do about it?

A self-contained C++ experiment that instruments the exact per-camera capture
front-end of the production app (`../../src/pipeline_builder.cpp`) and answers,
with data:

1. **When does each camera's frame arrive, in world time?**
2. **What is the frame's capture time (PTS) mapped into the world clock — and
   how far is that from its arrival?**
3. **What are the time differences among the 4 cameras right before
   nvstreammux?**
4. **What do those discrepancies do to the batches DeepStream actually
   processes?**

It also reproduces **Figure 5 of RT-BEV** (Liu et al., RTSS 2024, "Time
difference on synced nuScenes dataset") for this rig: the scatter of the
maximum capture-time difference among cameras per synchronized sample. RT-BEV
measures 39–46 ms on nuScenes' *hardware-triggered* cameras; this experiment
shows what the same plot looks like for free-running USB webcams — and, more
importantly, what the number is for the batches nvstreammux actually forms.

**TL;DR of the measured findings is at the bottom ([Findings](#findings)).**

---

## 1. What is being measured, exactly

### 1.1 The pipeline under test

```
          P0 (capture)              P1 (pre-mux)                P2 (post-mux)
          v4l2src src pad           mux sink_<i> pad            mux src pad
              │                         │                           │
 C920 #i ─ v4l2src ─ caps(MJPG WxH@30) ─ jpegparse ─ nvjpegdec ─ nvvideoconvert
              ─ caps(NVMM,NV12) ──►  nvstreammux (NEW, batch=4)  ──►  fakesink
```

This is byte-for-byte the same element chain the production C++ app builds for
a live camera, with two deliberate differences:

- **`fakesink` replaces nvinfer/nvtracker/etc.** Anything downstream of the
  mux exerts backpressure that would change *when* the mux can push batches —
  and this experiment is about what happens *before and inside* the mux. With
  a free-running sink, the mux's behaviour is driven purely by input timing,
  which is the variable under study.
- **Three pad probes** record timestamps (they are read-only taps and add
  ~1 µs each — two `clock_gettime` calls and a vector append under an
  uncontended mutex; no file I/O happens until teardown).

### 1.2 The three probe points

| probe | pad | what a record means |
|---|---|---|
| **P0** | `v4l2src` src pad | the frame's first appearance in userspace. Its **PTS is the kernel capture stamp** (see §2). Its `seq` is the V4L2 sequence number — gaps here mean the *kernel* dropped frames. |
| **P1** | `nvstreammux` `sink_<i>` pad | the frame knocking on the mux's door — after MJPEG parse, HW JPEG decode, and the NVMM copy. This is "right before DeepStream". |
| **P2** | `nvstreammux` src pad | a batch leaving the mux. `NvDsBatchMeta` is walked to record exactly which camera-frames the batch contains. |

At every probe we sample `CLOCK_MONOTONIC` **and** `CLOCK_REALTIME`
back-to-back, and record the buffer's PTS. That's the whole trick — everything
else is arithmetic.

### 1.3 Clock domains, and how PTS becomes world time

Four clocks appear in this experiment; conflating any two of them produces
garbage numbers, so here is the map:

| clock | who stamps it | where it appears |
|---|---|---|
| **kernel monotonic** (`CLOCK_MONOTONIC`) | `uvcvideo` driver, at the moment the last USB payload of a frame arrives | the V4L2 buffer timestamp |
| **pipeline running time** | GStreamer: `absolute_time − base_time` | every buffer PTS |
| **`CLOCK_MONOTONIC` (userspace)** | our probes | `mono_ns` columns |
| **`CLOCK_REALTIME` (world)** | our probes | `real_ns` columns |

The pipeline clock is the **monotonic** `GstSystemClock` (verified and recorded
in `meta.json` — `pipeline_clock_type`), so for any pipeline timestamp `T`:

```
monotonic instant  =  T + base_time_ns
world instant      =  T + base_time_ns + (CLOCK_REALTIME − CLOCK_MONOTONIC)
```

- `base_time_ns` is captured from the pipeline after PLAYING (it is constant
  for the whole run) and stored in `meta.json`.
- The `REALTIME − MONOTONIC` offset is measured by *bracketing* (a REALTIME
  sample sandwiched between two MONOTONIC samples, 9 attempts, tightest
  bracket wins — uncertainty is the bracket width, sub-µs in practice). It is
  measured at run start **and** run end and both are stored, so an NTP step
  during the run is detectable (`fig10` reports the drift; on this rig it is
  ~0 µs over 2 minutes).

### 1.4 Where each timestamp is born (provenance chain)

1. The C920 exposes a frame and streams it as MJPEG USB payloads.
2. `uvcvideo` completes the frame at the last URB and stamps the V4L2 buffer
   with `CLOCK_MONOTONIC` **at that moment**. This is the closest thing to a
   "capture time" a USB webcam gives you (it is end-of-transfer, i.e. already
   ~one frame-transmission after end-of-exposure, but it is *consistent across
   cameras*, which is what matters for difference measurements).
3. `v4l2src` dequeues the buffer (typically 2–4 ms later — measured in
   `fig10`) and converts the kernel stamp to running time → **P0's PTS is the
   kernel capture stamp**, not the dequeue time.
4. **`jpegparse` throws that away.** Measured on this rig (DS 7.1 /
   GStreamer 1.20): jpegparse re-stamps every output buffer onto an ideal
   `first_pts + n/framerate` grid. Verified by probing the same buffer before
   and after jpegparse: input PTS advance irregularly (real cadence), output
   PTS advance by exactly 33,333,333 ns. Two consequences:
   - Everything downstream of jpegparse — **including nvstreammux and
     `NvDsFrameMeta.buf_pts`** — sees a *synthetic, perfectly smooth* clock
     whose zero point is "whenever this camera's first frame appeared".
   - Real capture timing survives only in P0's records. This experiment
     matches P0↔P1↔P2 rows (per camera, in FIFO order for P0↔P1; by the
     synthetic PTS, which is unique per camera and preserved bit-exact from
     jpegparse through the mux, for P1↔P2) so every batched frame can be traced
     back to its true kernel capture stamp.

   (Update 2026-07-07: this is the *stock DeepStream* behaviour. The
   production pipeline now restores the true capture PTS across jpegparse —
   probes on the jpegparse sink/src pads in `../../src/pipeline_builder.cpp`
   re-apply the kernel capture stamp; ON by default in `cpp/multicam_rt`,
   `--no-pts-fix` to disable; this instrument has the same fix behind
   `--pts-fix`. Verified live 2026-07-07: with the fix, premux PTS == kernel
   capture PTS 100.00 % on all 4 cameras, and `NvDsFrameMeta.buf_pts` equals
   the true capture stamp 13940/13940.)
5. The mux also stamps `NvDsFrameMeta.ntp_timestamp` with system **realtime**
   when the frame reaches it (`attach-sys-ts` default). It's an *arrival*
   stamp, not a capture stamp — recorded for cross-checking, not used for the
   analysis.

### 1.5 What the probes cost

Per frame: two `clock_gettime(2)` vDSO calls, one 40-byte struct append into a
pre-reserved `std::vector` under a mutex whose only contender runs at ≤120 Hz
total. No allocation (capacity is reserved up front), no I/O (CSVs are written
after the pipeline reaches NULL and all streaming threads are dead). The
measurement does not disturb the measured system in any way we could detect
(batch cadence with and without probes is identical to within noise).

---

## 2. The instrument: `frame_timing_probe`

```
make                                   # builds ./frame_timing_probe
./frame_timing_probe --help
```

Key options (all defaults match the production app's camera config —
`config/camera_params.yaml`: 4× C920 at `/dev/video0,2,4,6`, 640×480 MJPG
30 fps, nvjpegdec):

| flag | meaning |
|---|---|
| `--out-dir DIR` | (required) where the CSVs + `meta.json` go |
| `--duration S` | capture length; 0 = run until Ctrl-C |
| `--sync` | `sync-inputs=1` on the mux (+ `--max-latency-ms`, default 33.333) |
| `--timeout-us` | mux `batched-push-timeout` (default 33333) |
| `--extra-controls "k=v,…"` | UVC controls applied to every camera, e.g. `exposure_dynamic_framerate=0` (the working fps-pinning control on kernel 5.15 — `exposure_auto_priority` no longer exists there and is a silent no-op; §5.1) |
| `--pts-fix` | live mode: restore true kernel capture PTS around jpegparse (mirrors the production app's 2026-07-07 fix, where it is ON by default). Off = historic instrument behaviour: the mux sees jpegparse's synthetic grids |
| `--devices a,b,…` / `--width/--height/--fps` / `--decoder` / `--mux-config` | pipeline shape |

The `USE_NEW_NVSTREAMMUX=yes` env var is set automatically (before
`gst_init`), and the tool *verifies* the new mux actually loaded, same as the
main app.

### Output schema

| file | columns | one row per |
|---|---|---|
| `capture.csv` | `cam,seq,pts_ns,mono_ns,real_ns` | frame at P0 |
| `premux.csv` | `cam,pts_ns,mono_ns,real_ns` | frame at P1 |
| `batches.csv` | `batch_idx,batch_pts_ns,mono_ns,real_ns,n_frames` | batch at P2 |
| `batch_frames.csv` | `batch_idx,source_id,frame_num,buf_pts_ns,ntp_ns` | frame *inside* a batch |
| `meta.json` | run parameters, `base_time_ns`, `pipeline_clock_type`, start/end `REALTIME−MONOTONIC` offsets + brackets | run |

---

## 3. Reproduction — the whole experiment in one command

```bash
cd cpp/experiments/frame_timing
./run_experiment.sh            # 120 s per run; or: ./run_experiment.sh 60
```

This builds, checks the 4 cameras are idle, and performs **three 120-second
runs** (3 s settle time between them):

| run | capture settings | mux settings | why it exists |
|---|---|---|---|
| `baseline` | production-identical (auto-exposure free) | `sync-inputs=0` | what the real app experiences |
| `baseline_pinned` | `exposure_auto_priority=0` — *intended* to hold 30 fps in any light, but a silent no-op on kernel 5.15 (errata 2026-07-07, §5.1): the working control is `exposure_dynamic_framerate=0` | `sync-inputs=0` | isolates *scheduling* discrepancies from *exposure* discrepancies |
| `sync_pinned` | `exposure_auto_priority=0` (same no-op caveat — §5.1) | `sync-inputs=1`, `max-latency=33.333 ms` | what "just turn on time alignment" costs |

then runs `analyze_timing.py` on each (plus two A/B comparison figures). All
statistics use only the **steady-state window** (2 s after first frame to
0.5 s before EOS) so pipeline warm-up doesn't contaminate them.

Requirements beyond the repo's own: none for the C++ tool (gstreamer dev +
DeepStream, same as `../../Makefile`); `python3 -m pip install pandas
matplotlib numpy` for the analysis (already present on this Jetson).
The C++ instrument does all measurement; Python only draws pictures from the
CSVs.

To analyse a single run manually:

```bash
python3 analyze_timing.py results/baseline_pinned [--compare results/sync_pinned]
```

### Reproducing individual claims

- *"jpegparse re-stamps PTS"*: probe the same stream before/after jpegparse —
  input PTS irregular, output PTS on a perfect 33.33 ms grid. (One-liner GST
  pipeline with two `identity` elements; or just compare `capture.csv` pts
  deltas with `premux.csv` pts deltas from any run.)
- *"the pipeline clock is monotonic"*: `meta.json → pipeline_clock_type`
  (`GstSystemClock`; GStreamer's default clock type is `GST_CLOCK_TYPE_MONOTONIC`),
  and `fig10`'s left panel would show a growing offset if it weren't.
- *"kernel stamps are trustworthy"*: `fig10` left panel — P0 probe time minus
  kernel stamp is a flat few ms (dequeue delay), not drifting.

---

## 4. Reading the figures (`<run>/figures/`)

| figure | question it answers | what to look for |
|---|---|---|
| `fig01_arrival_raster` | when does every frame arrive at the mux door (P1, world clock)? | staggered start of the 4 cameras; ticks never forming vertical columns (free-running); occasional all-camera gaps = system stalls |
| `fig02_interframe_jitter` | how regular is each camera's true capture cadence? | ~32 ms median; **steps to 66/100 ms in the `baseline` run = auto-exposure halving the frame rate in dim light** |
| `fig03_phase_drift` | do the cameras hold a fixed relative phase? | no — phase (capture time mod one period) sweeps continuously; each camera's crystal runs at a slightly different rate |
| `fig04_latency` | how long from capture to the mux door? | per-camera medians (decode+convert+queueing); tails = decode contention |
| `fig05_rtbev` | **the RT-BEV Fig. 5 replica.** (a) nearest-frame sample sets on true capture stamps — directly comparable to the paper; (b) the same measure for the batches nvstreammux *actually formed*; (c) the same batches measured with the synthetic PTS the mux *believes* | (a) is bounded by ~1 frame period; (b) is far worse than (a) — batching ≠ synchronizing; (c) is a constant fiction |
| `fig06_pairwise_offsets` | how far is camera *i*'s nearest capture from camera 0's? | sawtooth sweeps through ±half a period and wraps: beat frequency between free-running crystals; histogram ~uniform — there is **no** "typical" offset |
| `fig07_batch_composition` | what did the mux assemble? | batch-size histogram (red = partial); ✕ marks = batches pushed without a given camera |
| `fig08_staleness` | how old is each camera's pixel data when its batch leaves the mux? | **the headline defect**: per-camera CDFs separated by ~140 ms — the standing-queue skew (§5.3) |
| `fig09_spread_cdf` | distribution view of fig05's three definitions | gap between "nearest-frame" and "as-batched" curves = staleness the mux adds on top of physics |
| `fig10_clock_check` | can we trust the measurement? | flat small dequeue delay; µs-level REALTIME−MONOTONIC drift |
| `fig11_compare_*` | A/B between two runs | arrived-vs-batched per camera (throughput cost) and spread CDFs (alignment benefit) |

`summary.md` in the same folder holds every headline number as tables.

---

## 5. Findings

Measured 2026-07-06, 21:40–21:55 local (dim indoor lighting — which turned out
to matter), JetPack 6 / DS 7.1, 4× C920 on one USB-2 bus, 640×480 MJPG @
30 fps nominal, 120 s per run, steady-state statistics. All numbers below are
from `results/*/figures/summary.md`, regenerable with `./run_experiment.sh`.

### 5.1 The cameras are not "30 fps cameras". They are "up to 30 fps" cameras.

Auto-exposure trades frame rate for exposure time, per camera, per scene. In a
dim-scene validation run the production-identical pipeline delivered a flat
**~15 fps (66 ms intervals)** on all four cameras; in the 120 s `baseline` run
the median interval held 32.0 ms but **Δt p99 was 100–108 ms with 67–80
kernel-sequence gaps per camera** (the camera silently skipping exposure
slots). With `exposure_auto_priority=0` (the `*_pinned` runs) the results
split (errata 2026-07-07: "1–17 gaps per camera, p99 tightens to ~36 ms"
matches only `sync_pinned`; `baseline_pinned` actually had 51–55 gaps per
camera and Δt p99 100–108 ms — the original sentence wrongly credited both
runs). The reason is now known: **`exposure_auto_priority` does not exist on
this kernel (5.15)** — the control was renamed, setting the old name is a
silent no-op, so the historic `*_pinned` runs never actually applied any
pinning. The working control is `exposure_dynamic_framerate=0` (verified: it
pins the 32.0 ms cadence in dim light where the old name lets the cameras
fall to ~15 fps); with it, the 2026-07-07 rerun measured 9–10 gaps per
camera and p99 36 ms.
Consequence: in the wild, the "frame period" is scene-dependent, per-camera,
and can silently double.

### 5.2 Free-running means constantly-changing relative timing (`fig03`, `fig06`)

There is no hardware trigger and the four crystals run at slightly different
rates: each camera's capture phase slides continuously relative to the others
and wraps (`fig06`; over minutes, every possible misalignment occurs). The
RT-BEV-style nearest-frame time difference (`fig05a`) is bounded by roughly
one frame period: **p50 8–10 ms, p90 13–30 ms, p99 ~45 ms, max 69–82 ms**
across the three runs. Compare RT-BEV's hardware-synced nuScenes: a *constant*
~42 ms. Free-running USB cameras are better on median and worse in the tail —
and above all not constant, which is what breaks fixed-offset assumptions.

### 5.3 nvstreammux batching ≠ synchronization: the standing-queue skew (`fig05b`, `fig08`)

The baseline mux batches "one frame per source, head of each source's queue".
The cameras start streaming ~1.5–1.7 s apart (USB/UVC negotiation is
sequential), so early-started cameras accumulate frames in their per-source
queues, and once all cameras are live the mux forever pairs the **freshest
frame of the last-started camera with queue-aged frames of the others**.
Measured steady-state staleness at batch push (`baseline_pinned`): **32 / 172
/ 239 / 241 ms per camera** — a **~209 ms median true capture-time spread
inside every single batch** (p99 281 ms), for frames that individually arrived
at a healthy 30 fps. The backpressure of those full queues is also visible one
element upstream as capture→mux-door latency (27 ms for the freshest camera vs
~110 ms for the stalest). 100 % of batches were "full" — the defect is
invisible in any frames-per-second metric, and invisible in timestamps because
of §5.4.

### 5.4 The timestamps DeepStream sees are fiction (`fig05c`)

`jpegparse` re-stamps frames onto an ideal 33.33 ms grid whose origin is each
camera's own start instant (verified by probing the same buffers before/after:
input PTS irregular, output PTS exactly 33,333,333 ns apart). So nvstreammux,
`buf_pts`, and every downstream consumer see four perfectly regular cameras
whose mutual offset is a *constant* — measured **1468.5 ms (`baseline`) and
1050.1 ms (`baseline_pinned`)**, i.e. the camera start-time stagger — instead
of the true, changing capture skew. Corollaries:

- Nothing downstream can detect §5.3; the evidence is destroyed one element
  upstream of the mux.
- `sync-inputs=1` aligns on this fiction (§5.5).

(Update 2026-07-07: this section describes stock-DS behaviour — the pipeline
now restores the true capture PTS across jpegparse (production default ON in
`cpp/multicam_rt`, `--no-pts-fix` to disable; `--pts-fix` in this
instrument). Verified 2026-07-07: premux PTS == kernel capture PTS 100 % on
all 4 cameras; `buf_pts` carried the true capture stamp 13940/13940.)

### 5.5 `sync-inputs=1` pays 93 % of the data for alignment the physics gave for free (`fig11`)

In `sync_pinned` (max-latency = 1 frame), **14,029 frames arrived at the mux
and 917 left it — 93.5 % silently discarded**, batch cadence collapsed from
32.5 ms to 201 ms, and only 15.7 % of surviving batches were full. The
survivors *are* genuinely aligned (true-capture spread p50 4.5 ms), but the
membership explains the mechanism: 255 of 399 batches are exactly the pair
{cam 0, cam 3} — the two cameras whose *synthetic* PTS grids (§5.4) happened
to land within the 33 ms window; cam 1 and cam 2 survived almost never (71 and
96 frames of ~3,500 each). Which cameras win is decided by startup stagger,
i.e. by luck. This reproduces, and finally *explains*, the earlier verdicts
("sync dropped ~62 % for zero accuracy gain" in `../../README.md` — errata
2026-07-07: that ~62 % figure is errata-flagged; it was measured under
different settings and is not directly comparable to the 93.5 % discard
here, which is `sync_pinned` at max-latency 33 ms — and "sync never batches
more than 2 of the 4 C920s" in the Python-side experiments).

### 5.6 What this means for the perception pipeline

- For **independent per-camera detection** (this project's mode): per-frame
  discrepancies are harmless, but the standing-queue skew adds a hidden,
  camera-dependent 100–200 ms of latency to most cameras' data. If end-to-end
  latency matters, that skew is the thing to fix — e.g. flush/cap the mux
  input queues once all sources are live — not sync-inputs.
- For a future **fusion/BEV stage**: `fig05a` shows nearest-frame matching on
  *true* capture stamps would give ~±10 ms typical alignment with zero frame
  loss — strictly better than what sync-inputs achieved at 93 % loss. But it
  requires true capture stamps to survive to the fusion point: carry the
  kernel timestamp (P0 PTS) in metadata from a probe *upstream of jpegparse*,
  and never trust `buf_pts`. (Update 2026-07-07: now addressed — the
  PTS-restore fix in `../../src/pipeline_builder.cpp` (default ON) makes
  `buf_pts` the true capture stamp, verified 13940/13940, so `buf_pts` is
  trustworthy whenever the fix is enabled.)

---

## 6. The 2026-07-07 campaign: the fix, and the post-fix world

Everything in §5 describes the **stock DS 7.1 pipeline**. On 2026-07-07 the
timestamp destruction was fixed (a PTS-restore probe pair around each
jpegparse; default ON in the production app `cpp/multicam_rt`, `--pts-fix`
here) and the headline experiments were re-run. Step-by-step docs with every
command live in `../../../experiments/results/campaign_2026-07-07_ptsfix/`;
the short version:

| run (120 s live unless noted) | what it shows |
|---|---|
| `results/baseline_pinned_rerun` | §5 reproduced under the corrected pinning control (fix OFF): 100 % full batches, ~198 ms standing-queue spread, synthetic fiction constant 1468.8 ms — **plus** a newly measured +0.65 %/s common-mode synthetic-age drift (grids advance 33.33 ms per delivered frame vs 29.8 fps delivered) |
| `results/baseline_pinned_fixed` | fix ON: mux belief == reality at every percentile (premux PTS == kernel PTS 100.00 %; buf_pts true 13 940/13 940); sync-off behaviour unchanged |
| `results/sync_rerun_ml33` | sync-on (ml 33.3 ms), fix OFF: the disaster, fresh — 14.6 % kept, 40.0 % full, 200 ms cadence |
| `results/sync_fixed_ml33` | **sync-on (ml 33.3 ms), fix ON: 99.9 % kept, 100.0 % full batches, true in-batch spread p50 2.1 ms** (RT-BEV hw-synced reference: 39–46 ms); the standing-queue ladder is flushed; staleness/cadence are set by the INI `overall-min-fps` service cycle (no INI here → default min-fps 5 → 200 ms bursts) |
| `results/replay_skewed_rerun` / `_fixed` (42 s replay) | both timestamp worlds reproduced from re-derived injection parameters (skew 0/1134.8/1702.1/567.2 ms, per-camera rates, gap-every 44) |
| `results/sync_replay_sweep` (28 × 32 s replay) | sync_sweep + sync_grid re-run on skewed replay, both worlds: the fixed world fills (4.00) at **every** window ≥ 2 ms, discard 19 % → 0.1 % from ml 2 → 133 ms; `batched-push-timeout` proven inert under sync — and, per the app-side knob characterization, inert on the new mux generally (the INI `overall-min-fps` is the real push-deadline knob) |

## 7. Layout

```
frame_timing/
├── frame_timing_probe.cpp   # the C++ instrument (pipeline + probes + CSVs);
│                            # live v4l2 mode AND replay mode (--replay-dir);
│                            # --pts-fix = the production app's jpegparse fix
├── Makefile                 # make → ./frame_timing_probe
├── run_experiment.sh        # the whole LIVE experiment, one command
├── run_replay.sh            # the RECORDED-VIDEO experiment (no cameras);
│                            # injection params re-derived 2026-07-07
├── sync_replay_sweep.py     # sync_sweep + sync_grid on skewed replay,
│                            # both timestamp worlds (broken vs fixed)
├── REPLAY_SKEW.md           # how recorded clips + injected skew reproduce
│                            # the live real-time situation — read this before
│                            # trusting any replay-based timing result
├── clips/cam{0..3}.mp4      # replay clips (copied from experiments/clips)
├── analyze_timing.py        # CSVs → 11 figures + summary.md
├── README.md                # this file
└── results/<run>/           # capture.csv premux.csv batches.csv
                             # batch_frames.csv meta.json figures/
```
