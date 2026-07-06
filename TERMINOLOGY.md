# Terminology & Component Reference

Companion to `PIPELINE.md` (which draws the data/control planes of the Python
app). This doc defines every term and component name used across the project —
timing vocabulary, the new-nvstreammux internals, and how data/events/queries
flow **upstream** and **downstream**. Everything here was verified against the
mux source shipped on this Jetson
(`/opt/nvidia/deepstream/deepstream/sources/`) and live measurements on the
4×C920 rig (2026-07-06); code references are `file:line`.

---

## 1. Upstream / downstream — what the words mean

A GStreamer pipeline is a directed graph. **Downstream** = the direction
buffers (frames) flow: camera → sink. **Upstream** = toward the source.
Three different things travel through the graph, in different directions:

| what | direction | examples in this project |
|---|---|---|
| **Buffers** (frames + metadata) | downstream | camera frames, batched buffers |
| **Events** (in-band control) | mostly downstream | `SEGMENT` (defines the timeline; the mux captures it per-pad at `gstnvstreammux.cpp:1178`), `CAPS`, `EOS` |
| **Queries** (out-of-band questions) | mostly upstream | `LATENCY` — the mux asks upstream "how late can buffers legitimately be?" (`gstnvstreammux.cpp:1419`); v4l2src answers ~1 frame duration |
| **Backpressure** | upstream | a blocked/slow downstream element stops accepting buffers → upstream buffer pools drain → v4l2src stalls/drops (this is what killed the `ml500_*` grid runs) |

**Buffer pools**: NVMM (GPU) buffers are recycled from small fixed pools
(~4 per element). Any element that *holds* buffers (the mux under
`sync-inputs` holds EARLY frames) borrows from upstream pools; hold too many
for too long and capture stalls with `STREAMON`/`-5` errors.

**Pad probe**: a callback tapped onto an element's pad, seeing every buffer
that passes. All project metrics are probes (§5) — they observe, never modify.

---

## 2. Pipeline components (C++ app, `cpp/src/pipeline_builder.cpp`)

Upstream → downstream. Names in parentheses are the `gst_element` names used
in code (what `gst_bin_get_by_name` and the logs refer to).

### Per-camera source bin (`source-bin-<N>`, one per camera)

