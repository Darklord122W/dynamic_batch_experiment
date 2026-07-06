/* detection_parser.hpp — NvDsBatchMeta -> plain C++ detection structs.
 *
 * DeepStream attaches inference/tracking results to the GstBuffer as
 * NvDsBatchMeta. A pad probe on nvtracker's src pad hands the buffer here and
 * this walks the metadata tree:
 *
 *   NvDsBatchMeta
 *    └─ frame_meta_list          one NvDsFrameMeta per camera in the batch
 *        ├─ source_id            which camera (== nvstreammux sink pad index)
 *        ├─ frame_num            per-camera frame index
 *        ├─ buf_pts              the frame's original PTS (latency correlation)
 *        └─ obj_meta_list        one NvDsObjectMeta per tracked object
 *            ├─ obj_label / class_id / confidence
 *            ├─ object_id        persistent track ID from nvtracker
 *            └─ rect_params      bbox .left/.top/.width/.height in pixels
 *
 * With the NEW nvstreammux frames are never scaled by the mux, so the bbox
 * pixel coordinates are directly in each camera's native capture resolution.
 */
#pragma once

#include <cstdint>
#include <string>
#include <vector>

#include "nvdsmeta.h"

namespace mcrt {

/* A single tracked detection in one camera's frame. */
struct Detection {
  int camera_id = 0;      // nvstreammux source_id == index in camera_params.yaml
  int64_t track_id = -1;  // nvtracker persistent ID; -1 = not yet tracked
  std::string class_name; // human-readable label, e.g. "person"
  float confidence = 0.f; // detector confidence in [0, 1]
  float x = 0.f;          // bbox top-left x, pixels (source resolution)
  float y = 0.f;          // bbox top-left y, pixels
  float width = 0.f;      // bbox width, pixels
  float height = 0.f;     // bbox height, pixels
};

/* All detections for ONE camera in ONE processed frame — the output unit. */
struct FrameDetections {
  int camera_id = 0;
  int frame_num = 0;
  uint64_t buf_pts = 0;   // frame PTS (ns); correlates with source-side stamps
  std::vector<Detection> detections;
};

/* Walk a batch's metadata; returns one FrameDetections per camera present in
 * this batch (cameras with zero objects still get an entry). */
std::vector<FrameDetections> parse_batch_meta(NvDsBatchMeta* batch_meta);

}  // namespace mcrt
