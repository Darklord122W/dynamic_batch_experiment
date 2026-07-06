# multicam_perception_rt — C++ rewrite (NEW nvstreammux)

A C++ rewrite of the Python pipeline in the parent folder, running on the
**new nvstreammux** (`USE_NEW_NVSTREAMMUX=yes`). It implements exactly **two
pipeline variants**:

| variant | flag | what the mux does |
|---|---|---|
| **baseline** | *(default)* | `sync-inputs=0` — batch whatever frames have arrived; push a full batch immediately, or an incomplete one at the min-fps cadence |
| **sync-on** | `--sync` | `sync-inputs=1` + `max-latency` — time-align frames across cameras by timestamp; frames that can't align within the window are **dropped** |

The RT-experiment machinery of the Python variant (camera skipping / valves,
context providers, adaptive timeout & batch-size controllers) is **intentionally
not ported** — on the new mux those experiments would need re-designing anyway,
and the measured verdicts live in `../experiments/README.md`.

```
 C920x #0 ─► v4l2src ─► jpegparse ─► nvjpegdec ─► nvvideoconvert ┐
 C920x #1 ─► …                                                   │  NEW nvstreammux ─► nvinfer ─► nvtracker ─► probe ─► JSON
 C920x #2 ─► …                                                   │  (batch=N, sync    (YOLO11n,   (track IDs)         stdout
 C920x #3 ─► …                                                   ┘   on/off)          dyn 1..4)
```

**→ See [`TUTORIAL.md`](TUTORIAL.md) for the step-by-step how-to, and
[`ARCHITECTURE.md`](ARCHITECTURE.md) for how the modules fit together
(dependency + pipeline graphs, data flow, extension points).**

## Build & run

```bash
cd cpp && make                 # → ./multicam_rt
cd ..                          # run from the PROJECT ROOT:
./cpp/multicam_rt --config config/camera_params.yaml            # baseline
./cpp/multicam_rt --config config/camera_params.yaml --sync     # sync-on
```

## Layout

```
cpp/
├── Makefile                 # plain make; DeepStream-sample style
├── README.md                # this file
├── TUTORIAL.md              # step-by-step usage guide
├── ARCHITECTURE.md          # module + pipeline graphs; how files relate
└── src/
    ├── main.cpp             # CLI, env setup, bus/run loop, clean shutdown
    ├── app_config.[ch]pp    # YAML config load (yaml-cpp) + validation
    ├── pipeline_builder.[ch]pp  # sources, NEW mux, PGIE, tracker, tails
    ├── detection_parser.[ch]pp  # NvDsBatchMeta -> Detection structs
    ├── output_writer.[ch]pp # JSON / human / null sinks
    ├── metrics.[ch]pp       # per-batch CSV (same schema as ../metrics.py)
    └── fps_overlay.[ch]pp   # per-tile FPS labels (display/record modes)
```

Shared with the Python app (not duplicated): `../config/camera_params.yaml`,
`../config/pgie_config.txt`, `../config/tracker_config.yml`, `../models/*`.
New-mux-only: `../config/mux_config.txt` (batching INI).

## Design notes — what the NEW mux changes

- **Enablement.** The plugin picks its implementation from the
  `USE_NEW_NVSTREAMMUX` env var at registration time. `main.cpp` sets it to
  `yes` *before* `gst_init()` (your own explicit setting wins), and
  `pipeline_builder.cpp` **verifies** the new mux actually loaded — the legacy
  mux still exposes a `width` property, the new one doesn't — and fails with a
  clear message instead of silently running a different pipeline.
- **No scaling.** The new mux has no `width`/`height`: frames batch at native
  capture resolution, so bbox coords are in source pixels *by construction*
  (the legacy app achieved the same by setting mux size == capture size).
- **No `live-source`.** File replay is paced per camera by `identity
  sync=true` after the decoder — this simulates live arrival *at the mux*,
  which sink-side pacing can't do, and is what makes `--sync` experiments
  meaningful under replay.
- **Batching knobs move.** `batched-push-timeout` still exists, but under
  sync-on the operative cadence for pushing *incomplete* batches is
  **`overall-min-fps`** in `config/mux_config.txt` (measured on this device:
  min-fps 5 → one partial batch every 200 ms; min-fps 30 ≈ legacy 33 ms
  timeout behaviour). `max-same-source-frames=1` pins batches to at most one
  frame per camera, matching what the metrics/analysis tooling assumes.
- **Sync drops are silent.** The new mux has a `dropped` signal, but on
  DS 7.1 sync-inputs discards did **not** emit it (measured). The metrics CSV
  therefore counts true arrivals (`arrivals_cum`) so sync loss =
  arrivals − processed. Example (this device, 4 live C920s, 10 s):
  baseline processed **899/899** arrived frames; `--sync` processed
  **330/860** — sync dropped ~62% for zero accuracy gain, reconfirming the
  Python-side verdict that sync is for fusion pipelines only.

## Output & metrics compatibility

- JSON detections: same record shape as the Python app (one object per camera
  per frame).
- `--metrics-csv` writes the **same columns** as `../metrics.py` plus
  `drops_cum`/`arrivals_cum` at the end — `../scripts/analyze.py` and
  `../scripts/plot_in_time.py` work unchanged on these files.
