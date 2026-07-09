#include "pipeline_builder.hpp"

#include <cinttypes>
#include <cmath>
#include <cstdio>
#include <deque>
#include <filesystem>
#include <mutex>
#include <stdexcept>
#include <vector>

namespace mcrt {

namespace {

// --------------------------------------------------------------------------
// Small helpers
// --------------------------------------------------------------------------
GstElement* make_elem(const char* factory, const std::string& name) {
  GstElement* element = gst_element_factory_make(factory, name.c_str());
  if (element == nullptr)
    throw std::runtime_error(
        std::string("Failed to create GStreamer element '") + factory +
        "' (name='" + name + "'). Is the plugin installed? Check "
        "`gst-inspect-1.0 " + factory + "`.");
  return element;
}

void link_chain(const std::vector<GstElement*>& elements) {
  for (std::size_t i = 0; i + 1 < elements.size(); ++i) {
    if (!gst_element_link(elements[i], elements[i + 1]))
      throw std::runtime_error(
          std::string("Failed to link ") + GST_ELEMENT_NAME(elements[i]) +
          " -> " + GST_ELEMENT_NAME(elements[i + 1]) + ".");
  }
}

void set_caps(GstElement* capsfilter, const std::string& caps_str) {
  GstCaps* caps = gst_caps_from_string(caps_str.c_str());
  if (caps == nullptr)
    throw std::runtime_error("Bad caps string: " + caps_str);
  g_object_set(capsfilter, "caps", caps, nullptr);
  gst_caps_unref(caps);
}

/* qtdemux exposes its video pad dynamically; link it to h264parse on the fly. */
void on_demux_pad_added(GstElement*, GstPad* pad, gpointer user_data) {
  auto* parse = static_cast<GstElement*>(user_data);
  gchar* name = gst_pad_get_name(pad);
  const bool is_video = g_str_has_prefix(name, "video");
  g_free(name);
  if (!is_video) return;
  GstPad* sinkpad = gst_element_get_static_pad(parse, "sink");
  if (!gst_pad_is_linked(sinkpad)) gst_pad_link(pad, sinkpad);
  gst_object_unref(sinkpad);
}

// --------------------------------------------------------------------------
// The jpegparse PTS-restore fix
// --------------------------------------------------------------------------
/* jpegparse (GstBaseParse, GStreamer 1.20 / DS 7.1) re-stamps every output
 * buffer onto an ideal `first_pts + n/framerate` grid anchored at that
 * camera's own first frame. The kernel capture stamp v4l2src put on the
 * buffer — the only true capture time a USB camera provides — is destroyed,
 * and the four cameras' PTS disagree downstream by a constant 1.05–1.47 s
 * (the startup stagger; measured, see cpp/experiments/frame_timing/). The
 * grids also drift ~+0.65 %/s against real time (grid step 33.33 ms vs
 * ~29.8 fps delivered), so under sync-inputs every frame's apparent age
 * creeps toward the LATE cut and the pipeline decays progressively.
 *
 * The fix: v4l2src emits exactly one complete JPEG per buffer, so jpegparse
 * is 1-in-1-out here. A probe on its sink pad queues each true input PTS; a
 * probe on its src pad pops it and overwrites the synthetic output PTS.
 * Downstream elements (nvjpegdec, nvvideoconvert, nvstreammux) pass PTS
 * through bit-exact, so NvDsFrameMeta.buf_pts becomes the true capture
 * stamp. If jpegparse ever swallowed a corrupt frame the FIFO would drift by
 * one period; the depth guard below caps that and reports it.
 *
 * Measured impact (120 s live A/B, 2026-07-07, campaign_2026-07-07_ptsfix):
 *   sync-inputs=1 @ max-latency 33 ms:  14.7 % of frames kept, 40.4 % full
 *   batches (fix off)  ->  99.9 % kept, 100.0 % full batches, true in-batch
 *   spread p50 2.1 ms (fix on) — and the sync-off standing-queue staleness
 *   ladder (32/172/239/241 ms) flushes to uniform. Sync-off behaviour is
 *   bit-identical with the fix on (verified batch-for-batch), and the cost
 *   is two probe callbacks per frame: one deque push + one pop under an
 *   uncontended per-camera mutex, no extra buffering, no copies. */
struct PtsRestoreCtx {
  std::mutex mu;             // sink and src probes fire on one streaming
                             // thread today; the mutex keeps this correct if
                             // that ever changes
  std::deque<GstClockTime> fifo;
  int cam = 0;
  long warned = 0;
};

GstPadProbeReturn pts_fix_sink_probe(GstPad*, GstPadProbeInfo* info,
                                     gpointer user_data) {
  auto* ctx = static_cast<PtsRestoreCtx*>(user_data);
  GstBuffer* buf = GST_PAD_PROBE_INFO_BUFFER(info);
  if (buf == nullptr || !GST_BUFFER_PTS_IS_VALID(buf)) return GST_PAD_PROBE_OK;
  std::lock_guard<std::mutex> lock(ctx->mu);
  ctx->fifo.push_back(GST_BUFFER_PTS(buf));
  /* 1-in-1-out means depth ~1. Depth growth = jpegparse withheld a frame;
   * drop the stale head so the restore can never lag more than a few frames. */
  if (ctx->fifo.size() > 4) {
    ctx->fifo.pop_front();
    if (ctx->warned++ == 0)
      std::fprintf(stderr,
                   "[pts-fix] cam %d: jpegparse buffered more than 4 frames — "
                   "restored PTS may be off by one period.\n", ctx->cam);
  }
  return GST_PAD_PROBE_OK;
}

GstPadProbeReturn pts_fix_src_probe(GstPad*, GstPadProbeInfo* info,
                                    gpointer user_data) {
  auto* ctx = static_cast<PtsRestoreCtx*>(user_data);
  GstBuffer* buf = GST_PAD_PROBE_INFO_BUFFER(info);
  if (buf == nullptr) return GST_PAD_PROBE_OK;
  std::lock_guard<std::mutex> lock(ctx->mu);
  if (ctx->fifo.empty()) return GST_PAD_PROBE_OK;  // nothing recorded: leave as-is
  buf = gst_buffer_make_writable(buf);
  GST_BUFFER_PTS(buf) = ctx->fifo.front();
  ctx->fifo.pop_front();
  info->data = buf;
  return GST_PAD_PROBE_OK;
}

/* Attach the two restore probes around a jpegparse instance. The ctx is
 * stored (type-erased) in *ctxs and must outlive the streaming threads. */
void attach_pts_fix(GstElement* jparse, int index,
                    std::vector<std::shared_ptr<void>>* ctxs) {
  auto ctx = std::shared_ptr<PtsRestoreCtx>(new PtsRestoreCtx);
  ctx->cam = index;
  GstPad* sink = gst_element_get_static_pad(jparse, "sink");
  GstPad* src = gst_element_get_static_pad(jparse, "src");
  gst_pad_add_probe(sink, GST_PAD_PROBE_TYPE_BUFFER, pts_fix_sink_probe,
                    ctx.get(), nullptr);
  gst_pad_add_probe(src, GST_PAD_PROBE_TYPE_BUFFER, pts_fix_src_probe,
                    ctx.get(), nullptr);
  gst_object_unref(sink);
  gst_object_unref(src);
  ctxs->push_back(std::move(ctx));
}

// --------------------------------------------------------------------------
// Replay-skew injection (file sources) — ported from
// cpp/experiments/frame_timing/frame_timing_probe.cpp, where it was validated
// against the live baseline_pinned run (REPLAY_SKEW.md §7). A live camera
// carries two timelines; the replay keeps them separate:
//   true timeline   — when frames exist/arrive (cadence, stagger, gaps, ring
//                     drops). Built by the skew probe (PTS' = PTS*rate + skew,
//                     gap dropping) + identity sync=true pacing + the leaky
//                     ring queue. This paces buffers into the mux.
//   synthetic timeline — what an UNFIXED jpegparse would hand the mux: the
//                     restamp probe rewrites post-ring PTS onto an ideal
//                     first_pts + n*33.333 ms grid counting only survivors.
//                     Off by default: without it the mux sees the true
//                     timeline, i.e. the behaviour of the pts_fix pipeline.
//
// Parameter trap (restamp + sync experiments only): the emulated grids drift
// against real time at the rate set by the DELIVERED frame rate vs the
// 33.333 ms grid step. gap_every must make the delivered rate match live
// (~29.8 fps -> gap-every 44 for the 2026-07-07 reference); the naive
// modal-cadence derivation (gap-every 70/275) flips the drift sign, frames
// look future-stamped, nothing is ever LATE, and sync-on trivially succeeds
// — a pure artifact. Measured and documented in REPLAY_SKEW.md §9.
// --------------------------------------------------------------------------
struct ReplayFrontCtx {
  int cam = 0;
  double rate = 1.0;
  int64_t skew_ns = 0;
  int gap_every = 0;      // 0 = no injected gaps
  int gap_phase = 0;      // per-camera phase so cameras don't gap together
  int64_t in_idx = 0;
  int64_t out_idx = 0;
  int64_t first_syn_pts = -1;
};

GstPadProbeReturn replay_skew_probe(GstPad*, GstPadProbeInfo* info,
                                    gpointer user_data) {
  auto* ctx = static_cast<ReplayFrontCtx*>(user_data);

  /* qtdemux bounds the segment at clip duration; skewed PTS would fall
   * outside it and break pacing near EOS — lift the bound. */
  if (info->type & GST_PAD_PROBE_TYPE_EVENT_DOWNSTREAM) {
    GstEvent* ev = GST_PAD_PROBE_INFO_EVENT(info);
    if (ev != nullptr && GST_EVENT_TYPE(ev) == GST_EVENT_SEGMENT) {
      const GstSegment* seg = nullptr;
      gst_event_parse_segment(ev, &seg);
      GstSegment s = *seg;
      s.stop = GST_CLOCK_TIME_NONE;
      info->data = gst_event_new_segment(&s);
      gst_event_unref(ev);
    }
    return GST_PAD_PROBE_OK;
  }

  GstBuffer* buf = GST_PAD_PROBE_INFO_BUFFER(info);
  if (buf == nullptr || !GST_BUFFER_PTS_IS_VALID(buf)) return GST_PAD_PROBE_OK;

  const int64_t idx = ctx->in_idx++;
  if (ctx->gap_every > 0 &&
      (idx + ctx->gap_phase) % ctx->gap_every < 2)  // 2-frame gap, like live
    return GST_PAD_PROBE_DROP;

  buf = gst_buffer_make_writable(buf);
  const auto pts = static_cast<int64_t>(GST_BUFFER_PTS(buf));
  GST_BUFFER_PTS(buf) = static_cast<GstClockTime>(
      static_cast<int64_t>(pts * ctx->rate) + ctx->skew_ns);
  info->data = buf;
  return GST_PAD_PROBE_OK;
}

GstPadProbeReturn replay_restamp_probe(GstPad*, GstPadProbeInfo* info,
                                       gpointer user_data) {
  auto* ctx = static_cast<ReplayFrontCtx*>(user_data);
  GstBuffer* buf = GST_PAD_PROBE_INFO_BUFFER(info);
  if (buf == nullptr || !GST_BUFFER_PTS_IS_VALID(buf)) return GST_PAD_PROBE_OK;

  buf = gst_buffer_make_writable(buf);
  if (ctx->first_syn_pts < 0)
    ctx->first_syn_pts = static_cast<int64_t>(GST_BUFFER_PTS(buf));
  /* jpegparse's grid: anchor at the first output PTS, step by the NOMINAL
   * frame duration (33.333 ms for 30/1), count OUTPUT (surviving) frames. */
  GST_BUFFER_PTS(buf) = static_cast<GstClockTime>(
      ctx->first_syn_pts + ctx->out_idx * INT64_C(33333333));
  ctx->out_idx++;
  info->data = buf;
  return GST_PAD_PROBE_OK;
}

// --------------------------------------------------------------------------
// Per-camera source bin
// --------------------------------------------------------------------------
/* Live-capture front: v4l2src -> caps -> (JPEG decode | raw convert). Returns
 * the last element, to be linked into the shared NVMM tail. */
GstElement* build_v4l2_front(GstBin* nbin, int index, const CameraCfg& cam,
                             std::vector<std::shared_ptr<void>>* ctxs) {
  const std::string idx = std::to_string(index);

  GstElement* src = make_elem("v4l2src", "cam-src-" + idx);
  g_object_set(src, "device", cam.device.c_str(), "io-mode", 2, nullptr);
  gst_bin_add(nbin, src);
  std::vector<GstElement*> elements{src};

  if (cam.format == "mjpeg") {
    GstElement* srccaps = make_elem("capsfilter", "cam-srccaps-" + idx);
    set_caps(srccaps, "image/jpeg,width=" + std::to_string(cam.width) +
                          ",height=" + std::to_string(cam.height) +
                          ",framerate=" + std::to_string(cam.fps) + "/1");
    gst_bin_add(nbin, srccaps);
    elements.push_back(srccaps);
    /* C920 MJPEG is YUV 4:2:2 -> jpegparse ! nvjpegdec (HW) or jpegdec (SW).
     * NOT nvv4l2decoder mjpeg=1 (4:2:0 only) — see the main README. */
    if (cam.mjpeg_decoder == "nvjpegdec" || cam.mjpeg_decoder == "jpegdec") {
      GstElement* jparse = make_elem("jpegparse", "cam-jparse-" + idx);
      GstElement* jdec =
          make_elem(cam.mjpeg_decoder.c_str(), "cam-jpegdec-" + idx);
      gst_bin_add_many(nbin, jparse, jdec, nullptr);
      elements.push_back(jparse);
      elements.push_back(jdec);
      /* Restore the true kernel capture PTS that jpegparse would otherwise
       * replace with its synthetic per-camera grid (see attach_pts_fix). */
      if (cam.pts_fix) attach_pts_fix(jparse, index, ctxs);
    } else if (cam.mjpeg_decoder == "nvv4l2" ||
               cam.mjpeg_decoder == "nvv4l2decoder") {
      GstElement* dec = make_elem("nvv4l2decoder", "cam-jpegdec-" + idx);
      g_object_set(dec, "mjpeg", 1, nullptr);
      gst_bin_add(nbin, dec);
      elements.push_back(dec);
    } else {
      throw std::runtime_error("camera " + idx + ": unknown mjpeg_decoder '" +
                               cam.mjpeg_decoder +
                               "' (use 'nvjpegdec', 'jpegdec', or 'nvv4l2').");
    }
  } else if (cam.format == "raw" || cam.format == "yuyv" ||
             cam.format == "yuy2") {
    GstElement* srccaps = make_elem("capsfilter", "cam-srccaps-" + idx);
    set_caps(srccaps, "video/x-raw,format=YUY2,width=" +
                          std::to_string(cam.width) + ",height=" +
                          std::to_string(cam.height) + ",framerate=" +
                          std::to_string(cam.fps) + "/1");
    GstElement* swconv = make_elem("videoconvert", "cam-swconv-" + idx);
    gst_bin_add_many(nbin, srccaps, swconv, nullptr);
    elements.push_back(srccaps);
    elements.push_back(swconv);
  } else {
    throw std::runtime_error("camera " + idx + ": unknown capture format '" +
                             cam.format + "' (use 'mjpeg'/'raw').");
  }

  link_chain(elements);
  return elements.back();
}

/* Deterministic file-replay front: filesrc -> qtdemux -> h264parse ->
 * nvv4l2decoder -> identity(sync=true). The identity paces each stream to its
 * timestamps against the pipeline clock, simulating live per-camera arrival at
 * the mux — the new mux has no live-source property, and sink-side pacing
 * could not restore per-source arrival phase anyway (matters for sync-inputs
 * experiments). */
GstElement* build_file_front(GstBin* nbin, int index, const CameraCfg& cam,
                             const ReplayCfg& replay,
                             std::vector<std::shared_ptr<void>>* ctxs) {
  const std::string idx = std::to_string(index);
  if (cam.file.empty() || !std::filesystem::is_regular_file(cam.file))
    throw std::runtime_error("camera " + idx +
                             ": replay file not found: " + cam.file);

  GstElement* src = make_elem("filesrc", "cam-src-" + idx);
  g_object_set(src, "location", cam.file.c_str(), nullptr);
  GstElement* demux = make_elem("qtdemux", "cam-demux-" + idx);
  GstElement* parse = make_elem("h264parse", "cam-h264parse-" + idx);
  GstElement* dec = make_elem("nvv4l2decoder", "cam-dec-" + idx);
  /* The decoder's default output pool (~5 surfaces) is smaller than the
   * backlog a congested mux creates downstream; without headroom the pool —
   * not the ring queue below — becomes the throttle, the pacer starves, and
   * lateness accumulated during the stagger window freezes in permanently
   * (measured in the frame_timing experiment: a constant ~938 ms pacing
   * error). Extra surfaces keep the pacer on time and move the drop decision
   * to the ring, where it belongs. */
  g_object_set(dec, "num-extra-surfaces",
               static_cast<guint>(replay.surfaces), nullptr);
  GstElement* pace = make_elem("identity", "cam-pace-" + idx);
  g_object_set(pace, "sync", TRUE, nullptr);

  gst_bin_add_many(nbin, src, demux, parse, dec, pace, nullptr);
  if (!gst_element_link(src, demux))
    throw std::runtime_error("camera " + idx +
                             ": filesrc -> qtdemux link failed.");
  link_chain({parse, dec, pace});
  g_signal_connect(demux, "pad-added", G_CALLBACK(on_demux_pad_added), parse);

  /* Skew injection: rewrite PTS' = PTS*rate + skew (and drop gap frames) on
   * the decoder's src pad, BEFORE the pacing identity releases buffers at
   * running-time PTS' — this is what reproduces live startup stagger, true
   * cadence and kernel capture gaps on recorded clips. */
  const bool skewing = replay.skew_ms[index] != 0.0 ||
                       replay.rate[index] != 1.0 || replay.gap_every > 0;
  ReplayFrontCtx* rctx = nullptr;
  if (skewing || replay.restamp) {
    auto ctx = std::shared_ptr<ReplayFrontCtx>(new ReplayFrontCtx);
    ctx->cam = index;
    ctx->rate = replay.rate[index];
    ctx->skew_ns = static_cast<int64_t>(replay.skew_ms[index] * 1e6);
    ctx->gap_every = replay.gap_every;
    /* Stagger the gap pattern so cameras don't all gap on the same frame
     * index (live gaps are independent). */
    ctx->gap_phase = index * 17;
    rctx = ctx.get();
    ctxs->push_back(std::move(ctx));
  }
  if (skewing) {
    GstPad* dsrc = gst_element_get_static_pad(dec, "src");
    gst_pad_add_probe(dsrc,
                      static_cast<GstPadProbeType>(
                          GST_PAD_PROBE_TYPE_BUFFER |
                          GST_PAD_PROBE_TYPE_EVENT_DOWNSTREAM),
                      replay_skew_probe, rctx, nullptr);
    gst_object_unref(dsrc);
  }

  GstElement* tail = pace;
  if (replay.ring > 0) {
    /* The v4l2 kernel-ring stand-in: when the mux side backs up, this queue
     * fills to `ring` buffers and then DROPS THE NEWEST arrivals
     * (leaky=upstream) — the pacer never blocks, exactly like a live camera
     * whose driver drops frames when userspace can't dequeue. */
    GstElement* ringq = make_elem("queue", "cam-ring-" + idx);
    g_object_set(ringq, "max-size-buffers", static_cast<guint>(replay.ring),
                 "max-size-bytes", 0u, "max-size-time",
                 static_cast<guint64>(0), "leaky", 1 /* upstream */,
                 "silent", TRUE, nullptr);
    gst_bin_add(nbin, ringq);
    link_chain({pace, ringq});
    tail = ringq;
  }

  /* Optional jpegparse emulation (the UNFIXED live pipeline): rewrite the
   * post-ring PTS onto the ideal synthetic grid, counting only survivors.
   * Sits after the ring so injected drops vanish from the timeline, exactly
   * like live jpegparse never counting kernel-dropped frames. */
  if (replay.restamp) {
    GstPad* tsrc = gst_element_get_static_pad(tail, "src");
    gst_pad_add_probe(tsrc, GST_PAD_PROBE_TYPE_BUFFER, replay_restamp_probe,
                      rctx, nullptr);
    gst_object_unref(tsrc);
  }
  return tail;
}

/* One camera's capture branch as a self-contained bin exposing a single ghost
 * src pad emitting video/x-raw(memory:NVMM),NV12 ready for an nvstreammux
 * sink pad. (No valve: this app has no camera skipping.) */
GstElement* build_source_bin(int index, const CameraCfg& cam,
                             const ReplayCfg& replay,
                             std::vector<std::shared_ptr<void>>* ctxs,
                             bool dropold, int conv_output_buffers) {
  const std::string idx = std::to_string(index);
  GstElement* bin = gst_bin_new(("source-bin-" + idx).c_str());
  GstBin* nbin = GST_BIN(bin);

  GstElement* head_last = nullptr;
  if (cam.source_type == "v4l2") {
    head_last = build_v4l2_front(nbin, index, cam, ctxs);
  } else if (cam.source_type == "file") {
    head_last = build_file_front(nbin, index, cam, replay, ctxs);
  } else {
    throw std::runtime_error("camera " + idx + ": unknown source_type '" +
                             cam.source_type + "' (use 'v4l2' or 'file').");
  }

  /* Into GPU memory: NVMM NV12 is what nvstreammux and DeepStream require. */
  GstElement* conv = make_elem("nvvideoconvert", "cam-conv-" + idx);
  if (conv_output_buffers > 0)
    g_object_set(conv, "output-buffers",
                 static_cast<guint>(conv_output_buffers), nullptr);
  GstElement* nvmmcaps = make_elem("capsfilter", "cam-nvmmcaps-" + idx);
  set_caps(nvmmcaps, "video/x-raw(memory:NVMM),format=NV12");
  gst_bin_add_many(nbin, conv, nvmmcaps, nullptr);
  link_chain({head_last, conv, nvmmcaps});
  GstElement* bin_tail = nvmmcaps;

  if (dropold) {
    /* Keep-newest baseline: a 1-deep leaky=downstream queue drops the OLDEST
     * queued frame when a new one arrives and downstream is busy. This is the
     * config-only alternative to a scheduler; whether it has any effect
     * depends on the mux exerting backpressure (measured in gate G4). */
    GstElement* koq = make_elem("queue", "cam-dropold-" + idx);
    g_object_set(koq, "max-size-buffers", 1u, "max-size-bytes", 0u,
                 "max-size-time", static_cast<guint64>(0), "leaky",
                 2 /* downstream */, "silent", TRUE, nullptr);
    gst_bin_add(nbin, koq);
    link_chain({nvmmcaps, koq});
    bin_tail = koq;
  }

  GstPad* target = gst_element_get_static_pad(bin_tail, "src");
  GstPad* ghost = gst_ghost_pad_new("src", target);
  gst_pad_set_active(ghost, TRUE);
  gst_element_add_pad(bin, ghost);
  gst_object_unref(target);
  return bin;
}

// --------------------------------------------------------------------------
// nvstreammux (NEW) / nvinfer / nvtracker
// --------------------------------------------------------------------------
GstElement* build_streammux(const AppConfig& cfg, int num_cams) {
  GstElement* mux = make_elem("nvstreammux", "stream-muxer");

  /* The legacy mux has width/height (it scales); the new one does not. If we
   * see them, the env switch did not take — fail with the fix, not a subtly
   * different pipeline. */
  if (g_object_class_find_property(G_OBJECT_GET_CLASS(mux), "width") != nullptr) {
    gst_object_unref(mux);
    throw std::runtime_error(
        "The LEGACY nvstreammux was loaded, but this app is written for the "
        "NEW mux. Run with USE_NEW_NVSTREAMMUX=yes (the app sets it "
        "automatically unless your environment overrides it — check "
        "`echo $USE_NEW_NVSTREAMMUX`).");
  }

  /* batch-size = camera count normally; a scheduler run overrides it to K
   * (the per-release frame count) so a released K-burst completes the batch
   * via is_ready() and pushes immediately as ONE batch. */
  const int mux_batch =
      cfg.mux_batch_override > 0 ? cfg.mux_batch_override : num_cams;
  g_object_set(mux, "batch-size", static_cast<guint>(mux_batch), nullptr);

  /* Optional new-mux INI (batching algorithm / fps bounds / per-source caps).
   * Ours pins max-same-source-frames=1 so a batch never carries two frames of
   * one camera — matching the legacy one-frame-per-camera batching.
   *
   * THE INI IS THE ONLY PUSH-DEADLINE KNOB THAT WORKS on this mux build
   * (DS 7.1). Measured 2026-07-08, 8-run matrix, 1–100 ms with and without
   * an INI: the batched-push-timeout PROPERTY below changes nothing — the
   * mux re-reads its INI/defaults at state change, after any property set.
   * The deadline the mux honours is the INI's overall-min-fps (floor cadence
   * for pushing an incomplete batch); overall-max-fps must be >= min-fps.
   * Consequences that cost real latency until diagnosed:
   *   - the shipped min-fps=30 imposed a ~33 ms hold on every run no matter
   *     what --timeout-us said (the ~115 ms structural e2e penalty found in
   *     experiments/results/param_sweep_locked/);
   *   - with NO INI, the mux default min-fps=5 gives a 200 ms service cycle
   *     — under sync-inputs that alone throttled live capture to 20.5
   *     fps/cam and pushed staleness to ~310 ms (results/sync_fixed_ml33).
   * To vary the deadline per run, generate an INI with
   * overall-min-fps-n=1000000, -d=<push_us> — scripts/timeout_sweep_cpp.py
   * does exactly this. Measured effect once the knob is real: dynamic-engine
   * e2e 15.3 ms mean at a 1 ms deadline vs 69 ms at 33.3 ms (sync off). */
  if (!cfg.mux.config_file.empty())
    g_object_set(mux, "config-file-path", cfg.mux.config_file.c_str(), nullptr);

  /* Kept for the record only: measured inert on DS 7.1 (see the INI comment
   * above) — fill and batch rate are identical from 1 to 100 ms whatever
   * this is set to. Harmless, and the value still lands in the metrics CSV
   * as the run's intended deadline. Set after the INI so that IF a future
   * DS release honours the property, the CLI value wins. */
  g_object_set(mux, "batched-push-timeout",
               static_cast<gint>(cfg.mux.batched_push_timeout_us), nullptr);

  /* Baseline vs sync-on. sync-inputs=1 time-aligns frames across cameras and
   * drops any frame that cannot align within max-latency. (The mux has a
   * "dropped" signal, but on DS 7.1 sync discards did NOT emit it — metrics
   * measures loss as arrivals − processed instead.) Off = baseline: batch
   * whatever has arrived. */
  g_object_set(mux, "sync-inputs", cfg.mux.sync_inputs ? TRUE : FALSE, nullptr);
  if (cfg.mux.sync_inputs)
    g_object_set(mux, "max-latency",
                 static_cast<guint64>(cfg.mux.max_latency_ns), nullptr);
  return mux;
}

GstElement* build_pgie(const AppConfig& cfg, int num_cams) {
  GstElement* pgie = make_elem("nvinfer", "primary-inference");
  g_object_set(pgie, "config-file-path", cfg.pgie_config_file.c_str(), nullptr);
  /* Engine is dynamic-batch (min 1 / max 4): one engine serves any camera
   * count; a partial batch under sync-on runs natively (no padding). */
  const int pgie_batch =
      cfg.mux_batch_override > 0 ? cfg.mux_batch_override : num_cams;
  g_object_set(pgie, "batch-size", static_cast<guint>(pgie_batch), nullptr);
  return pgie;
}

GstElement* build_tracker(const AppConfig& cfg) {
  GstElement* tracker = make_elem("nvtracker", "tracker");
  const TrackerCfg& t = cfg.tracker;
  g_object_set(tracker, "ll-lib-file", t.ll_lib_file.c_str(),
               "ll-config-file", t.ll_config_file.c_str(),
               "tracker-width", static_cast<guint>(t.width),
               "tracker-height", static_cast<guint>(t.height),
               "gpu-id", static_cast<guint>(t.gpu_id),
               "display-tracking-id", TRUE, nullptr);
  return tracker;
}

// --------------------------------------------------------------------------
// Output tail: headless fakesink, or the debug display / recording branch
// --------------------------------------------------------------------------
void tiler_grid(int n, int* rows, int* cols) {
  *cols = static_cast<int>(std::ceil(std::sqrt(static_cast<double>(n))));
  *rows = static_cast<int>(std::ceil(static_cast<double>(n) / *cols));
}

/* Link a sink branch's first element to `head` (a tee needs a request pad). */
void branch_from(GstElement* head, bool head_is_tee, GstElement* first) {
  if (head_is_tee) {
    GstPad* teepad = gst_element_request_pad_simple(head, "src_%u");
    GstPad* sinkpad = gst_element_get_static_pad(first, "sink");
    const GstPadLinkReturn ret = gst_pad_link(teepad, sinkpad);
    gst_object_unref(teepad);
    gst_object_unref(sinkpad);
    if (ret != GST_PAD_LINK_OK)
      throw std::runtime_error("Failed to link tee -> sink branch.");
  } else {
    link_chain({head, first});
  }
}

void attach_display_sink(GstBin* pipeline, GstElement* head, bool head_is_tee,
                         const DisplayCfg& dcfg) {
  GstElement* queue = make_elem("queue", "disp-queue");
  GstElement* sink = make_elem("nv3dsink", "disp-sink");
  g_object_set(sink, "sync", FALSE, nullptr);  // live: never wait on the clock
  if (dcfg.window_width > 0)
    g_object_set(sink, "window-width", static_cast<guint>(dcfg.window_width),
                 nullptr);
  if (dcfg.window_height > 0)
    g_object_set(sink, "window-height", static_cast<guint>(dcfg.window_height),
                 nullptr);
  gst_bin_add_many(pipeline, queue, sink, nullptr);
  branch_from(head, head_is_tee, queue);
  link_chain({queue, sink});
}

void attach_record_branch(GstBin* pipeline, GstElement* head, bool head_is_tee,
                          const std::string& path) {
  GstElement* queue = make_elem("queue", "rec-queue");
  GstElement* conv = make_elem("nvvideoconvert", "rec-conv");
  GstElement* caps = make_elem("capsfilter", "rec-caps");
  set_caps(caps, "video/x-raw(memory:NVMM),format=NV12");
  GstElement* enc = make_elem("nvv4l2h264enc", "rec-enc");
  GstElement* parse = make_elem("h264parse", "rec-parse");
  GstElement* mux = make_elem("qtmux", "rec-mux");
  GstElement* sink = make_elem("filesink", "rec-sink");
  g_object_set(sink, "location", path.c_str(), "sync", FALSE, nullptr);
  gst_bin_add_many(pipeline, queue, conv, caps, enc, parse, mux, sink, nullptr);
  branch_from(head, head_is_tee, queue);
  link_chain({queue, conv, caps, enc, parse, mux, sink});
}

BuiltPipeline build_tail(GstBin* pipeline, GstElement* tracker,
                         const AppConfig& cfg, int num_cams, bool display,
                         const std::string& record_path, BuiltPipeline built) {
  if (!display && record_path.empty()) {
    /* Headless: frames are discarded; detections leave via the probe. File
     * replay is already real-time paced per camera (identity sync=true), so
     * the sink never waits on the clock in either mode. */
    GstElement* conv = make_elem("nvvideoconvert", "sink-conv");
    GstElement* sink = make_elem("fakesink", "sink");
    g_object_set(sink, "sync", FALSE, "enable-last-sample", FALSE, nullptr);
    gst_bin_add_many(pipeline, conv, sink, nullptr);
    link_chain({tracker, conv, sink});
    return built;
  }

  /* Visual branch: composite N cameras -> draw boxes/labels/track-IDs. */
  int rows = 1, cols = 1;
  tiler_grid(num_cams, &rows, &cols);
  GstElement* tiler = make_elem("nvmultistreamtiler", "tiler");
  g_object_set(tiler, "rows", static_cast<guint>(rows),
               "columns", static_cast<guint>(cols),
               "width", static_cast<guint>(cfg.display.width),
               "height", static_cast<guint>(cfg.display.height), nullptr);

  GstElement* osd_conv = make_elem("nvvideoconvert", "osd-conv");
  GstElement* osd_caps = make_elem("capsfilter", "osd-caps");
  set_caps(osd_caps, "video/x-raw(memory:NVMM),format=RGBA");  // nvdsosd needs RGBA
  GstElement* osd = make_elem("nvdsosd", "osd");
  g_object_set(osd, "process-mode", 1, "display-bbox", TRUE, "display-text",
               TRUE, nullptr);

  gst_bin_add_many(pipeline, tiler, osd_conv, osd_caps, osd, nullptr);
  link_chain({tracker, tiler, osd_conv, osd_caps, osd});
  built.tiler = tiler;

  /* One sink -> link straight off the OSD; two sinks -> fan out with a tee. */
  GstElement* head = osd;
  bool head_is_tee = false;
  if (display && !record_path.empty()) {
    GstElement* tee = make_elem("tee", "viz-tee");
    gst_bin_add(pipeline, tee);
    link_chain({osd, tee});
    head = tee;
    head_is_tee = true;
  }
  if (display) attach_display_sink(pipeline, head, head_is_tee, cfg.display);
  if (!record_path.empty())
    attach_record_branch(pipeline, head, head_is_tee, record_path);
  return built;
}

}  // namespace

// --------------------------------------------------------------------------
// Public API
// --------------------------------------------------------------------------
BuiltPipeline build_pipeline(const AppConfig& cfg, bool display,
                             const std::string& record_path) {
  const int num_cams = static_cast<int>(cfg.cameras.size());
  if (num_cams < 1)
    throw std::runtime_error("No cameras configured — 'cameras' list is empty.");

  BuiltPipeline built;
  built.pipeline = gst_pipeline_new("multicam-perception-rt");
  if (built.pipeline == nullptr)
    throw std::runtime_error("Failed to create GstPipeline.");
  GstBin* bin = GST_BIN(built.pipeline);

  // Shared, single-instance trunk elements.
  built.mux = build_streammux(cfg, num_cams);
  GstElement* pgie = build_pgie(cfg, num_cams);
  built.tracker = build_tracker(cfg);
  gst_bin_add_many(bin, built.mux, pgie, built.tracker, nullptr);

  /* Each camera branch links into one nvstreammux request sink pad. The pad
   * number IS the identity: sink_<i> becomes source_id=<i> in the metadata,
   * which becomes camera_id in every output record — so camera N in the
   * YAML's `cameras:` list is camera N everywhere downstream. */
  for (int index = 0; index < num_cams; ++index) {
    GstElement* source_bin =
        build_source_bin(index, cfg.cameras[index], cfg.replay,
                         &built.probe_ctxs, cfg.dropold,
                         cfg.conv_output_buffers);
    gst_bin_add(bin, source_bin);
    GstPad* srcpad = gst_element_get_static_pad(source_bin, "src");
    const std::string pad_name = "sink_" + std::to_string(index);
    GstPad* sinkpad =
        gst_element_request_pad_simple(built.mux, pad_name.c_str());
    if (sinkpad == nullptr) {
      gst_object_unref(srcpad);
      throw std::runtime_error("nvstreammux did not provide request pad '" +
                               pad_name + "'.");
    }
    const GstPadLinkReturn ret = gst_pad_link(srcpad, sinkpad);
    gst_object_unref(srcpad);
    gst_object_unref(sinkpad);
    if (ret != GST_PAD_LINK_OK)
      throw std::runtime_error("Failed to link source-bin-" +
                               std::to_string(index) + " to nvstreammux.");
  }

  // mux -> pgie -> tracker, then the selected tail.
  link_chain({built.mux, pgie, built.tracker});
  built = build_tail(bin, built.tracker, cfg, num_cams, display, record_path,
                     built);
  return built;
}

void attach_detection_probe(GstElement* tracker, GstPadProbeCallback probe_fn,
                            gpointer user_data) {
  GstPad* src_pad = gst_element_get_static_pad(tracker, "src");
  if (src_pad == nullptr)
    throw std::runtime_error(
        "nvtracker has no src pad to attach the probe to.");
  gst_pad_add_probe(src_pad, GST_PAD_PROBE_TYPE_BUFFER, probe_fn, user_data,
                    nullptr);
  gst_object_unref(src_pad);
}

}  // namespace mcrt
