/* main.cpp — entrypoint for the multi-camera DeepStream detection + tracking
 * app (C++ / NEW nvstreammux rewrite; baseline + sync-on variants only).
 *
 * Usage:
 *     ./multicam_rt --config config/camera_params.yaml           # baseline
 *     ./multicam_rt --config config/camera_params.yaml --sync    # sync-on
 *
 * What it does:
 *   1. Forces USE_NEW_NVSTREAMMUX=yes (unless already set) BEFORE gst_init,
 *      so the new mux implementation is what gets registered.
 *   2. Parses CLI args and the YAML config; fails fast if a camera is missing.
 *   3. Builds the pipeline (pipeline_builder.cpp) and attaches a pad probe on
 *      nvtracker's src pad that parses detections into an OutputWriter.
 *   4. Runs a GLib main loop until EOS / error / Ctrl-C, then shuts down
 *      cleanly (EOS first, so recordings and metrics finalize).
 */

#include <glib-unix.h>
#include <gst/gst.h>

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <memory>
#include <stdexcept>
#include <string>

#include "app_config.hpp"
#include "detection_parser.hpp"
#include "fps_overlay.hpp"
#include "gstnvdsmeta.h"
#include "metrics.hpp"
#include "output_writer.hpp"
#include "pipeline_builder.hpp"
#include "scheduler.hpp"

using namespace mcrt;

