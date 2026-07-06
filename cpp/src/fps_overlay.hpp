/* fps_overlay.hpp — live per-camera FPS text in each tile of the debug view.
 *
 * FpsMeter measures a rolling per-camera FPS; it is ticked once per camera per
 * frame from the detection probe on nvtracker's src pad (where the batch
 * reliably carries one frame_meta per camera). attach_fps_overlay() adds a
 * probe on nvmultistreamtiler's src pad that, once per composited buffer,
 * drops one text label at each camera's tile corner via nvdsosd display meta.
 *
 * Only used in --display / --record modes.
 */
#pragma once

#include <gst/gst.h>

#include <mutex>
#include <unordered_map>

namespace mcrt {

/* Rolling per-camera FPS estimate. Thread-safe: ticked from the tracker-src
 * streaming thread, read from the tiler-src streaming thread. */
class FpsMeter {
 public:
  explicit FpsMeter(double window_s = 0.5) : window_(window_s) {}
  void tick(int source_id);
  double get(int source_id);

 private:
  struct Slot {
    int count = 0;
    double start = 0.0;
    double fps = 0.0;
  };
  double window_;
  std::mutex mu_;
  std::unordered_map<int, Slot> slots_;
};

/* Attach the overlay probe. The tiler lays streams out row-major by source_id,
 * so camera i sits at tile (i / columns, i % columns); labels are positioned
 * in the composited output's pixel space. Throws on a missing src pad. */
void attach_fps_overlay(GstElement* tiler, int num_cams, FpsMeter* meter);

}  // namespace mcrt
