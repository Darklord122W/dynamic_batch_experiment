#include "detection_parser.hpp"

namespace mcrt {

namespace {
/* nvtracker's sentinel for objects without a confirmed track (nvdsmeta.h). */
int64_t normalize_track_id(guint64 object_id) {
  return object_id == UNTRACKED_OBJECT_ID ? -1
                                          : static_cast<int64_t>(object_id);
}
}  // namespace

std::vector<FrameDetections> parse_batch_meta(NvDsBatchMeta* batch_meta) {
  std::vector<FrameDetections> frames;
  if (batch_meta == nullptr) return frames;

  /* Two-level walk over DeepStream's GLib lists: one NvDsFrameMeta per
   * camera actually present in this batch (under sync-on that can be fewer
   * than the camera count), then one NvDsObjectMeta per tracked object.
   * Cameras with zero objects still yield a FrameDetections entry — the
   * writers and the FPS meter rely on seeing every processed frame. */
  for (NvDsMetaList* l_frame = batch_meta->frame_meta_list; l_frame != nullptr;
       l_frame = l_frame->next) {
    auto* frame_meta = static_cast<NvDsFrameMeta*>(l_frame->data);
    if (frame_meta == nullptr) continue;

    FrameDetections frame;
    frame.camera_id = static_cast<int>(frame_meta->source_id);
    frame.frame_num = frame_meta->frame_num;
    frame.buf_pts = frame_meta->buf_pts;

    for (NvDsMetaList* l_obj = frame_meta->obj_meta_list; l_obj != nullptr;
         l_obj = l_obj->next) {
      auto* obj_meta = static_cast<NvDsObjectMeta*>(l_obj->data);
      if (obj_meta == nullptr) continue;

      Detection det;
      det.camera_id = frame.camera_id;
      det.track_id = normalize_track_id(obj_meta->object_id);
      det.class_name = obj_meta->obj_label;   // char[MAX_LABEL_SIZE], NUL-terminated
      det.confidence = obj_meta->confidence;
      /* rect_params is what nvdsosd would draw: post-tracker bbox in pixels.
       * With the NEW mux these are native capture coordinates (no rescale). */
      det.x = obj_meta->rect_params.left;
      det.y = obj_meta->rect_params.top;
      det.width = obj_meta->rect_params.width;
      det.height = obj_meta->rect_params.height;
      frame.detections.push_back(std::move(det));
    }
    frames.push_back(std::move(frame));
  }
  return frames;
}

}  // namespace mcrt
