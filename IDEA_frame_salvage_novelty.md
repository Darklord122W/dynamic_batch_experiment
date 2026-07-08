# Idea: Deferred best-effort salvage of sync-dropped frames — novelty & prior-art check

**The idea (as scoped):** When `nvstreammux` sync/`max-latency` fires and would **drop** a
late frame, instead **buffer** it; within a bounded window, collect the timed-out frames
from cam0–3 and run them through a **separate, deferred, best-effort inference pass using
the same YOLO11n**, batched together. Goal = recover **coverage/recall** (don't lose objects
only a dropped camera saw). Trade **freshness → completeness**.

---

## TL;DR verdict

**The specific mechanism appears NOVEL.** No verified peer-reviewed paper buffers
deadline-missed / sync-dropped *video frames* and re-runs them through a *deferred
best-effort same-detector batch* to recover multi-camera coverage on an edge GPU. The
three angles that would most directly anticipate it — (1) catch-up/deferred reprocessing
of dropped frames in video analytics, (5) stream-processing late-data side-outputs applied
to DNN perception, (6) dual-queue real-time+salvage GPU scheduling — **returned zero
verified papers.** That absence is the core evidence for novelty.

**BUT novelty is not the hard part — utility is.** The strongest challenge is not "who did
it before" but "why is a *late* detection of a *past* scene worth the GPU it costs." You
must answer that head-on (see §4). The good news: there's a clean, principled framing that
makes it defensible — **"raw-frame OOSM"** (§3).

---

## 1. Prior art, mapped by relationship

### PARTIALLY OVERLAPS — same principle, different stack level (your best ancestor)
**Out-of-Sequence Measurement (OOSM) / retrodiction** — the classical tracking literature
that deliberately *retains and fuses* a late measurement instead of dropping it. This is
the decades-old formalization of your exact trade-off (delay for completeness), but it
operates on **measurements/state estimates in a Kalman filter**, not raw frames through a
DNN detector.
- **Bar-Shalom, Mallick, Chen & Washburn**, "One-step solution for the general (multistep)
  out-of-sequence-measurement problem in tracking," *IEEE T-AES* 40(1):27–37, **2004**.
  Reduces a multistep-lag late measurement to a single equivalent update; treats OOSM as an
  expected, recurring condition (arises even without comms delay, from differing sensor
  preprocessing times — *exactly your per-camera-latency situation*).
- **Shi et al.**, "A Modified Bayesian Framework for Multi-Sensor Target Tracking with
  Out-of-Sequence-Measurements," *Sensors* 20(14):3821, **2020**. RADAR+IR fusion;
  late measurements compensated via retrodiction rather than dropped.
- **Mauthner et al.**, "Out-of-Sequence Measurements Treatment in Sensor Fusion
  Applications: Buffering versus Advanced Algorithms," *FAS* **2006**. Explicitly frames
  **buffer-the-late-data vs fuse-immediately** — the architectural fork you're standing at.

> **Why this matters:** cite these as the *principle* you're lifting, and state precisely
> what's new: you apply retain-and-fuse-late-data **one level up the stack** — to a whole
> buffered *image frame* re-inferred through a *shared batched object detector*, then folded
> into a multi-camera scene, rather than a scalar measurement folded into a track state.

### ORTHOGONAL — OPPOSITE trade-off (freshness over coverage): the streaming-perception family
Every one of these trades *coverage for freshness* — the mirror image of your idea. They
**predict forward** or **drop stale frames** so output aligns with the present. None
buffers or reprocesses a dropped frame. **Cite them to define the axis, then say you take
the other direction, and why (coverage-critical, not control-critical — see §4).**
- **Li, Wang & Ramanan**, "Towards Streaming Perception," **ECCV 2020** (Best Paper Hon.
  Mention). Introduces *streaming accuracy*; its explicit design choice is to **ignore/skip
  stale frames** arriving during computation, solving latency via async tracking + forecasting.
