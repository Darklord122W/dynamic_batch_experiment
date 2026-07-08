# Step 5 — push-deadline sweeps on the skewed replay: engines × sync modes

**Date:** 2026-07-08 (clocks locked: `jetson_clocks`, GPU pinned 612 MHz,
MODE_30W) · **App:** `cpp/multicam_rt` (NEW mux, PTS fix ON) · **Input:**
identical for every run — 4-clip replay with the reproduced baseline_pinned
skew (STEP3): `--skew-ms 0,1134.8,1702.1,567.2
--rate 0.96063,0.96099,0.96087,0.96128 --gap-every 44 --ring 4`, 45 s/run,
5 s warmup after the first batch.

## 1. Design

`scripts/timeout_sweep_cpp.py` sweeps the incomplete-batch push deadline
{1, 5, 10, 20, 33.3, 50, 66.7, 100} ms across four configurations:

| sweep | engine | sync | out dir |
|---|---|---|---|
| 1 | static batch-4 (`config/_pgie_static.txt` → `model_static_b4_gpu0_fp16.engine`) | off | `experiments/results/timeout_sweep_cpp_static` |
| 2 | dynamic 1–4 (`config/pgie_config.txt` → `model_b4_gpu0_fp16.engine`) | off | `.../timeout_sweep_cpp_dynamic` |
| 3 | static batch-4 | **on** (ml 33.333 ms) | `.../timeout_sweep_cpp_static_sync` |
| 4 | dynamic 1–4 | **on** (ml 33.333 ms) | `.../timeout_sweep_cpp_dynamic_sync` |

plus a max-latency mini-sweep (dynamic, sync-on, push 33.3 ms, ml ∈ {16,
66.7, 133} ms). Each run records the per-batch metrics CSV (batch fill, e2e =
source-bin arrival → tracker out, compute = mux → tracker) and one JSON line
per processed camera-frame (keyed by `buf_pts`) for detection analysis.

**The knob had to be re-plumbed.** An 8-run characterization matrix (1–100 ms
× {shipped INI, no INI}) proved the `batched-push-timeout` *property* is
completely inert on the new mux — fill and batch rate identical everywhere
(the mux re-reads its INI/defaults at state change, after any property set).
The sweep therefore generates a **per-run mux INI**: `overall-min-fps-n=1000000,
-d=<push_µs>` (the floor cadence for pushing incomplete batches — the real
deadline knob) with `overall-max-fps = max(120, ⌈min-fps⌉)`; the INI files are
archived next to each run's CSV. Command form:

```bash
python3 scripts/timeout_sweep_cpp.py \
  --skew-ms 0,1134.8,1702.1,567.2 --rate 0.96063,0.96099,0.96087,0.96128 \
  --gap-every 44 --ring 4 --duration 45 --warmup 5 \
  --pgie config/_pgie_static.txt --tag "fixed batch-4" \
  --out experiments/results/timeout_sweep_cpp_static
# variants: --pgie config/pgie_config.txt | + --sync --max-latency-ms 33.333
```

Figures per sweep: `timeout_sweep.png` (the 4-panel layout of the original
live sweep: fill distribution, mean fill, throughput, latency) and
`detection_perf.png`; cross-sweep overlay: `sweep_comparison.png` (this dir).

## 2. Batching results (steady state)

Selected rows (full tables in each `summary.csv`):

| push (ms) | static/off fill · b/s · e2e | dynamic/off fill · b/s · e2e | static/sync fill · b/s · e2e · cover | dynamic/sync fill · b/s · e2e · cover |
|---|---|---|---|---|
| 1 | 2.79 · 42.3 · 176 | **1.00 · 117.6 · 15.3** | 2.34 · 40.6 · 190 · 80 % | 1.15 · 101.2 · 33.1 · 97.9 % |
| 5 | 3.23 · 35.7 · 375* | 1.20 · 98.2 · 18.4 | 2.49 · 39.8 · 206 · 83 % | 1.37 · 86.0 · **26.3** · 99.7 % |
| 10 | 2.82 · 41.8 · 193 | 1.86 · 63.4 · 30.5 | 2.57 · 40.7 · 199 · 88 % | 1.58 · 74.4 · 38.1 · 99.7 % |
| 20 | 2.85 · 41.4 · 96 | 3.40 · 33.7 · 378* | 2.96 · 39.7 · 146 · 99 % | 2.64 · 44.7 · 62.4 · 99.7 % |
| 33.3 | 3.85 · 30.6 · **66.5** | 3.85 · 30.6 · 69.3 | 3.85 · 30.5 · 106 | 3.86 · 30.5 · 102 |
| 66.7 | 3.95 · 29.9 · 106 | 3.95 · 30.0 · 110 | 3.95 · 29.8 · 174 | 3.95 · 29.9 · 174 |
| 100 | 3.95 · 29.8 · 232 | 3.96 · 29.9 · 236 | 3.95 · 29.8 · 215 | 3.96 · 29.8 · 220 |

\* capacity-boundary instability (see §4). Coverage is 100 % in every
sync-off cell; the sync-on cells not shown (≥ 33.3 ms) sit at 98.4–99.4 %
(full columns in each `summary.csv`). Frames/s through nvinfer ≈ 118 (the
paced 4×29.8 fps) in every stable cell — the knob moves *when* frames are
processed, not *whether*.

