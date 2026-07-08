# Step 1 — Fixing the jpegparse timestamp destruction

**Date:** 2026-07-07 · **Machine:** Jetson AGX Orin 64 GB, JetPack 6.2.2 / DS 7.1 /
GStreamer 1.20.3, MODE_30W · **Code:** `cpp/src/pipeline_builder.cpp`,
`cpp/src/app_config.{hpp,cpp}`, `cpp/src/main.cpp`,
`cpp/experiments/frame_timing/frame_timing_probe.cpp`

## 1. The problem being fixed

`jpegparse` (a `GstBaseParse` subclass in GStreamer 1.20.3 — confirmed with
`gst-inspect-1.0 jpegparse`) re-stamps every output buffer onto an ideal
`first_pts + n / framerate` grid anchored at **that camera's own first
frame**. The kernel capture stamp that `v4l2src` puts on the buffer — the only
true capture time a USB camera provides — is destroyed one element upstream of
`nvstreammux`. Because the four C920s enumerate sequentially over USB, their
grids disagree by the startup stagger (measured this campaign: **0 / 567 /
1135 / 1703 ms**), and everything downstream (`nvstreammux` sync-inputs,
`NvDsFrameMeta.buf_pts`, every latency metric) operates on that fiction.
Full pre-fix anatomy: *Why the Batch Never Filled*
(`why_the_batch_never_filled_overleaf.zip`, repo root) and
`cpp/experiments/frame_timing/README.md`.

## 2. The fix

`v4l2src` emits exactly **one complete JPEG per buffer**, so jpegparse is
strictly 1-in-1-out here. The fix is a pair of pad probes around each
`jpegparse` instance:

* **sink-pad probe** — pushes each incoming (true, kernel-derived) PTS into a
  per-camera FIFO;
* **src-pad probe** — pops the FIFO and overwrites the synthetic output PTS
  with the recorded true PTS.

A depth guard (FIFO capped at 4, warns once) bounds the damage if jpegparse
ever withheld a frame. Downstream elements (`nvjpegdec`, `nvvideoconvert`,
`nvstreammux`) pass PTS through bit-exact, so `NvDsFrameMeta.buf_pts` becomes
the true kernel capture stamp.

Where it lives:

| binary | switch | default |
|---|---|---|
| `cpp/multicam_rt` (production) | `--no-pts-fix` to disable (`capture: pts_fix:` in YAML) | **ON** |
| `frame_timing_probe` (instrument) | `--pts-fix` to enable | off (historic behaviour preserved) |

Two companion fixes landed in the same pass:

1. **Mux INI ordering** — in the new mux, the `batched-push-timeout` property
   and the INI's `overall-min-fps` are the *same internal knob* (last writer
   wins, `nvstreammux_batch.cpp:333`). Both apps used to set the property
   *before* loading the INI, so the shipped `min-fps=30` silently overrode
   every `--timeout-us`. The INI is now loaded first; the CLI value is
   authoritative. (This is what makes Step 5's timeout sweep physically real.)
2. **`buf_pts` in the JSON output** (`cpp/src/output_writer.cpp`) — gives every
   detection record a deterministic frame identity for cross-run matching.

## 3. Verification commands and results

```bash
cd cpp && make && cd experiments/frame_timing && make

# 15 s live probe run, fix ON (4x C920 at /dev/video0,2,4,6, 640x480@30 MJPG):
./frame_timing_probe --out-dir <dir> --duration 15 \
    --extra-controls "exposure_dynamic_framerate=0" --pts-fix
```

Checks on the 120 s definitive run (`results/baseline_pinned_fixed`, Step 2):

| check | result |
|---|---|
| pre-mux PTS == kernel capture PTS (per camera, exact) | **100.00 % on all 4 cameras** |
| `buf_pts` inside pushed batches == a true kernel stamp | **13 940 / 13 940 (100.00 %)** |
| per-camera PTS monotonic, no negative steps | yes (0 negative steps) |
| real cadence preserved (not the synthetic 33 333 333 ns grid) | Δ PTS p50 32.0 ms, p99 36.1 ms, std 18–35 ms |
| mux-visible startup stagger == true capture stagger | 1703.3 / 0 / 567.2 / 1135.3 ms (bit-identical) |
| sync-off batching behaviour unchanged | 100 % full batches, 32.2 ms cadence (same as unfixed leg) |

End-to-end sanity of the production app (live, full YOLO11n + NvSORT):

```bash
./cpp/multicam_rt --config config/camera_params.yaml --duration 12 \
    --log none --metrics-csv <csv>
# -> processed 1023 of 1023 arrived frames; 21 tracks; pts-fix=ON in banner
```

## 4. A second bug found while verifying: the pinning control never existed

The historic `*_pinned` runs passed `--extra-controls
"exposure_auto_priority=0"`. Enumerating the camera's real V4L2 controls via
`VIDIOC_QUERYCTRL` shows **`exposure_auto_priority` does not exist on kernel
5.15** — it was renamed **`exposure_dynamic_framerate`** (and the cameras had
it at 1 = allowed to halve the frame rate). The old control name was a silent
no-op; the historic "pinned" runs were only pinned because the scene happened
to be bright enough. Measured today in a dim room:

| control passed | capture cadence |
|---|---|
| `exposure_auto_priority=0` (broken name) | 68 ms (≈ 15 fps, auto-exposure halving) |
| `exposure_dynamic_framerate=0` (correct) | **32.0 ms p50** (36 ms p99, gaps 1–9/12 s) |

All campaign runs use the corrected control. Side effect: kernel sequence
gaps drop from 51–55 per camera (historic baseline_pinned, 117 s) to 9–10.

## 5. Evaluation

The fix restores exact, loss-free true-capture timestamps to every downstream
consumer at the cost of two pad probes per camera (~1 µs each, no extra
buffering, no behavioural change under sync-off). It is the prerequisite for
Step 4's sync-on result and makes the app's own e2e metrics honest (age vs
`buf_pts` is now age vs reality). Risk: if a camera ever delivered fragmented
JPEGs, the 1-in-1-out assumption would need the depth guard — never triggered
in ~40 min of campaign runs.
