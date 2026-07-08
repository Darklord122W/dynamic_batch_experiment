# Campaign report — fixing the jpegparse timestamp destruction and re-measuring the world (2026-07-07/08)

**Rig:** Jetson AGX Orin 64 GB, JetPack 6.2.2 (L4T r36.5), DeepStream 7.1.0,
GStreamer 1.20.3, TensorRT 10.3, MODE_30W, 4× Logitech C920 (640×480@30 MJPG,
one USB-2 bus, `/dev/video0,2,4,6`). Detector YOLO11n FP16 (dynamic-batch
`models/model_b4_gpu0_fp16.engine` and static batch-4
`models/model_static_b4_gpu0_fp16.engine`), tracker NvSORT. All experiments
run the **C++ app / NEW nvstreammux** (`cpp/multicam_rt`) or the C++
instrument (`cpp/experiments/frame_timing/frame_timing_probe`).

**Context:** the 2026-07-06 report *Why the Batch Never Filled*
(`why_the_batch_never_filled_overleaf.zip`, repo root) proved that
`jpegparse` re-stamps each camera's PTS onto a synthetic 33.33 ms grid
anchored at that camera's own startup, so cross-camera timestamps disagree by
a constant 1.05–1.5 s and `sync-inputs=1` could never form a full batch at
any affordable window. This campaign fixes that at the source and re-measures
everything downstream of the fix.

**File map:** [INDEX.md](INDEX.md) lists every new experiment directory, its
file schema, and the code that produced it.

Per-step detail (design → commands → results → evaluation):

| step | doc | headline |
|---|---|---|
| 1 | [STEP1_jpegparse_pts_fix.md](STEP1_jpegparse_pts_fix.md) | PTS-restore fix, default ON; 100.00 % true stamps end-to-end; plus two companion finds (dead UVC pinning control; INI-vs-property ordering) |
| 2 | [STEP2_baseline_pinned_redo.md](STEP2_baseline_pinned_redo.md) | baseline_pinned reproduced (fix off) + proven clean (fix on); new +0.65 %/s synthetic drift discovered |
| 3 | [STEP3_replay_skewed_reproduction.md](STEP3_replay_skewed_reproduction.md) | replay_skewed re-derived from the fresh live run, both timestamp worlds validated |
| 4 | [STEP4_sync_after_fix.md](STEP4_sync_after_fix.md) | **sync-on now works**: 99.9 % kept, 100 % full, 2.1 ms true alignment (live); window map + timeout-property inertness |
| 5 | [STEP5_timeout_sweeps_engines.md](STEP5_timeout_sweeps_engines.md) | batched-push-timeout sweeps, static vs dynamic engine, sync-off and sync-on, batching + detection performance |

---

## The command log

Everything below ran from the repo root (or `cpp/experiments/frame_timing`
where shown), in this order. Retries for the known v4l2 `-5` reopen error
(15 s settle, up to 3 attempts) are omitted for readability but appear in the
step docs.

### 0. Build + environment

```bash
cd cpp && make && cd experiments/frame_timing && make
# clock lock (root; applied before Step 5's engine sweeps — earlier probe
# steps are fakesink-only and clock-insensitive):
sudo jetson_clocks        # GPU pinned 612 MHz (MODE_30W), CPU min=max
```

### 1. Fix verification (STEP1)

```bash
# UVC control discovery (found exposure_auto_priority does not exist on 5.15):
python3 - # VIDIOC_QUERYCTRL walk, see STEP1 §4

# 15 s live probe A/B (fix on):
./frame_timing_probe --out-dir <scratch>/ptsfix_on --duration 15 \
    --extra-controls "exposure_dynamic_framerate=0" --pts-fix
# -> premux PTS == capture PTS: 100.0 % on all 4 cams

# production app end-to-end sanity (12 s live, YOLO+tracker):
./cpp/multicam_rt --config config/camera_params.yaml --duration 12 \
    --log none --metrics-csv <scratch>/sanity_live.csv
# -> processed 1023/1023 arrived frames, pts-fix=ON
```

### 2. baseline_pinned re-do (STEP2) — 2 × 120 s live