namespace {

// ---------------------------------------------------------------------------
// CLI
// ---------------------------------------------------------------------------
struct Args {
  std::string config = "config/camera_params.yaml";
  std::string log_mode;              // "" -> json (or human with --debug)
  bool display = false;
  bool debug = false;
  std::string record_path;
  std::string metrics_csv;
  double duration_s = 0.0;
  Overrides ov;
  SchedCfg sched;                    // SPARQ scheduler (default: off)
  bool dropold = false;              // keep-newest config baseline
};

void usage(const char* prog) {
  std::fprintf(stderr,
"Multi-camera DeepStream YOLO11n detection + tracking (Jetson, C++, NEW nvstreammux).\n"
"\n"
"Usage: %s [options]\n"
"\n"
"  --config PATH        YAML config (default: config/camera_params.yaml)\n"
"  --sync               sync-on variant: nvstreammux sync-inputs=1 (time-align\n"
"                       frames across cameras; late frames are DROPPED)\n"
"  --no-sync            force the baseline (sync-inputs=0), overriding the YAML\n"
"  --max-latency-ms N   sync-on only: extra wait for late frames (default 33)\n"
"  --timeout-us N       batched-push-timeout in microseconds (default 33333)\n"
"  --mux-config PATH    new-mux INI (default: config/mux_config.txt; 'none' to\n"
"                       run on the mux's built-in defaults)\n"
"  --pgie-config PATH   nvinfer config override (engine A/B testing, e.g.\n"
"                       config/pgie_config.txt = dynamic-batch engine,\n"
"                       config/_pgie_static.txt = fixed batch-4 engine)\n"
"  --no-pts-fix         DISABLE the jpegparse PTS-restore fix (live MJPG\n"
"                       sources). Default ON: true kernel capture stamps are\n"
"                       restored onto jpegparse's output, instead of the\n"
"                       synthetic per-camera 33.33 ms grid that carries a\n"
"                       constant 1.05-1.47 s cross-camera offset\n"
"  --source v4l2|file   live cameras (default) or deterministic file replay\n"
"  --replay-dir DIR     per-camera replay clips cam0.mp4.. (default:\n"
"                       experiments/clips) for --source file\n"
"\n"
"replay-skew injection (--source file; simulates the live rig's timing on\n"
"recorded clips — validated in cpp/experiments/frame_timing/REPLAY_SKEW.md):\n"
"  --skew-ms a,b,..     per-camera start delay in ms (startup stagger)\n"
"  --rate r0,r1,..      per-camera PTS rate factor; 0.9608 turns a 30 fps\n"
"                       clip into the C920's true 32.026 ms cadence; small\n"
"                       per-camera differences simulate crystal drift\n"
"  --gap-every N        drop 2 consecutive frames every N frames per camera\n"
"                       (kernel capture gaps; measured live: ~70)\n"
"  --ring N             bounded drop-newest queue after the pacer (v4l2\n"
"                       kernel-ring stand-in; live: 4). 0 = off (default)\n"
"  --restamp            emulate the UNFIXED jpegparse: rewrite PTS onto the\n"
"                       synthetic per-camera grid (default: off = the mux\n"
"                       sees true pacing timestamps, like the fixed app)\n"
"  --display            live tiled window with boxes + labels + track IDs\n"
"  --record PATH        also record the annotated, tiled view to an H.264 MP4\n"
"  --log MODE           console output: json (default) | human | none\n"
"  --debug              shorthand for --display --log human\n"
"  --metrics-csv PATH   write per-batch latency/throughput metrics (same schema\n"
"                       as the Python harness; scripts/analyze.py reads it)\n"
"  --duration SECS      stop cleanly after this many seconds (benchmarks)\n"
"\n"
"SPARQ scheduler (importance-aware bounded-queue batch former; see\n"
"cpp/src/scheduler.hpp). Runs with sync-inputs=0 and needs the scheduler\n"
"mux INI (config/mux_sched.txt is used by default when --sched is on):\n"
"  --sched MODE         off (default) | fresh | imp | salvage\n"
"  --sched-k N          frames per release = mux batch-size (default 2)\n"
"  --sched-depth N      release gate: in-flight <= (N-1)*k frames (default 2)\n"
"  --sched-tau-max MS   staleness bound for fresh frames (default 150)\n"
"  --sched-tau-salvage MS  staleness bound for held frames (default 250)\n"
"  --sched-w F,I,R      value weights fresh,importance,fairness\n"
"                       (default 0.40,0.35,0.25)\n"
"  --sched-csv PATH     per-decision log (admit/salvage/evict/displace)\n"
"  --dropold            keep-newest config baseline: per-camera 1-deep\n"
"                       leaky=downstream queue before the mux (no scheduler)\n"
"  -h, --help           this help\n",
               prog);
}

/* Returns the value of a --key VALUE pair, advancing i; throws if missing. */
std::string need_value(int argc, char** argv, int& i, const char* key) {
  if (i + 1 >= argc)
    throw std::runtime_error(std::string(key) + " needs a value.");
  return argv[++i];
}

/* Strict numeric parsing: the whole token must be a number, and errors name
 * the offending option ("--duration: expected a number, got 'abc'"). */
double need_double(int argc, char** argv, int& i, const char* key) {
  const std::string s = need_value(argc, argv, i, key);
  std::size_t pos = 0;
  double v = 0.0;
  try {
    v = std::stod(s, &pos);
  } catch (const std::exception&) {
    pos = std::string::npos;
  }
  if (pos != s.size())
    throw std::runtime_error(std::string(key) + ": expected a number, got '" +
                             s + "'.");
  return v;
}

int64_t need_int(int argc, char** argv, int& i, const char* key) {
  const std::string s = need_value(argc, argv, i, key);
  std::size_t pos = 0;
  int64_t v = 0;
  try {
    v = std::stoll(s, &pos);
  } catch (const std::exception&) {
    pos = std::string::npos;
  }
  if (pos != s.size())
    throw std::runtime_error(std::string(key) + ": expected an integer, got '" +
                             s + "'.");
  return v;
}

Args parse_args(int argc, char** argv) {
  Args a;
  for (int i = 1; i < argc; ++i) {
    const std::string arg = argv[i];
    if (arg == "-h" || arg == "--help") {
      usage(argv[0]);
      std::exit(0);
    } else if (arg == "--config") {
      a.config = need_value(argc, argv, i, "--config");
    } else if (arg == "--sync") {
      a.ov.sync = 1;
    } else if (arg == "--no-sync") {
      a.ov.sync = 0;
    } else if (arg == "--max-latency-ms") {
      a.ov.max_latency_ns =
          static_cast<int64_t>(need_double(argc, argv, i, arg.c_str()) * 1e6);
    } else if (arg == "--timeout-us") {
      a.ov.timeout_us = need_int(argc, argv, i, arg.c_str());
    } else if (arg == "--mux-config") {
      a.ov.mux_config = need_value(argc, argv, i, arg.c_str());
    } else if (arg == "--source") {
      a.ov.source = need_value(argc, argv, i, arg.c_str());
    } else if (arg == "--replay-dir") {
      a.ov.replay_dir = need_value(argc, argv, i, arg.c_str());
    } else if (arg == "--pgie-config") {
      a.ov.pgie_config = need_value(argc, argv, i, arg.c_str());
    } else if (arg == "--pts-fix") {
      a.ov.pts_fix = 1;
    } else if (arg == "--no-pts-fix") {
      a.ov.pts_fix = 0;
    } else if (arg == "--skew-ms") {
      a.ov.skew_ms = need_value(argc, argv, i, arg.c_str());
    } else if (arg == "--rate") {
      a.ov.rate = need_value(argc, argv, i, arg.c_str());
    } else if (arg == "--gap-every") {
      a.ov.gap_every = static_cast<int>(need_int(argc, argv, i, arg.c_str()));
    } else if (arg == "--ring") {
      a.ov.ring = static_cast<int>(need_int(argc, argv, i, arg.c_str()));
    } else if (arg == "--restamp") {
      a.ov.restamp = 1;
    } else if (arg == "--no-restamp") {
      a.ov.restamp = 0;
    } else if (arg == "--display") {
      a.display = true;
    } else if (arg == "--record") {
      a.record_path = need_value(argc, argv, i, arg.c_str());
    } else if (arg == "--log") {
      a.log_mode = need_value(argc, argv, i, arg.c_str());
    } else if (arg == "--debug") {
      a.debug = true;
    } else if (arg == "--metrics-csv") {
      a.metrics_csv = need_value(argc, argv, i, arg.c_str());
    } else if (arg == "--duration") {
      a.duration_s = need_double(argc, argv, i, arg.c_str());
    } else if (arg == "--sched") {
      a.sched.mode = need_value(argc, argv, i, arg.c_str());
      if (a.sched.mode != "off" && a.sched.mode != "fresh" &&
          a.sched.mode != "imp" && a.sched.mode != "salvage")
        throw std::runtime_error(
            "--sched must be off|fresh|imp|salvage, got '" + a.sched.mode +
            "'.");
    } else if (arg == "--sched-k") {
      a.sched.k = static_cast<int>(need_int(argc, argv, i, arg.c_str()));
    } else if (arg == "--sched-depth") {
      a.sched.depth = static_cast<int>(need_int(argc, argv, i, arg.c_str()));
    } else if (arg == "--sched-tau-max") {
      a.sched.tau_max_ms = need_double(argc, argv, i, arg.c_str());
    } else if (arg == "--sched-tau-salvage") {
      a.sched.tau_salvage_ms = need_double(argc, argv, i, arg.c_str());
    } else if (arg == "--sched-w") {
      const std::string v = need_value(argc, argv, i, arg.c_str());
      if (std::sscanf(v.c_str(), "%lf,%lf,%lf", &a.sched.w_fresh,
                      &a.sched.w_imp, &a.sched.w_fair) != 3)
        throw std::runtime_error("--sched-w expects F,I,R (three numbers).");
    } else if (arg == "--sched-csv") {
      a.sched.decision_csv = need_value(argc, argv, i, arg.c_str());
    } else if (arg == "--dropold") {
      a.dropold = true;
    } else {
      usage(argv[0]);
      throw std::runtime_error("unknown argument: " + arg);
    }
  }
  return a;
}

// ---------------------------------------------------------------------------
// Detection probe (nvtracker src): parse meta -> writer (+ FPS meter)
//
// Runs on nvtracker's streaming thread, once per pushed BATCH (not per
// camera frame) — one buffer carries the frames of every camera in the
// batch. Returning GST_PAD_PROBE_OK lets the buffer continue to the tail
// unchanged; this probe only reads.
// ---------------------------------------------------------------------------
struct ProbeCtx {
  OutputWriter* writer;
  FpsMeter* meter;  // nullptr when headless without recording
};

GstPadProbeReturn detection_probe(GstPad*, GstPadProbeInfo* info,
                                  gpointer user_data) {
  auto* ctx = static_cast<ProbeCtx*>(user_data);
  GstBuffer* buf = GST_PAD_PROBE_INFO_BUFFER(info);
  if (buf == nullptr) return GST_PAD_PROBE_OK;
  NvDsBatchMeta* batch_meta = gst_buffer_get_nvds_batch_meta(buf);
  if (batch_meta == nullptr) return GST_PAD_PROBE_OK;

  const auto frames = parse_batch_meta(batch_meta);
  if (ctx->meter != nullptr)
    for (const auto& f : frames) ctx->meter->tick(f.camera_id);
  ctx->writer->write_batch(frames);
  return GST_PAD_PROBE_OK;
}

// ---------------------------------------------------------------------------
// Bus / signals / duration
// ---------------------------------------------------------------------------
struct MainCtx {
  GMainLoop* loop = nullptr;
  GstElement* pipeline = nullptr;
  int exit_code = 0;
  int sigints = 0;
  double duration_s = 0.0;
  bool duration_fired = false;  // its source auto-removes after firing
};

gboolean bus_call(GstBus*, GstMessage* msg, gpointer data) {
  auto* ctx = static_cast<MainCtx*>(data);
  switch (GST_MESSAGE_TYPE(msg)) {
    case GST_MESSAGE_EOS:
      std::fprintf(stderr, "[main] End-of-stream — shutting down.\n");
      g_main_loop_quit(ctx->loop);
      break;
    case GST_MESSAGE_ERROR: {
      GError* err = nullptr;
      gchar* dbg = nullptr;
      gst_message_parse_error(msg, &err, &dbg);
      std::fprintf(stderr, "[main] ERROR from %s: %s\n",
                   GST_OBJECT_NAME(msg->src), err ? err->message : "?");
      if (dbg != nullptr) std::fprintf(stderr, "[main] debug: %s\n", dbg);
      g_clear_error(&err);
      g_free(dbg);
      ctx->exit_code = 1;
      g_main_loop_quit(ctx->loop);
      break;
    }
    case GST_MESSAGE_WARNING: {
      GError* warn = nullptr;
      gchar* dbg = nullptr;
      gst_message_parse_warning(msg, &warn, &dbg);
      std::fprintf(stderr, "[main] WARNING from %s: %s\n",
                   GST_OBJECT_NAME(msg->src), warn ? warn->message : "?");
      g_clear_error(&warn);
      g_free(dbg);
      break;
    }
    default:
      break;
  }
  return TRUE;
}

/* First Ctrl-C: send EOS so the MP4/metrics finalize and the bus EOS quits the
 * loop. Second Ctrl-C: quit immediately. */
gboolean on_sigint(gpointer data) {
  auto* ctx = static_cast<MainCtx*>(data);
  ctx->sigints += 1;
  if (ctx->sigints == 1) {
    std::fprintf(stderr, "\n[main] Interrupted — flushing EOS (Ctrl-C again to force quit).\n");
    gst_element_send_event(ctx->pipeline, gst_event_new_eos());
  } else {
    g_main_loop_quit(ctx->loop);
  }
  return G_SOURCE_CONTINUE;
}

gboolean on_duration(gpointer data) {
  auto* ctx = static_cast<MainCtx*>(data);
  ctx->duration_fired = true;
  std::fprintf(stderr, "[main] duration %.1fs elapsed — stopping.\n",
               ctx->duration_s);
  gst_element_send_event(ctx->pipeline, gst_event_new_eos());
  return G_SOURCE_REMOVE;
}

void make_parent_dirs(const std::string& path) {
  if (path.empty()) return;
  gchar* dir = g_path_get_dirname(path.c_str());
  g_mkdir_with_parents(dir, 0755);
  g_free(dir);
}

// ---------------------------------------------------------------------------
// run() — the wiring hub. Order matters:
//   1. config + validation (fail fast, before touching GStreamer)
//   2. build the pipeline
//   3. create writer/meter/metrics and attach their probes
//   4. main loop (bus watch, SIGINT, optional duration timer)
//   5. teardown: pipeline to NULL and unref FIRST, then close the writers —
//      the pad probes hold raw pointers to objects on this stack, so those
//      objects must outlive all streaming threads.
// ---------------------------------------------------------------------------
int run(const Args& args) {
  // -- 1. Configuration. Both throw with an actionable message; main()
  //       catches and exits 2 without a stack trace.
  AppConfig cfg = load_config(args.config, args.ov);
  validate_cameras(cfg.cameras);

  // SPARQ scheduler / baseline arms mutate the built pipeline:
  cfg.dropold = args.dropold;
  if (args.sched.enabled()) {
    if (args.dropold)
      throw std::runtime_error("--sched and --dropold are mutually exclusive.");
    if (cfg.mux.sync_inputs)
      throw std::runtime_error(
          "--sched requires sync-inputs=0 (the scheduler replaces alignment).");
    cfg.mux_batch_override = args.sched.k;   // mux + pgie batch-size = K
    cfg.conv_output_buffers = 12;            // stash holds refs; pool slack
    if (args.ov.mux_config.empty()) {
      // Default to the scheduler INI (slow deadline anchors + 2 frames per
      // source per batch) sitting next to the run's mux INI.
      namespace fs = std::filesystem;
      const fs::path base = cfg.mux.config_file.empty()
                                ? fs::path(cfg.pgie_config_file).parent_path()
                                : fs::path(cfg.mux.config_file).parent_path();
      const fs::path sched_ini = base / "mux_sched.txt";
      if (!fs::is_regular_file(sched_ini))
        throw std::runtime_error("scheduler mux INI not found: " +
                                 sched_ini.string());
      cfg.mux.config_file = sched_ini.string();
    }
  }

  const bool display = args.display || args.debug;
  const std::string log_mode =
      !args.log_mode.empty() ? args.log_mode : (args.debug ? "human" : "json");
  const int n = static_cast<int>(cfg.cameras.size());

  std::string banner =
      "[main] " + std::to_string(n) + " camera(s) [" + cfg.source_type + "] " +
      std::to_string(cfg.cameras[0].width) + "x" +
      std::to_string(cfg.cameras[0].height) + "@" +
      std::to_string(cfg.cameras[0].fps) + " (" + cfg.cameras[0].format +
      "); NEW nvstreammux sync-inputs=" +
      (cfg.mux.sync_inputs ? "ON" : "OFF") + " timeout=" +
      std::to_string(cfg.mux.batched_push_timeout_us) + "us";
  if (cfg.mux.config_file.empty()) banner += " mux-config=none";
  if (cfg.source_type == "v4l2" && cfg.cameras[0].format == "mjpeg")
    banner += std::string("; pts-fix=") +
              (cfg.cameras[0].pts_fix ? "ON" : "off");
  if (cfg.source_type == "file") {
    bool skewed = cfg.replay.gap_every > 0 || cfg.replay.ring > 0 ||
                  cfg.replay.restamp;
    for (std::size_t i = 0; i < cfg.cameras.size(); ++i)
      if (cfg.replay.skew_ms[i] != 0.0 || cfg.replay.rate[i] != 1.0)
        skewed = true;
    if (skewed) {
      banner += "; replay-skew [skew-ms=";
      for (std::size_t i = 0; i < cfg.replay.skew_ms.size(); ++i)
        banner += (i ? "," : "") + std::to_string(cfg.replay.skew_ms[i]);
      banner += " rate=";
      for (std::size_t i = 0; i < cfg.replay.rate.size(); ++i)
        banner += (i ? "," : "") + std::to_string(cfg.replay.rate[i]);
      banner += " gap-every=" + std::to_string(cfg.replay.gap_every) +
                " ring=" + std::to_string(cfg.replay.ring) +
                " restamp=" + (cfg.replay.restamp ? "on" : "off") + "]";
    }
  }
  banner += "; pgie=" + cfg.pgie_config_file;
  if (args.sched.enabled())
    banner += "; sched=" + args.sched.mode + " k=" +
              std::to_string(args.sched.k);
  if (cfg.dropold) banner += "; dropold baseline";
  banner += " log=" + log_mode;
  if (display) banner += "; display window";
  if (!args.record_path.empty()) banner += "; recording -> " + args.record_path;
  if (!args.metrics_csv.empty()) banner += "; metrics -> " + args.metrics_csv;
  if (cfg.mux.sync_inputs)
    banner += "; max-latency=" +
              std::to_string(cfg.mux.max_latency_ns / 1000000) + "ms";
  std::fprintf(stderr, "%s.\n", banner.c_str());

  // -- 2. Build the full GStreamer graph (see pipeline_builder.hpp for the
  //       shape). `built` holds the owning pipeline ref plus borrowed
  //       pointers to the elements we attach probes to.
  BuiltPipeline built = build_pipeline(cfg, display, args.record_path);

  // -- 3. Detection path: probe on nvtracker src -> parse -> writer.
  //       The FpsMeter only exists when something visual will show it.
  auto writer = make_writer(log_mode, cfg.output);
  std::unique_ptr<FpsMeter> meter;
  if (display || !args.record_path.empty()) meter = std::make_unique<FpsMeter>();

  ProbeCtx probe_ctx{writer.get(), meter.get()};
  attach_detection_probe(built.tracker, detection_probe, &probe_ctx);
  if (meter != nullptr && built.tiler != nullptr)
    attach_fps_overlay(built.tiler, n, meter.get());

  // Optional per-batch CSV; attaches its own probes at the source bins,
  // the mux and the tracker (see metrics.hpp for the three tap points).
  std::unique_ptr<MetricsCollector> metrics;
  if (!args.metrics_csv.empty()) {
    metrics = std::make_unique<MetricsCollector>(
        args.metrics_csv, n, cfg.mux.batched_push_timeout_us, n,
        cfg.mux.sync_inputs);
    metrics->attach(built.pipeline);
  }

  // SPARQ scheduler. Attached AFTER metrics so the metrics arrival probe
  // stamps each frame before the scheduler's probe stashes it (probes fire
  // in attach order) — e2e_ms then includes the stash wait.
  std::unique_ptr<Scheduler> sched;
  if (args.sched.enabled()) {
    sched = std::make_unique<Scheduler>(args.sched, n);
    sched->attach(built.pipeline);
  }

  // -- 4. Main loop plumbing. Everything that can stop the app funnels into
  //       one place: EOS on the bus quits the loop (errors do too). SIGINT
  //       and --duration don't quit directly — they *send EOS* so sinks
  //       (MP4 index!) and metrics finalize first.
  MainCtx ctx;
  ctx.loop = g_main_loop_new(nullptr, FALSE);
  ctx.pipeline = built.pipeline;
  ctx.duration_s = args.duration_s;

  GstBus* bus = gst_element_get_bus(built.pipeline);
  const guint bus_watch = gst_bus_add_watch(bus, bus_call, &ctx);
  gst_object_unref(bus);

  const guint sig_watch = g_unix_signal_add(SIGINT, on_sigint, &ctx);
  guint dur_watch = 0;
  if (args.duration_s > 0.0)
    dur_watch = g_timeout_add(static_cast<guint>(args.duration_s * 1000.0),
                              on_duration, &ctx);

  if (gst_element_set_state(built.pipeline, GST_STATE_PLAYING) ==
      GST_STATE_CHANGE_FAILURE) {
    std::fprintf(stderr, "[main] Failed to set pipeline to PLAYING.\n");
    gst_element_set_state(built.pipeline, GST_STATE_NULL);
    gst_object_unref(built.pipeline);
    g_main_loop_unref(ctx.loop);
    return 1;
  }

  std::fprintf(stderr,
               "[main] Running. First launch may build the TensorRT engine "
               "(several minutes). Press Ctrl-C to stop.\n");
  g_main_loop_run(ctx.loop);

  // -- 5. Teardown. NULL-state stops all streaming threads, so after the
  //       unref no probe can fire — only THEN is it safe to close (and later
  //       destroy) the writer/metrics objects the probes point at.
  if (dur_watch != 0 && !ctx.duration_fired) g_source_remove(dur_watch);
  g_source_remove(sig_watch);
  g_source_remove(bus_watch);
  /* Scheduler teardown is two-phase: request the stop first (its thread may
   * be blocked in gst_pad_push; the NULL transition below flushes pads and
   * unblocks it), then join + release stashed buffers after NULL but before
   * the pipeline (and its buffer pools) is unreffed. */
  if (sched != nullptr) sched->request_stop();
  gst_element_set_state(built.pipeline, GST_STATE_NULL);
  if (sched != nullptr) {
    sched->join_and_cleanup();
    sched->print_summary();
  }
  gst_object_unref(built.pipeline);
  g_main_loop_unref(ctx.loop);

  writer->close();
  if (metrics != nullptr) metrics->close();
  return ctx.exit_code;
}

}  // namespace

int main(int argc, char** argv) {
  /* Must happen BEFORE gst_init: the env var decides which nvstreammux
   * implementation the plugin registers. overwrite=0 → an explicit user
   * setting wins (build_pipeline still verifies the new mux loaded). */
  setenv("USE_NEW_NVSTREAMMUX", "yes", 0);

  try {
    Args args = parse_args(argc, argv);
    std::fprintf(stderr, "[main] USE_NEW_NVSTREAMMUX=%s\n",
                 getenv("USE_NEW_NVSTREAMMUX"));
    gst_init(nullptr, nullptr);
    make_parent_dirs(args.record_path);
    make_parent_dirs(args.metrics_csv);
    return run(args);
  } catch (const std::exception& exc) {
    // Configuration / device / pipeline-build errors: clear message, no trace.
    std::fprintf(stderr, "[main] ERROR: %s\n", exc.what());
    return 2;
  }
}
