# Step-by-step: recording + end-to-end experiments

A hands-on guide to recording reproducible video and running an evaluation. Run
everything from the project root:

```bash
cd ~/Documents/deepstream_batch/multicam_perception_rt
```

---

## Step 0 — one-time prerequisites

- Cameras plugged in (for recording). For replay experiments you only need clips.
- Model built once: `ls models/*.engine` → `model_b4_gpu0_fp16.engine`. If missing:
  ```bash
  ./scripts/download_yolo11n.sh
  python3 scripts/build_engine.py
  ```
- Smoke test: `python3 main.py --config config/camera_params.yaml --log human --duration 5`
  (you should see `[cam0 …]` lines, then it stops).

---

## Step 1 — record reproducible clips

Live cameras give different frames every run, so you can't compare fairly. Record
once, then replay identical frames into every experiment.

```bash
# fixed length:
python3 scripts/record_replay_clips.py --duration 40 --out-dir experiments/clips

# stop manually (press ENTER, or Ctrl-C) — no fixed length:
python3 scripts/record_replay_clips.py --out-dir experiments/clips

# watch live 4-camera inference WHILE recording (raw is still what's saved):
python3 scripts/record_replay_clips.py --display
```

- Writes `experiments/clips/cam0.mp4 … cam3.mp4` — always the **raw** feed (no
  overlays baked in), so replaying + inferring fresh is meaningful.
