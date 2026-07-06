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
  std::string pgie_config_file;         // resolved absolute
  TrackerCfg tracker;
  OutputCfg output;
  DisplayCfg display;
  std::string source_type = "v4l2";     // "v4l2" | "file"
  std::string replay_dir = "experiments/clips";
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
};

/* Load + normalize the YAML config. Throws std::runtime_error with a clear,
 * actionable message on any problem (missing file, bad key, no cameras). */
AppConfig load_config(const std::string& path, const Overrides& ov);

/* Fail fast — before touching GStreamer — if a configured camera device or
 * replay clip is missing. Lists what IS present to make the fix obvious. */
void validate_cameras(const std::vector<CameraCfg>& cameras);

}  // namespace mcrt
