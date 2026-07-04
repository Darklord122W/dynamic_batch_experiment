# multicam_perception_rt — RT-BEV-inspired experiment variant

> **This is the experiment/research variant** of `multicam_perception`. It is the
> full working pipeline **plus** a dynamic batching timeout, context-aware camera
> skipping, and a measurement/benchmark harness — for studying the latency vs.
> throughput vs. accuracy trade-offs discussed in the RT-BEV paper.
> **See [`experiments/README.md`](experiments/README.md) for the experiment guide.**
>
> New vs. the base pipeline:
> - **Dynamic timeout** — `--timeout-policy fixed|adaptive` (`nvstreammux batched-push-timeout` adapted at runtime; `controllers.py`).
> - **Camera skipping** — a `valve` per camera + `--context all|activity|scheduled` (`context.py`), for context-aware sub-batching.
> - **Reproducible replay** — `--source file --replay-dir …` (record with `scripts/record_replay_clips.py`).
> - **Metrics** — `--metrics-csv PATH` per-batch latency/throughput; aggregate with `scripts/analyze.py`; sweep with `scripts/benchmark.py`.
> - **New modules:** `context.py`, `controllers.py`, `metrics.py`; config sections `source`/`timeout`/`context`/`control`.
>
> Quick start:
> ```bash
> # baseline behaviour (identical to the base pipeline):
> python3 main.py --config config/camera_params.yaml
> # record reproducible clips, then compare timeout values:
> python3 scripts/record_replay_clips.py --duration 40
> python3 scripts/benchmark.py --experiment e1 --source file --duration 25 --warmup 4
> ```

---

Multi-camera **DeepStream** detection + tracking on **Jetson AGX Orin**. Up to four
Logitech C920x USB webcams are batched through one pipeline running a pretrained
**YOLO11n** detector, given persistent per-camera track IDs by `nvtracker`, and
their detections are emitted as structured JSON — one record per camera per frame.

Standalone Python (`pyds` + PyGObject). **No ROS 2.** No SGIE, no fusion, no
recording — single-stage detection + tracking only.

```
 C920x #0 ─► v4l2src ─► jpegparse ─► nvjpegdec ─► nvvideoconvert ┐
 C920x #1 ─► v4l2src ─► jpegparse ─► nvjpegdec ─► nvvideoconvert │
 C920x #2 ─► v4l2src ─► jpegparse ─► nvjpegdec ─► nvvideoconvert ├─► nvstreammux ─► nvinfer ─► nvtracker ─► probe ─► JSON
 C920x #3 ─► v4l2src ─► jpegparse ─► nvjpegdec ─► nvvideoconvert ┘   (batch=N)     (YOLO11n)  (track IDs)  (pyds)
```

Everything from `nvstreammux` on is a single shared instance (one PGIE, one
tracker) processing the whole batch in one pass. Only capture is parallel.

---

## 1. Prerequisites

Verified against the target device:

| Component | Version |
|---|---|
| Jetson AGX Orin 64GB | L4T r36.5 / **JetPack 6.2.2** |
| DeepStream | **7.1.0** |
| CUDA / TensorRT / cuDNN | 12.6 / 10.3 / 9.0 |
| Python / GStreamer | 3.10.12 / 1.20.3 |
| Cameras | 1–4× Logitech C920 / C920x (USB `046d:08e5`) |

Confirm DeepStream with `deepstream-app --version-all`.

### 1a. Python bindings (`pyds`)

`pyds` is not part of the base image. Install the **v1.2.0** wheel (the one built
for DeepStream 7.1 / Ubuntu 22.04 / Python 3.10 — do **not** use `master`, which
targets newer stacks):

```bash
python3 -m pip install --upgrade pip
wget https://github.com/NVIDIA-AI-IOT/deepstream_python_apps/releases/download/v1.2.0/pyds-1.2.0-cp310-cp310-linux_aarch64.whl
pip3 install --user ./pyds-1.2.0-cp310-cp310-linux_aarch64.whl
python3 -c "import pyds; print('pyds', pyds.__file__)"
```

`python3-gi` and `python3-gst-1.0` are already present on JetPack 6. This app does
not use `numpy` at runtime, but if you later call `get_nvds_buf_surface()`, keep
**`numpy < 2`** (pyds 1.2.0 predates NumPy 2).

### 1b. (Optional) `v4l2-ctl`

Handy for inspecting camera capabilities. Not required to run.

```bash
sudo apt-get install v4l-utils
v4l2-ctl -d /dev/video0 --list-formats-ext
```

---

## 2. Get the model (YOLO11n → ONNX + parser)

One script downloads YOLO11n, exports it to a DeepStream-compatible ONNX, and
compiles the custom bbox parser. It writes everything into `models/`:

```bash
./scripts/download_yolo11n.sh
```

