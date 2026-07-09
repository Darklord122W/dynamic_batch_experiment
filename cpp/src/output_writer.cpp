#include "output_writer.hpp"

#include <glib.h>
#include <unistd.h>

#include <cstdio>
#include <sstream>
#include <stdexcept>

namespace mcrt {

namespace {

/* Minimal JSON string escaping (labels are plain ASCII, but be correct). */
std::string json_escape(const std::string& s) {
  std::string out;
  out.reserve(s.size());
  for (char ch : s) {
    switch (ch) {
      case '"': out += "\\\""; break;
      case '\\': out += "\\\\"; break;
      case '\n': out += "\\n"; break;
      case '\t': out += "\\t"; break;
      case '\r': out += "\\r"; break;
      default:
        if (static_cast<unsigned char>(ch) < 0x20) {
          char buf[8];
          g_snprintf(buf, sizeof(buf), "\\u%04x", ch);
          out += buf;
        } else {
          out += ch;
        }
    }
  }
  return out;
}

/* Compact float: no trailing zeros, always a valid JSON number. */
std::string num(double v) {
  char buf[32];
  g_snprintf(buf, sizeof(buf), "%.6g", v);
  return buf;
}

double mono_secs() { return g_get_monotonic_time() / 1e6; }

constexpr const char* kReset = "\033[0m";
/* One color per camera so a human can tell the streams apart at a glance. */
constexpr int kCamColors[] = {36, 32, 33, 35, 34, 91, 92, 93};

}  // namespace

// ---------------------------------------------------------------------------
// JsonWriter — one JSON object per camera per processed frame on stdout.
//
// The record shape is a COMPATIBILITY CONTRACT with the Python app (and any
// consumer already parsing it): camera_id / frame_num / num_detections /
// detections[{camera_id, track_id, class_name, confidence, x, y, width,
// height}]. Add new fields if needed, but don't rename or remove these.
// Compact mode emits exactly one line per record so consumers can stream
// with line-based tools (`grep '^{'` drops NVIDIA plugin chatter).
// ---------------------------------------------------------------------------
void JsonWriter::write(const FrameDetections& frame) {
  if (only_nonempty_ && frame.detections.empty()) return;

  const char* nl = pretty_ ? "\n" : "";
  const char* ind1 = pretty_ ? "  " : "";
  const char* ind2 = pretty_ ? "    " : "";

  std::ostringstream os;
  os << "{" << nl
     << ind1 << "\"camera_id\":" << frame.camera_id << "," << nl
     << ind1 << "\"frame_num\":" << frame.frame_num << "," << nl
     /* buf_pts is the frame's deterministic identity: with the jpegparse
      * PTS fix (live) or paced replay it is the true capture stamp, stable
      * across runs on identical input — (camera_id, buf_pts) lets two runs'
      * detections be compared frame-for-frame (frame_num cannot: the mux
      * renumbers survivors, so any drop desynchronizes it). */
     << ind1 << "\"buf_pts\":" << frame.buf_pts << "," << nl
     /* t_emit: CLOCK_MONOTONIC seconds at which this record leaves the
      * pipeline — buf_pts says WHICH instant the pixels show, t_emit says
      * WHEN the system knew. Their difference is the output staleness that
      * time-to-awareness metrics charge against event recall. */
     << ind1 << "\"t_emit\":" << (g_get_monotonic_time() / 1e6) << "," << nl
     << ind1 << "\"num_detections\":" << frame.detections.size() << "," << nl
     << ind1 << "\"detections\":[";
  for (std::size_t i = 0; i < frame.detections.size(); ++i) {
    const Detection& d = frame.detections[i];
    if (i) os << ",";
    os << nl << ind2
       << "{\"camera_id\":" << d.camera_id
       << ",\"track_id\":" << d.track_id
       << ",\"class_name\":\"" << json_escape(d.class_name) << "\""
       << ",\"confidence\":" << num(d.confidence)
       << ",\"x\":" << num(d.x) << ",\"y\":" << num(d.y)
       << ",\"width\":" << num(d.width) << ",\"height\":" << num(d.height)
       << "}";
  }
  if (pretty_ && !frame.detections.empty()) os << nl << ind1;
  os << "]" << nl << "}";

  std::fputs(os.str().c_str(), stdout);
  std::fputc('\n', stdout);
  std::fflush(stdout);
}

// ---------------------------------------------------------------------------
// HumanLogWriter — throttled per-camera one-liners.
//
// Called ~30x/s per camera but prints at most one line per camera per
// `interval_` seconds: every call counts the frame; only when the interval
// has elapsed does it emit a line (with FPS = frames counted / elapsed) and
// reset that camera's counter. Colors are enabled only on a TTY, so
// redirecting to a file yields clean plain text.
// ---------------------------------------------------------------------------
HumanLogWriter::HumanLogWriter(double interval_s, int max_objects)
    : interval_(interval_s),
      max_objects_(max_objects),
      color_(isatty(fileno(stdout)) != 0) {}

std::string HumanLogWriter::c(const std::string& text, int code) const {
  if (!color_) return text;
  return "\033[" + std::to_string(code) + "m" + text + kReset;
}

void HumanLogWriter::write(const FrameDetections& frame) {
  const int cid = frame.camera_id;
  frames_[cid] += 1;

  const double now = mono_secs();
  auto it = last_t_.find(cid);
  if (it != last_t_.end() && (now - it->second) < interval_) return;

  const int n = frames_[cid];
  const double fps =
      (it != last_t_.end() && now > it->second) ? n / (now - it->second) : 0.0;
  last_t_[cid] = now;
  frames_[cid] = 0;

  const int cam_col = kCamColors[cid % (sizeof(kCamColors) / sizeof(int))];
  char meta[64];
  g_snprintf(meta, sizeof(meta), "f=%d %5.1ffps", frame.frame_num, fps);

  std::ostringstream body;
  if (!frame.detections.empty()) {
    const auto& dets = frame.detections;
    const std::size_t shown =
        std::min<std::size_t>(dets.size(), static_cast<std::size_t>(max_objects_));
    body << c(std::to_string(dets.size()) + " obj", 1) << ": ";
    for (std::size_t i = 0; i < shown; ++i) {
      char conf[16];
      g_snprintf(conf, sizeof(conf), "%.2f", dets[i].confidence);
      if (i) body << " | ";
      body << c(dets[i].class_name, cam_col)
           << c("#" + std::to_string(dets[i].track_id), 90) << " " << conf;
    }
    if (dets.size() > shown)
      body << "  " << c("+" + std::to_string(dets.size() - shown) + " more", 90);
  } else {
    body << c("no objects", 90);
  }

  std::string line = "[" + c("cam" + std::to_string(cid), cam_col) + " " +
                     c(meta, 90) + "]  " + body.str() + "\n";
  std::fputs(line.c_str(), stdout);
  std::fflush(stdout);
}

// ---------------------------------------------------------------------------
std::unique_ptr<OutputWriter> make_writer(const std::string& mode,
                                          const OutputCfg& out_cfg) {
  if (mode == "json")
    return std::make_unique<JsonWriter>(out_cfg.only_nonempty, out_cfg.pretty);
  if (mode == "human")
    return std::make_unique<HumanLogWriter>(out_cfg.log_interval_s);
  if (mode == "none") return std::make_unique<NullWriter>();
  throw std::runtime_error("unknown log mode '" + mode +
                           "' (use json | human | none)");
}

}  // namespace mcrt
