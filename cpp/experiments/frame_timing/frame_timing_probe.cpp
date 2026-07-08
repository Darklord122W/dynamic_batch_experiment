/* frame_timing_probe.cpp — multi-camera frame ARRIVAL-TIMING experiment.
 *
 * Question this tool answers:
 *   "When does each camera's frame actually arrive (world time), what is the
 *    time difference among cameras right before nvstreammux, and what does
 *    that discrepancy do to batch formation?"
 *
 * It runs the SAME per-camera front-end as ../../src/pipeline_builder.cpp
 * (v4l2src -> MJPG caps -> jpegparse -> nvjpegdec -> nvvideoconvert -> NVMM
 * NV12 -> NEW nvstreammux), but replaces everything after the mux with a
 * fakesink. No nvinfer / nvtracker: downstream compute would exert
 * backpressure and pollute the PRE-mux timing this experiment measures.
 *
 *          P0 (capture)          P1 (pre-mux)            P2 (post-mux)
 *          v4l2src src pad       mux sink_<i> pad        mux src pad
 *              |                     |                       |
 *   cam i:  v4l2src -> caps -> jpegparse -> nvjpegdec -> nvvideoconvert
 *              -> caps(NVMM,NV12) ->  nvstreammux(NEW)  ->  fakesink
 *
 * At every probe point we record, per buffer:
 *   - pts_ns   : GStreamer buffer PTS (pipeline running-time domain).
 *                v4l2src derives it from the KERNEL's V4L2 buffer timestamp
 *                (CLOCK_MONOTONIC, stamped by uvcvideo when the frame finishes
 *                arriving over USB) — i.e. PTS is a genuine capture-time
 *                stamp, immune to downstream queueing.
 *   - mono_ns  : CLOCK_MONOTONIC at the probe = when the buffer PASSED here.
 *   - real_ns  : CLOCK_REALTIME  at the probe = the same instant, world clock.
 *   - seq      : GstBuffer offset. v4l2src sets it to the V4L2 sequence
 *                number, so gaps reveal KERNEL-level frame drops (P0 only).
 *
 * Clock-domain bridge (what makes "PTS in world time" possible):
 *   The pipeline clock is the monotonic GstSystemClock, so
 *       mono_capture = pts_ns + base_time
 *       real_capture = mono_capture + (CLOCK_REALTIME - CLOCK_MONOTONIC)
 *   base_time and the realtime-monotonic offset (sampled at start AND end so
 *   NTP steps are detectable) are written to meta.json.
 *
 * Outputs (CSV, one directory per run):
 *   capture.csv       P0: cam,seq,pts_ns,mono_ns,real_ns
 *   premux.csv        P1: cam,pts_ns,mono_ns,real_ns
 *   batches.csv       P2: batch_idx,batch_pts_ns,mono_ns,real_ns,n_frames
 *   batch_frames.csv  P2: batch_idx,source_id,frame_num,buf_pts_ns,ntp_ns
 *   meta.json         run parameters + clock bridge + environment
 *
 * Records are buffered in RAM (pre-reserved vectors, one tiny mutex per
 * table) and flushed only at teardown, so the probes themselves add no
 * file-I/O jitter to the measurement.
 *
 * Build:  make            (this directory)
 * Run:    ./frame_timing_probe --out-dir <dir> --duration 90 [--sync]
 * See:    README.md here for the full methodology + reproduction guide.
 */

#include <glib-unix.h>
#include <gst/gst.h>

#include <cinttypes>
#include <cstdio>
#include <deque>
#include <fstream>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <memory>
#include <mutex>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

#include "gstnvdsmeta.h"

namespace {

// ---------------------------------------------------------------------------
// Time helpers — always sample both clocks, as close together as possible.
// ---------------------------------------------------------------------------
int64_t now_ns(clockid_t id) {
  timespec ts{};
  clock_gettime(id, &ts);
  return static_cast<int64_t>(ts.tv_sec) * 1000000000LL + ts.tv_nsec;
}

/* Offset real-mono, measured by bracketing: real is sampled between two mono
 * samples; the pair with the tightest bracket wins. Error bound = bracket/2
 * (sub-microsecond in practice). */
struct ClockOffset {
  int64_t real_minus_mono_ns;
  int64_t bracket_ns;  // measurement uncertainty window
};

ClockOffset measure_clock_offset() {
  ClockOffset best{0, INT64_MAX};
  for (int i = 0; i < 9; ++i) {
    const int64_t m0 = now_ns(CLOCK_MONOTONIC);
    const int64_t r = now_ns(CLOCK_REALTIME);
    const int64_t m1 = now_ns(CLOCK_MONOTONIC);
    const int64_t bracket = m1 - m0;
    if (bracket < best.bracket_ns)
      best = ClockOffset{r - (m0 + bracket / 2), bracket};
  }
  return best;
}

// ---------------------------------------------------------------------------
// CLI
// ---------------------------------------------------------------------------
struct Args {
  std::vector<std::string> devices{"/dev/video0", "/dev/video2", "/dev/video4",
                                   "/dev/video6"};
  int width = 640;
  int height = 480;
  int fps = 30;
  std::string decoder = "nvjpegdec";  // nvjpegdec | jpegdec
  double duration_s = 60.0;
  std::string out_dir;
  bool sync_inputs = false;
  int64_t max_latency_ns = 33333333;  // 33ms, one frame @30
  int64_t timeout_us = 33333;         // batched-push-timeout
  std::string mux_config;             // optional new-mux INI ('' = none)
  std::string extra_controls;         // v4l2 UVC controls "k=v,k=v" ('' = none)
  bool pts_fix = false;               // live mode: restore true kernel capture
                                      // PTS around jpegparse (the production
                                      // app's fix; default off = the historic
                                      // instrument behaviour)

