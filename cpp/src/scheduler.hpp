/* scheduler.hpp — SPARQ: Semantic Priority-Aware bounded-Queue batching.
 *
 * A completion-clocked batch former for droppable multi-camera frames.
 * Instead of letting transport backpressure decide which frames die under
 * overload (FIFO + leaky ring = process stale, drop fresh, camera-arbitrary),
 * SPARQ owns a bounded per-camera stash upstream of nvstreammux and, each
 * service opportunity, admits the most valuable K frames into the batch:
 *
 *   value(f) = w_f·fresh(f) + w_i·importance(cam f) + w_r·fairness(cam f)
 *
 * with a hard staleness bound (τ_max), a hard per-camera service-interval
 * bound (D_hard force-admission), and — in `salvage` mode — one held slot
 * per camera that re-admits a recently displaced frame from an active camera
 * into remaining batch capacity (bounded deferred recovery).
 *
 * Mechanism: a BUFFER probe on every source-bin ghost src pad refs + stashes
 * each arriving frame and returns GST_PAD_PROBE_DROP; a dedicated scheduler
 * thread releases exactly K frames per service via gst_pad_push on the same
 * pads (thread_local re-entrancy guard lets its own pushes pass the probe).
 * The mux runs with batch-size = K and a slow INI deadline, so the K-burst
 * completes via is_ready() as ONE batch, immediately. in_flight is counted
 * in FRAMES (release += K, completion −= num_frames_in_batch) and gates the
 * next release at (depth−1)·K — GPU-clocked, work-conserving, no queue
 * growth. All ages come from local CLOCK_MONOTONIC arrival stamps — never
 * from PTS (which this platform fabricates; see the PTS-fix in
 * pipeline_builder.cpp). sync-inputs stays 0.
 *
 * Modes: off (probes never attached — bit-identical baseline), fresh
 * (w_i = 0), imp, salvage (imp + held slots, needs max-same-source-frames=2
 * in the mux INI, see config/mux_sched.txt).
 */
#pragma once

#include <gst/gst.h>

#include <atomic>
#include <condition_variable>
#include <cstdio>
#include <deque>
#include <memory>
#include <mutex>
#include <set>
#include <string>
#include <thread>
#include <utility>
#include <vector>

namespace mcrt {

struct SchedCfg {
  std::string mode = "off";      // off | fresh | imp | salvage
  int k = 2;                     // frames per release (= mux batch-size)
  int depth = 2;                 // release gate: in_flight <= (depth-1)*k
  double tau_max_ms = 150.0;     // staleness bound for fresh frames
  double tau_salvage_ms = 250.0; // staleness bound for held frames
  double w_fresh = 0.40;
  double w_imp = 0.35;
  double w_fair = 0.25;
  double imp_halflife_s = 2.0;   // importance EWMA half-life
  double imp_max = 10.0;         // clip (absolute, not normalized per tick)
  double retention_thresh = 0.30;// imp_score >= this at displacement -> held
  std::string decision_csv;      // "" = no per-decision log

  bool enabled() const { return mode != "off"; }
  bool use_importance() const { return mode == "imp" || mode == "salvage"; }
  bool use_salvage() const { return mode == "salvage"; }
};

class Scheduler {
 public:
  Scheduler(const SchedCfg& cfg, int num_cams);
  ~Scheduler();

  /* Find source-bin-<i> ghost src pads + tracker src pad and attach the
   * arrival/EOS/completion probes, then start the release thread. Call this
   * AFTER MetricsCollector::attach so metrics stamps arrivals first. */
  void attach(GstElement* pipeline);

  /* Two-phase teardown (see main.cpp): request_stop() before the pipeline
   * goes NULL (a blocked push is unblocked by the state change), then
   * join_and_cleanup() after NULL but before the pipeline is unreffed. */
  void request_stop();
  void join_and_cleanup();

  /* One-line run summary for stderr. */
  void print_summary() const;

 private:
  struct Slot {                 // one stashed frame
    GstBuffer* buf = nullptr;   // owned ref (nullptr = empty)
    double t_arrival = 0.0;     // CLOCK_MONOTONIC seconds
  };
  struct CamState {
    Slot fresh;
    Slot held;                  // salvage slot (mode=salvage only)
    double importance = 0.0;    // EWMA, decayed lazily
    double imp_updated = 0.0;
    double last_served = 0.0;
    long policy_drops = 0;      // displaced/evicted by policy
    long admitted_fresh = 0;
    long admitted_held = 0;
    bool eos = false;           // EOS passed through; camera done
    GstPad* pad = nullptr;      // ghost src pad (owned ref)
    bool pad_dead = false;      // push returned EOS/FLUSHING
    std::set<int64_t> seen_ids; // for new-track importance events
  };
  struct ArrivalCtx {
    Scheduler* self;
    int cam;
  };

  static GstPadProbeReturn arrival_probe(GstPad*, GstPadProbeInfo*, gpointer);
  static GstPadProbeReturn event_probe(GstPad*, GstPadProbeInfo*, gpointer);
  static GstPadProbeReturn completion_probe(GstPad*, GstPadProbeInfo*, gpointer);

  void on_arrival(int cam, GstBuffer* buf);
  void on_completion(GstBuffer* buf);
  void thread_main();
  bool release_once();          // returns false when fully drained + all EOS
  void drop_slot(Slot& slot, int cam, const char* why);
  double importance_now(int cam, double now);
  void log_decision(double t, const char* event, int cam, const char* slot,
                    double age_ms, double fresh_s, double imp_s, double fair_s,
                    double value, int released, long in_flight);

  const SchedCfg cfg_;
  const int num_cams_;

  std::mutex mu_;
  std::condition_variable cv_;
  std::vector<CamState> cams_;
  std::atomic<bool> stop_{false};
  std::atomic<long> in_flight_{0};        // FRAMES released, not yet completed
  double s_hat_ms_ = 50.0;                // EWMA service time per batch
  std::deque<std::pair<double, int>> released_;  // (t_release, k) FIFO
  long releases_ = 0;
  long completions_ = 0;
  long salvage_admits_ = 0;
  double t_start_ = 0.0;
  double last_completion_ = 0.0;          // watchdog

  std::thread thread_;
  std::vector<std::unique_ptr<ArrivalCtx>> arrival_ctxs_;
  std::FILE* dlog_ = nullptr;
  std::mutex dlog_mu_;
};

/* Set while the scheduler thread is inside gst_pad_push / push_event, so the
 * arrival & event probes let the scheduler's own traffic through. */
extern thread_local bool t_sched_pushing;

}  // namespace mcrt
