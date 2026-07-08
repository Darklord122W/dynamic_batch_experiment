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

In the MJPEG source bins, `jpegparse` carries a **PTS-restore step** (probes
on its sink/src pads put the true kernel capture stamps back — GStreamer
1.20's jpegparse otherwise re-stamps live PTS onto a per-camera 33.33 ms grid
anchored at its own first frame). Default ON; disable with `--no-pts-fix`.
The PGIE runs the dynamic-batch engine (`models/model_b4_gpu0_fp16.engine`,
batch 1..4); a static-batch companion pair also exists for A/B tests —
`models/model_static_b4_gpu0_fp16.engine` + `config/_pgie_static.txt`
(fixed batch=4; point `pgie.config_file` at it) — see
`../experiments/static_engine_test/`.

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
├── experiments/
│   └── frame_timing/        # camera arrival-timing experiment: when frames
│                            # really arrive, inter-camera skew at the mux,
│                            # RT-BEV Fig.5 replica (own README + Makefile)
│                            # + replay-skew simulator (REPLAY_SKEW.md,
│                            # run_replay.sh) and results/{baseline,
│                            # baseline_pinned,replay_ideal,replay_skewed,
│                            # sync_pinned,...}
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
  sync=true` after the decoder — this restores *real-time pacing* at the mux,
  which sink-side pacing can't do. It does **not** reproduce the live
  cameras' startup stagger or per-camera rate skew, so plain paced replay is
  the *ideal-timing* case. Faithful skewed replay exists both in
  `experiments/frame_timing/REPLAY_SKEW.md` and in the app itself via
  `--skew-ms/--rate/--gap-every/--ring/--restamp` (added 2026-07-07).
- **Batching knobs move — and collide.** `batched-push-timeout` (property) and
  the INI's `overall-min-fps` are the **same internal knob** — last writer
  wins (`nvstreammux_batch.cpp:333`). Until 2026-07-07 the app set the
  property *before* loading the INI, so the shipped INI silently overrode
  every `--timeout-us`; the app now loads the INI first and the CLI property
  wins. `overall-min-fps=30` in `config/mux_config.txt` governs the sync-on
  partial-batch cadence (measured on this device: min-fps 5 → one partial
  batch every 200 ms) — but it ALSO held **full baseline batches** back
  ~115 ms (measured: e2e p50 162.7 ms with the shipped INI vs 28.3 ms with
  min-fps=120). For latency work use a min-fps=120 INI or `--mux-config
  none` — see
  [`../experiments/results/param_sweep_locked/README.md`](../experiments/results/param_sweep_locked/README.md).
  `max-same-source-frames=1` pins batches to at most one frame per camera,
  matching what the metrics/analysis tooling assumes.
- **Sync drops are silent.** The new mux has a `dropped` signal, but on
  DS 7.1 sync-inputs discards did **not** emit it (measured). The metrics CSV
  therefore counts true arrivals (`arrivals_cum`) so sync loss =
  arrivals − processed. Example (this device, 4 live C920s, 10 s):
  baseline processed **899/899** arrived frames; `--sync` processed
  **330/860** — sync dropped ~62% for zero accuracy gain, reconfirming the
  Python-side verdict that sync is for fusion pipelines only.
  (errata 2026-07-07: the 899/899 vs 330/860 A/B is a **pre-PTS-fix**
  measurement — taken while jpegparse's fabricated grid timestamps drove
  sync-inputs — and was flagged non-reproducing in the report verification.
  Numbers pending re-measurement in the 2026-07-07 post-PTS-fix campaign.)

## Output & metrics compatibility

- JSON detections: same record shape as the Python app (one object per camera
  per frame).
- `--metrics-csv` writes the **same columns** as `../metrics.py` plus
  `drops_cum`/`arrivals_cum` at the end — `../scripts/analyze.py` and
  `../scripts/plot_in_time.py` work unchanged on these files.
