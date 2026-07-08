# Baseline C++ vs Python — exact commands & effective pipeline config

Companion to [`baseline_cpp_vs_py/`](baseline_cpp_vs_py/README.md) (campaign 1,
clocks unlocked), [`baseline_cpp_vs_py_locked/`](baseline_cpp_vs_py_locked/README.md)
(campaign 2, `jetson_clocks`) and [`param_sweep_locked/`](param_sweep_locked/README.md)
(campaign 3, batching-knob sweep, `jetson_clocks`). Records exactly what was
executed on 2026-07-07 and what configuration was in effect.

## Commands

### Campaign 1 — clocks unlocked (DVFS active)

Six sequential runs, C++ first, then Python:

```bash
cd ~/Documents/deepstream_batch/multicam_perception_rt
OUT=experiments/results/baseline_cpp_vs_py

# C++ app (new nvstreammux), 3 repeats
for i in 1 2 3; do
  ./cpp/multicam_rt --config config/camera_params.yaml --source file --log none \
      --metrics-csv $OUT/cpp_baseline_r$i.csv --duration 25
done

# Python app (legacy nvstreammux), 3 repeats
for i in 1 2 3; do
  python3 main.py --config config/camera_params.yaml --source file --log none \
      --metrics-csv $OUT/py_baseline_r$i.csv --duration 25
done
```

### Campaign 2 — clocks locked

Identical, except the clock lock first and a different output dir:

```bash
sudo jetson_clocks --store /tmp/jetson_clocks_backup.conf   # save DVFS state
sudo jetson_clocks                                          # pin CPU 1.73 GHz / GPU 612 MHz / EMC 3.2 GHz (MODE_30W caps)
OUT=experiments/results/baseline_cpp_vs_py_locked
# ... same two loops as above ...
sudo jetson_clocks --restore /tmp/jetson_clocks_backup.conf # undo when finished
```

### Campaign 3 — parameter sweep, clocks locked

Same lock and protocol as campaign 2 (25 s runs, replay clips, `--warmup 4` at
analysis), one run per config + 3 repeats for the two winners. Output dir
`experiments/results/param_sweep_locked/`:

```bash
sudo jetson_clocks                     # same lock as campaign 2 (MODE_30W caps)
OUT=experiments/results/param_sweep_locked

# --timeout-us sweep (µs; both apps) — e.g. 20000/10000/5000:
./cpp/multicam_rt --config config/camera_params.yaml --source file --log none \
    --timeout-us 20000 --metrics-csv $OUT/cpp_t20000.csv --duration 25
python3 main.py --config config/camera_params.yaml --source file --log none \
    --timeout-us 5000 --metrics-csv $OUT/py_t5000.csv --duration 25

# mux INI variants (C++ / new mux only):
./cpp/multicam_rt --config config/camera_params.yaml --source file --log none \
    --mux-config $OUT/mux_minfps120.txt --metrics-csv $OUT/cpp_minfps120.csv --duration 25
./cpp/multicam_rt --config config/camera_params.yaml --source file --log none \
    --mux-config none --metrics-csv $OUT/cpp_muxnone.csv --duration 25
```

Campaign-3-specific flags:

| Flag | Effect |
|---|---|
| `--timeout-us N` | `nvstreammux batched-push-timeout` in µs (both apps). **C++ caveat (errata 2026-07-07):** in the new mux this property and the INI's `overall-min-fps` are the *same* internal knob (last writer wins); the pre-2026-07-07 binary set the property *before* loading the INI, so the shipped INI (min-fps=30) silently overrode every C++ `--timeout-us` in campaign 3. Fixed 2026-07-07 — the INI now loads first and the CLI property wins |
| `--mux-config PATH` | C++ only — new-mux batching INI loaded instead of `config/mux_config.txt`. The sweep variants live in `param_sweep_locked/`: `mux_minfps60.txt` (`overall-min-fps=60`, max 240) and `mux_minfps120.txt` (`overall-min-fps=120`, max 240 — the campaign-3 winner, e2e p50 28 ms) |
| `--mux-config none` | C++ only — load no INI; the new mux runs on built-in defaults |

