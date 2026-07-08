#include "app_config.hpp"

#include <yaml-cpp/yaml.h>

#include <algorithm>
#include <filesystem>
#include <sstream>
#include <stdexcept>

namespace fs = std::filesystem;

namespace mcrt {

namespace {

std::string lower(std::string s) {
  std::transform(s.begin(), s.end(), s.begin(),
                 [](unsigned char c) { return std::tolower(c); });
  return s;
}

/* Paths in the YAML are written relative to the PROJECT ROOT (the parent of
 * the config file's directory) — same convention as the Python app. */
std::string resolve(const fs::path& project_root, const std::string& p) {
  if (p.empty()) return p;
  fs::path pp(p);
  if (pp.is_absolute()) return pp.lexically_normal().string();
  return (project_root / pp).lexically_normal().string();
}

/* yaml-cpp helper: read a scalar with a default. A missing or empty key falls
 * back to the default; a key that is PRESENT but unconvertible (e.g.
 * `fps: thirty`) throws instead of silently masking the typo. */
template <typename T>
T get(const YAML::Node& n, const std::string& key, const T& dflt) {
  if (!n || !n[key] || n[key].IsNull()) return dflt;
  try {
    return n[key].as<T>();
  } catch (const YAML::Exception&) {
    throw std::runtime_error("Config key '" + key + "' has an invalid value: " +
                             YAML::Dump(n[key]));
  }
}

/* "a,b,c" -> vector<double>; throws naming the option on a bad token. */
std::vector<double> parse_double_list(const std::string& s, const char* what) {
  std::vector<double> out;
  std::stringstream ss(s);
  std::string tok;
  while (std::getline(ss, tok, ',')) {
    if (tok.empty()) continue;
    std::size_t pos = 0;
    double v = 0.0;
    try {
      v = std::stod(tok, &pos);
    } catch (const std::exception&) {
      pos = std::string::npos;
    }
    if (pos != tok.size())
      throw std::runtime_error(std::string(what) + ": bad number '" + tok +
                               "' in '" + s + "'.");
    out.push_back(v);
  }
  return out;
}

/* Bool that accepts both YAML spellings: 0/1 and true/false/on/off. */
bool get_bool(const YAML::Node& n, const std::string& key, bool dflt) {
  if (!n || !n[key] || n[key].IsNull()) return dflt;
  try {
    return n[key].as<int>() != 0;
  } catch (const YAML::Exception&) {
  }
  try {
    return n[key].as<bool>();
  } catch (const YAML::Exception&) {
    throw std::runtime_error("Config key '" + key + "' has an invalid value: " +
                             YAML::Dump(n[key]));
  }
}

}  // namespace

AppConfig load_config(const std::string& path, const Overrides& ov) {
  if (!fs::is_regular_file(path))
    throw std::runtime_error("Config file not found: " + path);

  YAML::Node root;
  try {
    root = YAML::LoadFile(path);
  } catch (const YAML::Exception& e) {
    throw std::runtime_error("Failed to parse " + path + ": " + e.what());
  }
  if (!root.IsMap())
    throw std::runtime_error("Config file " + path + " is not a YAML mapping.");

  const fs::path cfg_dir = fs::absolute(path).parent_path();
  const fs::path project_root = cfg_dir.parent_path();

  AppConfig cfg;

  // --- capture defaults (per-camera entries may override) -------------------
  YAML::Node capture = root["capture"];
  const std::string def_format = lower(get<std::string>(capture, "format", "mjpeg"));
  const int def_width = get<int>(capture, "width", 1280);
  const int def_height = get<int>(capture, "height", 720);
  const int def_fps = get<int>(capture, "fps", 30);
  const std::string def_mjpeg_dec =
      get<std::string>(capture, "mjpeg_decoder", "nvjpegdec");
  /* jpegparse (GStreamer 1.20) re-stamps live-camera PTS onto a synthetic
   * per-camera grid, destroying true capture timing (measured: constant
   * 1.05–1.47 s cross-camera offsets). pts_fix restores the kernel capture
   * stamp on jpegparse's output — ON by default; --no-pts-fix for A/B. */
  bool def_pts_fix = get_bool(capture, "pts_fix", true);
  if (ov.pts_fix >= 0) def_pts_fix = (ov.pts_fix != 0);

  // --- source: live v4l2 cameras (default) or deterministic file replay -----
  YAML::Node source = root["source"];
  cfg.source_type = lower(get<std::string>(source, "type", "v4l2"));
  cfg.replay_dir = get<std::string>(source, "replay_dir", "experiments/clips");
  if (!ov.source.empty()) cfg.source_type = lower(ov.source);
  if (!ov.replay_dir.empty()) cfg.replay_dir = ov.replay_dir;
  if (cfg.source_type != "v4l2" && cfg.source_type != "file")
    throw std::runtime_error("source.type must be 'v4l2' or 'file'; got '" +
                             cfg.source_type + "'.");

  // --- cameras ---------------------------------------------------------------
  YAML::Node cams = root["cameras"];
  if (!cams || !cams.IsSequence() || cams.size() == 0)
    throw std::runtime_error("Config file " + path + " has no 'cameras' configured.");

  for (std::size_t i = 0; i < cams.size(); ++i) {
    const YAML::Node& entry = cams[i];
    CameraCfg cam;
    cam.source_type = cfg.source_type;
    cam.format = def_format;
    cam.width = def_width;
    cam.height = def_height;
    cam.fps = def_fps;
    cam.mjpeg_decoder = def_mjpeg_dec;
    cam.pts_fix = def_pts_fix;

    if (entry.IsScalar()) {
      cam.device = entry.as<std::string>();
    } else if (entry.IsMap()) {
      cam.device = get<std::string>(entry, "device", "");
      cam.file = get<std::string>(entry, "file", "");
      cam.format = lower(get<std::string>(entry, "format", cam.format));
      cam.width = get<int>(entry, "width", cam.width);
      cam.height = get<int>(entry, "height", cam.height);
      cam.fps = get<int>(entry, "fps", cam.fps);
      cam.mjpeg_decoder = get<std::string>(entry, "mjpeg_decoder", cam.mjpeg_decoder);
    } else {
      throw std::runtime_error("cameras[" + std::to_string(i) +
                               "] must be a string or a mapping.");
    }
    if (cam.source_type == "v4l2" && cam.device.empty())
      throw std::runtime_error("cameras[" + std::to_string(i) +
                               "] needs a 'device' path for the v4l2 source.");
    // File replay: default clip <replay_dir>/cam{i}.mp4, resolved absolute.
    if (cam.source_type == "file") {
      std::string clip = cam.file.empty()
                             ? cfg.replay_dir + "/cam" + std::to_string(i) + ".mp4"
                             : cam.file;
      cam.file = resolve(project_root, clip);
    }
    cfg.cameras.push_back(std::move(cam));
  }

  // --- new nvstreammux --------------------------------------------------------
  YAML::Node smux = root["streammux"];
  cfg.mux.batched_push_timeout_us =
      get<int>(smux, "batched_push_timeout_us", 33333);
  cfg.mux.sync_inputs = get_bool(smux, "sync_inputs", false);
  cfg.mux.max_latency_ns =
      static_cast<uint64_t>(get<int64_t>(smux, "max_latency_ns", 33333333));
  std::string mux_ini = get<std::string>(smux, "config_file", "config/mux_config.txt");
  bool mux_ini_explicit = smux && smux["config_file"];

  if (ov.sync >= 0) cfg.mux.sync_inputs = (ov.sync != 0);
  if (ov.timeout_us >= 0) cfg.mux.batched_push_timeout_us = static_cast<int>(ov.timeout_us);
  if (ov.max_latency_ns >= 0) cfg.mux.max_latency_ns = static_cast<uint64_t>(ov.max_latency_ns);
  if (!ov.mux_config.empty()) {
    mux_ini = ov.mux_config;
    mux_ini_explicit = true;
  }
  if (lower(mux_ini) == "none" || mux_ini.empty()) {
    cfg.mux.config_file.clear();
  } else {
    cfg.mux.config_file = resolve(project_root, mux_ini);
    if (!fs::is_regular_file(cfg.mux.config_file)) {
      if (mux_ini_explicit)
        throw std::runtime_error("streammux config file not found: " +
                                 cfg.mux.config_file);
      cfg.mux.config_file.clear();  // default file absent — run on mux defaults
    }
  }

  // --- replay-skew injection (file sources; CLI-only, no YAML section) --------
  if (!ov.skew_ms.empty())
    cfg.replay.skew_ms = parse_double_list(ov.skew_ms, "--skew-ms");
  if (!ov.rate.empty())
    cfg.replay.rate = parse_double_list(ov.rate, "--rate");
  if (ov.gap_every >= 0) cfg.replay.gap_every = ov.gap_every;
  if (ov.ring >= 0) cfg.replay.ring = ov.ring;
  if (ov.restamp >= 0) cfg.replay.restamp = (ov.restamp != 0);
  const bool replay_knobs_used =
      !cfg.replay.skew_ms.empty() || !cfg.replay.rate.empty() ||
      cfg.replay.gap_every > 0 || cfg.replay.ring > 0 || cfg.replay.restamp;
  if (replay_knobs_used && cfg.source_type != "file")
    throw std::runtime_error(
        "--skew-ms/--rate/--gap-every/--ring/--restamp only apply to file "
        "replay (--source file).");
  const std::size_t ncams = cfg.cameras.size();
  if (cfg.replay.skew_ms.empty()) cfg.replay.skew_ms.assign(ncams, 0.0);
  if (cfg.replay.rate.empty()) cfg.replay.rate.assign(ncams, 1.0);
  if (cfg.replay.skew_ms.size() != ncams || cfg.replay.rate.size() != ncams)
    throw std::runtime_error(
        "--skew-ms / --rate need exactly one value per camera (" +
        std::to_string(ncams) + " configured).");

  // --- pgie / tracker ---------------------------------------------------------
  cfg.pgie_config_file = resolve(
      project_root, get<std::string>(root["pgie"], "config_file", "config/pgie_config.txt"));
  if (!ov.pgie_config.empty())
    cfg.pgie_config_file = resolve(project_root, ov.pgie_config);
  if (!fs::is_regular_file(cfg.pgie_config_file))
    throw std::runtime_error("nvinfer config file not found: " +
                             cfg.pgie_config_file);
  YAML::Node tr = root["tracker"];
  cfg.tracker.ll_lib_file = get<std::string>(tr, "ll_lib_file", cfg.tracker.ll_lib_file);
  cfg.tracker.ll_config_file = resolve(
      project_root, get<std::string>(tr, "ll_config_file", "config/tracker_config.yml"));
  cfg.tracker.width = get<int>(tr, "width", 640);
  cfg.tracker.height = get<int>(tr, "height", 384);
  cfg.tracker.gpu_id = get<int>(tr, "gpu_id", 0);

  // --- output / display ---------------------------------------------------------
  YAML::Node out = root["output"];
  cfg.output.only_nonempty = get_bool(out, "only_nonempty", false);
  cfg.output.pretty = get_bool(out, "pretty", false);
  cfg.output.log_interval_s = get<double>(out, "log_interval_s", 1.0);

  YAML::Node disp = root["display"];
  cfg.display.width = get<int>(disp, "width", 1280);
  cfg.display.height = get<int>(disp, "height", 720);
  cfg.display.window_width = get<int>(disp, "window_width", 0);
  cfg.display.window_height = get<int>(disp, "window_height", 0);

  return cfg;
}

void validate_cameras(const std::vector<CameraCfg>& cameras) {
  // File-replay sources: every clip must exist.
  std::vector<std::string> file_missing;
  for (const auto& c : cameras)
    if (c.source_type == "file" && (c.file.empty() || !fs::is_regular_file(c.file)))
      file_missing.push_back(c.file.empty() ? "<unset replay clip>" : c.file);
  if (!file_missing.empty()) {
    std::ostringstream msg;
    msg << "Replay clip(s) not found: ";
    for (std::size_t i = 0; i < file_missing.size(); ++i)
      msg << (i ? ", " : "") << file_missing[i];
    msg << ".\nRecord them first: python3 scripts/record_replay_clips.py";
    throw std::runtime_error(msg.str());
  }

  // Live v4l2 sources: every device node must exist.
  std::vector<std::string> dev_missing;
  for (const auto& c : cameras)
    if (c.source_type == "v4l2" && !fs::exists(c.device))
      dev_missing.push_back(c.device);
  if (!dev_missing.empty()) {
    std::vector<std::string> available;
    std::error_code ec;
    for (const auto& e : fs::directory_iterator("/dev", ec)) {
      const std::string name = e.path().filename().string();
      if (name.rfind("video", 0) == 0) available.push_back(e.path().string());
    }
    std::sort(available.begin(), available.end());
    std::ostringstream msg;
    msg << "Configured camera device(s) not found: ";
    for (std::size_t i = 0; i < dev_missing.size(); ++i)
      msg << (i ? ", " : "") << dev_missing[i];
    msg << ".\nDevices present: ";
    if (available.empty()) {
      msg << "(none)";
    } else {
      for (std::size_t i = 0; i < available.size(); ++i)
        msg << (i ? ", " : "") << available[i];
    }
    msg << "\nCheck the cameras are plugged in and the 'cameras' list in the config.";
    throw std::runtime_error(msg.str());
  }
}

}  // namespace mcrt
