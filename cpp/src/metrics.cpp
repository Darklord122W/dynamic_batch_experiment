#include "metrics.hpp"

#include <glib.h>

#include <algorithm>
#include <stdexcept>

#include "detection_parser.hpp"
#include "gstnvdsmeta.h"

namespace mcrt {

namespace {
double mono_secs() { return g_get_monotonic_time() / 1e6; }

/* Evict the oldest entry (smallest PTS — timestamps rise monotonically). */
void cap_map(std::map<guint64, double>& m, std::size_t cap) {
  while (m.size() > cap) m.erase(m.begin());
}
}  // namespace

MetricsCollector::MetricsCollector(const std::string& csv_path, int num_cams,
                                   int timeout_us, int mux_batch, bool sync_on)
    : num_cams_(num_cams),
      timeout_us_(timeout_us),
      mux_batch_(mux_batch),
      sync_on_(sync_on),
      csv_path_(csv_path),
      src_pts_(num_cams) {
  file_ = std::fopen(csv_path.c_str(), "w");
  if (file_ == nullptr)
    throw std::runtime_error("MetricsCollector: cannot open CSV for writing: " +
                             csv_path);
  std::fputs(
      "batch_idx,t_mono,n_in_batch,n_real,n_active,active_mask,timeout_us,"
      "mux_batch,compute_ms,e2e_ms,total_dets,new_ids_cum",
      file_);
  for (int i = 0; i < num_cams_; ++i) std::fprintf(file_, ",dets_cam%d", i);
  std::fputs(",drops_cum,arrivals_cum\n", file_);
  t_start_ = mono_secs();
}

MetricsCollector::~MetricsCollector() { close(); }

void MetricsCollector::attach(GstElement* pipeline) {
  GstElement* mux = gst_bin_get_by_name(GST_BIN(pipeline), "stream-muxer");
  GstElement* tracker = gst_bin_get_by_name(GST_BIN(pipeline), "tracker");
  if (mux == nullptr || tracker == nullptr) {
    if (mux) gst_object_unref(mux);
    if (tracker) gst_object_unref(tracker);
    throw std::runtime_error(
        "MetricsCollector: could not find stream-muxer / tracker.");
  }

  GstPad* mux_src = gst_element_get_static_pad(mux, "src");
  gst_pad_add_probe(mux_src, GST_PAD_PROBE_TYPE_BUFFER, mux_probe, this, nullptr);
  gst_object_unref(mux_src);

  GstPad* trk_src = gst_element_get_static_pad(tracker, "src");
  gst_pad_add_probe(trk_src, GST_PAD_PROBE_TYPE_BUFFER, tracker_probe, this,
                    nullptr);
  gst_object_unref(trk_src);

  /* The NEW mux emits "dropped" when sync-inputs discards a late frame —
   * count them (the legacy mux has no such signal; guard the lookup). */
  if (g_signal_lookup("dropped", G_OBJECT_TYPE(mux)) != 0)
    g_signal_connect(mux, "dropped", G_CALLBACK(on_dropped), this);

  for (int i = 0; i < num_cams_; ++i) {
    const std::string name = "source-bin-" + std::to_string(i);
    GstElement* bin = gst_bin_get_by_name(GST_BIN(pipeline), name.c_str());
    if (bin == nullptr) continue;
    GstPad* pad = gst_element_get_static_pad(bin, "src");
    if (pad != nullptr) {
      auto ctx = std::make_unique<SrcCtx>();
      ctx->self = this;
      ctx->cam_id = i;
      gst_pad_add_probe(pad, GST_PAD_PROBE_TYPE_BUFFER, src_probe, ctx.get(),
                        nullptr);
      src_ctxs_.push_back(std::move(ctx));
      gst_object_unref(pad);
    }
    gst_object_unref(bin);
  }

  gst_object_unref(mux);
  gst_object_unref(tracker);
}

GstPadProbeReturn MetricsCollector::src_probe(GstPad*, GstPadProbeInfo* info,
                                              gpointer user_data) {
  auto* ctx = static_cast<SrcCtx*>(user_data);
  GstBuffer* buf = GST_PAD_PROBE_INFO_BUFFER(info);
  if (buf != nullptr && GST_BUFFER_PTS_IS_VALID(buf)) {
    MetricsCollector* self = ctx->self;
    self->arrivals_cum_.fetch_add(1);
    std::lock_guard<std::mutex> lock(self->mu_);
    auto& m = self->src_pts_[ctx->cam_id];
    m[GST_BUFFER_PTS(buf)] = mono_secs();
    cap_map(m, kPtsCap);
  }
  return GST_PAD_PROBE_OK;
}

GstPadProbeReturn MetricsCollector::mux_probe(GstPad*, GstPadProbeInfo* info,
                                              gpointer user_data) {
  auto* self = static_cast<MetricsCollector*>(user_data);
  GstBuffer* buf = GST_PAD_PROBE_INFO_BUFFER(info);
  if (buf != nullptr && GST_BUFFER_PTS_IS_VALID(buf)) {
    std::lock_guard<std::mutex> lock(self->mu_);
    self->mux_pts_[GST_BUFFER_PTS(buf)] = mono_secs();
    cap_map(self->mux_pts_, kPtsCap);
  }
  return GST_PAD_PROBE_OK;
}

GstPadProbeReturn MetricsCollector::tracker_probe(GstPad*,
                                                  GstPadProbeInfo* info,
                                                  gpointer user_data) {
  auto* self = static_cast<MetricsCollector*>(user_data);
  GstBuffer* buf = GST_PAD_PROBE_INFO_BUFFER(info);
  if (buf != nullptr) self->handle_tracker_buffer(buf);
  return GST_PAD_PROBE_OK;
}

void MetricsCollector::on_dropped(GstElement*, gpointer, gpointer user_data) {
  static_cast<MetricsCollector*>(user_data)->drops_cum_.fetch_add(1);
}

/* The heart of the collector: runs once per batch on the tracker-src
 * streaming thread and writes one CSV row. Latencies are computed by looking
 * up the stamps the earlier probes stored, KEYED BY PTS — a frame the mux
 * dropped simply never gets its stamp consumed (and is eventually evicted by
 * cap_map), so drops can't desynchronize the pairing the way a FIFO would. */
void MetricsCollector::handle_tracker_buffer(GstBuffer* buf) {
  const double now = mono_secs();
  NvDsBatchMeta* batch_meta = gst_buffer_get_nvds_batch_meta(buf);
  const auto frames = parse_batch_meta(batch_meta);

  std::lock_guard<std::mutex> lock(mu_);
  if (file_ == nullptr) return;  // closed while a buffer was in flight

  /* compute latency (mux src -> tracker src), matched by the batch PTS. */
  double compute_ms = -1.0;
  if (GST_BUFFER_PTS_IS_VALID(buf)) {
    auto it = mux_pts_.find(GST_BUFFER_PTS(buf));
    if (it != mux_pts_.end()) {
      compute_ms = (now - it->second) * 1e3;
      mux_pts_.erase(it);
    }
  }

  /* Per-camera detections + true source->output latency + stability proxy. */
  std::vector<int> per_cam(num_cams_, 0);
  int total = 0;
  double max_e2e = -1.0;
  int n_real = 0;  // frames whose PTS matches a real source arrival stamp
  for (const auto& frame : frames) {
    const int cid = frame.camera_id;
    total += static_cast<int>(frame.detections.size());
    if (cid >= 0 && cid < num_cams_) {
      per_cam[cid] = static_cast<int>(frame.detections.size());
      auto& m = src_pts_[cid];
      auto it = m.find(frame.buf_pts);
      if (it != m.end()) {
        ++n_real;
        const double lat_ms = (now - it->second) * 1e3;
        if (lat_ms > max_e2e) max_e2e = lat_ms;
        m.erase(it);
      }
    }
    /* Tracking-stability proxy: count each (camera, track_id) pair the first
     * time it is seen. A pipeline variant that fragments tracks (e.g. by
     * dropping frames) shows up as a faster-growing new_ids_cum. */
    for (const auto& det : frame.detections) {
      if (det.track_id >= 0 &&
          seen_ids_.insert({cid, det.track_id}).second)
        ++new_ids_cum_;
    }
  }

  const int n_in_batch =
      batch_meta != nullptr ? static_cast<int>(batch_meta->num_frames_in_batch) : 0;

  /* No skipping in this app: all cameras are always active. */
  const std::string active_mask(num_cams_, '1');
  std::fprintf(file_, "%ld,%.4f,%d,%d,%d,%s,%d,%d,%.3f,%.3f,%d,%ld",
               batch_idx_, now - t_start_, n_in_batch, n_real, num_cams_,
               active_mask.c_str(), timeout_us_, mux_batch_, compute_ms,
               max_e2e, total, new_ids_cum_);
  for (int i = 0; i < num_cams_; ++i) std::fprintf(file_, ",%d", per_cam[i]);
  std::fprintf(file_, ",%ld,%ld\n", drops_cum_.load(), arrivals_cum_.load());
  ++batch_idx_;
  real_frames_cum_ += n_real;
}

void MetricsCollector::close() {
  std::lock_guard<std::mutex> lock(mu_);
  if (closed_) return;
  closed_ = true;
  if (file_ != nullptr) {
    std::fflush(file_);
    std::fclose(file_);
    file_ = nullptr;
  }
  const double dur = std::max(1e-6, mono_secs() - t_start_);
  const long arrived = arrivals_cum_.load();
  std::fprintf(stderr,
               "[metrics] wrote %ld batches to %s (%.1f batches/s over %.1fs; "
               "%ld distinct tracks; sync %s; processed %ld of %ld arrived "
               "frames; mux dropped-signal count %ld).\n",
               batch_idx_, csv_path_.c_str(), batch_idx_ / dur, dur,
               new_ids_cum_, sync_on_ ? "ON" : "off", real_frames_cum_,
               arrived, drops_cum_.load());
}

}  // namespace mcrt
