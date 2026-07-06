# Tutorial — building and using the C++ pipeline (NEW nvstreammux)

A hands-on guide to the C++ app: build it, run the **baseline** and **sync-on**
variants live and on replay clips, collect metrics, and analyze them with the
existing scripts. Run everything from the **project root**:

```bash
cd ~/Documents/deepstream_batch/multicam_perception_rt
```

---

## Step 0 — one-time prerequisites

Everything is already on a stock JetPack 6.2 + DeepStream 7.1 image — no
Python bindings (`pyds`) needed for the C++ app.

- **Toolchain / libs** (verify once):
  ```bash
  g++ --version                      # 11.x is fine
  pkg-config --modversion yaml-cpp   # 0.7.x
  deepstream-app --version-all       # DeepStream 7.1.0
  ```
- **Model artifacts** in `models/` (shared with the Python app):
  `yolo11n.onnx`, `labels.txt`, `nvdsinfer_custom_impl_Yolo/…​.so`, and the
  prebuilt engine `model_b4_gpu0_fp16.engine`. If missing:
  ```bash
  ./scripts/download_yolo11n.sh
  python3 scripts/build_engine.py     # optional: pre-build without cameras
  ```
- **Cameras**: up to 4× C920 on `/dev/video0,2,4,6` at 640×480@30 MJPG (the
  4-cameras-on-one-USB-2-bus setting — see the USB bandwidth notes in the main
  README). For replay-only usage you need no cameras, just clips.

## Step 1 — build

```bash
cd cpp
make            # → cpp/multicam_rt
cd ..
```

`make clean && make` after pulling changes. The binary hard-links against
`/opt/nvidia/deepstream/deepstream/lib` (rpath), so no `LD_LIBRARY_PATH` needed.

## Step 2 — smoke test on replay clips (no cameras needed)

Clips live in `experiments/clips/cam0..3.mp4` (record fresh ones with
`python3 scripts/record_replay_clips.py --duration 40`).

```bash
./cpp/multicam_rt --config config/camera_params.yaml --source file \
                  --log human --duration 8
```

You should see the engine load (a few seconds), then per-camera lines:

```
[cam0 f=176  25.4fps]  1 obj: person#1 0.87
[cam2 f=205  25.5fps]  2 obj: keyboard#3 0.64 | person#4 0.86
```

then `duration 8.0s elapsed — stopping` and a clean exit. First-ever launch
without a prebuilt engine takes several minutes (TensorRT build) — that's
normal.

> The app prints `[main] USE_NEW_NVSTREAMMUX=yes` at startup — it sets the env
> var itself. If you ever see
> `ERROR: The LEGACY nvstreammux was loaded…`, your shell has
> `USE_NEW_NVSTREAMMUX` explicitly set to something else — `unset` it.

## Step 3 — the baseline (live cameras)

```bash
./cpp/multicam_rt --config config/camera_params.yaml
```

- One JSON line per camera per processed frame on stdout:
  ```json
  {"camera_id":0,"frame_num":42,"num_detections":1,"detections":[
    {"camera_id":0,"track_id":7,"class_name":"person","confidence":0.9,
     "x":34.6,"y":2.5,"width":605.4,"height":469.2}]}
  ```
  `camera_id` = index in the config's `cameras:` list; `track_id` = persistent
  nvtracker ID (−1 = not yet tracked); bbox in **source pixels** (the new mux
  never rescales).
- NVIDIA plugins print a few of their own lines to stdout (`Opening in
  BLOCKING MODE`, `max_fps_dur …`). When piping into a consumer, keep only
  JSON: `./cpp/multicam_rt … | grep '^{'`.
- Stop with **Ctrl-C** (first one flushes EOS so recordings/metrics finalize;
  a second one force-quits). `--duration N` stops automatically.

Behaviour to expect (measured on this device, 4 live C920s): batches are
almost always **full (4 frames)**, ~30 batches/s steady state, every arrived
frame processed.

## Step 4 — the sync-on variant

```bash
./cpp/multicam_rt --config config/camera_params.yaml --sync
```

`--sync` sets `sync-inputs=1` and `max-latency` (default 33 ms; change with
`--max-latency-ms N` or `streammux.max_latency_ns` in the YAML) on the mux.
The mux now holds each frame to its timestamp and **drops any frame that
can't be aligned within the window**.

What you will see with free-running USB cameras (measured, 10 s live run):

| | baseline | `--sync` |
|---|---|---|
| batch fullness | ~4 | **1–2** |
| frames processed | 899 of 899 arrived | **330 of 860 arrived (~62% dropped)** |

That is the point of keeping this variant: it demonstrates (like the Python
campaign did) that input synchronization is **pure cost** for independent
per-camera detection — it exists for camera-fusion pipelines. USB webcams have
no hardware trigger, so it aligns by *arrival* time, not true exposure.

Sync-on tuning knobs:
- `--max-latency-ms` — how late a frame may be and still join the batch.
  Larger = fuller batches, more latency.
- `overall-min-fps` in `config/mux_config.txt` — the cadence at which the mux
  pushes **incomplete** batches. We ship 30 (≈ the legacy mux's 33 ms
  `batched-push-timeout` behaviour); at the new-mux default of 5 a partial
  batch leaves only every 200 ms.

## Step 5 — metrics + analysis (the A/B workflow)

