#include "pipeline_builder.hpp"

#include <cmath>
#include <filesystem>
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
// Per-camera source bin
// --------------------------------------------------------------------------
/* Live-capture front: v4l2src -> caps -> (JPEG decode | raw convert). Returns
 * the last element, to be linked into the shared NVMM tail. */
GstElement* build_v4l2_front(GstBin* nbin, int index, const CameraCfg& cam) {
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
GstElement* build_file_front(GstBin* nbin, int index, const CameraCfg& cam) {
  const std::string idx = std::to_string(index);
  if (cam.file.empty() || !std::filesystem::is_regular_file(cam.file))
    throw std::runtime_error("camera " + idx +
                             ": replay file not found: " + cam.file);

  GstElement* src = make_elem("filesrc", "cam-src-" + idx);
  g_object_set(src, "location", cam.file.c_str(), nullptr);
  GstElement* demux = make_elem("qtdemux", "cam-demux-" + idx);
  GstElement* parse = make_elem("h264parse", "cam-h264parse-" + idx);
  GstElement* dec = make_elem("nvv4l2decoder", "cam-dec-" + idx);
  GstElement* pace = make_elem("identity", "cam-pace-" + idx);
  g_object_set(pace, "sync", TRUE, nullptr);

  gst_bin_add_many(nbin, src, demux, parse, dec, pace, nullptr);
  if (!gst_element_link(src, demux))
    throw std::runtime_error("camera " + idx +
                             ": filesrc -> qtdemux link failed.");
  link_chain({parse, dec, pace});
  g_signal_connect(demux, "pad-added", G_CALLBACK(on_demux_pad_added), parse);
  return pace;
}

/* One camera's capture branch as a self-contained bin exposing a single ghost
 * src pad emitting video/x-raw(memory:NVMM),NV12 ready for an nvstreammux
 * sink pad. (No valve: this app has no camera skipping.) */
GstElement* build_source_bin(int index, const CameraCfg& cam) {
  const std::string idx = std::to_string(index);
  GstElement* bin = gst_bin_new(("source-bin-" + idx).c_str());
  GstBin* nbin = GST_BIN(bin);

  GstElement* head_last = nullptr;
  if (cam.source_type == "v4l2") {
    head_last = build_v4l2_front(nbin, index, cam);
  } else if (cam.source_type == "file") {
    head_last = build_file_front(nbin, index, cam);
  } else {
    throw std::runtime_error("camera " + idx + ": unknown source_type '" +
                             cam.source_type + "' (use 'v4l2' or 'file').");
  }

  /* Into GPU memory: NVMM NV12 is what nvstreammux and DeepStream require. */
  GstElement* conv = make_elem("nvvideoconvert", "cam-conv-" + idx);
  GstElement* nvmmcaps = make_elem("capsfilter", "cam-nvmmcaps-" + idx);
  set_caps(nvmmcaps, "video/x-raw(memory:NVMM),format=NV12");
  gst_bin_add_many(nbin, conv, nvmmcaps, nullptr);
  link_chain({head_last, conv, nvmmcaps});

  GstPad* target = gst_element_get_static_pad(nvmmcaps, "src");
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

  g_object_set(mux, "batch-size", static_cast<guint>(num_cams), nullptr);
  /* New-mux batched-push-timeout: how long to wait before pushing an
   * incomplete batch. ~one frame interval, same default as the Python app. */
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

  /* Optional new-mux INI (batching algorithm / fps bounds / per-source caps).
   * Ours pins max-same-source-frames=1 so a batch never carries two frames of
   * one camera — matching the legacy one-frame-per-camera batching. */
  if (!cfg.mux.config_file.empty())
    g_object_set(mux, "config-file-path", cfg.mux.config_file.c_str(), nullptr);
  return mux;
}

GstElement* build_pgie(const AppConfig& cfg, int num_cams) {
  GstElement* pgie = make_elem("nvinfer", "primary-inference");
  g_object_set(pgie, "config-file-path", cfg.pgie_config_file.c_str(), nullptr);
  /* Engine is dynamic-batch (min 1 / max 4): one engine serves any camera
   * count; a partial batch under sync-on runs natively (no padding). */
  g_object_set(pgie, "batch-size", static_cast<guint>(num_cams), nullptr);
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
    GstElement* source_bin = build_source_bin(index, cfg.cameras[index]);
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