```bash
cd cpp/experiments/frame_timing
./frame_timing_probe --devices /dev/video0,/dev/video2,/dev/video4,/dev/video6 \
    --duration 120 --extra-controls "exposure_dynamic_framerate=0" \
    --out-dir results/baseline_pinned_rerun
./frame_timing_probe --devices /dev/video0,/dev/video2,/dev/video4,/dev/video6 \
    --duration 120 --extra-controls "exposure_dynamic_framerate=0" --pts-fix \
    --out-dir results/baseline_pinned_fixed
python3 analyze_timing.py results/baseline_pinned_rerun --compare results/baseline_pinned
python3 analyze_timing.py results/baseline_pinned_fixed --compare results/baseline_pinned_rerun
```

Key outputs: rerun = 100 % full batches, true in-batch spread p50 198.4 ms,
synthetic constant 1468.8 ms, stagger 0/567.2/1134.8/1702.1 ms; fixed =
synthetic ≡ true (200.1 ms) — the fiction is gone.

### 3. replay_skewed reproduction (STEP3) — 2 × 42 s replay

```bash
SKEW="0,1134.8,1702.1,567.2"; RATE="0.96063,0.96099,0.96087,0.96128"
./frame_timing_probe --replay-dir clips --num-cams 4 --duration 42 \
    --skew-ms "$SKEW" --rate "$RATE" --gap-every 275 --ring 4 \
    --out-dir results/replay_skewed_rerun            # broken world (restamp ON)
./frame_timing_probe --replay-dir clips --num-cams 4 --duration 42 \
    --skew-ms "$SKEW" --rate "$RATE" --gap-every 275 --ring 4 --no-restamp \
    --out-dir results/replay_skewed_fixed            # fixed world
python3 analyze_timing.py results/replay_skewed_rerun --compare results/baseline_pinned_rerun
python3 analyze_timing.py results/replay_skewed_fixed --compare results/baseline_pinned_fixed
```

Validated: synthetic constant 1438.1 ms vs live 1468.8 (broken world);
synthetic ≡ true (fixed world). For **sync-on** replay work the delivered
rate must also match live → `--gap-every 44` (see STEP3 §3); `run_replay.sh`
now carries the corrected parameter set.

### 4. sync-on after the fix (STEP4)

```bash
# live A/B, 2 × 120 s:
./frame_timing_probe --devices ... --duration 120 \
    --extra-controls "exposure_dynamic_framerate=0" \
    --pts-fix --sync --max-latency-ms 33.333 --timeout-us 33333 \
    --out-dir results/sync_fixed_ml33
# same without --pts-fix -> results/sync_rerun_ml33

# parameter sweep on skewed replay (28 cells × 32 s, both worlds + 2-D grid):
python3 sync_replay_sweep.py --skew-ms "0,1134.8,1702.1,567.2" \
    --rate "0.96063,0.96099,0.96087,0.96128" --gap-every 44 --ring 4 \
    --grid --out results/sync_replay_sweep
```

Headlines: live fix ON = 99.9 % kept / 100.0 % full / true spread p50 2.1 ms;
live fix OFF = 14.7 % kept / 40.4 % full. Replay fixed world: fill ≥ 3.99 at
every window; discard 19 % (ml 2 ms) → 4.5 % (33.3) → 1.0 % (66.7) → 0.1 % (133).
Grid: `batched-push-timeout` is **inert** under sync — and (knob
characterization, 8 × 16 s app runs) inert on the new mux generally; the
INI's `overall-min-fps` is the real push-deadline knob.

### 5. Engine × sync timeout sweeps on skewed replay (STEP5)

