/* pipeline_builder.hpp — constructs the GStreamer / DeepStream pipeline.
 *
 * Pipeline shape (both variants share the whole trunk):
 *
 *  per camera:  v4l2src ─► caps ─► jpegparse ─► nvjpegdec ─► nvvideoconvert ─► caps(NVMM,NV12)
 *      (or)     filesrc ─► qtdemux ─► h264parse ─► nvv4l2decoder ─► identity(sync) ─► …
 *                                                                        │
 *  all cameras ───────────────────────────────► NEW nvstreammux ◄────────┘  (batch = N)
 *                                │  sync-inputs = 0 (baseline)  |  1 (sync-on)
 *                                ▼
 *                            nvinfer (PGIE: YOLO11n, dynamic engine 1..N)
 *                                ▼
 *                            nvtracker (persistent IDs; detection probe on src)
 *                                ▼
 *                    fakesink (headless)  or  tiler ─► nvdsosd ─► window / MP4
 *
 * NEW nvstreammux notes (vs the legacy mux the Python app used):
 *  - enabled via USE_NEW_NVSTREAMMUX=yes (main.cpp sets it before gst_init);
 *    build_pipeline() verifies the new mux actually loaded and fails clearly
 *    if the legacy one did.
 *  - no width/height: the mux never scales — frames batch at native capture
 *    resolution and bbox output coords are already in source pixels.
 *  - no live-source: file replay is paced per camera by identity sync=true
 *    right after the decoder, which simulates live arrival at the mux (this
 *    also keeps sync-inputs experiments meaningful under replay).
 *  - sync-inputs=1 + max-latency is the "sync-on" variant: the mux time-aligns
 *    frames and DROPS any that miss the window. Drops are silent on DS 7.1
 *    (the "dropped" signal did not fire for sync discards — measured), so
 *    metrics.cpp counts loss as arrivals − processed frames.
 */
#pragma once

#include <gst/gst.h>

#include <memory>
#include <string>
#include <vector>

#include "app_config.hpp"

namespace mcrt {

struct BuiltPipeline {
  GstElement* pipeline = nullptr;  // owning ref — unref after set_state(NULL)
  GstElement* mux = nullptr;       // borrowed (owned by the pipeline)
  GstElement* tracker = nullptr;   // borrowed
  GstElement* tiler = nullptr;     // borrowed; nullptr when headless
  /* User-data of the pad probes the builder attaches (PTS-restore fix,
   * replay-skew injection). The probes hold raw pointers into these, so keep
   * the BuiltPipeline alive until after set_state(NULL). shared_ptr<void>
   * type-erases the ctx types (they are private to pipeline_builder.cpp). */
  std::vector<std::shared_ptr<void>> probe_ctxs;
};

/* Build the full pipeline from the parsed config. Throws std::runtime_error
 * on any element-creation or linking failure. */
BuiltPipeline build_pipeline(const AppConfig& cfg, bool display,
                             const std::string& record_path);

/* Attach a BUFFER probe on nvtracker's src pad — the probe sits on the
 * tracker (not PGIE) because object_id is only populated after nvtracker. */
void attach_detection_probe(GstElement* tracker, GstPadProbeCallback probe_fn,
                            gpointer user_data);

}  // namespace mcrt