### Analysis (campaigns 1 & 2)

```bash
python3 scripts/analyze.py $OUT/cpp_baseline_r*.csv $OUT/py_baseline_r*.csv --warmup 4
python3 $OUT/make_plots.py    # figures + summary JSON into $OUT/plots/
```

## What each flag does

| Flag | Effect |
|---|---|
| `--config config/camera_params.yaml` | The shared config — same file for both apps |
| `--source file` | Replaces the 4 live `v4l2src` cameras with replay of `experiments/clips/cam0..3.mp4` (`source.replay_dir` in the YAML) and sets `live-source=0` so playback paces to real time. Both apps see identical input |
| `--log none` | No per-frame JSON on stdout, so console I/O doesn't pollute latency |
| `--metrics-csv PATH` | Per-batch metrics CSV (`compute_ms`, `e2e_ms`, `n_in_batch`, per-camera dets, `new_ids_cum`, …) — same schema in both apps |
| `--duration 25` | Clean stop after 25 s (per-clip durations: cam0 45.7 s, cam1 75.3 s, cam2 66.3 s, cam3 60.0 s — the shortest still leaves >20 s headroom, so no loop/EOS effects) |
| `--warmup 4` (analyze.py) | Drops the first 4 s per run: engine deserialization + pipeline fill, which otherwise dominate p99/max |

No `--sync`, no `--timeout-policy`, no `--context` — everything at **baseline defaults**.

## Effective pipeline (both apps)

```
4× filesrc(cam{i}.mp4) → h264 decode → nvvideoconvert ─┐
                                                        ├→ nvstreammux (batch=4) → nvinfer → nvtracker → probe → metrics CSV
                                                        ┘
```

| Setting | Value | Source |
|---|---|---|
| Input | 4 streams, 640×480@30 (recorded C920 clips) | `capture:` / `experiments/clips/` |
| Mux batch size | 4, `sync_inputs=0` (off), `batched-push-timeout` 33333 µs | `streammux:` |
| **Mux implementation** | **Python: legacy mux** (uses width/height/live-source/timeout above) · **C++: new mux** (`USE_NEW_NVSTREAMMUX=yes`, set by the binary; reads `config/mux_config.txt` — `overall-min-fps=30` — and ignores the legacy fields) | `streammux.config_file` |
| Detector | YOLO11n FP16, `models/model_b4_gpu0_fp16.engine` (dynamic batch ≤ 4) | `pgie.config_file` → `config/pgie_config.txt` |
| Tracker | NvSORT (`libnvds_nvmultiobjecttracker.so`), 640×384 | `tracker:` |
| Timeout policy | `fixed`, 33333 µs | `timeout:` (default) |
| Camera skipping | `context: all` — every camera active, valves open | `context:` (default) |
| Batch-size policy | `fixed` (= 4) | `batch:` (default) |

The mux-implementation row is the one intentional asymmetry — it is the point of
the C++ port, so this baseline is really **legacy-mux batching vs new-mux
batching** with identical input, model, and tracker on both sides.

## Environment

Jetson AGX Orin 64 GB, JetPack 6.2.2 / L4T r36.5, DeepStream 7.1.0, power mode
**MODE_30W**. Campaign 2 pinned: CPU 8×1.728 GHz, GPU 612 MHz, EMC 3.199 GHz,
idle states off (`jetson_clocks --show` output in the session log).


## Campaign 4 — jpegparse PTS fix + sync rehabilitation + engine sweeps (2026-07-07/08)

Full command log with parameters and results:
`experiments/results/campaign_2026-07-07_ptsfix/REPORT.md` (+ STEP1..STEP5).
Headlines: PTS-restore fix default-ON in `cpp/multicam_rt`; sync-inputs=1 now
gives 100% full batches at 2.1 ms true alignment (live); the
`batched-push-timeout` property is inert on the new mux — push deadlines are
swept via per-run INI `overall-min-fps` (`scripts/timeout_sweep_cpp.py`);
frame-rate pinning control on kernel 5.15 is `exposure_dynamic_framerate=0`.