- **Yang et al.**, "Real-time Object Detection for Streaming Perception" (**StreamYOLO**),
  **CVPR 2022** (Oral). Predicts the next frame's boxes (DualFlow + trend-aware loss).
- **Jo et al.**, "DaDe: Delay-adaptive Detector for Streaming Perception," **VISIGRAPP 2023**.
  Forecasts a future timestep to compensate delay at no extra compute.
- **LASP**, "Latency-Aware Streaming Perception," arXiv:2504.19115, **2025** ⚠️. Predictive
  trajectory compensation.
- **Ghosh et al.**, "Chanakya: Learning Runtime Decisions for Adaptive Real-Time
  Perception," **NeurIPS 2023**. Learns runtime knobs (res/model); unprocessed frames never
  contribute — no salvage.
- **Lee et al.**, "ROMA: Run-Time Object Detection To Maximize Real-Time Accuracy,"
  arXiv:2210.16083 ⚠️ (WACV'23?). **Closest video-analytics contrast:** it *does* handle the
  can't-keep-up case — but by **dropping the frame and reusing the previous frame's boxes**,
  i.e. stale-box substitution, *not* deferred reprocessing. This is the paper a reviewer
  will point to; be ready to distinguish (ROMA fabricates coverage from the past; you
  recover *real* coverage from the actual dropped frame, late).

### ORTHOGONAL — shed WITHOUT recovery: imprecise computation / load-shedding
Sheds optional work to meet deadlines and **discards** it. You do load-shedding **with**
deferred recovery — the novel twist relative to this family.
- **Liu, Lin, Shih et al.**, seminal imprecise-computation model, *Foundations of Real-Time
  Computing*, Springer **1991** (mandatory + optional; optional skipped when time is short).
- **Yao et al.**, "Scheduling Real-time Deep Learning Services as Imprecise Computations,"
  **RTCSA 2020** ⚠️ (arXiv:2011.01112). DNN work as mandatory+optional; sheds least-necessary
  optional parts — no buffering/deferral of shed inputs.

### EMPTY — the load-bearing gap (angles 1, 5, 6)
No verified paper for: deferred/catch-up reprocessing of deadline-missed frames in edge
video analytics; Flink/Spark/Dataflow **allowed-lateness / side-output** late-data handling
applied to DNN perception; or **dual-queue** (real-time + background salvage) GPU inference
scheduling on a shared edge accelerator. The stream-processing *engineering* pattern
(Flink "allowed lateness" + side outputs for late events) is a real architectural precedent
worth citing as an analogy — but no *paper* ties it to this use.

---

## 2. Novelty caveat (do this before you claim it in print)
The verdict rests **partly on absence of evidence** — angles 1/5/6 came back empty, which is
"not found," not "proven absent." Before a hard novelty claim, run targeted searches on:
`catch-up inference`, `straggler frame reprocessing`, `best-effort deferred batch inference`,
`late-frame recovery video analytics`, `DART best-effort batching`, and DeepStream/
`nvstreammux` frame-drop behavior. If those stay empty, the claim is solid.

---

## 3. The framing that maximizes the contribution: **"raw-frame OOSM"**
Position the mechanism as **lifting the classical OOSM retain-and-fuse principle from the
measurement level to the raw-frame/DNN-detector level in a shared multi-camera edge
pipeline.** This gives you three things at once:
1. A respected classical ancestor (Bar-Shalom) instead of an orphan idea.
2. A crisp one-sentence novelty: *"what's new = the data type (whole image frame), the
   fusion stage (shared batched detector, not a Kalman update), and the platform (edge
   streammux)."*
3. It slots into your existing paper's thesis. Your campaign already shows
   **skip = compute/power win, not latency win ("no free lunch")**. Salvage is the natural
   counterpart on the *other* axis: when you **cannot** skip because you need coverage, here
   is how to recover the dropped frames' information at a bounded latency cost. Together they
   span the trade space — *skip-to-save-power* vs *salvage-to-recover-coverage*, both riding
   the same shared-mux drop mechanism. That's a more complete, more publishable paper than
   either half alone.

---

## 4. The real threat: utility. Answer these or a reviewer sinks it.

**Q1 — "A late detection is of a scene that already changed. Why is it useful?"**
Scope the claim to **coverage-/monitoring-critical CPS**, not **control-critical** ones. For
high-speed AV control, streaming perception (predict-forward) is correct and salvage is
wrong — concede this explicitly. For **surveillance, situational awareness, multi-camera
mapping, area monitoring, intrusion detection** — where a *complete* environmental
understanding matters more than sub-100 ms freshness — a late-but-real detection beats a
permanent blind spot. Say which regime you target.

**Q2 — "Doesn't the NvSORT tracker already fill the gap?"** *(sharpest question — nail it.)*
The tracker maintains **existing** tracks through a gap via prediction. It **cannot invent a
NEW object that first appears in a dropped frame.** Salvage recovers exactly those genuinely
new detections the tracker/forecaster structurally cannot produce. That is the precise,
defensible utility of the mechanism — measure it directly (Q4).

**Q3 — "The deferred batch competes for the same GPU as the live path."** *(the honest risk.)*
On a saturated Orin, salvage inference steals cycles from live inference and can hurt the
real-time path you care about. This is why **angle 6 (dual-queue scheduling) is unaddressed
prior art you'd have to design** — a background/best-effort queue that yields to the live
queue. This tension may itself yield a "no free lunch" result consistent with your existing
findings, which is fine — measure and report it.

**Q4 — What to measure (a reviewer will demand all four):**
- **Coverage/recall gain:** detections recovered vs baseline-drop — *especially* the count of
  *unique new objects* only a dropped camera saw and no recent frame/other camera did (Q2).
- **Staleness distribution:** how *late* are salvaged detections (age at emit)? Is it bounded?
- **Actionability fraction:** of salvaged detections, what share represent something *not
  otherwise known* (not already covered by a live track or another camera)? This is the
  metric that says whether salvage earns its keep — the rest is wasted compute.
- **Cost:** extra GPU compute / power of the deferred batch, and its effect on live-path
  p99 latency and throughput (Q3).

---

## 5. Suggested one-paragraph positioning (drop-in)
> Prior real-time perception under latency pressure resolves the freshness–coverage tension
> by favoring **freshness**: streaming-perception methods drop or forecast past stale frames
> [Li ECCV'20; StreamYOLO CVPR'22; DaDe'23], and imprecise-computation schedulers shed
> optional work without recovering it [Liu'91; Yao RTCSA'20]. The opposite choice —
> **retaining late data for completeness** — is classical only in multi-sensor tracking, where
> out-of-sequence-measurement (OOSM) methods fuse a delayed *measurement* into a track state
> rather than discarding it [Bar-Shalom T-AES'04; Shi Sensors'20; Mauthner FAS'06]. We lift
> that principle to the **raw-frame / detector** level: sync-dropped camera frames are buffered
> and re-inferred in a deferred best-effort batch on the shared edge mux, recovering
> multi-camera **coverage** at a bounded freshness cost — a salvage counterpart to
> context-aware camera skipping, and, to our knowledge, not previously done for a batched DNN
> perception pipeline.

---

## 6. Citations to verify before print (⚠️ = confirm venue/year/authors via DOI)
Tier-A (adversarially verified existence+claims): Bar-Shalom T-AES 2004; Shi Sensors 2020;
Mauthner FAS 2006; Li "Towards Streaming Perception" ECCV 2020; StreamYOLO CVPR 2022;
DaDe VISIGRAPP 2023; Chanakya NeurIPS 2023; ROMA arXiv:2210.16083; Liu imprecise-computation
1991; Yao arXiv:2011.01112.
⚠️ confirm exact venue: LASP (2504.19115, 2025), ROMA (WACV'23?), Yao (RTCSA 2020), DaDe.

Full source URLs: *(errata 2026-07-07: the workflow-output file that held the `sources`
array lived in a since-cleared `/tmp` scratchpad and no longer exists — the URLs must be
re-fetched during pre-print verification.)*
