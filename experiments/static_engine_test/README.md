# Static vs dynamic engine — how nvinfer handles a *partial* batch

**Question tested:** when a batch reaches `nvinfer` with **fewer frames than the
engine's batch size** — because cameras were skipped, or `sync-inputs` dropped a
late frame — what does a **fixed/static batch-4** TensorRT engine do? Process the
actual N? Pad to 4? Error with an "empty batch"?

**Short answer:** it processes the **actual N**, with **no error and no empty
batch** — whether the partial batch comes from *skipping* or from *sync-inputs
dropping a late frame*. See the numbers below.

---

## Setup (both engines are kept)

| engine | file | how built |
|---|---|---|
| **dynamic** (shipped default) | `models/model_b4_gpu0_fp16.engine` | min=1, opt=4, max=4 |
| **static** (this test) | `models/model_static_b4_gpu0_fp16.engine` | batch fixed at 4 (min=opt=max=4) |

The static engine was built from the **existing dynamic ONNX** (no re-export) with:

```bash
/usr/src/tensorrt/bin/trtexec --onnx=models/yolo11n.onnx \
  --minShapes=input:4x3x640x640 --optShapes=input:4x3x640x640 --maxShapes=input:4x3x640x640 \
  --fp16 --saveEngine=models/model_static_b4_gpu0_fp16.engine
```

Test config `config/_pgie_static.txt` points nvinfer at the static engine.
**The app's `config/pgie_config.txt` is unchanged (still dynamic)** — the app runs
exactly as before; this test uses a separate config only.

---

## Tests & results

### 1. Skip-induced partial batch (`static_partial_test.py`, videotestsrc + valve drop)
Drop valves to force 4 → 3 → 2 frame batches:

| phase | MUX produced | NVINFER output | errors |
|---|---|---|---|
| all 4 | `num_frames=4` | `num_frames=4` | none |
| drop 1 | `num_frames=3` | `num_frames=3` | none |
| drop 2 | `num_frames=2` | `num_frames=2` | none |

Buffers keep flowing; partial batches pass straight through. **No "empty batch".**

### 2. Skip on real content (replay clips) — *pad-to-4 vs run-actual-N?*
Measured **steady-state** (window t=6–14 s, after warmup — see the warning below):

| phase | compute |
|---|---|
| static, 4 cameras (no skip) | 100 ms |
| static, 2 cameras (partial) | **51 ms** |

Compute **halves** with fewer frames → nvinfer runs the **actual N**, it does **not**
pad the batch to 4 (padding would keep compute ~flat at the 4-cam cost).

> ⚠️ **Warmup warning (learned the hard way).** An earlier version of this test read
> the 4-cam window at t=1–3 s and reported 258 ms compute + only 40 detections — both
> were **warmup artifacts** (engine just loaded, pipeline filling, clips' empty
> opening seconds; detections were literally 0 at t=0–2 s). Always measure a
> post-warmup window. The static engine is **not** slower than dynamic — both are
> ~100 ms at batch 4.

### 3. Sync-on partial batch (`sync_static_test.py`, 3 live cameras, `sync-inputs=1`)
| metric | value |
|---|---|
| num_frames_in_batch | `{1: 28, 2: 138}` (sync dropped late frames) |
| buffers out of nvinfer | 166 |
| detections | 482 ✓ |
| errors | none |

Same behavior when the partial batch is caused by **sync-inputs** rather than skip.

---

## Findings

1. **No empty batch, no crash.** A static batch-4 engine processes partial batches
   from *either* skip *or* sync-drop.
2. **It runs the actual frame count** (compute scales), not padded to 4.
3. **Why:** nvinfer loaded this trtexec engine as an **implicit-batch** engine
   (log: `[Implicit Engine Info]: layers num: 0`, "Implicit layer support has been
   deprecated"), max batch 4 — an implicit-batch engine runs *any* batch ≤ max. So
   in practice DeepStream nvinfer handles partial batches gracefully regardless of
   how the engine was built. You do **not** need to guard against partial batches
   crashing nvinfer.
4. **Static ≈ dynamic on speed** (both ~100 ms at batch 4, steady-state). The static
   engine is a touch slower at *partial* batches (~51 vs ~34 ms at 2 cams) but the
   same at full batch — no reason to switch. (An earlier draft wrongly said "2.5×
   slower"; that was warmup — see the warning above.)

## Practical takeaway

Keep the **dynamic** engine as the default — it handles partial batches natively and
is at least as fast. A partial batch (from skipping or sync) will **not** crash or
produce an empty inference; nvinfer simply infers the frames that are present.

## Reproduce

```bash
cd multicam_perception_rt
# 1. skip-induced partial (isolated, videotestsrc)
python3 experiments/static_engine_test/static_partial_test.py
# 2. skip on real content (compute scaling)
python3 main.py --config experiments/exp_skip_big.yaml --source file \
  --replay-dir experiments/clips --context scheduled --metrics-csv /tmp/s.csv --duration 16
#    (temporarily point pgie at config/_pgie_static.txt to use the static engine)
# 3. sync-on partial (3 live cameras)
python3 experiments/static_engine_test/sync_static_test.py
```
