# Literature Review & Venue Positioning
### Target: IEEE *Systems, Man & Cybernetics Letters* (L-SMC) — Special Issue on Cyber-Physical Autonomous Systems
### Paper angle: **adaptive resource-management method** for real-time multi-camera edge perception

> Submission deadline **2026-07-15** · 4–7 pages · APC $525 (accepted).
> Guest editors incl. Junfei Xie (SDSU), Yan Wan (UT Arlington), Peter Langendörfer (BTU).

This document is the fact-checked related-work foundation for a paper on your
`multicam_perception_rt` system. All papers below were surfaced by a fan-out web
search and passed a source-fetch extraction; the **Tier-A** set additionally passed
3-vote adversarial existence/claim verification. Venues flagged with ⚠️ should be
double-checked against the DOI before you commit them to a bibliography (see §7).

---

## 1. Your contribution — the anchor everything is positioned against

> A runtime **adaptive resource-management mechanism** for real-time multi-camera
> perception on a resource-constrained edge GPU (Jetson AGX Orin, DeepStream 7.1,
> YOLO11n + NvSORT, ≤4 USB cameras on a shared `nvstreammux`). Two coupled levers:
> **(1) context-aware whole-camera skipping** (per-camera valves driven by
> activity/scheduled context providers) and **(2) an adaptive batching timeout**
> (`nvstreammux batched-push-timeout` adapted at runtime to the active-camera count),
> evaluated with an RT-BEV-style methodology and the **FES** metric.

**The honest empirical result is the contribution, not a weakness:** on a shared
legacy mux, camera skipping is a **GPU-compute/power win but not a latency win** —
partial batches still pay the full mux timeout — and adaptive *batch-size* does not
help. "No free lunch" on the latency–throughput–power Pareto frontier. Frame this as
a **measurement-grounded characterization of an adaptive mechanism**, which is
exactly what a Letters venue rewards: a crisp, reproducible, correctly-scoped result.

---

## 2. Related-work map (grouped by thread)

Each entry: **method → how it relates to you** (Supports / Competes / Orthogonal).

### Thread 1 — Adaptive DNN batching under latency SLOs
*The closest mechanistic neighbors: everyone here tunes batch size/timing for a
latency–throughput trade-off, but on general/cloud serving stacks with no notion of
per-camera stream context.*

| Paper | Venue | Core method | Relation |
|---|---|---|---|
| **DVABatch** (Cui et al.) [Tier-A] | USENIX ATC 2022 | Multi-entry/multi-exit batching (new/stretch/split meta-ops) adjusts an *in-flight* batch instead of committing at one timeout; −46.4% avg latency, up to 2.12× throughput. | **Competes / motivates.** The canonical modern dynamic-batching baseline. Their "split" op is exactly the early-push-on-partial-batch your legacy mux *cannot* do — a clean way to explain your negative result. |
| **Clockwork** (Gujarati et al.) [Tier-A] | OSDI 2020 | Centralized controller exploits deterministic inference times to batch/schedule thousands of models; meets 100 ms for 99.9999% of requests. | **Orthogonal (cloud-scale).** Shows predictable inference enables tight tail-latency batching — contrast with your uncontrolled mux wait. |
| **Jellyfish** (Nigade et al.) [Tier-A] | ⚠️ Real-Time Systems (Springer) 2024 / RTSS'22 lineage | Soft end-to-end latency SLO guarantees on dynamic edge networks by jointly adapting input data + DNN variant. | **Supports framing.** Argues "timely inference" is a distinct, understudied objective — your exact stance. |
| **BCEdge** (Zhang et al.) [Tier-A] | ⚠️ arXiv 2305.01519 (2023) | Max-entropy DRL co-optimizes batch size + concurrent-model count on edge; formalizes throughput-vs-latency utility. | **Competes (heavier).** Same trade-off, solved with DRL. Your active-camera heuristic is the lightweight, deployable alternative — a good "we avoid learned control" contrast. |
| **Clipper** (Crankshaw et al.) [fetch-only] | NSDI 2017 | Serving layer with online **adaptive batch sizing under a latency SLO**, caching, model selection. | **Foundational.** The origin of timeout/SLO-driven adaptive batching — cite as the lineage your timeout controller sits in. |
| **Nexus** (Shen et al.) [fetch-only] | SOSP 2019 | Batching-aware bin-packing scheduler; aggregates many streams because no single video stream saturates a GPU; 1.8–12.7× higher rate vs TF-Serving/Clipper. | **Supports motivation.** The multi-stream-aggregation argument *is* why you batch cameras through one mux. |