- `--display` opens a tiled window showing detections in real time (inference runs
  in parallel, tee'd off the sources; it is *not* written into the files).
- **Stopping:** `--duration N` auto-stops after N s; otherwise press **ENTER**
  (or Ctrl-C). Either way the clips are cleanly finalized.
- **Verify:**
  ```bash
  ls -la experiments/clips/
  gst-discoverer-1.0 experiments/clips/cam0.mp4 | grep Duration
  ```
- **Tips:** record ~40 s so experiments have room. For a *tracking-under-motion*
  test, move around in view. To test **activity skipping**, point one camera at a
  blank wall so it's genuinely idle.

> Clips may differ slightly in length (USB bandwidth causes per-camera frame
> drops). Keep experiment `--duration` a few seconds under the shortest clip so
> all cameras have data the whole time.

---

## Step 2 — decide what you're testing (change ONE thing)

Knobs (all overridable on the CLI; defaults live in `config/camera_params.yaml`):

| Flag | Meaning |
|---|---|
| `--timeout-policy fixed\|adaptive` | batch wait: constant vs. shrinks with active-camera count (**works**) |
| `--context all\|activity\|scheduled` | camera selection: none / detection-driven / scripted timeline |
| `--timeout-us N` | base batch wait (µs), default 33333 (~1/30 s) |
| `--batch-policy fixed\|adaptive` | nvstreammux batch-size (adaptive is an experiment — measured NOT to help) |
| `--control-ms N` | how often controllers re-evaluate (default 500) |

For a **scheduled skip**, use a config with a schedule (`experiments/exp_scheduled.yaml`
skips 2 cameras at t=5 s, restores at t=12 s).

---

## Step 3 — run ONE experiment and collect metrics

```bash
python3 main.py \
  --config experiments/exp_scheduled.yaml \
  --source file --replay-dir experiments/clips \
  --context scheduled --timeout-policy adaptive \
  --log none \
  --metrics-csv experiments/results/run1.csv \
  --duration 25
```

- `--source file` replays the clips (reproducible) and paces to real time.
- `--metrics-csv` writes one row per batch (the raw data).
- `--duration 25` stops cleanly after 25 s (prints `[metrics] wrote N batches …`).

---

## Step 4 — analyze one run

```bash
python3 scripts/analyze.py experiments/results/run1.csv --warmup 4
```

Prints latency (mean/p50/**p99**/max), throughput (frames/s), batch fullness,
stability proxy, FES. **Always `--warmup 4`** — it drops the first 4 s (engine
load + pipeline fill), which otherwise dominates the tail.

---

## Step 5 — the A/B: compare configs automatically

`benchmark.py` runs several variants on the **same clips** and prints a comparison:

```bash
# E1 — does the timeout value matter? (sweeps 10/20/33/50 ms, all cameras)
python3 scripts/benchmark.py --experiment e1 \
  --source file --replay-dir experiments/clips --duration 20 --warmup 4

# policy — baseline vs skip vs skip+adaptive-timeout
python3 scripts/benchmark.py --experiment policy \
  --config experiments/exp_scheduled.yaml \
  --source file --replay-dir experiments/clips --duration 20 --warmup 4
```

CSVs land in `experiments/results/`.

---

## Step 6 — visualize the results

```bash
python3 scripts/plot_results.py experiments/results/*.csv \
  --warmup 4 --out experiments/results/plot.png
# optional nicer labels:
python3 scripts/plot_results.py \
  experiments/results/policy_baseline.csv experiments/results/policy_skip_adaptive.csv \
  --labels "baseline,skip+adaptive" --out experiments/results/policy.png
```

Produces a 4-panel PNG: (A) e2e latency percentiles per run, (B) throughput +
compute, (C) e2e over time, (D) batch fullness over time (skipping shows as 4→2).

---

## Step 7 — read it, and trust it (the discipline)

What "better" looks like, by column / panel:
- **`e2e p99` / `e2e max`** ↓ = lower tail latency (what a real-time system lives on).
- **`frames/s`** ↑ = throughput.
- **`compute mean`** ↓ = less GPU work (power/thermal).
- **`avg batch fullness`** shows skipping is active (4 → 2).

Make it trustworthy:
1. **Repeat each config ≥3×** — there is ~30 ms run-to-run noise (GPU scheduling,
   decode contention, thermal), even on identical replay input. If a difference is
   smaller than that spread, it's noise.
2. Always **`--warmup 4`**.
3. Use **`--source file`** for any comparison. Use **live** (`--source v4l2`, no
   `--replay-dir`) only to see real absolute latencies, never for A/B.
4. When studying the timeout, look at the **wait** (`e2e − compute`) — the
   low-noise signal (the compute noise cancels).
5. **Replay inflates absolutes ~3×** (4× H.264 decode load) vs live — relative
   comparisons transfer, absolute numbers don't.

---

## What we've already measured (so you don't re-derive it)

- **Skipping cameras halves compute** (~100→50 ms on replay) — a real compute/power
  win — but on the legacy mux it *adds* batch-wait (the batch never fills), so it's
  **not** a latency win by itself.
- **Adaptive timeout works:** it shrinks the skip wait (~26→16 ms in a controlled
  replay A/B), tracking the shortened timeout. It's the functioning latency lever.
- **Batch-size adaptation does NOT work** (negative result): the legacy nvstreammux
  pushes on "all pads delivered OR timeout", not on batch-size, so shrinking it
  never triggers an early push (isolated test: ~54 ms wait at bs=4, ~90 ms at bs=2).
  Leave `--batch-policy fixed`. A real fix needs the new mux or pad removal.
- **FES/accuracy:** with webcams there's no ground truth, so `analyze.py` uses a
  rough track-stability *proxy*. For real accuracy, replay a labelled dataset and
  pass `--accuracy <mAP>`. For most 2D questions, compare `e2e p99` / `frames/s`
  directly instead of FES.

---

## The whole flow, condensed

```bash
python3 scripts/record_replay_clips.py --duration 40                 # 1. record once
python3 scripts/benchmark.py --experiment policy \                   # 2. run the A/B
  --config experiments/exp_scheduled.yaml --source file --duration 20 --warmup 4
python3 scripts/plot_results.py experiments/results/policy_*.csv --out experiments/results/policy.png
# 3. read table + plot  →  4. repeat 3× to beat the noise  →  5. decide
```