Both variants log the same per-batch CSV the Python harness used, so the
existing analysis scripts work unchanged:

```bash
# reproducible A/B on the same clips:
./cpp/multicam_rt --config config/camera_params.yaml --source file --log none \
                  --metrics-csv results/cpp_baseline.csv --duration 25
./cpp/multicam_rt --config config/camera_params.yaml --source file --log none \
                  --metrics-csv results/cpp_sync.csv --duration 25 --sync

python3 scripts/analyze.py results/cpp_baseline.csv results/cpp_sync.csv --warmup 4
python3 scripts/plot_in_time.py results/cpp_sync.csv   # "frames in time" view
```

Columns (same as `metrics.py`): `n_in_batch` (frames the mux batched),
`n_real` (frames matched to a true source arrival), `compute_ms`
(mux→tracker), `e2e_ms` (source→tracker, includes the batch wait),
per-camera detections, `new_ids_cum` (track-stability proxy). Two extra
trailing columns:

- `arrivals_cum` — frames that arrived at the source pads so far. **Sync loss
  = arrivals_cum − Σ n_real**; the closing summary prints it
  (`processed X of Y arrived frames`).
- `drops_cum` — the new mux's `dropped` signal count. Measured caveat: on
  DS 7.1 sync discards do *not* fire it; trust `arrivals_cum` instead.

Always discard a warmup window (`--warmup 4`): the first seconds include
engine deserialization and CUDA autotuning. Replay inflates absolute compute
vs live (extra 4× H.264 decode) — compare runs *relatively*, like the Python
experiments did.

> **Reading analyze.py output for sync runs:** its "REAL frames/s" is computed
> from `n_active`, which made sense for the legacy-mux *skipping* experiments
> (the legacy mux padded batches with phantom repeats, so `n_in_batch` lied).
> On the new mux the situation inverts: there is no padding, `n_in_batch` is
> truthful, and `n_active` is a constant 4 (this app never skips). So for
> `--sync` runs read **"reported frames/s"** (n_in_batch-based) as the true
> throughput — e.g. a measured live A/B: baseline 118.7 vs sync 45.6 fps.

## Step 6 — seeing what the cameras see

```bash
./cpp/multicam_rt --config config/camera_params.yaml --debug          # window + human log
./cpp/multicam_rt --config config/camera_params.yaml --record out.mp4 # headless recording
./cpp/multicam_rt --config config/camera_params.yaml --display --record out.mp4
```

`--display` tiles all cameras into one window (`nvmultistreamtiler` →
`nvdsosd` → `nv3dsink`) with boxes, labels, track IDs and a live per-camera
FPS readout per tile; it needs `$DISPLAY`. `--record` encodes the same
annotated view to H.264 MP4 and works headless. Stop with Ctrl-C (not
`kill`) so the MP4 index is written — the app flushes EOS first.

## CLI reference

| flag | meaning |
|---|---|
| `--config PATH` | YAML config (default `config/camera_params.yaml`) |
| `--sync` / `--no-sync` | force sync-on / baseline (overrides `streammux.sync_inputs`) |
| `--max-latency-ms N` | sync window for late frames (default 33 ms) |
| `--timeout-us N` | mux `batched-push-timeout` (default 33333) |
| `--mux-config PATH\|none` | new-mux batching INI (default `config/mux_config.txt`) |
| `--source v4l2\|file` | live cameras (default) or replay clips |
| `--replay-dir DIR` | clip directory for `--source file` (default `experiments/clips`) |
| `--log json\|human\|none` | console output style (default json) |
| `--debug` | `--display` + `--log human` |
| `--display` / `--record PATH` | live window / annotated MP4 |
| `--metrics-csv PATH` | per-batch metrics CSV |
| `--duration SECS` | clean stop after N seconds |

Everything else (camera list, resolution, tracker, output tweaks) lives in
`config/camera_params.yaml` — the same file the Python app reads. Keys only
the legacy mux understands (`streammux.width/height/live_source/
nvbuf_memory_type`) and the RT-experiment sections (`timeout:`/`context:`/
`batch:`/`control:`) are ignored by the C++ app.

## Troubleshooting

- **`ERROR: The LEGACY nvstreammux was loaded`** — `unset USE_NEW_NVSTREAMMUX`
  (the app sets it to `yes` itself; an explicit `no` in your environment wins).
- **`Configured camera device(s) not found`** — the error lists present
  `/dev/video*` nodes; edit the `cameras:` list. Remember only the even nodes
  are capture devices for the C920.
- **3rd/4th camera fails with `Failed to allocate required memory`** — USB-2
  bandwidth ceiling; keep 640×480 MJPG for 4 cameras on one bus (see the main
  README's bandwidth table).
- **Wrong colors / decode stall** — set `mjpeg_decoder: jpegdec` (software)
  under `capture:` in the YAML.
- **Engine rebuild loop or "engine file not found"** — regenerate with
  `python3 scripts/build_engine.py`; engines are not portable across
  TensorRT/JetPack versions.
- **Sync-on processes almost nothing** — expected with free-running cameras
  and a tight window; raise `--max-latency-ms`, or accept the drop (that's the
  experiment's finding). Check `processed X of Y` in the metrics summary.
- **JSON consumer chokes on plugin chatter** — filter stdout with
  `grep '^{'`; all app JSON is single-line and starts at column 0.