### Thread 2 — Context/content-aware frame filtering & skipping
*Everyone here drops redundant work by content — but **within a stream**, and none
couples the dropping to a shared multi-camera batch timeout.*

| Paper | Venue | Core method | Relation |
|---|---|---|---|
| **Reducto** (Li, Padmanabhan et al.) [Tier-A] | SIGCOMM 2020 | On-camera frame-differencing adapts to content correlation; filters 51–97% of frames while meeting accuracy; explicit two-sided threshold trade-off. | **Closest in spirit / Competes.** The reference "context-aware dropping" system. Key distinction: Reducto drops *frames per stream*; you drop *whole cameras* and couple it to the mux — and you show the coupling changes the latency story. |
| **Chameleon** (Jiang et al.) [Tier-A] | SIGCOMM 2018 | Dynamically adapts config knobs (resolution/frame-rate/detector) at second granularity, amortized *across multiple cameras*; 20–50% higher accuracy at equal cost, or same accuracy at 30–50% of resources. | **Competes (config, not skip).** Nearest multi-camera adaptive controller; adapts quality knobs, not camera membership + batch timing. |
| **NoScope** (Kang et al.) [Tier-A] | VLDB 2017 | Cascade of cheap difference detectors + specialized models before the full detector; 265–15,500× on binary queries. | **Orthogonal (query-specific).** Establishes exploiting temporal redundancy; your activity provider is the online, general-detection analogue. |
| **FrameHopper** (Arefeen et al.) [Tier-A] | ⚠️ DCOSS 2022 | Offline-trained RL agent sets per-frame skip lengths; frame-skipping as error-vs-rate optimization. | **Competes (per-frame RL).** Lower-tier venue, but a clean example of *learned* skipping — contrast with your interpretable activity/scheduled providers. |

### Thread 3 — Real-time multi-camera edge perception (the nearest antecedents)

| Paper | Venue | Core method | Relation |
|---|---|---|---|
| **RT-BEV** (Liu, Lee, Shin) [Tier-A] | **IEEE RTSS 2024** | Co-optimizes message communication + detection for real-time BEV; ROI-aware **camera synchronizer** + dynamic ROIs + time-to-collision-driven priority; reports 1.5× lower avg e2e latency, ~2× frames, **2.9× FES**, 19.3× lower worst-case. | **THE antecedent — your explicit methodology/FES source.** *Critical distinction to state plainly:* RT-BEV **synchronizes** cameras and uses **spatial ROIs** on a **fused BEV model** (nuScenes, PyTorch/ROS, server-class); it does **not drop whole cameras**, does **not adapt a mux batching timeout**, and does **not run per-camera independent detection on a Jetson DeepStream USB rig**. That platform + mechanism gap is your niche. |
| **REVAMP²T** (Neff et al.) [Tier-A] | IEEE IoT-J 2020 | End-to-end multi-camera edge analytics (detect + re-ID + track) on **Jetson AGX Xavier**, TensorRT FP16; >2× real-time FPS at **1/5 the power** of prior SOTA at −4.3% accuracy; proposes composite **Accuracy-Efficiency (AE)** metric. | **Supports + validates your framing.** Prior evidence that on edge, the win is **throughput/power at bounded accuracy** — precisely your "skip = compute/power win" result. AE is a prior-art composite metric alongside FES (cite both to justify FES). |

