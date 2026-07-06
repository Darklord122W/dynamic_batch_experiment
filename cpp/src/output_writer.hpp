/* output_writer.hpp — the single, swappable exit point for parsed detections.
 *
 * Every detection leaves the app through ONE writer (the pad probe calls
 * write_batch() and nothing else), so pointing the output somewhere new — a
 * socket, a queue, ROS 2, a DB — only touches this file.
 *
 * Writers:
 *   JsonWriter     one JSON object per camera per frame on stdout (default)
 *   HumanLogWriter compact, throttled, colorized per-camera terminal log
 *   NullWriter     discards everything (benchmark / display-only runs)
 */
#pragma once

#include <memory>
#include <string>
#include <unordered_map>
#include <vector>

#include "app_config.hpp"
#include "detection_parser.hpp"

namespace mcrt {

class OutputWriter {
 public:
  virtual ~OutputWriter() = default;
  virtual void write(const FrameDetections& frame) = 0;
  void write_batch(const std::vector<FrameDetections>& frames) {
    for (const auto& f : frames) write(f);
  }
  virtual void close() {}
};

/* One JSON line per camera per processed frame (machine-readable default). */
class JsonWriter : public OutputWriter {
 public:
  JsonWriter(bool only_nonempty, bool pretty)
      : only_nonempty_(only_nonempty), pretty_(pretty) {}
  void write(const FrameDetections& frame) override;

 private:
  bool only_nonempty_;
  bool pretty_;
};

/* Throttled per-camera one-liners: at most one line per camera per interval.
 * Example:  [cam0 f=312  29.7fps]  2 obj: person#7 0.91 | chair#3 0.55       */
class HumanLogWriter : public OutputWriter {
 public:
  explicit HumanLogWriter(double interval_s, int max_objects = 8);
  void write(const FrameDetections& frame) override;

 private:
  std::string c(const std::string& text, int code) const;  // ANSI iff a TTY

  double interval_;
  int max_objects_;
  bool color_;
  std::unordered_map<int, double> last_t_;   // camera -> mono secs of last line
  std::unordered_map<int, int> frames_;      // camera -> frames since last line
};

class NullWriter : public OutputWriter {
 public:
  void write(const FrameDetections&) override {}
};

/* Factory for --log json | human | none. Throws on an unknown mode. */
std::unique_ptr<OutputWriter> make_writer(const std::string& mode,
                                          const OutputCfg& out_cfg);

}  // namespace mcrt
