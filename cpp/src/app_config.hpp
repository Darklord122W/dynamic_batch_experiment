/* app_config.hpp — config structs + YAML loading for the C++ pipeline.
 *
 * Reads the SAME config/camera_params.yaml as the Python app (single source of
 * truth for cameras / capture / pgie / tracker / output / display / source).
 * Legacy-mux-only keys (streammux width/height/live_source/nvbuf_memory_type)
 * and the RT-experiment sections (timeout/context/batch/control) are ignored:
 * this app targets the NEW nvstreammux and implements only the baseline and
 * sync-on pipeline variants.
 */
#pragma once

#include <cstdint>
#include <string>
#include <vector>

namespace mcrt {

/* One camera (live v4l2) or one replay clip (file) — an entry of `cameras:`. */
struct CameraCfg {
  std::string source_type = "v4l2";        // "v4l2" | "file"
  std::string format = "mjpeg";            // "mjpeg" | "raw"
  std::string mjpeg_decoder = "nvjpegdec"; // "nvjpegdec" | "jpegdec" | "nvv4l2"
  std::string device;                      // /dev/videoN (v4l2 sources)
  std::string file;                        // absolute clip path (file sources)
  int width = 640;
  int height = 480;
  int fps = 30;
  bool pts_fix = true;                     // restore true capture PTS around
                                           // jpegparse (see pipeline_builder)
};

/* Replay-skew injection (file sources only) — reproduces the live rig's
 * timing imperfections on recorded clips, so timing/batching experiments can
 * run without cameras. Ported from cpp/experiments/frame_timing (validated
 * against the live baseline_pinned run; see REPLAY_SKEW.md). All off by
 * default: a plain --source file run stays the old ideal replay. */
struct ReplayCfg {
  std::vector<double> skew_ms;  // per-camera start delay (startup stagger)
  std::vector<double> rate;     // per-camera PTS rate factor (true cadence /
                                // crystal drift); 0.9608 = C920's 32.026 ms
  int gap_every = 0;            // drop 2 consecutive frames every N (0 = off)
  int ring = 0;                 // bounded drop-newest queue after the pacer
                                // (v4l2 kernel-ring stand-in); 0 = off
  bool restamp = false;         // emulate jpegparse's synthetic-grid PTS
                                // rewrite (the UNFIXED pipeline); off = mux
                                // sees the true pacing timeline (the FIXED
                                // pipeline, pts_fix behaviour)
};

/* NEW nvstreammux. No width/height/live-source here: the new mux never scales
 * or converts — frames are batched at their native resolution. */
struct MuxCfg {
  int batched_push_timeout_us = 33333;  // push an incomplete batch after this
  bool sync_inputs = false;             // false = baseline, true = sync-on
  uint64_t max_latency_ns = 33333333;   // sync-on: extra wait for late frames
  std::string config_file;              // optional new-mux INI ("" = none)
};

struct TrackerCfg {
  std::string ll_lib_file =
      "/opt/nvidia/deepstream/deepstream/lib/libnvds_nvmultiobjecttracker.so";
  std::string ll_config_file;           // resolved absolute
  int width = 640;                      // processing size — multiples of 32
  int height = 384;
  int gpu_id = 0;
};

struct OutputCfg {
  bool only_nonempty = false;
  bool pretty = false;
  double log_interval_s = 1.0;
};

struct DisplayCfg {
  int width = 1280;                     // tiled composite size
  int height = 720;
  int window_width = 0;                 // 0 = default to composite size
  int window_height = 0;
};

struct AppConfig {
  std::vector<CameraCfg> cameras;
  MuxCfg mux;
  ReplayCfg replay;
  std::string pgie_config_file;         // resolved absolute
  TrackerCfg tracker;
  OutputCfg output;
  DisplayCfg display;
  std::string source_type = "v4l2";     // "v4l2" | "file"
  std::string replay_dir = "experiments/clips";

  /* Set by main.cpp (not by YAML) for the SPARQ scheduler / baseline arms. */
  int mux_batch_override = -1;          // >0: mux+pgie batch-size = this (=K)
  bool dropold = false;                 // per-camera keep-newest queue
                                        // (leaky=downstream, depth 1) before
                                        // the mux — the config-only baseline
  int conv_output_buffers = -1;         // >0: nvvideoconvert output pool size
                                        // (scheduler holds refs; needs slack)
};

/* Flat CLI overrides merged before normalization. Sentinels: empty string /
 * -1 mean "not given on the command line". */
struct Overrides {
  std::string source;                   // "v4l2" | "file"
  std::string replay_dir;
  int sync = -1;                        // -1 unset | 0 baseline | 1 sync-on
  int64_t timeout_us = -1;
  int64_t max_latency_ns = -1;
  std::string mux_config;               // "" unset; "none" disables the INI
  std::string pgie_config;              // "" unset; nvinfer config (engine A/B)
  int pts_fix = -1;                     // -1 unset | 0 off | 1 on
  std::string skew_ms;                  // "" unset; comma list, ms per camera
  std::string rate;                     // "" unset; comma list per camera
  int gap_every = -1;                   // -1 unset
  int ring = -1;                        // -1 unset
  int restamp = -1;                     // -1 unset | 0 off | 1 on
};

/* Load + normalize the YAML config. Throws std::runtime_error with a clear,
 * actionable message on any problem (missing file, bad key, no cameras). */
AppConfig load_config(const std::string& path, const Overrides& ov);

/* Fail fast — before touching GStreamer — if a configured camera device or
 * replay clip is missing. Lists what IS present to make the fix obvious. */
void validate_cameras(const std::vector<CameraCfg>& cameras);

}  // namespace mcrt