  // ---- replay mode (file sources instead of live cameras) ----
  std::string replay_dir;             // '' = live v4l2 mode
  int num_cams = 4;                   // replay only: cam0.mp4 .. cam<N-1>.mp4
  std::vector<double> skew_ms;        // per-cam start skew injected on the
                                      // pacing timeline (simulates startup
                                      // stagger); default all 0
  std::vector<double> rate;           // per-cam pacing-timeline rate factor;
                                      // 0.9608 makes 33.33ms clips pace at the
                                      // C920's true 32.026ms period; default 1
  int gap_every = 0;                  // drop 2 consecutive frames every N
                                      // frames per camera (simulates the ~1
                                      // per-2s kernel capture gap); 0 = off
  bool restamp = true;                // emulate jpegparse: rewrite PTS after
                                      // pacing onto an ideal 33.33ms grid
  int ring = 4;                       // emulate the bounded v4l2 kernel ring:
                                      // a leaky (drop-newest) queue of N
                                      // buffers between pacer and mux, so mux
                                      // backpressure drops frames instead of
                                      // stalling the pacer. 0 = unbounded
                                      // backpressure (ablation)
};

void usage(const char* prog) {
  std::fprintf(stderr,
"frame_timing_probe — camera arrival-timing experiment (NEW nvstreammux).\n"
"\n"
"Usage: %s --out-dir DIR [options]\n"
"\n"
"  --out-dir DIR        output directory for CSVs + meta.json (required)\n"
"  --devices a,b,...    V4L2 capture nodes (default: /dev/video0,2,4,6)\n"
"  --width N --height N --fps N    capture mode (default: 640x480@30 MJPG)\n"
"  --decoder NAME       nvjpegdec (default, HW) | jpegdec (SW)\n"
"  --duration SECS      capture length, 0 = until Ctrl-C (default: 60)\n"
"  --sync               nvstreammux sync-inputs=1 (time-align, drop late)\n"
"  --max-latency-ms N   sync-on alignment window (default: 33.333)\n"
"  --timeout-us N       batched-push-timeout (default: 33333)\n"
"  --mux-config PATH    new-mux batching INI (default: none)\n"
"  --extra-controls S   v4l2 UVC controls for every camera, 'k=v,k=v'.\n"
"                       e.g. 'exposure_auto_priority=0' pins the frame rate\n"
"                       (stops auto-exposure from halving fps in dim light)\n"
"  --pts-fix            live mode: restore the true kernel capture PTS around\n"
"                       jpegparse (what the production app now does by\n"
"                       default). Off = historic behaviour: the mux sees\n"
"                       jpegparse's synthetic per-camera grid\n"
"\n"
"replay mode (recorded clips instead of live cameras — see REPLAY_SKEW.md):\n"
"  --replay-dir DIR     read cam0.mp4..cam<N-1>.mp4; each is decoded, skewed,\n"
"                       paced in real time (identity sync=true) and re-stamped\n"
"                       like jpegparse would, so the mux sees a live-like feed\n"
"  --num-cams N         number of clips to replay (default: 4)\n"
"  --skew-ms a,b,..     per-camera start delay in ms, injected on the pacing\n"
"                       timeline = simulated startup stagger (default: all 0)\n"
"  --rate r0,r1,..      per-camera pacing rate factor; 0.9608 turns a 30fps\n"
"                       clip into the C920's true 32.026ms cadence; small\n"
"                       per-camera differences simulate crystal drift\n"
"  --gap-every N        drop 2 consecutive frames every N frames per camera\n"
"                       (simulated kernel capture gap; measured live: ~70)\n"
"  --no-restamp         do NOT emulate jpegparse's PTS rewrite (ablation:\n"
"                       mux then sees true capture-timeline PTS)\n"
"  --ring N             emulate the v4l2 kernel ring: bounded drop-newest\n"
"                       queue of N buffers after the pacer, so backlogged\n"
"                       cameras drop frames like live ones instead of\n"
"                       stalling (default: 4; 0 = unbounded backpressure)\n"
"  -h, --help           this help\n", prog);
}

std::string need_value(int argc, char** argv, int& i, const char* key) {
  if (i + 1 >= argc)
    throw std::runtime_error(std::string(key) + " needs a value.");
  return argv[++i];
}

Args parse_args(int argc, char** argv) {
  Args a;
  for (int i = 1; i < argc; ++i) {
    const std::string arg = argv[i];
    if (arg == "-h" || arg == "--help") {
      usage(argv[0]);
      std::exit(0);
    } else if (arg == "--out-dir") {
      a.out_dir = need_value(argc, argv, i, arg.c_str());
    } else if (arg == "--devices") {
      a.devices.clear();
      std::stringstream ss(need_value(argc, argv, i, arg.c_str()));
      std::string tok;
      while (std::getline(ss, tok, ','))
        if (!tok.empty()) a.devices.push_back(tok);
    } else if (arg == "--width") {
      a.width = std::stoi(need_value(argc, argv, i, arg.c_str()));
    } else if (arg == "--height") {
      a.height = std::stoi(need_value(argc, argv, i, arg.c_str()));
    } else if (arg == "--fps") {
      a.fps = std::stoi(need_value(argc, argv, i, arg.c_str()));
    } else if (arg == "--decoder") {
      a.decoder = need_value(argc, argv, i, arg.c_str());
    } else if (arg == "--duration") {
      a.duration_s = std::stod(need_value(argc, argv, i, arg.c_str()));
    } else if (arg == "--sync") {
      a.sync_inputs = true;
    } else if (arg == "--max-latency-ms") {
      a.max_latency_ns = static_cast<int64_t>(
          std::stod(need_value(argc, argv, i, arg.c_str())) * 1e6);
    } else if (arg == "--timeout-us") {
      a.timeout_us = std::stoll(need_value(argc, argv, i, arg.c_str()));
    } else if (arg == "--mux-config") {
      a.mux_config = need_value(argc, argv, i, arg.c_str());
    } else if (arg == "--extra-controls") {
      a.extra_controls = need_value(argc, argv, i, arg.c_str());
    } else if (arg == "--pts-fix") {
      a.pts_fix = true;
    } else if (arg == "--replay-dir") {
      a.replay_dir = need_value(argc, argv, i, arg.c_str());
    } else if (arg == "--num-cams") {
      a.num_cams = std::stoi(need_value(argc, argv, i, arg.c_str()));
    } else if (arg == "--skew-ms") {
      a.skew_ms.clear();
      std::stringstream ss(need_value(argc, argv, i, arg.c_str()));
      std::string tok;
      while (std::getline(ss, tok, ','))
        if (!tok.empty()) a.skew_ms.push_back(std::stod(tok));
    } else if (arg == "--rate") {
      a.rate.clear();
      std::stringstream ss(need_value(argc, argv, i, arg.c_str()));
      std::string tok;
      while (std::getline(ss, tok, ','))
        if (!tok.empty()) a.rate.push_back(std::stod(tok));
    } else if (arg == "--gap-every") {
      a.gap_every = std::stoi(need_value(argc, argv, i, arg.c_str()));
    } else if (arg == "--no-restamp") {
      a.restamp = false;
    } else if (arg == "--ring") {
      a.ring = std::stoi(need_value(argc, argv, i, arg.c_str()));
    } else {
      usage(argv[0]);
      throw std::runtime_error("unknown argument: " + arg);
    }
  }
  if (a.out_dir.empty()) {
    usage(argv[0]);
    throw std::runtime_error("--out-dir is required.");
  }
  if (a.devices.empty()) throw std::runtime_error("--devices list is empty.");
  if (!a.replay_dir.empty()) {
    if (a.num_cams < 1) throw std::runtime_error("--num-cams must be >= 1.");
    if (a.skew_ms.empty()) a.skew_ms.assign(a.num_cams, 0.0);
    if (a.rate.empty()) a.rate.assign(a.num_cams, 1.0);
    if (static_cast<int>(a.skew_ms.size()) != a.num_cams ||
        static_cast<int>(a.rate.size()) != a.num_cams)
      throw std::runtime_error(
          "--skew-ms / --rate need exactly one value per camera "
          "(--num-cams " + std::to_string(a.num_cams) + ").");
  }
  return a;
}

/* Number of camera branches: clips in replay mode, devices in live mode. */
int cam_count(const Args& a) {
  return a.replay_dir.empty() ? static_cast<int>(a.devices.size())
                              : a.num_cams;
}

// ---------------------------------------------------------------------------
// Record tables. Each probe appends PODs to a pre-reserved vector under a
// mutex (contention is negligible at 4x30 Hz; correctness over cleverness).
// Flushed to CSV once, at teardown, after all streaming threads stopped.
// ---------------------------------------------------------------------------
struct FrameRec {   // P0 / P1: one row per buffer per camera
  int cam;
  int64_t seq;      // v4l2 sequence (P0); -1 where not meaningful (P1)
  int64_t pts_ns;
  int64_t mono_ns;
  int64_t real_ns;
};

struct BatchRec {   // P2: one row per pushed batch
  int64_t batch_idx;
  int64_t pts_ns;
  int64_t mono_ns;
  int64_t real_ns;
  int n_frames;     // -1 if no NvDsBatchMeta was attached
};

struct BatchFrameRec {  // P2: one row per frame INSIDE a batch
  int64_t batch_idx;
  int source_id;
  int64_t frame_num;
  int64_t buf_pts_ns;
  int64_t ntp_ns;
};

struct Recorder {
  std::mutex mu_capture, mu_premux, mu_batch;
  std::vector<FrameRec> capture, premux;
  std::vector<BatchRec> batches;
  std::vector<BatchFrameRec> batch_frames;
  int64_t next_batch_idx = 0;