| element (name) | role |
|---|---|
| `v4l2src` (`cam-src-N`) | live capture from `/dev/videoX`, `io-mode=2` (MMAP). PTS comes from the kernel driver (§3) |
| `capsfilter` (`cam-srccaps-N`) | pins format: `image/jpeg 640×480 @30fps` (MJPEG) or YUY2 (raw) |
| `jpegparse` → `nvjpegdec` (`cam-jparse-N`, `cam-jpegdec-N`) | parse + hardware-decode MJPEG (C920 is 4:2:2 — `nvv4l2decoder mjpeg=1` can't do it) |
| `filesrc`→`qtdemux`→`h264parse`→`nvv4l2decoder`→`identity sync=true` (`cam-pace-N`) | **replay front**: decodes a recorded clip and paces it to its container PTS so the mux sees live-like per-camera arrival phase |
| `nvvideoconvert` (`cam-conv-N`) + `capsfilter` (`cam-nvmmcaps-N`) | upload into GPU memory: `video/x-raw(memory:NVMM),NV12` — the only thing nvstreammux accepts |

### Shared tail

| element (name) | role |
|---|---|
| `nvstreammux` (`stream-muxer`) | batches N cameras' frames into one buffer — the heart of every experiment; internals in §4 |
| `nvinfer` (`primary-inference`, "PGIE") | runs the YOLO11n TensorRT engine once per **batch** (that's the whole point of batching) |
| `nvtracker` (`tracker`) | assigns persistent per-camera track IDs to detections |
| `queue` (`disp-queue`) + `nv3dsink` (`disp-sink`) | display path (debug); `sync=FALSE` — never waits on the clock |
| `nvmultistreamtiler` (`tiler`) + `nvdsosd` (`osd`) | tile 4 streams into one image + draw boxes (display mode) |
| `tee` (`viz-tee`) | forks the stream when display and recording are both on |
| `nvvideoconvert`→`nvv4l2h264enc`→`h264parse`→`qtmux`→`filesink` (`rec-*`) | recording branch |
| `fakesink` (`sink`) | headless mode: discard output after metadata is read |

**Metadata**: `NvDsBatchMeta` rides on the batched buffer (frames-in-batch,
per-frame `NvDsFrameMeta` with `source_id`, `buf_pts`, detections). Parsed in
`cpp/src/detection_parser.cpp`.

---

## 3. Timing vocabulary

| term | meaning |
|---|---|
| **pipeline clock** | the shared time source; here `GstSystemClock` = `CLOCK_MONOTONIC` (verified: 17 µs apart) |
| **base_time** | snapshot of the clock when the pipeline went PLAYING. Constant per run, changes every restart. `gst_element_get_base_time()` |
| **running time** | `clock_now − base_time`, "time since PLAYING" |
| **PTS** | presentation timestamp on a buffer, expressed as running time. `guint64` nanoseconds |
| **driver timestamp** | uvcvideo stamps each V4L2 buffer with `CLOCK_MONOTONIC` at frame reception. This rig: `clock=CLOCK_MONOTONIC`, **`hwtimestamps=0`** → the stamp is *host time at start of USB delivery*, i.e. after in-camera exposure+readout+MJPEG-encode. v4l2src rebases it: `PTS = driver_stamp − base_time` |
| **absolute capture time** | `base_time + PTS` (monotonic); add the `REALTIME − MONOTONIC` offset for wall clock. Measured: frames are already **20–28 ms old** at userspace arrival (in-camera latency + USB transfer spread over the frame period) |
| **DTS** | decode timestamp; empty on live capture. `do-timestamp=true` only *fills* it with push-time — it does **not** touch the driver PTS (measured; basesrc never overwrites a valid timestamp), and the mux reads only PTS, so the flag is a no-op here |
| **segment** | per-stream event mapping buffer timestamps → running time; the mux keeps one per sink pad (`NvTimeSync::SetSegment`) |
| **phase** | where a camera's capture instants sit inside the repeating 1/30 s grid: `PTS mod 33.33 ms`. Free-running C920s have independent phases |
| **wobble** | the C920 delivers a ~32/32/36 ms step pattern (4 ms quantization, averaging exactly 30 fps) — each camera's phase jitters ±2 ms frame-to-frame |
| **drift** | cameras' crystals differ (measured: −121…+111 ms/min phase slide, i.e. real rates ≈ 29.94–30.06 fps), so relative phase between two cameras slides through the whole period in seconds-to-minutes; camera pairings under sync are transient |

---

## 4. New nvstreammux internals (the terms that matter for sync)

Source: `sources/gst-plugins/gst-nvmultistream2/` (GStreamer wrapper, incl.
`gstnvtimesynch.cpp` = the sync module) and `sources/libs/nvstreammux/`
(batching core). Key vocabulary:

| term | meaning |
|---|---|
| **sink pad queue** | per-camera FIFO of wrapped buffers + events inside the mux |
| **batch policy** | `BatchPolicy` (`nvstreammux_batch.cpp`) — decides what goes in a batch (round-robin over sources, `algorithm-type=1`) |
| **adaptive batching** | batch-size = number of connected sources (our mode; `batch-size` property mostly moot) |
| **max-same-source-frames=1** | ≤1 frame per camera per batch (`mux_config.txt`) |
| **NvTimeSync verdict** | with `sync-inputs=1`, every queued buffer is classified per scan (`get_synch_info`, `gstnvtimesynch.cpp:119-168`): |

```
EARLY   if  buffer_running_time > now − minFpsDuration          → wait (stop scanning this pad)
LATE    if  buffer_running_time + upstreamLatency < now − minFpsDuration → erase + "dropped" signal
ONTIME  otherwise                                                → eligible for the batch
```

| term | meaning |
|---|---|
| **minFpsDuration** | the EARLY gate: a frame is unbatchable until it is this old. = `1/overall-min-fps` (33.3 ms here) — **but** setting the `batched-push-timeout` property *overwrites* overall-min-fps (`set_batch_push_timeout`, `nvstreammux_batch.cpp:333`); last writer wins (property at construction vs config-file parse — an ordering landmine). Pushed into the sync module on each SEGMENT event (`gstnvstreammux.cpp:1179`) |
| **upstreamLatency** | the LATE gate width: `max-latency property + queried upstream latency` (`gstnvstreammux.cpp:1423`; just `max-latency` at property-set time, :1616; init 0) |
| **Window A — eligibility window** | per-frame: age ∈ `[minFpsDuration, minFpsDuration + upstreamLatency]`. Wide (tens–hundreds of ms). Controls *retention/drop*, not batch composition |
| **Window B — co-batching window** | a non-full batch holding ≥1 frame is **force-pushed** once `now ≥ last_batch_time + 1/overall-max-fps` (`min_dur_time`, `nvstreammux_batch.cpp:96`; enforced by `is_ready_or_due` → `calculate_delay()==0`, :214-231). With our `overall-max-fps=120` this is **8.33 ms** — the narrow window that decides who shares a batch. Frames co-batch only if they mature within the same ≤8.33 ms slot |
| **partial batch** | `num_frames_in_batch < batch-size` — documented-normal under sync with unaligned sources |
| **dropped signal** | emitted per LATE-erased frame; counted as `drops_cum` in `cpp/src/metrics.cpp:66` |

**Why sync-on caps at ~2 on this rig**: four free-running phases spread over
33.3 ms; Window B is 8.33 ms wide and re-anchors at every push. A pair
coincides within one slot regularly (6 pairs, few-% each); a triple squares
that small probability; drift dissolves any alignment within seconds.
**Untested knob**: `overall-max-fps` was 120 in *every* experiment. Lowering
it to 30 widens Window B to a full frame period — the one configuration that
might legitimately produce full sync-on batches. (`overall-min-fps` /
push-timeout and `max-latency` — the two axes we swept — only move Window A.)

---

## 5. Measurement terminology (`cpp/src/metrics.cpp`, CSV columns)

| term | meaning |
|---|---|
| `src_probe` / `mux_probe` / `tracker_probe` | pad probes at source-bin exit / mux output / tracker output |
| `t_mono` | seconds since run start (`g_get_monotonic_time`, same clock as PTS) |
| `n_in_batch` | `batch_meta.num_frames_in_batch` — the headline sync metric |
| `n_real` | frames whose `buf_pts` matched a recorded source-arrival stamp (erase-on-match ⇒ a repeated/padded frame can never match twice) |
| `compute_ms` | mux-src → tracker-src latency, joined on the *batch* buffer PTS |
| `e2e_ms` | source-arrival → tracker latency, joined on per-frame `buf_pts`. **Understates** true glass-to-out by the ~20–28 ms pre-arrival age; the capture-referenced version is `now − (base_time + buf_pts)` |
| `buf_pts` | the original per-source PTS, copied by DeepStream into `NvDsFrameMeta` — the join key tying output frames back to capture |
| `drops_cum` / `arrivals_cum` | mux "dropped" signal count / source-arrival count (columns exist only in CSVs from the current binary; the sync-grid CSVs predate them) |

---

## 6. One-paragraph flow summary

Downstream: each C920 exposes a frame (unknown instant), encodes MJPEG,
streams it over USB (~20–28 ms total, PTS stamped at delivery start),
`nvjpegdec` decodes, `nvvideoconvert` uploads to NVMM, the frame queues at its
mux sink pad. The mux — every wake, at most one push per Window B — scans pads
round-robin, takes ≤1 ONTIME frame per camera, attaches `NvDsBatchMeta`, and
pushes one batched buffer. `nvinfer` runs the engine once on the whole batch,
`nvtracker` adds IDs, probes harvest metadata, and the sink discards or
displays. Upstream, meanwhile: the mux answers latency queries from
downstream and issues them to the sources, SEGMENT/EOS events ride down with
the data, and backpressure propagates up — if the mux holds too many buffers
(sync-on with large `max-latency`), the per-camera NVMM pools drain and
capture itself stalls.