Produces:

```
models/
├── yolo11n.onnx                                   # pretrained YOLO11n, DeepStream layout
├── labels.txt                                     # 80 COCO class names
└── nvdsinfer_custom_impl_Yolo/
    └── libnvdsinfer_custom_impl_Yolo.so           # YOLO bbox parser (built for DS 7.1/CUDA 12.6)
```

The script uses an isolated Python venv for the export (CPU-only Ultralytics/
torch) so it never touches the system Python or the DeepStream runtime. Only the
three artifacts above are used at run time. `config/pgie_config.txt` already
points at them.

> The script's scratch dir `models/_build/` (venv + torch + the DeepStream-Yolo
> clone, ~5 GB) is only needed to *regenerate* the model. Once the artifacts above
> exist you can delete it to reclaim space: `rm -rf models/_build`.

> **TensorRT engine.** This project already ships a prebuilt
> `models/model_b4_gpu0_fp16.engine` (dynamic batch, max 4 — serves any camera
> count 1–4), so the first launch loads instantly. If you regenerate the model,
> change precision, or move to another device, delete that file and either launch
> `main.py` (it rebuilds on first use, several minutes) or pre-build it without
> cameras:
> ```bash
> python3 scripts/build_engine.py
> ```
> Engines are non-portable — never copy one from another TensorRT/CUDA/JetPack.

---

## 3. Configure the cameras

Everything is driven by `config/camera_params.yaml` — camera count, device paths,
resolution, fps, tracker settings. Nothing is hardcoded in the `.py` files.

On this device the four C920 **capture** nodes are `/dev/video0, 2, 4, 6` (the odd
nodes are UVC metadata). Add/remove entries in the `cameras:` list to change the
count (1–4).

### Logitech C920x capture cheat-sheet (verified on this hardware)

The C920 exposes exactly two usable formats over USB-2:

| Format | 640×480 | 1280×720 | 1920×1080 |
|---|---|---|---|
| **MJPG** (`image/jpeg`) | 30 | 30 | 30 |
| **YUYV** (`video/x-raw`) | 30 | **10** | **5** |

- Use `format: mjpeg` for anything above VGA (the default). Raw YUYV is only
  full-rate at ≤640×480 — uncompressed 1080p needs ~200 Mbit/s, over the USB-2
  single-stream ceiling, so the firmware caps it at 5 fps.
- `nvstreammux` `width`/`height` are set equal to the capture size, so the bbox
  coordinates in the output are **already in source-image pixels** — no rescaling.

### ⚠️ USB bandwidth (this rig: all 4 cameras on one USB-2 bus)

`uvcvideo` reserves USB bandwidth from each camera's *peak* declared payload, not
the true compressed MJPEG rate, so a single USB-2 bus fills quickly. **Measured on
this exact device:**

| Cameras | Resolution | Result |
|---|---|---|
| 4 | 640×480 @30 MJPG | ✅ works — **the default** |
| 2 | 1280×720 @30 MJPG | ✅ works |
| 4 | 1280×720 @30 MJPG | ❌ 3rd camera fails: `Failed to allocate required memory` (STREAMON ENOSPC) |

That's why the shipped default is **640×480@30 MJPG** — the setting that runs all
four cameras on one bus. If you hit the bandwidth error:
1. Run fewer cameras at higher resolution (drop `cameras:` to 1–2 and set
   `width: 1280, height: 720` — or `1920, 1080`), **or**
