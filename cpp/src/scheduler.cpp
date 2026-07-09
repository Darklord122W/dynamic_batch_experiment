#include "scheduler.hpp"

#include <glib.h>
#include <pthread.h>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <stdexcept>

#include "gstnvdsmeta.h"

namespace mcrt {

thread_local bool t_sched_pushing = false;

namespace {
double mono_secs() { return g_get_monotonic_time() / 1e6; }
}  // namespace

Scheduler::Scheduler(const SchedCfg& cfg, int num_cams)
    : cfg_(cfg), num_cams_(num_cams), cams_(num_cams) {
  if (cfg_.k < 1 || cfg_.k > num_cams * 2)
    throw std::runtime_error("--sched-k must be in 1..2*num_cams");
  if (!cfg_.decision_csv.empty()) {
    dlog_ = std::fopen(cfg_.decision_csv.c_str(), "w");
    if (dlog_ == nullptr)
      throw std::runtime_error("Scheduler: cannot open decision CSV: " +
                               cfg_.decision_csv);
    std::fputs(
        "t,event,cam,slot,age_ms,fresh_score,imp_score,fair_score,value,"
        "released,in_flight,buf_pts\n",
        dlog_);
  }
}

Scheduler::~Scheduler() {
  request_stop();
  join_and_cleanup();
  if (dlog_ != nullptr) {
    std::fclose(dlog_);
    dlog_ = nullptr;
  }
}

void Scheduler::attach(GstElement* pipeline) {
  const double now = mono_secs();
  t_start_ = now;
  last_completion_ = now;
  for (int i = 0; i < num_cams_; ++i) {
    const std::string name = "source-bin-" + std::to_string(i);
    GstElement* bin = gst_bin_get_by_name(GST_BIN(pipeline), name.c_str());
    if (bin == nullptr)
      throw std::runtime_error("Scheduler: could not find " + name);
    GstPad* pad = gst_element_get_static_pad(bin, "src");
    gst_object_unref(bin);
    if (pad == nullptr)
      throw std::runtime_error("Scheduler: no src pad on " + name);

    auto ctx = std::make_unique<ArrivalCtx>();
    ctx->self = this;
    ctx->cam = i;
    gst_pad_add_probe(pad, GST_PAD_PROBE_TYPE_BUFFER, arrival_probe, ctx.get(),
                      nullptr);
    gst_pad_add_probe(pad, GST_PAD_PROBE_TYPE_EVENT_DOWNSTREAM, event_probe,
                      ctx.get(), nullptr);
    arrival_ctxs_.push_back(std::move(ctx));
    cams_[i].pad = pad;  // keep the ref; released in join_and_cleanup()
    cams_[i].last_served = now;
  }

  GstElement* tracker = gst_bin_get_by_name(GST_BIN(pipeline), "tracker");
  if (tracker == nullptr)
    throw std::runtime_error("Scheduler: could not find tracker.");
  GstPad* trk_src = gst_element_get_static_pad(tracker, "src");
  gst_object_unref(tracker);
  if (trk_src == nullptr)
    throw std::runtime_error("Scheduler: tracker has no src pad.");
  gst_pad_add_probe(trk_src, GST_PAD_PROBE_TYPE_BUFFER, completion_probe, this,
                    nullptr);
  gst_object_unref(trk_src);

  thread_ = std::thread(&Scheduler::thread_main, this);
  std::fprintf(stderr,
               "[sched] mode=%s k=%d depth=%d tau_max=%.0fms tau_salvage=%.0fms "
               "w=(%.2f,%.2f,%.2f)%s\n",
               cfg_.mode.c_str(), cfg_.k, cfg_.depth, cfg_.tau_max_ms,
               cfg_.tau_salvage_ms, cfg_.w_fresh, cfg_.w_imp, cfg_.w_fair,
               dlog_ ? " (decision log on)" : "");
}

// ---------------------------------------------------------------------------
// Probes (streaming threads)
// ---------------------------------------------------------------------------
GstPadProbeReturn Scheduler::arrival_probe(GstPad*, GstPadProbeInfo* info,
                                           gpointer user_data) {
  if (t_sched_pushing) return GST_PAD_PROBE_OK;  // our own release: pass
  auto* ctx = static_cast<ArrivalCtx*>(user_data);
  GstBuffer* buf = GST_PAD_PROBE_INFO_BUFFER(info);
  if (buf == nullptr) return GST_PAD_PROBE_OK;
  gst_buffer_ref(buf);  // we own a ref; the DROP below releases the pad's
  ctx->self->on_arrival(ctx->cam, buf);
  return GST_PAD_PROBE_DROP;
}

GstPadProbeReturn Scheduler::event_probe(GstPad*, GstPadProbeInfo* info,
                                         gpointer user_data) {
  GstEvent* ev = GST_PAD_PROBE_INFO_EVENT(info);
  if (ev == nullptr || GST_EVENT_TYPE(ev) != GST_EVENT_EOS)
    return GST_PAD_PROBE_OK;
  auto* ctx = static_cast<ArrivalCtx*>(user_data);
  Scheduler* self = ctx->self;
  {
    /* Let the EOS PASS THROUGH untouched (the pipeline's normal, known-good
     * teardown path — a v1 swallow-and-forward-later drain deadlocked the
     * pipeline at EOS). The scheduler just stops scheduling this camera and
     * releases its stashed refs: at most 2 tail frames per camera are not
     * processed, which is irrelevant for steady-state benchmarks (--duration
     * runs trim warmup and use rates/distributions, not totals). */
    std::lock_guard<std::mutex> lock(self->mu_);
    CamState& c = self->cams_[ctx->cam];
    c.eos = true;
    self->drop_slot(c.fresh, ctx->cam, "eos");
    self->drop_slot(c.held, ctx->cam, "eos");
  }
  self->cv_.notify_all();
  return GST_PAD_PROBE_OK;
}

GstPadProbeReturn Scheduler::completion_probe(GstPad*, GstPadProbeInfo* info,
                                              gpointer user_data) {
  auto* self = static_cast<Scheduler*>(user_data);
  GstBuffer* buf = GST_PAD_PROBE_INFO_BUFFER(info);
  if (buf != nullptr) self->on_completion(buf);
  return GST_PAD_PROBE_OK;
}

void Scheduler::on_arrival(int cam, GstBuffer* buf) {
  const double now = mono_secs();
  {
    std::lock_guard<std::mutex> lock(mu_);
    CamState& c = cams_[cam];
    if (stop_.load() || c.eos) {  // late arrival during teardown: discard
      gst_buffer_unref(buf);
      return;
    }
    if (c.fresh.buf != nullptr) {
      // Displace the previous fresh frame.
      const double imp_s =
          std::min(importance_now(cam, now) / cfg_.imp_max, 1.0);
      if (cfg_.use_salvage() && imp_s >= cfg_.retention_thresh) {
        drop_slot(c.held, cam, "displace-held");
        c.held = c.fresh;  // retained for possible salvage
        log_decision(now - t_start_, "retain-held", cam, "held",
                     (now - c.held.t_arrival) * 1e3, 0, imp_s, 0, 0, 0,
                     in_flight_.load(), GST_BUFFER_PTS(c.held.buf));
      } else {
        drop_slot(c.fresh, cam, "displace");
      }
      c.fresh = Slot{};
    }
    c.fresh.buf = buf;
    c.fresh.t_arrival = now;
  }
  cv_.notify_all();
}

void Scheduler::on_completion(GstBuffer* buf) {
  NvDsBatchMeta* batch_meta = gst_buffer_get_nvds_batch_meta(buf);
  if (batch_meta == nullptr) return;
  const double now = mono_secs();
  int frames = static_cast<int>(batch_meta->num_frames_in_batch);

  {
    std::lock_guard<std::mutex> lock(mu_);
    // Importance feedback: per processed frame, 3*new_tracks + detections.
    for (NvDsMetaList* l = batch_meta->frame_meta_list; l != nullptr;
         l = l->next) {
      auto* fm = static_cast<NvDsFrameMeta*>(l->data);
      const int cam = static_cast<int>(fm->source_id);
      if (cam < 0 || cam >= num_cams_) continue;
      CamState& c = cams_[cam];
      int dets = 0, new_tracks = 0;
      for (NvDsMetaList* lo = fm->obj_meta_list; lo != nullptr; lo = lo->next) {
        auto* obj = static_cast<NvDsObjectMeta*>(lo->data);
        ++dets;
        const auto tid = static_cast<int64_t>(obj->object_id);
        if (tid >= 0 && c.seen_ids.insert(tid).second) ++new_tracks;
      }
      /* Importance measures ACTIVITY (new objects appearing), not standing
       * content. The v1 increment (3*new_tracks + 1*dets) saturated at
       * I_max on any persistent-object scene — measured: median imp_score
       * 1.000 on every camera of an office scene, 68% of admissions at
       * >=0.99 — making the importance term a constant and imp mode
       * structurally identical to fresh mode. v2: new-track events only. */
      const double inc = 1.0 * new_tracks;
      const double old = importance_now(cam, now);  // decays to now
      c.importance = std::min(old + inc, cfg_.imp_max);
      c.imp_updated = now;
      (void)dets;
    }

    // Service-time estimate from the release FIFO.
    if (!released_.empty()) {
      const double dt_ms = (now - released_.front().first) * 1e3;
      released_.pop_front();
      s_hat_ms_ = 0.8 * s_hat_ms_ + 0.2 * dt_ms;
    }
    ++completions_;
    last_completion_ = now;
  }

  long prev = in_flight_.fetch_sub(frames);
  if (prev - frames < 0) in_flight_.store(0);  // clamp (split accounting)
  cv_.notify_all();
}

// ---------------------------------------------------------------------------
// Scheduler thread
// ---------------------------------------------------------------------------
double Scheduler::importance_now(int cam, double now) {
  CamState& c = cams_[cam];
  if (c.imp_updated <= 0.0) return 0.0;
  const double dt = now - c.imp_updated;
  if (dt > 0.0)
    return c.importance * std::exp2(-dt / cfg_.imp_halflife_s);
  return c.importance;
}

void Scheduler::drop_slot(Slot& slot, int cam, const char* why) {
  if (slot.buf == nullptr) return;
  gst_buffer_unref(slot.buf);
  slot = Slot{};
  cams_[cam].policy_drops++;
  (void)why;
}

void Scheduler::log_decision(double t, const char* event, int cam,
                             const char* slot, double age_ms, double fresh_s,
                             double imp_s, double fair_s, double value,
                             int released, long in_flight, guint64 buf_pts) {
  if (dlog_ == nullptr) return;
  std::lock_guard<std::mutex> lock(dlog_mu_);
  std::fprintf(dlog_,
               "%.4f,%s,%d,%s,%.1f,%.3f,%.3f,%.3f,%.3f,%d,%ld,%" G_GUINT64_FORMAT "\n",
               t, event, cam, slot, age_ms, fresh_s, imp_s, fair_s, value,
               released, in_flight, buf_pts);
}

void Scheduler::thread_main() {
  pthread_setname_np(pthread_self(), "sparq-sched");  // /proc-visible for
                                                      // overhead accounting
  std::unique_lock<std::mutex> lock(mu_);
  while (!stop_.load()) {
    // Wake on arrivals/completions/EOS; timeout keeps the watchdog alive.
    cv_.wait_for(lock, std::chrono::milliseconds(5));
    if (stop_.load()) break;

    // Watchdog: releases outstanding but no completion for 10x service time.
    // Armed only after a few real completions — the first batches include
    // TensorRT engine load and can legitimately take seconds.
    const double now = mono_secs();
    if (completions_ >= 3 && in_flight_.load() > 0 &&
        (now - last_completion_) * 1e3 >
            std::max(10.0 * std::max(s_hat_ms_, 50.0), 2000.0)) {
      std::fprintf(stderr,
                   "[sched] WATCHDOG: no batch completion for %.0f ms with "
                   "%ld frames in flight — resetting gate.\n",
                   (now - last_completion_) * 1e3, in_flight_.load());
      in_flight_.store(0);
      released_.clear();
      last_completion_ = now;
    }

    // Release as long as the gate allows (a completion may free room for
    // more than one release at small k).
    while (!stop_.load() && release_once()) {
    }

    // All cameras at EOS: nothing left to schedule; the pipeline's own EOS
    // (which passed through untouched) finishes the run.
    bool all_done = true;
    for (int i = 0; i < num_cams_; ++i) all_done = all_done && cams_[i].eos;
    if (all_done) break;
  }
}

/* One release attempt. Must be called with mu_ held; unlocks around the
 * pushes. Returns true if a release happened (caller loops). */
bool Scheduler::release_once() {
  const double now = mono_secs();

  // Gate: keep at most (depth-1)*k frames in flight.
  if (in_flight_.load() > static_cast<long>((cfg_.depth - 1) * cfg_.k))
    return false;

  // Evict stale slots.
  for (int i = 0; i < num_cams_; ++i) {
    CamState& c = cams_[i];
    if (c.fresh.buf != nullptr &&
        (now - c.fresh.t_arrival) * 1e3 > cfg_.tau_max_ms)
      drop_slot(c.fresh, i, "evict-stale");
    if (c.held.buf != nullptr &&
        (now - c.held.t_arrival) * 1e3 > cfg_.tau_salvage_ms)
      drop_slot(c.held, i, "evict-held");
  }

  // Build the candidate list.
  struct Cand {
    int cam;
    bool held;
    double age_ms, fresh_s, imp_s, fair_s, value;
    bool forced;
  };
  std::vector<Cand> cands;
  const double d_fair_ms =
      2.0 * (static_cast<double>(num_cams_) / cfg_.k) * s_hat_ms_;
  const double d_hard_ms = 4.0 * d_fair_ms;
  int n_eos = 0;
  for (int i = 0; i < num_cams_; ++i) {
    CamState& c = cams_[i];
    if (c.eos) ++n_eos;
    const double imp_raw = importance_now(i, now);
    const double imp_s =
        cfg_.use_importance() ? std::min(imp_raw / cfg_.imp_max, 1.0) : 0.0;
    const double since_served = (now - c.last_served) * 1e3;
    const double fair_s = std::min(since_served / d_fair_ms, 1.0);
    const bool forced = since_served > d_hard_ms && c.fresh.buf != nullptr;
    if (c.fresh.buf != nullptr) {
      const double age = (now - c.fresh.t_arrival) * 1e3;
      const double fresh_s = std::max(0.0, 1.0 - age / cfg_.tau_max_ms);
      cands.push_back({i, false, age, fresh_s, imp_s, fair_s,
                       cfg_.w_fresh * fresh_s + cfg_.w_imp * imp_s +
                           cfg_.w_fair * fair_s,
                       forced});
    }
    if (cfg_.use_salvage() && c.held.buf != nullptr) {
      const double age = (now - c.held.t_arrival) * 1e3;
      const double fresh_s = std::max(0.0, 1.0 - age / cfg_.tau_salvage_ms);
      cands.push_back({i, true, age, fresh_s, imp_s, fair_s,
                       cfg_.w_fresh * fresh_s + cfg_.w_imp * imp_s +
                           cfg_.w_fair * fair_s,
                       false});
    }
  }

  // Not enough material for a full K-batch: wait for more arrivals (frames
  // arrive at 30 fps/cam, so this resolves within one frame period). Once
  // cameras hit EOS, allow short releases from the remaining live cameras.
  if (static_cast<int>(cands.size()) < cfg_.k && n_eos == 0) return false;
  if (cands.empty()) return false;

  // Selection: forced cameras first, then by value.
  std::stable_sort(cands.begin(), cands.end(), [](const Cand& a, const Cand& b) {
    if (a.forced != b.forced) return a.forced;
    return a.value > b.value;
  });
  std::vector<Cand> admitted;
  for (const auto& cd : cands) {
    if (static_cast<int>(admitted.size()) >= cfg_.k) break;
    admitted.push_back(cd);  // <=1 fresh + <=1 held per camera by stash shape
  }

  // Take the buffers out of the stash while still locked.
  struct PushItem {
    int cam;
    bool held;
    GstBuffer* buf;
    GstPad* pad;
    Cand cd;
  };
  std::vector<PushItem> items;
  for (const auto& cd : admitted) {
    CamState& c = cams_[cd.cam];
    Slot& s = cd.held ? c.held : c.fresh;
    if (s.buf == nullptr || c.pad_dead) continue;
    items.push_back({cd.cam, cd.held, s.buf, c.pad, cd});
    s = Slot{};
    c.last_served = now;
    if (cd.held) {
      c.admitted_held++;
      salvage_admits_++;
    } else {
      c.admitted_fresh++;
    }
  }
  if (items.empty()) return false;

  // Ascending PTS within a camera: held (older) before fresh.
  std::stable_sort(items.begin(), items.end(),
                   [](const PushItem& a, const PushItem& b) {
                     if (a.cam != b.cam) return a.cam < b.cam;
                     return a.held && !b.held;
                   });

  in_flight_.fetch_add(static_cast<long>(items.size()));
  released_.emplace_back(now, static_cast<int>(items.size()));
  ++releases_;
  const long inflt = in_flight_.load();
  for (const auto& it : items)
    log_decision(now - t_start_, it.held ? "admit-salvage" : "admit", it.cam,
                 it.held ? "held" : "fresh", it.cd.age_ms, it.cd.fresh_s,
                 it.cd.imp_s, it.cd.fair_s, it.cd.value,
                 static_cast<int>(items.size()), inflt,
                 GST_BUFFER_PTS(it.buf));

  // Push outside the lock (the arrival probes must not deadlock against us).
  mu_.unlock();
  t_sched_pushing = true;
  for (const auto& it : items) {
    const GstFlowReturn ret = gst_pad_push(it.pad, it.buf);  // consumes ref
    if (ret == GST_FLOW_EOS || ret == GST_FLOW_FLUSHING) {
      std::lock_guard<std::mutex> lk(mu_);
      cams_[it.cam].pad_dead = true;
    } else if (ret != GST_FLOW_OK) {
      std::fprintf(stderr, "[sched] push on cam %d returned %s\n", it.cam,
                   gst_flow_get_name(ret));
    }
  }
  t_sched_pushing = false;
  mu_.lock();
  return true;
}

// ---------------------------------------------------------------------------
// Teardown
// ---------------------------------------------------------------------------
void Scheduler::request_stop() {
  stop_.store(true);
  cv_.notify_all();
}

void Scheduler::join_and_cleanup() {
  if (thread_.joinable()) thread_.join();
  std::lock_guard<std::mutex> lock(mu_);
  for (auto& c : cams_) {
    if (c.fresh.buf != nullptr) {
      gst_buffer_unref(c.fresh.buf);
      c.fresh = Slot{};
    }
    if (c.held.buf != nullptr) {
      gst_buffer_unref(c.held.buf);
      c.held = Slot{};
    }
    if (c.pad != nullptr) {
      gst_object_unref(c.pad);
      c.pad = nullptr;
    }
  }
  if (dlog_ != nullptr) std::fflush(dlog_);
}

void Scheduler::print_summary() const {
  const double dur = std::max(1e-6, mono_secs() - t_start_);
  long drops = 0, af = 0, ah = 0;
  for (const auto& c : cams_) {
    drops += c.policy_drops;
    af += c.admitted_fresh;
    ah += c.admitted_held;
  }
  std::fprintf(stderr,
               "[sched] %s: %ld releases (%.1f/s), %ld fresh + %ld salvage "
               "admitted, %ld policy drops, s_hat %.1f ms over %.1f s.\n",
               cfg_.mode.c_str(), releases_, releases_ / dur, af, ah, drops,
               s_hat_ms_, dur);
}

}  // namespace mcrt