```bash
SKEW="0,1134.8,1702.1,567.2"; RATE="0.96063,0.96099,0.96087,0.96128"
COMMON="--skew-ms $SKEW --rate $RATE --gap-every 44 --ring 4 --duration 45 --warmup 5"
# sync-OFF, both engines:
python3 scripts/timeout_sweep_cpp.py $COMMON --pgie config/_pgie_static.txt \
    --tag "fixed batch-4" --out experiments/results/timeout_sweep_cpp_static
python3 scripts/timeout_sweep_cpp.py $COMMON --pgie config/pgie_config.txt \
    --tag "dynamic batch" --out experiments/results/timeout_sweep_cpp_dynamic
# sync-ON (ml 33.333 ms), both engines:
python3 scripts/timeout_sweep_cpp.py $COMMON --pgie config/_pgie_static.txt \
    --tag "fixed batch-4" --sync --max-latency-ms 33.333 \
    --out experiments/results/timeout_sweep_cpp_static_sync
python3 scripts/timeout_sweep_cpp.py $COMMON --pgie config/pgie_config.txt \
    --tag "dynamic batch" --sync --max-latency-ms 33.333 \
    --out experiments/results/timeout_sweep_cpp_dynamic_sync
# bonus: sync-on max-latency mini-sweep at 33.3 ms push (dynamic engine):
for ML in 16 66.7 133; do python3 scripts/timeout_sweep_cpp.py $COMMON \
    --pgie config/pgie_config.txt --tag "dynamic batch" --sync \
    --max-latency-ms $ML --ms 33.3 \
    --out experiments/results/timeout_sweep_cpp_dynamic_sync_ml$ML; done
```

Swept values: batched-push-timeout ∈ {1, 5, 10, 20, 33.3, 50, 66.7, 100} ms —
**implemented as a per-run mux INI** (`overall-min-fps-n=1000000,
-d=<push_µs>`; `overall-max-fps = max(120, ⌈min-fps⌉)`) because an 8-run
characterization proved the property inert on the new mux. Detection
performance per run: per-frame JSON (`--log json`, keyed by camera_id +
buf_pts) + per-batch metrics CSV. Full analysis: STEP5; figures:
`timeout_sweep.png` + `detection_perf.png` in each
`experiments/results/timeout_sweep_cpp_*/` dir, cross-sweep overlay
`sweep_comparison.png` here.

Results in one paragraph: below one frame period the engines diverge
completely — the **dynamic engine** rides the tradeoff down to **e2e
15.3 ms mean at 117 single-frame invocations/s** (1 ms deadline, 100 %
coverage), while the **static batch-4 engine** pays full-batch cost per
invocation, saturates the GPU at ~40 invocations/s (compute 78–311 ms, e2e
up to 375 ms mean / 1.4 s p99) and, under sync-on at fast cycles, its
queueing delays push frames past the LATE cut so **sync erases 12–20 % of
the input** (dynamic: ≤ 2.2 %). At deadline ≥ 33.3 ms the engines are
indistinguishable (30 batches/s, fill 3.85→3.96, compute 30–35 ms) and
longer deadlines only add latency (e2e 66→232 ms for +0.10 frames of fill).
Sync-on costs ~one service cycle of e2e (102 vs 69 ms at 33.3 ms) at
≥ 99 % coverage; the ml mini-sweep confirms 66.7 ms as the
diminishing-returns window (99.6 % kept). Detection output is **invariant**
across all of it: ~100 % frame-matched agreement with the reference run,
dets/frame flat, track churn flat — batching policy chooses when pixels are
processed, never what is detected.

---

## Verdicts (campaign level)

1. **The fix works and costs nothing**: true kernel capture stamps survive to
   every downstream consumer (100.00 % exact), sync-off behaviour unchanged.
2. **sync-inputs=1 is rehabilitated**: from 85–94 % data loss and 2-camera
   batches to 99.9 % kept, 100 % full batches, and 2.1 ms median true
   alignment — better than RT-BEV's hardware-synchronized 39–46 ms reference.
   Recommended: `--sync --max-latency-ms 66.7`, cadence via INI min-fps.
3. **Knob truth on the new mux**: `batched-push-timeout` (property) does
   nothing; INI `overall-min-fps` is the push deadline; `overall-max-fps`
   bounds it; `max-latency` is the sync window. The historic sweeps' second
   axis was dead on this code path.
4. **Methodology repairs**: UVC pinning control renamed on kernel 5.15
   (`exposure_dynamic_framerate=0`); replay injection needs delivered-rate
   matching (`gap-every 44`) for sync fidelity; INI-vs-property ordering
   fixed in both apps.
5. For the project's per-camera-detection workload, **sync-off remains the
   production setting** (lossless, simpler); sync-on is now a real option for
   fusion work.