### Thread 4 — Energy-efficient / resource-constrained inference on embedded (Jetson) GPUs
*Grounds the "compute/power win" half of your result — that saving GPU work on an
edge SWaP-constrained device is a first-class objective.*

| Paper | Venue | Core method | Relation |
|---|---|---|---|
| **NeuOS** (Bateni & Liu) [fetch-only] | USENIX ATC 2020 | Runtime for multi-DNN autonomous workloads on **Jetson TX2 / AGX Xavier**: coordinates **DVFS + dynamic accuracy** for latency-predictability + energy; near-zero deadline misses. | **Complementary lever.** DVFS is the *other* knob you don't pull — position camera-skipping as a coarser, application-level power lever, and note DVFS as future combinable work. |
| **PolyThrottle** (Yan, Wang, Venkataraman) [fetch-only] | ⚠️ arXiv 2310.19991 (2023; MLSys'24 lineage) | Constrained Bayesian optimization tunes GPU/mem/CPU frequency for energy under a latency constraint; up to 36% energy saved. | **Supports Pareto framing.** Formalizes edge inference as a *constrained* latency–energy optimization — the same frontier you characterize. |

### Thread 5 — Real-time scheduling & end-to-end latency for CPS/AV perception
*Anchors the paper to the L-SMC "cyber-physical autonomous systems" framing — this
is the thread that makes it a CPS paper, not just a GStreamer engineering note.*

| Paper | Venue | Core method | Relation |
|---|---|---|---|
| **D³ / ERDOS-Pylot** (Kalra, Schafhalter et al.) [fetch-only] | EuroSys 2022 | Dynamic **deadline-driven** execution for AV pipelines; adapts computation to varying deadlines; **68% fewer collisions**; observes perception p99 latency 3.3× the mean, forcing **message dropping**. | **Supports (load-shedding precedent).** Your camera-skipping *is* application-level load shedding; D³ shows dropping under deadline pressure is an accepted AV-perception tactic. |
| **Prophet** (Liu et al.) [fetch-only] | ⚠️ RTSS 2022 | Predicts per-model inference-time variation to make multi-tenant AV perception predictable; coordinates via **early-exit + frame-skipping**; bounds fusion delay to 150 ms. | **Competes (scheduling-driven skip).** Skips frames by *deadline prediction*; you skip cameras by *scene context*. Good contrast of skip trigger. |
| **DART** (Xiang & Kim) [fetch-only] | ⚠️ RTSS 2019 | Pipeline scheduling with data parallelism across CPU/GPU for deterministic WCRT; explicitly **restricts batching to best-effort tasks** because batching adds unpredictable delay. | **Directly supports your negative result.** DART's thesis — *batching hurts real-time predictability* — is independent prior evidence for why your batch-timeout wait dominates tail latency. Strong citation for the discussion. |
| **Self-Cueing Attention Scheduling** (Liu, Abdelzaher et al.) [fetch-only] | ⚠️ RTAS 2022 | Criticality-aware **selective region processing** on **Jetson Xavier**; optical-flow (activity) module cues which regions to inspect; batched proportional-balancing GPU scheduler. | **Nearest philosophy match.** Activity-driven selective processing to save GPU on a Jetson — same idea as your activity valves, applied *within a frame* rather than *across cameras*. Excellent "closest related" citation. |

### Foundational primitives (cite briefly, 1–2 lines each)

| Paper | Venue | Why you cite it |
|---|---|---|
| **SORT** (Bewley et al.) [Tier-A] | ICIP 2016 | Foundation of your **NvSORT** tracker: Kalman + Hungarian IOU association at 260 Hz — justifies the lightweight, no-reID tracking choice for edge. |
| **DeepSORT** (Wojke et al.) [fetch-only] | ICIP 2017 | Appearance-metric extension (−45% ID switches); cite as the heavier alternative you deliberately *don't* use (no re-ID CNN), consistent with your compute budget. |

---

## 3. The gap statement (drop-in for your intro/related-work close)

> Adaptive DNN-serving systems (DVABatch, Clockwork, Nexus, Clipper, BCEdge) size or
> time batches for a latency–throughput trade-off, but on general serving stacks that
> are **blind to per-camera scene context**. Content-aware analytics systems (Reducto,
> Chameleon, NoScope, FrameHopper) exploit that context to **drop frames within a
> single stream**, but never couple the dropping to a **shared multi-camera batch
> boundary**. Real-time multi-camera perception frameworks (RT-BEV, REVAMP²T)
> **synchronize** cameras into a fused model on server- or Xavier-class hardware, and
> RT scheduling work (D³, Prophet, DART) sheds load by **deadline**, not by scene
> context. **No prior work couples context-driven *whole-camera* skipping to a
> runtime-adapted shared-mux batching timeout on a single legacy `nvstreammux` edge
> pipeline — nor reports the resulting Pareto pathology: that partial batches still pay
> the full mux timeout, making skipping a compute/power win but not a latency win.**
> We characterize this mechanism and trade-off on a deployed Jetson AGX Orin system.

---

## 4. Venue positioning for L-SMC (how to frame + which CFP topics to claim)

**Map your paper onto these exact CFP topic lines** (use their words in your intro):
- *Real-time perception, obstacle detection, and avoidance* ✅ (primary)
- *Edge computing and resource-constrained AI for autonomous systems* ✅ (primary)
- *System modeling, simulation, and performance evaluation* ✅ (your harness/metrics)
- *Sensor data fusion and multimodal perception* ✅ (multi-camera; note you do
  *late/independent* per-camera detection, not fusion — be precise so a reviewer
  doesn't ding you for over-claiming fusion).

**How to make the negative results a strength (this is the whole game for a Letter):**
1. **Lead with a question, not a system.** "Does context-aware camera skipping reduce
   end-to-end latency on a shared batched edge pipeline?" Then answer it with
   measurement. A crisp, correctly-scoped *answer* (even "no, and here's precisely
   why") is a legitimate Letters contribution.
2. **Name the mechanism as the artifact.** The valve + adaptive-timeout controller +
   reproducible replay/metrics harness is a concrete, reusable deliverable — that's
   what makes it more than a null result.
3. **Use DART as independent theoretical backing** that batching harms RT
   predictability — your measurement instantiates a known principle on a real edge
   stack, which reads as *confirmatory science*, not a failed optimization.
4. **Report the Pareto plot** (`tradeoff.png`) as the headline figure — reviewers
   trust a clear cost/benefit frontier far more than a single win number.
5. **Quantify the actual win honestly:** compute/power reduction from skipping (with
   `n_active`, not the phantom-padded batch count — your metric-reliability lesson is
   itself a citable methodological point).

**Scope discipline for 4–7 pages** (don't try to do everything):
- ~1 pg intro + gap (§3), ~0.75 pg related work (compress §2 to 2 sentences/thread),
  ~1.5 pg system + mechanism, ~2 pg evaluation (E1 timeout sweep + the skip/adaptive
  campaign table + Pareto fig), ~0.5 pg discussion (DART tie-in, DVFS future work),
  short conclusion. Cut E2 static-engine detail to one sentence.

---

## 5. Suggested paper skeleton

1. **Introduction** — CPS multi-camera edge perception; the shared-batch tension;
   your question; contributions (mechanism + characterization + honest Pareto result).
2. **Related work** — the four one-liners from §2 threads + the §3 gap sentence.
3. **System & mechanism** — DeepStream pipeline (the README ASCII diagram), the valve
   camera-skip, the adaptive `batched-push-timeout` controller, context providers.
4. **Evaluation methodology** — replay harness, metrics (compute_ms, e2e/wait, FES),
   why `n_active` not batch-fullness (reliability lesson), RT-BEV/FES lineage.
5. **Results** — E1 timeout sweep; skip vs skip+adaptive-timeout campaign; Pareto fig;
   the "adaptive batch-size doesn't help / legacy-mux pushes on all-pads-or-timeout"
   root-cause.
6. **Discussion** — DART/RT-predictability tie-in; when skipping *does* pay (desync/
   power/thermal); path to a real latency win (new `nvstreammux`, dynamic pad release).
7. **Conclusion + future work** — combine with DVFS (NeuOS/PolyThrottle); new-mux
   deadline-based push.

---

## 6. BibTeX (verify ⚠️ fields against DOI before submission — see §7)

```bibtex
@inproceedings{cui2022dvabatch,
  title={{DVABatch}: Diversity-aware Multi-Entry Multi-Exit Batching for Efficient Processing of {DNN} Services on {GPU}s},
  author={Cui, Weihao and Han, Zhenhua and Ouyang, Lingji and others},
  booktitle={USENIX Annual Technical Conference (ATC)}, year={2022}}

@inproceedings{gujarati2020clockwork,
  title={Serving {DNN}s like Clockwork: Performance Predictability from the Bottom Up},
  author={Gujarati, Arpan and Karimi, Reza and Alzayat, Safya and others},
  booktitle={USENIX OSDI}, year={2020}}

@article{nigade2024jellyfish,
  title={{Jellyfish}: Timely Inference Serving for Dynamic Edge Networks},
  author={Nigade, Vinod and others}, journal={Real-Time Systems}, year={2024}, note={verify venue/year}}

@article{zhang2023bcedge,
  title={{BCEdge}: SLO-Aware DNN Inference Services with Adaptive Batching on Edge Platforms},
  author={Zhang, Ziyang and others}, journal={arXiv:2305.01519}, year={2023}, note={verify final venue}}

@inproceedings{crankshaw2017clipper,
  title={{Clipper}: A Low-Latency Online Prediction Serving System},
  author={Crankshaw, Daniel and Wang, Xin and others}, booktitle={USENIX NSDI}, year={2017}}

@inproceedings{shen2019nexus,
  title={{Nexus}: A GPU Cluster Engine for Accelerating {DNN}-Based Video Analysis},
  author={Shen, Haichen and Chen, Lequn and others}, booktitle={ACM SOSP}, year={2019}}

@inproceedings{li2020reducto,
  title={{Reducto}: On-Camera Filtering for Resource-Efficient Real-Time Video Analytics},
  author={Li, Yuanqi and Padmanabhan, Arthi and others}, booktitle={ACM SIGCOMM}, year={2020}}

@inproceedings{jiang2018chameleon,
  title={{Chameleon}: Scalable Adaptation of Video Analytics},
  author={Jiang, Junchen and Ananthanarayanan, Ganesh and others}, booktitle={ACM SIGCOMM}, year={2018}}

@article{kang2017noscope,
  title={{NoScope}: Optimizing Neural Network Queries over Video at Scale},
  author={Kang, Daniel and Emmons, John and others}, journal={PVLDB}, year={2017}}

@inproceedings{arefeen2022framehopper,
  title={{FrameHopper}: Selective Processing of Video Frames in Detection-driven Real-Time Video Analytics},
  author={Arefeen, Md Adnan and others}, booktitle={IEEE DCOSS}, year={2022}, note={verify venue}}

@inproceedings{liu2024rtbev,
  title={{RT-BEV}: Enhancing Real-Time {BEV} Perception for Autonomous Vehicles},
  author={Liu, Liangkai and Lee, Jinkyu and Shin, Kang G.}, booktitle={IEEE RTSS}, year={2024}}

@article{neff2020revamp2t,
  title={{REVAMP2T}: Real-Time Edge Video Analytics for Multicamera Privacy-Aware Pedestrian Tracking},
  author={Neff, Christopher and Mendieta, Matias and others}, journal={IEEE Internet of Things Journal}, year={2020}}

@inproceedings{bateni2020neuos,
  title={{NeuOS}: A Latency-Predictable Multi-Dimensional Optimization Framework for DNN-driven Autonomous Systems},
  author={Bateni, Soroush and Liu, Cong}, booktitle={USENIX ATC}, year={2020}}

@article{yan2023polythrottle,
  title={{PolyThrottle}: Energy-efficient Neural Network Inference on Edge Devices},
  author={Yan, Minghao and Wang, Hongyi and Venkataraman, Shivaram}, journal={arXiv:2310.19991}, year={2023}, note={verify final venue}}

@inproceedings{kalra2022d3,
  title={{D3}: A Dynamic Deadline-Driven Approach for Building Autonomous Vehicles},
  author={Kalra, Ionel and Schafhalter, Peter and others}, booktitle={ACM EuroSys}, year={2022}, note={verify author list}}

@inproceedings{liu2022prophet,
  title={{Prophet}: Realizing a Predictable Real-Time Perception Pipeline for Autonomous Vehicles},
  author={Liu, Liangkai and others}, booktitle={IEEE RTSS}, year={2022}, note={verify venue/year}}

@inproceedings{xiang2019dart,
  title={{DART}: A Real-Time Deep Learning Inference Framework},
  author={Xiang, Yecheng and Kim, Hyoseung}, booktitle={IEEE RTSS}, year={2019}, note={verify exact title}}

@inproceedings{liu2022selfcueing,
  title={Self-Cueing Real-Time Attention Scheduling in Criticality-Aware Visual Machine Perception},
  author={Liu, Shengzhong and Yao, Shuochao and Abdelzaher, Tarek and others}, booktitle={IEEE RTAS}, year={2022}, note={verify author list}}

@inproceedings{bewley2016sort,
  title={Simple Online and Realtime Tracking},
  author={Bewley, Alex and Ge, Zongyuan and Ott, Lionel and Ramos, Fabio and Upcroft, Ben}, booktitle={IEEE ICIP}, year={2016}}

@inproceedings{wojke2017deepsort,
  title={Simple Online and Realtime Tracking with a Deep Association Metric},
  author={Wojke, Nicolai and Bewley, Alex and Paulus, Dietrich}, booktitle={IEEE ICIP}, year={2017}}
```

---

## 7. Verification status & things to double-check before you submit

**Fully adversarially verified (Tier-A — existence + core claims, 3-vote):**
DVABatch, Clockwork, Jellyfish, BCEdge, Reducto, Chameleon, NoScope, FrameHopper,
RT-BEV, REVAMP²T, SORT. These are safe to cite; only re-confirm the ⚠️ *venue/year*
fields below.

**Found + fetched but NOT through the 3-vote pass (verify before citing):**
Clipper, Nexus, NeuOS, PolyThrottle, D³, Prophet, DART, Self-Cueing, DeepSORT. All are
well-known real papers, but the workflow's verification budget stopped at 25 claims
(threads 1–3), so treat their metadata as "very likely correct, confirm the DOI."

**Specific flags:**
- **RT-BEV "FlexibleTimeSync"** — this term is a *paraphrase*, not RT-BEV's own wording
  (their paper says "flexible synchronization mechanism"). The **FES metric is genuine**
  and defined in RT-BEV. In your `experiments/README.md` you use "FlexibleTimeSync";
  when citing RT-BEV in the paper, use their actual terminology to avoid a reviewer
  who knows the paper flagging it. (Minor, but reviewers of an RTSS-adjacent topic will.)
- **RT-BEV is *not* a whole-camera-skipping antecedent** — it *synchronizes* cameras.
  State the distinction explicitly so you don't appear to conflate the mechanisms.
- **Jellyfish / BCEdge / PolyThrottle / Prophet / DART / D³ / Self-Cueing venues** are
  marked ⚠️ — confirm each DOI (IEEE Xplore / ACM DL / DBLP) for the exact
  venue-year-authors before the bibliography is final.
- **Not yet in the corpus (optional adds if you have room):** VideoStorm (NSDI'17),
  Glimpse (SenSys'15), a DeepStream/`nvstreammux` systems reference, and a
  BEVFusion citation if you contrast fused vs independent per-camera detection.

*(errata 2026-07-07: the workflow-output file that recorded the fetched papers' source
URLs lived in a since-cleared `/tmp` scratchpad and no longer exists — pull the DOIs
directly via DBLP / IEEE Xplore instead.)*