  void reserve(std::size_t n_cams, double duration_s, int fps) {
    // 2x headroom; duration 0 (Ctrl-C mode) reserves for 10 minutes.
    const double secs = duration_s > 0 ? duration_s : 600.0;
    const auto per_cam = static_cast<std::size_t>(secs * fps * 2);
    capture.reserve(per_cam * n_cams);
    premux.reserve(per_cam * n_cams);
    batches.reserve(per_cam);
    batch_frames.reserve(per_cam * n_cams);
  }
};

/* user data for the per-camera probes */
struct CamProbeCtx {
  Recorder* rec;
  int cam;
};

// ---------------------------------------------------------------------------
// The three probes. All are read-only taps: GST_PAD_PROBE_OK always.
// ---------------------------------------------------------------------------
GstPadProbeReturn capture_probe(GstPad*, GstPadProbeInfo* info, gpointer ud) {
  auto* ctx = static_cast<CamProbeCtx*>(ud);
  GstBuffer* buf = GST_PAD_PROBE_INFO_BUFFER(info);
  if (buf == nullptr) return GST_PAD_PROBE_OK;
  const int64_t mono = now_ns(CLOCK_MONOTONIC);
  const int64_t real = now_ns(CLOCK_REALTIME);
  FrameRec r{ctx->cam,
             GST_BUFFER_OFFSET_IS_VALID(buf)
                 ? static_cast<int64_t>(GST_BUFFER_OFFSET(buf)) : -1,
             GST_BUFFER_PTS_IS_VALID(buf)
                 ? static_cast<int64_t>(GST_BUFFER_PTS(buf)) : -1,
             mono, real};
  std::lock_guard<std::mutex> lock(ctx->rec->mu_capture);
  ctx->rec->capture.push_back(r);
  return GST_PAD_PROBE_OK;
}

GstPadProbeReturn premux_probe(GstPad*, GstPadProbeInfo* info, gpointer ud) {
  auto* ctx = static_cast<CamProbeCtx*>(ud);
  GstBuffer* buf = GST_PAD_PROBE_INFO_BUFFER(info);
  if (buf == nullptr) return GST_PAD_PROBE_OK;
  const int64_t mono = now_ns(CLOCK_MONOTONIC);
  const int64_t real = now_ns(CLOCK_REALTIME);
  FrameRec r{ctx->cam, -1,
             GST_BUFFER_PTS_IS_VALID(buf)
                 ? static_cast<int64_t>(GST_BUFFER_PTS(buf)) : -1,
             mono, real};
  std::lock_guard<std::mutex> lock(ctx->rec->mu_premux);
  ctx->rec->premux.push_back(r);
  return GST_PAD_PROBE_OK;
}

GstPadProbeReturn postmux_probe(GstPad*, GstPadProbeInfo* info, gpointer ud) {
  auto* rec = static_cast<Recorder*>(ud);
  GstBuffer* buf = GST_PAD_PROBE_INFO_BUFFER(info);
  if (buf == nullptr) return GST_PAD_PROBE_OK;
  const int64_t mono = now_ns(CLOCK_MONOTONIC);
  const int64_t real = now_ns(CLOCK_REALTIME);
  const int64_t pts = GST_BUFFER_PTS_IS_VALID(buf)
                          ? static_cast<int64_t>(GST_BUFFER_PTS(buf)) : -1;

  std::lock_guard<std::mutex> lock(rec->mu_batch);
  const int64_t idx = rec->next_batch_idx++;
  NvDsBatchMeta* bm = gst_buffer_get_nvds_batch_meta(buf);
  int n = -1;
  if (bm != nullptr) {
    n = 0;
    for (NvDsMetaList* l = bm->frame_meta_list; l != nullptr; l = l->next) {
      auto* fm = static_cast<NvDsFrameMeta*>(l->data);
      rec->batch_frames.push_back(
          BatchFrameRec{idx, static_cast<int>(fm->source_id),
                        static_cast<int64_t>(fm->frame_num),
                        static_cast<int64_t>(fm->buf_pts),
                        static_cast<int64_t>(fm->ntp_timestamp)});
      ++n;
    }
  }
  rec->batches.push_back(BatchRec{idx, pts, mono, real, n});
  return GST_PAD_PROBE_OK;
}

// ---------------------------------------------------------------------------
// Live-mode jpegparse PTS-restore fix (--pts-fix) — mirrors the production
// app (../../src/pipeline_builder.cpp). jpegparse (GstBaseParse) re-stamps
// output onto an ideal first_pts + n/fps grid; since v4l2src emits one
// complete JPEG per buffer the element is 1-in-1-out, so recording each true
// input PTS on the sink pad and re-applying it on the src pad restores the
// kernel capture stamp downstream (premux.csv then shows TRUE timestamps).
// ---------------------------------------------------------------------------
struct PtsFixCtx {
  std::mutex mu;
  std::deque<GstClockTime> fifo;
  int cam = 0;
  long warned = 0;
};

GstPadProbeReturn pts_fix_sink_probe(GstPad*, GstPadProbeInfo* info,
                                     gpointer ud) {
  auto* ctx = static_cast<PtsFixCtx*>(ud);
  GstBuffer* buf = GST_PAD_PROBE_INFO_BUFFER(info);
  if (buf == nullptr || !GST_BUFFER_PTS_IS_VALID(buf)) return GST_PAD_PROBE_OK;
  std::lock_guard<std::mutex> lock(ctx->mu);
  ctx->fifo.push_back(GST_BUFFER_PTS(buf));
  if (ctx->fifo.size() > 4) {  // 1-in-1-out: depth should stay ~1
    ctx->fifo.pop_front();
    if (ctx->warned++ == 0)
      std::fprintf(stderr,
                   "[probe] pts-fix cam %d: jpegparse buffered >4 frames — "
                   "restored PTS may lag one period.\n", ctx->cam);
  }
  return GST_PAD_PROBE_OK;
}

GstPadProbeReturn pts_fix_src_probe(GstPad*, GstPadProbeInfo* info,
                                    gpointer ud) {
  auto* ctx = static_cast<PtsFixCtx*>(ud);
  GstBuffer* buf = GST_PAD_PROBE_INFO_BUFFER(info);
  if (buf == nullptr) return GST_PAD_PROBE_OK;
  std::lock_guard<std::mutex> lock(ctx->mu);
  if (ctx->fifo.empty()) return GST_PAD_PROBE_OK;
  buf = gst_buffer_make_writable(buf);
  GST_BUFFER_PTS(buf) = ctx->fifo.front();
  ctx->fifo.pop_front();
  info->data = buf;
  return GST_PAD_PROBE_OK;
}

// ---------------------------------------------------------------------------
// Replay-mode skew machinery (see REPLAY_SKEW.md for the full rationale).
//
// A live camera has TWO timelines that a naive file replay collapses into one:
//   1. the TRUE capture/arrival timeline — 32.026 ms cadence, startup stagger,
//      occasional 2-frame gaps; this is what paces buffers into the mux;
//   2. the SYNTHETIC PTS timeline — jpegparse's ideal first_pts + n/30 s grid
//      that ignores gaps and true cadence; this is what the mux *sees*.
// So replay uses two probes per camera:
//   skew probe   (decoder src, BEFORE the pacing identity): drops gap frames,
//                then rewrites PTS' = PTS*rate + skew. identity sync=true then
//                releases each buffer at running-time PTS' -> the mux-door
//                ARRIVAL pattern reproduces the live true timeline.
//   restamp probe (identity src, AFTER the P0 recording probe): rewrites PTS
//                onto first_pts + out_index * 33.333 ms — byte-faithful
//                jpegparse emulation (counts OUTPUT frames, so injected gaps
//                vanish from the timeline, exactly like live).
// P0 records fire between pacing and restamp, so capture.csv holds the true
// timeline and premux.csv the synthetic one — same as in live mode.
// ---------------------------------------------------------------------------
struct ReplayCtx {
  Recorder* rec;  // unused by the probes, kept for symmetry/debug
  int cam;
  double rate;
  int64_t skew_ns;
  int gap_every;   // 0 = no injected gaps
  int gap_phase;   // per-camera phase so cameras don't gap in sync
  int64_t in_idx = 0;
  int64_t out_idx = 0;
  int64_t first_syn_pts = -1;
};

GstPadProbeReturn skew_probe(GstPad*, GstPadProbeInfo* info, gpointer ud) {
  auto* ctx = static_cast<ReplayCtx*>(ud);

  /* qtdemux bounds the segment at clip duration; skewed PTS would fall
   * outside it and break pacing near EOS — lift the bound. */
  if (info->type & GST_PAD_PROBE_TYPE_EVENT_DOWNSTREAM) {
    GstEvent* ev = GST_PAD_PROBE_INFO_EVENT(info);
    if (ev != nullptr && GST_EVENT_TYPE(ev) == GST_EVENT_SEGMENT) {
      const GstSegment* seg = nullptr;
      gst_event_parse_segment(ev, &seg);
      GstSegment s = *seg;
      s.stop = GST_CLOCK_TIME_NONE;
      info->data = gst_event_new_segment(&s);
      gst_event_unref(ev);
    }
    return GST_PAD_PROBE_OK;
  }

  GstBuffer* buf = GST_PAD_PROBE_INFO_BUFFER(info);
  if (buf == nullptr || !GST_BUFFER_PTS_IS_VALID(buf)) return GST_PAD_PROBE_OK;

  const int64_t idx = ctx->in_idx++;
  if (ctx->gap_every > 0 &&
      (idx + ctx->gap_phase) % ctx->gap_every < 2)  // 2-frame gap, like live
    return GST_PAD_PROBE_DROP;

  buf = gst_buffer_make_writable(buf);
  const auto pts = static_cast<int64_t>(GST_BUFFER_PTS(buf));
  GST_BUFFER_PTS(buf) = static_cast<GstClockTime>(
      static_cast<int64_t>(pts * ctx->rate) + ctx->skew_ns);
  info->data = buf;
  return GST_PAD_PROBE_OK;
}

GstPadProbeReturn restamp_probe(GstPad*, GstPadProbeInfo* info, gpointer ud) {
  auto* ctx = static_cast<ReplayCtx*>(ud);
  GstBuffer* buf = GST_PAD_PROBE_INFO_BUFFER(info);
  if (buf == nullptr || !GST_BUFFER_PTS_IS_VALID(buf)) return GST_PAD_PROBE_OK;

  buf = gst_buffer_make_writable(buf);
  if (ctx->first_syn_pts < 0)
    ctx->first_syn_pts = static_cast<int64_t>(GST_BUFFER_PTS(buf));
  /* jpegparse's grid: anchor at the first output PTS, step by the NOMINAL
   * frame duration (33.333 ms for 30/1 caps), count OUTPUT frames. */
  GST_BUFFER_PTS(buf) = static_cast<GstClockTime>(
      ctx->first_syn_pts + ctx->out_idx * INT64_C(33333333));
  ctx->out_idx++;
  info->data = buf;
  return GST_PAD_PROBE_OK;
}

// ---------------------------------------------------------------------------
// Pipeline construction (mirrors ../../src/pipeline_builder.cpp)
// ---------------------------------------------------------------------------
GstElement* make_elem(const char* factory, const std::string& name) {
  GstElement* e = gst_element_factory_make(factory, name.c_str());
  if (e == nullptr)
    throw std::runtime_error(std::string("Failed to create element '") +
                             factory + "' — check `gst-inspect-1.0 " +
                             factory + "`.");
  return e;
}

void link_chain(const std::vector<GstElement*>& elems) {
  for (std::size_t i = 0; i + 1 < elems.size(); ++i)
    if (!gst_element_link(elems[i], elems[i + 1]))
      throw std::runtime_error(std::string("Failed to link ") +
                               GST_ELEMENT_NAME(elems[i]) + " -> " +
                               GST_ELEMENT_NAME(elems[i + 1]) + ".");
}

void set_caps(GstElement* capsfilter, const std::string& caps_str) {
  GstCaps* caps = gst_caps_from_string(caps_str.c_str());
  if (caps == nullptr) throw std::runtime_error("Bad caps: " + caps_str);
  g_object_set(capsfilter, "caps", caps, nullptr);
  gst_caps_unref(caps);
}

/* qtdemux exposes its video pad dynamically; link it to h264parse on the fly. */
void on_demux_pad_added(GstElement*, GstPad* pad, gpointer user_data) {
  auto* parse = static_cast<GstElement*>(user_data);
  gchar* name = gst_pad_get_name(pad);
  const bool is_video = g_str_has_prefix(name, "video");
  g_free(name);
  if (!is_video) return;
  GstPad* sinkpad = gst_element_get_static_pad(parse, "sink");
  if (!gst_pad_is_linked(sinkpad)) gst_pad_link(pad, sinkpad);
  gst_object_unref(sinkpad);
}

struct Built {
  GstElement* pipeline = nullptr;
  GstElement* mux = nullptr;
  std::vector<GstElement*> p0_elems;  // element whose src pad is probe P0:
                                      // v4l2src (live) / pacing identity (replay)
  std::vector<GstElement*> decoders;  // replay only: skew probes attach here
  std::vector<GstElement*> jparsers;  // live only: --pts-fix probes attach here
};

Built build_pipeline(const Args& args) {
  Built b;
  b.pipeline = gst_pipeline_new("frame-timing-probe");
  GstBin* bin = GST_BIN(b.pipeline);

  b.mux = make_elem("nvstreammux", "stream-muxer");
  /* Same new-mux verification trick as the main app: the legacy mux has a
   * width property, the new one doesn't. */
  if (g_object_class_find_property(G_OBJECT_GET_CLASS(b.mux), "width") !=
      nullptr)
    throw std::runtime_error(
        "LEGACY nvstreammux loaded — USE_NEW_NVSTREAMMUX=yes did not take "
        "(check your environment).");
  g_object_set(b.mux, "batch-size", static_cast<guint>(cam_count(args)),
               nullptr);
  /* The push deadline the new mux HONOURS is the INI's overall-min-fps —
   * the batched-push-timeout property below was measured inert on DS 7.1
   * (identical fill/batch-rate from 1 to 100 ms, with and without an INI;
   * campaign_2026-07-07_ptsfix). With no --mux-config the mux default
   * min-fps=5 applies: a 200 ms service cycle, which under --sync throttled
   * live capture to 20.5 fps/cam and set staleness ~310 ms (sync_fixed_ml33).
   * Pass an INI to control the cycle. Property set after the INI so a future
   * DS release that honours it would let the CLI win. */
  if (!args.mux_config.empty())
    g_object_set(b.mux, "config-file-path", args.mux_config.c_str(), nullptr);
  g_object_set(b.mux, "batched-push-timeout",
               static_cast<gint>(args.timeout_us), nullptr);
  g_object_set(b.mux, "sync-inputs", args.sync_inputs ? TRUE : FALSE, nullptr);
  if (args.sync_inputs)
    g_object_set(b.mux, "max-latency",
                 static_cast<guint64>(args.max_latency_ns), nullptr);
  gst_bin_add(bin, b.mux);

  for (int i = 0; i < cam_count(args); ++i) {
    const std::string idx = std::to_string(i);
    GstElement* head_last = nullptr;  // last element before nvvideoconvert

    if (args.replay_dir.empty()) {
      // ---- live camera front (production-identical) ----
      GstElement* src = make_elem("v4l2src", "cam-src-" + idx);
      g_object_set(src, "device", args.devices[i].c_str(), "io-mode", 2,
                   nullptr);
      if (!args.extra_controls.empty()) {
        GstStructure* s = gst_structure_from_string(
            ("controls," + args.extra_controls).c_str(), nullptr);
        if (s == nullptr)
          throw std::runtime_error("--extra-controls: cannot parse '" +
                                   args.extra_controls +
                                   "' (want 'k=v,k=v').");
        g_object_set(src, "extra-controls", s, nullptr);
        gst_structure_free(s);
      }
      GstElement* srccaps = make_elem("capsfilter", "cam-srccaps-" + idx);
      set_caps(srccaps, "image/jpeg,width=" + std::to_string(args.width) +
                            ",height=" + std::to_string(args.height) +
                            ",framerate=" + std::to_string(args.fps) + "/1");
      GstElement* jparse = make_elem("jpegparse", "cam-jparse-" + idx);
      GstElement* jdec = make_elem(args.decoder.c_str(), "cam-jpegdec-" + idx);
      gst_bin_add_many(bin, src, srccaps, jparse, jdec, nullptr);
      link_chain({src, srccaps, jparse, jdec});
      b.p0_elems.push_back(src);
      b.jparsers.push_back(jparse);
      head_last = jdec;
    } else {
      // ---- replay front: decode, inject skew, pace, re-stamp ----
      const std::string file = args.replay_dir + "/cam" + idx + ".mp4";
      if (!std::ifstream(file).good())
        throw std::runtime_error("replay clip not found: " + file);
      GstElement* src = make_elem("filesrc", "cam-src-" + idx);
      g_object_set(src, "location", file.c_str(), nullptr);
      GstElement* demux = make_elem("qtdemux", "cam-demux-" + idx);
      GstElement* parse = make_elem("h264parse", "cam-h264parse-" + idx);
      GstElement* dec = make_elem("nvv4l2decoder", "cam-dec-" + idx);
      /* The decoder's default output pool (~5 surfaces) is smaller than the
       * backlog a congested mux creates; without headroom the pool — not the
       * ring below — becomes the throttle, the pacer starves, and lateness
       * accumulated during the stagger window freezes in forever (measured:
       * ~938 ms). Extra surfaces let the pacer always run on time and move
       * the drop decision to the ring, where it belongs. */
      g_object_set(dec, "num-extra-surfaces", 20u, nullptr);
      GstElement* pace = make_elem("identity", "cam-pace-" + idx);
      /* sync=true releases each buffer when pipeline running-time reaches its
       * (already skewed) PTS — this is what simulates live arrival. */
      g_object_set(pace, "sync", TRUE, nullptr);
      gst_bin_add_many(bin, src, demux, parse, dec, pace, nullptr);
      if (!gst_element_link(src, demux))
        throw std::runtime_error("cam " + idx + ": filesrc->qtdemux failed.");
      link_chain({parse, dec, pace});
      g_signal_connect(demux, "pad-added", G_CALLBACK(on_demux_pad_added),
                       parse);
      /* P0 is attached to nvvideoconvert's src pad (set below), NOT the
       * pacer: live P0 (v4l2src src) sits AFTER the kernel ring, so its
       * replay analog must sit after the ring stand-in too. The buffer PTS
       * there is still the pace/capture stamp (rewritten only by the restamp
       * probe, which fires after the P0 probe on the same pad), and its mono
       * time includes ring wait — exactly like live fig10's dequeue delay. */
      b.decoders.push_back(dec);
      head_last = pace;

      if (args.ring > 0) {
        /* The v4l2 kernel ring stand-in: when the mux side backs up, this
         * queue fills to `ring` buffers and then DROPS THE NEWEST arrivals
         * (leaky=upstream) — the pacer never blocks, exactly like a live
         * camera whose driver drops frames when the app can't dequeue. */
        GstElement* ringq = make_elem("queue", "cam-ring-" + idx);
        g_object_set(ringq, "max-size-buffers", static_cast<guint>(args.ring),
                     "max-size-bytes", 0u, "max-size-time",
                     static_cast<guint64>(0), "leaky", 1 /* upstream */,
                     "silent", TRUE, nullptr);
        gst_bin_add(bin, ringq);
        link_chain({pace, ringq});
        head_last = ringq;
      }
    }

    GstElement* conv = make_elem("nvvideoconvert", "cam-conv-" + idx);
    GstElement* nvmmcaps = make_elem("capsfilter", "cam-nvmmcaps-" + idx);
    set_caps(nvmmcaps, "video/x-raw(memory:NVMM),format=NV12");
    gst_bin_add_many(bin, conv, nvmmcaps, nullptr);
    link_chain({head_last, conv, nvmmcaps});
    if (!args.replay_dir.empty()) b.p0_elems.push_back(conv);  // post-ring P0

    GstPad* srcpad = gst_element_get_static_pad(nvmmcaps, "src");
    GstPad* sinkpad =
        gst_element_request_pad_simple(b.mux, ("sink_" + idx).c_str());
    if (sinkpad == nullptr)
      throw std::runtime_error("nvstreammux gave no pad sink_" + idx + ".");
    if (gst_pad_link(srcpad, sinkpad) != GST_PAD_LINK_OK)
      throw std::runtime_error("Failed to link camera " + idx + " to mux.");
    gst_object_unref(srcpad);
    gst_object_unref(sinkpad);
  }

  GstElement* sink = make_elem("fakesink", "sink");
  g_object_set(sink, "sync", FALSE, "enable-last-sample", FALSE, nullptr);
  gst_bin_add(bin, sink);
  link_chain({b.mux, sink});
  return b;
}

void attach_probes(const Built& b, Recorder* rec,
                   std::vector<std::unique_ptr<CamProbeCtx>>* ctxs) {
  for (std::size_t i = 0; i < b.p0_elems.size(); ++i) {
    // P0: v4l2src src pad (live) / pacing identity src pad (replay) —
    // the true capture/arrival timeline.
    ctxs->push_back(std::make_unique<CamProbeCtx>(
        CamProbeCtx{rec, static_cast<int>(i)}));
    GstPad* p0 = gst_element_get_static_pad(b.p0_elems[i], "src");
    gst_pad_add_probe(p0, GST_PAD_PROBE_TYPE_BUFFER, capture_probe,
                      ctxs->back().get(), nullptr);
    gst_object_unref(p0);

    // P1: the mux's own sink pad — literally the last point before batching.
    ctxs->push_back(std::make_unique<CamProbeCtx>(
        CamProbeCtx{rec, static_cast<int>(i)}));
    GstPad* p1 = gst_element_get_static_pad(
        b.mux, ("sink_" + std::to_string(i)).c_str());
    if (p1 == nullptr)
      throw std::runtime_error("mux sink pad vanished before probe attach.");
    gst_pad_add_probe(p1, GST_PAD_PROBE_TYPE_BUFFER, premux_probe,
                      ctxs->back().get(), nullptr);
    gst_object_unref(p1);
  }
  // P2: mux src pad — the moment a batch is pushed downstream.
  GstPad* p2 = gst_element_get_static_pad(b.mux, "src");
  gst_pad_add_probe(p2, GST_PAD_PROBE_TYPE_BUFFER, postmux_probe, rec,
                    nullptr);
  gst_object_unref(p2);
}

/* Live only (--pts-fix): restore true capture PTS around each jpegparse.
 * These attach to jpegparse's own pads, so they cannot race the P0/P1 probes
 * (different pads); P1 then records the RESTORED (true) timestamps. */
void attach_pts_fix_probes(const Built& b,
                           std::vector<std::unique_ptr<PtsFixCtx>>* ctxs) {
  for (std::size_t i = 0; i < b.jparsers.size(); ++i) {
    auto ctx = std::make_unique<PtsFixCtx>();
    ctx->cam = static_cast<int>(i);
    GstPad* sink = gst_element_get_static_pad(b.jparsers[i], "sink");
    GstPad* src = gst_element_get_static_pad(b.jparsers[i], "src");
    gst_pad_add_probe(sink, GST_PAD_PROBE_TYPE_BUFFER, pts_fix_sink_probe,
                      ctx.get(), nullptr);
    gst_pad_add_probe(src, GST_PAD_PROBE_TYPE_BUFFER, pts_fix_src_probe,
                      ctx.get(), nullptr);
    gst_object_unref(sink);
    gst_object_unref(src);
    ctxs->push_back(std::move(ctx));
  }
}

/* Replay only. MUST run after attach_probes: probes on one pad fire in the
 * order they were added, and the restamp probe has to see each buffer AFTER
 * the P0 capture probe recorded its true (pace) PTS. */
void attach_replay_probes(const Built& b, const Args& args,
                          std::vector<std::unique_ptr<ReplayCtx>>* ctxs) {
  for (std::size_t i = 0; i < b.decoders.size(); ++i) {
    auto ctx = std::make_unique<ReplayCtx>();
    ctx->cam = static_cast<int>(i);
    ctx->rate = args.rate[i];
    ctx->skew_ns = static_cast<int64_t>(args.skew_ms[i] * 1e6);
    ctx->gap_every = args.gap_every;
    /* stagger the gap pattern so cameras don't all gap on the same frame
     * index (live gaps are independent) */
    ctx->gap_phase = static_cast<int>(i) * 17;

    GstPad* dsrc = gst_element_get_static_pad(b.decoders[i], "src");
    gst_pad_add_probe(dsrc,
                      static_cast<GstPadProbeType>(
                          GST_PAD_PROBE_TYPE_BUFFER |
                          GST_PAD_PROBE_TYPE_EVENT_DOWNSTREAM),
                      skew_probe, ctx.get(), nullptr);
    gst_object_unref(dsrc);

    if (args.restamp) {
      GstPad* psrc = gst_element_get_static_pad(b.p0_elems[i], "src");
      gst_pad_add_probe(psrc, GST_PAD_PROBE_TYPE_BUFFER, restamp_probe,
                        ctx.get(), nullptr);
      gst_object_unref(psrc);
    }
    ctxs->push_back(std::move(ctx));
  }
}

// ---------------------------------------------------------------------------
// Output writers
// ---------------------------------------------------------------------------
std::FILE* open_or_die(const std::string& path) {
  std::FILE* f = std::fopen(path.c_str(), "w");
  if (f == nullptr) throw std::runtime_error("cannot write " + path);
  return f;
}

void write_csvs(const Args& args, const Recorder& rec) {
  {
    std::FILE* f = open_or_die(args.out_dir + "/capture.csv");
    std::fprintf(f, "cam,seq,pts_ns,mono_ns,real_ns\n");
    for (const auto& r : rec.capture)
      std::fprintf(f, "%d,%" PRId64 ",%" PRId64 ",%" PRId64 ",%" PRId64 "\n",
                   r.cam, r.seq, r.pts_ns, r.mono_ns, r.real_ns);
    std::fclose(f);
  }
  {
    std::FILE* f = open_or_die(args.out_dir + "/premux.csv");
    std::fprintf(f, "cam,pts_ns,mono_ns,real_ns\n");
    for (const auto& r : rec.premux)
      std::fprintf(f, "%d,%" PRId64 ",%" PRId64 ",%" PRId64 "\n", r.cam,
                   r.pts_ns, r.mono_ns, r.real_ns);
    std::fclose(f);
  }
  {
    std::FILE* f = open_or_die(args.out_dir + "/batches.csv");
    std::fprintf(f, "batch_idx,batch_pts_ns,mono_ns,real_ns,n_frames\n");
    for (const auto& r : rec.batches)
      std::fprintf(f, "%" PRId64 ",%" PRId64 ",%" PRId64 ",%" PRId64 ",%d\n",
                   r.batch_idx, r.pts_ns, r.mono_ns, r.real_ns, r.n_frames);
    std::fclose(f);
  }
  {
    std::FILE* f = open_or_die(args.out_dir + "/batch_frames.csv");
    std::fprintf(f, "batch_idx,source_id,frame_num,buf_pts_ns,ntp_ns\n");
    for (const auto& r : rec.batch_frames)
      std::fprintf(f,
                   "%" PRId64 ",%d,%" PRId64 ",%" PRId64 ",%" PRId64 "\n",
                   r.batch_idx, r.source_id, r.frame_num, r.buf_pts_ns,
                   r.ntp_ns);
    std::fclose(f);
  }
}

void write_meta(const Args& args, int64_t base_time_ns,
                const std::string& clock_type, ClockOffset off_start,
                ClockOffset off_end, int64_t run_start_real_ns,
                int64_t run_end_real_ns) {
  std::FILE* f = open_or_die(args.out_dir + "/meta.json");
  std::fprintf(f, "{\n");
  std::fprintf(f, "  \"tool\": \"frame_timing_probe\",\n");
  std::fprintf(f, "  \"devices\": [");
  for (std::size_t i = 0; i < args.devices.size(); ++i)
    std::fprintf(f, "%s\"%s\"", i ? ", " : "", args.devices[i].c_str());
  std::fprintf(f, "],\n");
  std::fprintf(f, "  \"width\": %d, \"height\": %d, \"fps\": %d,\n",
               args.width, args.height, args.fps);
  std::fprintf(f, "  \"decoder\": \"%s\",\n", args.decoder.c_str());
  std::fprintf(f, "  \"sync_inputs\": %s,\n",
               args.sync_inputs ? "true" : "false");
  std::fprintf(f, "  \"max_latency_ns\": %" PRId64 ",\n",
               args.max_latency_ns);
  std::fprintf(f, "  \"batched_push_timeout_us\": %" PRId64 ",\n",
               args.timeout_us);
  std::fprintf(f, "  \"mux_config\": \"%s\",\n", args.mux_config.c_str());
  std::fprintf(f, "  \"extra_controls\": \"%s\",\n",
               args.extra_controls.c_str());
  std::fprintf(f, "  \"pts_fix\": %s,\n", args.pts_fix ? "true" : "false");
  std::fprintf(f, "  \"replay_dir\": \"%s\",\n", args.replay_dir.c_str());
  if (!args.replay_dir.empty()) {
    std::fprintf(f, "  \"skew_ms\": [");
    for (int i = 0; i < args.num_cams; ++i)
      std::fprintf(f, "%s%.3f", i ? ", " : "", args.skew_ms[i]);
    std::fprintf(f, "],\n  \"rate\": [");
    for (int i = 0; i < args.num_cams; ++i)
      std::fprintf(f, "%s%.6f", i ? ", " : "", args.rate[i]);
    std::fprintf(f, "],\n  \"gap_every\": %d,\n", args.gap_every);
    std::fprintf(f, "  \"restamp\": %s,\n", args.restamp ? "true" : "false");
    std::fprintf(f, "  \"ring\": %d,\n", args.ring);
  }
  std::fprintf(f, "  \"duration_s\": %.3f,\n", args.duration_s);
  std::fprintf(f, "  \"pipeline_clock_type\": \"%s\",\n", clock_type.c_str());
  std::fprintf(f, "  \"base_time_ns\": %" PRId64 ",\n", base_time_ns);
  std::fprintf(f,
               "  \"real_minus_mono_ns_start\": %" PRId64
               ", \"real_minus_mono_bracket_ns_start\": %" PRId64 ",\n",
               off_start.real_minus_mono_ns, off_start.bracket_ns);
  std::fprintf(f,
               "  \"real_minus_mono_ns_end\": %" PRId64
               ", \"real_minus_mono_bracket_ns_end\": %" PRId64 ",\n",
               off_end.real_minus_mono_ns, off_end.bracket_ns);
  std::fprintf(f, "  \"run_start_real_ns\": %" PRId64 ",\n",
               run_start_real_ns);
  std::fprintf(f, "  \"run_end_real_ns\": %" PRId64 "\n", run_end_real_ns);
  std::fprintf(f, "}\n");
  std::fclose(f);
}

// ---------------------------------------------------------------------------
// Bus / signals (same shutdown discipline as the main app: EOS first)
// ---------------------------------------------------------------------------
struct MainCtx {
  GMainLoop* loop = nullptr;
  GstElement* pipeline = nullptr;
  int exit_code = 0;
  int sigints = 0;
  bool duration_fired = false;  // its source auto-removes after firing
};

gboolean bus_call(GstBus*, GstMessage* msg, gpointer data) {
  auto* ctx = static_cast<MainCtx*>(data);
  switch (GST_MESSAGE_TYPE(msg)) {
    case GST_MESSAGE_EOS:
      g_main_loop_quit(ctx->loop);
      break;
    case GST_MESSAGE_ERROR: {
      GError* err = nullptr;
      gchar* dbg = nullptr;
      gst_message_parse_error(msg, &err, &dbg);
      std::fprintf(stderr, "[probe] ERROR from %s: %s\n",
                   GST_OBJECT_NAME(msg->src), err ? err->message : "?");
      if (dbg) std::fprintf(stderr, "[probe] debug: %s\n", dbg);
      g_clear_error(&err);
      g_free(dbg);
      ctx->exit_code = 1;
      g_main_loop_quit(ctx->loop);
      break;
    }
    default:
      break;
  }
  return TRUE;
}

gboolean on_sigint(gpointer data) {
  auto* ctx = static_cast<MainCtx*>(data);
  if (++ctx->sigints == 1) {
    std::fprintf(stderr, "\n[probe] Interrupted — flushing EOS.\n");
    gst_element_send_event(ctx->pipeline, gst_event_new_eos());
  } else {
    g_main_loop_quit(ctx->loop);
  }
  return G_SOURCE_CONTINUE;
}

gboolean on_duration(gpointer data) {
  auto* ctx = static_cast<MainCtx*>(data);
  ctx->duration_fired = true;
  std::fprintf(stderr, "[probe] duration elapsed — stopping.\n");
  gst_element_send_event(ctx->pipeline, gst_event_new_eos());
  return G_SOURCE_REMOVE;
}

int run(const Args& args) {
  g_mkdir_with_parents(args.out_dir.c_str(), 0755);

  Built built = build_pipeline(args);

  Recorder rec;
  rec.reserve(cam_count(args), args.duration_s, args.fps);
  std::vector<std::unique_ptr<CamProbeCtx>> ctxs;
  attach_probes(built, &rec, &ctxs);
  std::vector<std::unique_ptr<PtsFixCtx>> pts_fix_ctxs;
  if (args.pts_fix && args.replay_dir.empty())
    attach_pts_fix_probes(built, &pts_fix_ctxs);
  std::vector<std::unique_ptr<ReplayCtx>> replay_ctxs;
  if (!args.replay_dir.empty())
    attach_replay_probes(built, args, &replay_ctxs);

  MainCtx ctx;
  ctx.loop = g_main_loop_new(nullptr, FALSE);
  ctx.pipeline = built.pipeline;
  GstBus* bus = gst_element_get_bus(built.pipeline);
  const guint bus_watch = gst_bus_add_watch(bus, bus_call, &ctx);
  gst_object_unref(bus);
  const guint sig_watch = g_unix_signal_add(SIGINT, on_sigint, &ctx);
  guint dur_watch = 0;
  if (args.duration_s > 0)
    dur_watch = g_timeout_add(static_cast<guint>(args.duration_s * 1000.0),
                              on_duration, &ctx);

  const ClockOffset off_start = measure_clock_offset();
  const int64_t run_start_real = now_ns(CLOCK_REALTIME);

  if (gst_element_set_state(built.pipeline, GST_STATE_PLAYING) ==
      GST_STATE_CHANGE_FAILURE) {
    std::fprintf(stderr, "[probe] Failed to reach PLAYING.\n");
    gst_element_set_state(built.pipeline, GST_STATE_NULL);
    gst_object_unref(built.pipeline);
    return 1;
  }
  if (args.replay_dir.empty()) {
    std::fprintf(stderr,
                 "[probe] %d cam(s) %dx%d@%d MJPG->%s, sync-inputs=%s, "
                 "timeout=%" PRId64 "us, pts-fix=%s -> %s (%.0fs)\n",
                 cam_count(args), args.width, args.height, args.fps,
                 args.decoder.c_str(), args.sync_inputs ? "ON" : "OFF",
                 args.timeout_us, args.pts_fix ? "ON" : "off",
                 args.out_dir.c_str(), args.duration_s);
  } else {
    std::string skews, rates;
    for (int i = 0; i < args.num_cams; ++i) {
      skews += (i ? "," : "") + std::to_string(args.skew_ms[i]);
      rates += (i ? "," : "") + std::to_string(args.rate[i]);
    }
    std::fprintf(stderr,
                 "[probe] REPLAY %d clip(s) from %s, skew-ms=[%s], "
                 "rate=[%s], gap-every=%d, restamp=%s, sync-inputs=%s, "
                 "timeout=%" PRId64 "us -> %s (%.0fs)\n",
                 args.num_cams, args.replay_dir.c_str(), skews.c_str(),
                 rates.c_str(), args.gap_every,
                 args.restamp ? "on" : "OFF",
                 args.sync_inputs ? "ON" : "OFF", args.timeout_us,
                 args.out_dir.c_str(), args.duration_s);
  }
  g_main_loop_run(ctx.loop);

  /* base_time is fixed once PLAYING starts; read it before NULL wipes it.
   * The clock object also survives until NULL. */
  const int64_t base_time_ns =
      static_cast<int64_t>(gst_element_get_base_time(built.pipeline));
  GstClock* clk = gst_element_get_clock(built.pipeline);
  std::string clock_type = "unknown";
  if (clk != nullptr) {
    clock_type = G_OBJECT_TYPE_NAME(clk);
    gst_object_unref(clk);
  }
  const ClockOffset off_end = measure_clock_offset();
  const int64_t run_end_real = now_ns(CLOCK_REALTIME);

  if (dur_watch != 0 && !ctx.duration_fired) g_source_remove(dur_watch);
  g_source_remove(sig_watch);
  g_source_remove(bus_watch);
  gst_element_set_state(built.pipeline, GST_STATE_NULL);  // stops all threads
  gst_object_unref(built.pipeline);
  g_main_loop_unref(ctx.loop);

  // Only now is it safe to read the tables without the mutexes.
  write_csvs(args, rec);
  write_meta(args, base_time_ns, clock_type, off_start, off_end,
             run_start_real, run_end_real);
  std::fprintf(stderr,
               "[probe] wrote %zu capture rows, %zu premux rows, %zu batches "
               "(%zu batched frames) -> %s\n",
               rec.capture.size(), rec.premux.size(), rec.batches.size(),
               rec.batch_frames.size(), args.out_dir.c_str());
  return ctx.exit_code;
}

}  // namespace

int main(int argc, char** argv) {
  setenv("USE_NEW_NVSTREAMMUX", "yes", 0);  // before gst_init, like the app
  try {
    Args args = parse_args(argc, argv);
    gst_init(nullptr, nullptr);
    return run(args);
  } catch (const std::exception& exc) {
    std::fprintf(stderr, "[probe] ERROR: %s\n", exc.what());
    return 2;
  }
}