2. Spread cameras across separate USB host controllers (don't share one bus/hub),
   **or**
3. Lower fps (e.g. 15).

The app fails fast with a clear bus/allocation error rather than hanging.

### MJPEG decoder

Default is `nvjpegdec` (hardware, handles the C920's 4:2:2 JPEG). If you see wrong
colors or a decode stall on your L4T build, switch to the always-safe software
decoder by setting `mjpeg_decoder: jpegdec` under `capture:` (or per camera).
`nvv4l2decoder mjpeg=1` is intentionally **not** the default — it only decodes
4:2:0-sourced JPEG and mishandles the C920.

---

## 4. Run

```bash
python3 main.py --config config/camera_params.yaml
```

- Camera count is derived from the config.
- If a configured device is missing, it exits immediately with a clear message
  listing the missing paths and the devices actually present — it does not crash
  opaquely inside GStreamer.
- The prebuilt engine (`models/model_b4_gpu0_fp16.engine`) loads instantly. If it
  is missing, the first launch rebuilds it from the ONNX (several minutes).
- Stop with `Ctrl-C`.

---

## 5. Debug / visualization mode

For eyeballing what each camera sees, there's a debug mode that tiles all cameras
into one window and draws the bounding boxes, class labels, **track IDs**, and a
live **per-camera FPS** readout in each tile's top-left corner (via `nvdsosd`),
plus a readable per-camera terminal log.

```bash
# Live window (2×2 tile of the 4 cameras) + readable per-camera log:
python3 main.py --config config/camera_params.yaml --debug
```

`--debug` is shorthand for `--display --log human`. The pieces are independent:

| Flag | Effect |
|---|---|
| `--display` | Live on-screen window (`nvmultistreamtiler` → `nvdsosd` → `nv3dsink`) with boxes + labels + track IDs + per-camera FPS. Needs a display (`$DISPLAY`). |
| `--log human` | Compact, colorized, throttled per-camera terminal log (see below). |
| `--log json` | Default machine-readable output (§6). |
| `--log none` | Silence the console (e.g. when you only want the window or a recording). |
| `--record out.mp4` | Encode the same annotated, tiled view to an H.264 MP4. Works with or without `--display` (great for a headless box — record, then copy the file off). |

The human log prints at most one line per camera per second (tune with
`output.log_interval_s`):

```
[cam0 f=176  25.4fps]  1 obj: person#1 0.87
[cam1 f=186  24.7fps]  3 obj: keyboard#8 0.83 | mouse#17 0.59 | person#10 0.90
[cam2 f=205  25.5fps]  2 obj: keyboard#3 0.64 | person#4 0.86
[cam3 f=205  25.5fps]  2 obj: laptop#7 0.45 | person#6 0.79
```

`#N` is the persistent track ID; the number after it is confidence. Tile size is
set by the `display:` block in the config.

> **Recording note:** stop with `Ctrl-C` (not `kill`) so the MP4 is finalized —
> the app flushes an EOS to write the file index before exiting. A hard kill
> leaves an unplayable file.

## 6. Output

One JSON object per camera per processed frame, via the swappable
`OutputWriter` (default: stdout). Example:

```json
{"camera_id":0,"frame_num":42,"num_detections":2,"detections":[
  {"camera_id":0,"track_id":7,"class_name":"person","confidence":0.91,"x":812.0,"y":334.0,"width":140.0,"height":388.0},
  {"camera_id":0,"track_id":9,"class_name":"chair","confidence":0.55,"x":25.0,"y":402.0,"width":180.0,"height":150.0}
]}
```

- `camera_id` = `nvstreammux` source_id = the camera's index in the config list.
- `track_id` = `nvtracker`'s persistent object ID (a lightweight tracker may
  reassign an ID after a long occlusion — expected). `-1` = not yet tracked.
- `x, y, width, height` = bbox in source-image pixels.

To send detections somewhere else (socket, queue, ROS 2, DB), subclass
`OutputWriter` in `output_writer.py` and pass it into `main.run` — the probe calls
`writer.write(...)` and nothing else, so this is the only file you touch.

---

## 7. Project layout

```
multicam_perception/
├── main.py                 # CLI entry: load config, validate cameras, build, run loop
├── pipeline_builder.py     # constructs/wires the GStreamer/DeepStream elements
├── detection_parser.py     # pyds NvDsBatchMeta -> Detection dataclasses (with track_id)
├── output_writer.py        # swappable detection sink (JSON / human log / null)
├── fps_overlay.py          # per-camera FPS text drawn on the debug display
├── config/
│   ├── camera_params.yaml  # cameras, resolution, fps, tracker, output  ← edit this
│   ├── pgie_config.txt      # nvinfer/YOLO11n config (GKeyFile)
│   └── tracker_config.yml   # nvtracker low-level config (NvSORT)
├── scripts/
│   ├── download_yolo11n.sh # fetch + export model, build parser lib
│   └── build_engine.py     # pre-build the TensorRT engine (no cameras needed)
├── models/                 # ONNX, labels, parser .so, prebuilt engine
└── README.md
```

---

## 8. Design notes / gotchas

- **Probe on `nvtracker`, not PGIE.** `track_id` (`object_id`) is only populated
  after the tracker runs, so the pad probe sits on `nvtracker`'s src pad.
- **Tracker = NvSORT.** Kalman filter + association gives persistent IDs through
  brief occlusions at near-zero extra compute (no re-ID CNN). Lighter than NvDCF;
  more stable than plain IOU. Swap by pointing `tracker.ll_config_file` at a
  different config (e.g. `.../config_tracker_IOU.yml`).
- **`pgie_config.txt` is a GKeyFile** — comments must be on their own line; a
  trailing `# note` after `key=value` becomes part of the value and breaks
  parsing.
- **YOLO parser:** uses `NvDsInferParseYoloCuda` (GPU) to avoid a reported DS 7.1
  Python segfault in the CPU parser. If inference misbehaves, the CPU fallback
  `NvDsInferParseYolo` is available (commented in `pgie_config.txt`).
- **`nvtracker` batch mode** is on by default in DS 7.1; the old
  `enable-batch-process` property no longer exists (setting it would error).
```