**Engine story (sync off).** Below one frame period the two engines diverge
completely. The dynamic engine rides the tradeoff curve gracefully: at a 1 ms
deadline it runs 117 single-frame invocations/s at **e2e 15.3 ms mean /
28.9 ms p99** (compute 15.0 ms — a batch-1 invocation is cheap). The static
engine pays full batch-4 cost per invocation regardless of fill, saturating
the 612 MHz GPU at ~40 invocations/s: compute 78–311 ms, e2e means 96–375 ms
with chaotic tails (p99 up to 1.4 s). At ≥ 33.3 ms both engines behave
identically (30 batches/s, fill 3.85→3.96, compute 30–35 ms) — batching has
converged and the engine choice stops mattering. Longer deadlines only buy
latency: e2e rises 66 → 232 ms from 33.3 → 100 ms while fill gains just
+0.10 frames.

**Sync story (ml 33.3 ms).** Under sync-on the swept min-fps is also the
mux's service cycle/EARLY gate, and e2e tracks it: the dynamic-sync curve
runs 33 → 26 → 38 → 62 → 102 ms across 1→33.3 ms. Sync costs roughly one
service cycle vs sync-off at the same setting (102 vs 69 ms at 33.3) and
~0.3–2 % discard. The static engine interacts badly with sync at small
cycles: its inference queueing delays frames past the LATE cut and
**sync erases 12–20 % of the input** (coverage 80–88 % at 1–10 ms) — an
engine-speed → timestamp-classification feedback that simply does not exist
with the dynamic engine (≥ 97.9 % everywhere).

**Window mini-sweep (dynamic, sync-on, push 33.3).** ml 16 → 66.7 → 133 ms
moves coverage 98.9 → 99.6 → 99.7 % at unchanged fill (3.84–3.86) and e2e
(99–102 ms) — confirming the replay probe's discard-vs-window curve inside
the full inference pipeline, with diminishing returns past ~66.7 ms.

## 3. Detection performance

From the per-frame JSON records (per run: ~5,250 processed frames, ~4,600 of
them matched by `(camera_id, buf_pts)` against the 100 ms reference run of
the same sweep):

* **Detection output is invariant to the batching policy.** Mean detections
  per processed frame is flat across the sweep (per camera and overall:
  cam0 ≈ 0.82, cam1 ≈ 2.18, cam2 ≈ 0.73, cam3 ≈ 0.41, all-cam ≈ 1.04 —
  content-determined, not timing-determined); frame-matched agreement with
  the reference is ~100 % of frames with identical counts, mean |Δ| ≤ 0.01
  detections/frame, in every stable full-coverage cell of every sweep
  (exceptions: the static×sync sub-frame cells that erase 12–20 % of the
  input dip to 96.6–98.0 % agreement, |Δ| ≤ 0.04; the unstable dynamic@20
  cell sits at 99.2 %).
* **Coverage is the only detection-relevant variable**, and only sync-on ×
  static-engine at sub-frame cycles loses input (§2).
* **Track churn** is flat (62–63 distinct IDs per 40 s across all sync-off
  cells; dynamic sync-on within ±2; only the static×sync sub-frame cells
  drop to 54–61 IDs, from the erased input) — batching policy itself does
  not fragment tracks on this input.
* Static vs dynamic engines produce equivalent per-frame outputs (same
  dets/frame profile) — padding a partial batch does not corrupt the real
  frames; it only wastes compute.

## 4. Caveats

* **Capacity boundary.** Full-batch inference costs ~30–35 ms on the locked
  612 MHz GPU, so ~30 full-batch invocations/s ≈ 100 % utilization. Cells
  that push near-full batches *above* 30/s sit on the stability boundary and
  can drown episodically (static@5 ms: compute p50 190 ms from t=0;
  dynamic@20 ms: e2e 378 ms mean, p99 1.3 s). These rows are honest
  measurements of an over-committed regime, not measurement noise — but their
  exact values are run-to-run chaotic (the known episodic-queue-burst
  behaviour).
* Absolute e2e in replay excludes one MJPEG-decode (~25 ms) present live,
  and includes the reproduced ~200–300 ms standing-queue skew for the stalest
  camera in sync-off full-batch cells (e2e is worst-frame-in-batch).
* The 1/5 ms cells necessarily raise `overall-max-fps` (co-batch slot
  narrows); documented coupling of the new mux's dual-rate design.

## 5. Operating-point recommendations

| goal | configuration | measured |
|---|---|---|
| lowest latency (per-camera detection) | dynamic engine, sync off, deadline 1–5 ms | e2e 15–18 ms mean, 100 % coverage, 98–118 invocations/s |
| balanced (production default) | dynamic engine, sync off, deadline 33.3 ms | fill 3.85, 30 invocations/s, e2e ~69 ms |
| aligned batches for fusion | dynamic engine, **sync on**, cycle 5–33.3 ms, ml 66.7 ms | e2e 26–102 ms, ≥ 99.6 % kept, members aligned within the window |
| static engine | only at deadline ≥ 33.3 ms | identical to dynamic there; never below one frame period, never with sync at fast cycles |
