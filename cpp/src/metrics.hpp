/* metrics.hpp — per-batch latency / throughput instrumentation.
 *
 * Writes one CSV row per processed batch with the SAME columns as the Python
 * harness (metrics.py), so scripts/analyze.py and scripts/plot_in_time.py work
 * unchanged on this app's output. Two extra trailing columns:
 *   drops_cum    frames the NEW nvstreammux reported via its "dropped" signal.
 *                Caveat (measured on DS 7.1): sync-inputs discards did NOT
 *                emit this signal — don't rely on it alone.
 *   arrivals_cum frames stamped at the source-bin src pads so far. The robust
 *                sync-loss measure is arrivals_cum − Σ n_real (frames that
 *                arrived but never came out of the tracker).
 *
 * Three probe points (all keyed by PTS, not FIFO order, so a dropped frame
 * can never permanently desync the pairing):
 *
 *   source-bin src pad  ─ arrival stamp per camera            (①)
 *   nvstreammux src pad ─ batch-push stamp = compute starts   (②)
 *   nvtracker src pad   ─ done stamp + detections walked      (③)
 *
 *   compute_ms = ② → ③   (inference + tracking)
 *   e2e_ms     = ① → ③   worst frame in the batch (includes the batch wait)
 *
 * Since this app has no camera skipping, n_active == num_cams and active_mask
 * is all-ones — kept in the CSV purely for schema compatibility.
 */
#pragma once

#include <gst/gst.h>

#include <atomic>
#include <cstdio>
#include <map>
#include <memory>
#include <mutex>
#include <set>
#include <string>
#include <utility>
#include <vector>

namespace mcrt {

class MetricsCollector {
 public:
  /* timeout_us / mux_batch are recorded verbatim into each row (they are fixed
   * for a whole run in this app — no runtime controllers). */
  MetricsCollector(const std::string& csv_path, int num_cams, int timeout_us,
                   int mux_batch, bool sync_on);
  ~MetricsCollector();

  /* Find stream-muxer / tracker / source-bin-{i} in the pipeline and attach
   * the three probe layers + the mux "dropped" signal. Throws on failure. */
  void attach(GstElement* pipeline);

  /* Flush the CSV and print a one-line summary to stderr. Idempotent. */
  void close();

 private:
  struct SrcCtx {           // user data for one camera's arrival probe
    MetricsCollector* self;
    int cam_id;
  };

  static GstPadProbeReturn src_probe(GstPad* pad, GstPadProbeInfo* info,
                                     gpointer user_data);
  static GstPadProbeReturn mux_probe(GstPad* pad, GstPadProbeInfo* info,
                                     gpointer user_data);
  static GstPadProbeReturn tracker_probe(GstPad* pad, GstPadProbeInfo* info,
                                         gpointer user_data);
  static void on_dropped(GstElement* mux, gpointer arg0, gpointer user_data);

  void handle_tracker_buffer(GstBuffer* buf);

  const int num_cams_;
  const int timeout_us_;
  const int mux_batch_;
  const bool sync_on_;
  std::string csv_path_;
  std::FILE* file_ = nullptr;
  bool closed_ = false;

  std::mutex mu_;  // guards the PTS maps + CSV writes (probes run on
                   // per-camera streaming threads and the mux output thread)
  std::map<guint64, double> mux_pts_;                  // batch PTS -> mono secs
  std::vector<std::map<guint64, double>> src_pts_;     // per camera
  static constexpr std::size_t kPtsCap = 600;          // bound memory

  std::set<std::pair<int, int64_t>> seen_ids_;         // tracking-stability proxy
  long new_ids_cum_ = 0;
  long batch_idx_ = 0;
  long real_frames_cum_ = 0;
  double t_start_ = 0.0;
  std::atomic<long> drops_cum_{0};
  std::atomic<long> arrivals_cum_{0};

  std::vector<std::unique_ptr<SrcCtx>> src_ctxs_;
};

}  // namespace mcrt
