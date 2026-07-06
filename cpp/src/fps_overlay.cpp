#include "fps_overlay.hpp"

#include <glib.h>

#include <algorithm>
#include <memory>
#include <stdexcept>

#include "gstnvdsmeta.h"
#include "nvdsmeta.h"

namespace mcrt {

namespace {
double mono_secs() { return g_get_monotonic_time() / 1e6; }

struct OverlayCtx {
  FpsMeter* meter;
  int num_cams;
  int cols;
  double tile_w;
  double tile_h;
};

GstPadProbeReturn overlay_probe(GstPad*, GstPadProbeInfo* info,
                                gpointer user_data) {
  auto* ctx = static_cast<OverlayCtx*>(user_data);
  GstBuffer* buf = GST_PAD_PROBE_INFO_BUFFER(info);
  if (buf == nullptr) return GST_PAD_PROBE_OK;
  NvDsBatchMeta* batch_meta = gst_buffer_get_nvds_batch_meta(buf);
  if (batch_meta == nullptr || batch_meta->frame_meta_list == nullptr)
    return GST_PAD_PROBE_OK;

  /* After the tiler the batch is ONE composited frame; text offsets are
   * absolute pixels in that surface, so the first frame_meta can carry every
   * tile's label. */
  auto* frame_meta =
      static_cast<NvDsFrameMeta*>(batch_meta->frame_meta_list->data);
  if (frame_meta == nullptr) return GST_PAD_PROBE_OK;

  NvDsDisplayMeta* display_meta =
      nvds_acquire_display_meta_from_pool(batch_meta);
  int n = 0;
  const int max_labels =
      std::min(ctx->num_cams, static_cast<int>(MAX_ELEMENTS_IN_DISPLAY_META));
  for (int sid = 0; sid < max_labels; ++sid) {
    const int col = sid % ctx->cols;
    const int row = sid / ctx->cols;
    NvOSD_TextParams* txt = &display_meta->text_params[n];
    /* display_text is freed by the OSD meta pool — must be g_malloc'd. */
    txt->display_text =
        g_strdup_printf("cam%d  %4.1f FPS", sid, ctx->meter->get(sid));
    txt->x_offset = static_cast<unsigned int>(col * ctx->tile_w) + 12;
    txt->y_offset = static_cast<unsigned int>(row * ctx->tile_h) + 10;
    txt->font_params.font_name = const_cast<char*>("Serif");
    txt->font_params.font_size = 12;
    txt->font_params.font_color = {1.0, 1.0, 1.0, 1.0};  // white text
    txt->set_bg_clr = 1;
    txt->text_bg_clr = {0.0, 0.0, 0.0, 0.55};            // translucent black
    ++n;
  }
  display_meta->num_labels = n;
  nvds_add_display_meta_to_frame(frame_meta, display_meta);
  return GST_PAD_PROBE_OK;
}
}  // namespace

void FpsMeter::tick(int source_id) {
  const double now = mono_secs();
  std::lock_guard<std::mutex> lock(mu_);
  Slot& s = slots_[source_id];
  if (s.count == 0 && s.start == 0.0) s.start = now;
  s.count += 1;
  const double elapsed = now - s.start;
  if (elapsed >= window_) {
    s.fps = s.count / elapsed;
    s.count = 0;
    s.start = now;
  }
}

double FpsMeter::get(int source_id) {
  std::lock_guard<std::mutex> lock(mu_);
  auto it = slots_.find(source_id);
  return it == slots_.end() ? 0.0 : it->second.fps;
}

void attach_fps_overlay(GstElement* tiler, int num_cams, FpsMeter* meter) {
  gint cols = 1, rows = 1;
  guint out_w = 1280, out_h = 720;
  g_object_get(tiler, "columns", &cols, "rows", &rows, "width", &out_w,
               "height", &out_h, nullptr);

  /* Owned by the probe: the destroy-notify below deletes it when the pad
   * (and with it the probe) is torn down, so no explicit cleanup needed. */
  auto* ctx = new OverlayCtx();
  ctx->meter = meter;
  ctx->num_cams = num_cams;
  ctx->cols = std::max(cols, 1);
  ctx->tile_w = static_cast<double>(out_w) / std::max(cols, 1);
  ctx->tile_h = static_cast<double>(out_h) / std::max(rows, 1);

  GstPad* src_pad = gst_element_get_static_pad(tiler, "src");
  if (src_pad == nullptr) {
    delete ctx;
    throw std::runtime_error(
        "nvmultistreamtiler has no src pad for the FPS overlay probe.");
  }
  gst_pad_add_probe(
      src_pad, GST_PAD_PROBE_TYPE_BUFFER, overlay_probe, ctx,
      [](gpointer data) { delete static_cast<OverlayCtx*>(data); });
  gst_object_unref(src_pad);
}

}  // namespace mcrt
