# Paper Position & Takeaways — Dynamic Batching

*Written 2026-07-07 (Peng), for Zhiyuan. Based on: `Experiments/Why_the_Batch_Never_Filled.pdf`
(2026-07-06), the timeout-sweep / sync-sweep / frame-timing data in the
[dynamic_batch_experiment](https://github.com/Darklord122W/dynamic_batch_experiment) repo,
and the 2026-07-02 / 07-04 / 07-06 meetings.*

---

## 1. The position (what our paper claims)

**A synchronization-free, importance-aware adaptive batching method for commodity
multi-camera edge inference — designed to preserve critical visual frames under
low-timeout, real-time conditions, without industrial cameras, hardware triggers,
or precise sensor timestamps.**

One-sentence version:

> This work turns unused batch capacity under low-timeout dynamic batching into a
> bounded semantic recovery opportunity, preserving recent critical frames that
> conventional frame-agnostic batching would otherwise drop or ignore.

The core contribution is **not** "making batches fuller." Batch fullness is a solved
non-problem (sync-off already fills 78–99% of batches at sensible timeouts). The
contribution is **making low-timeout batches more semantically useful**: when waiting
time is limited, *which* frames should be processed?

Proposed mechanism: a **bounded queue-based adaptive batching scheduler**. Instead of
waiting longer to fill a batch, the scheduler uses unused batch capacity under low
timeout to recover recent, high-importance frames from the queue — selected by
semantic importance, freshness, source fairness, and drop history.

## 2. The argument, step by step

1. Commodity multi-camera systems (e.g., 4× C920 on one USB bus) lack hardware
   synchronization and reliable sensor timestamps.
2. Fixed batching needs a moderate timeout to form full batches, but longer timeout
   costs latency.
3. Dynamic batching runs at low timeout and holds throughput, but produces many
   partial, frame-agnostic batches.
4. In latency-constrained settings the system cannot simply wait longer.
5. So the key question: **when waiting time is limited, which frames should be
   processed?**
6. We propose an importance-aware, freshness-bounded adaptive batching scheduler that
   preserves critical frames under low timeout.
7. Experiments with four C920s show improved important-frame preservation and event
   recall vs. fixed-timeout, FIFO, and standard dynamic batching baselines.

## 3. What the current results already prove

These are measured facts we can cite in the paper's motivation — steps 1–4 of the
argument are done:

- **Timestamps on commodity rigs are fiction (step 1 — proven, strongly).**
  jpegparse re-stamps every camera onto its own ideal 33.33 ms grid anchored at that
  camera's startup instant; USB enumeration staggers startups 0.6–1.7 s, so the grids
  disagree by a **constant 1.05–1.47 s**. Time-alignment (`sync-inputs=1`) on this
  fiction silently discarded **93.6% of all frames**. Meanwhile the *true* capture
  instants interleave within **p50 8.9 ms** — better on median than RT-BEV's
  hardware-synced nuScenes reference (39–46 ms). Sync on commodity rigs isn't just
  unavailable; it's measurably harmful. "Synchronization-free" is the measured verdict,
  not a design preference.
- **Fixed batching: the cost of waiting is latency (step 2 — measured).** The control
  sweep reaches ~4 frames/batch only at one frame period (33.3 ms); larger timeouts buy
  latency, not fill.
- **Dynamic batching: low-timeout partial batches = unused capacity (step 3 —
  measured).** At a 33.3 ms push: dynamic engine 78.3% full vs. static 93.6% (99.1% at
  100 ms), with much lower end-to-end latency at small timeouts. The dynamic TensorRT
  engine (batch 1–4) already accepts variable fill, so slotting recovered frames into
  spare capacity is mechanically straightforward.
- **FIFO batching is unfair in practice (bonus motivation for the scheduler).** The
  standing-queue skew: a permanent per-camera staleness penalty of **p50 241/172/32/239 ms,
  ordered by startup order** — the mux serves early-started cameras stale, forever,
  invisibly to any FPS metric. Our freshness + source-fairness criteria directly correct
  a defect that measurably exists.

**The main insight connecting the results to the method:** fixed batching shows the
cost of waiting; dynamic batching shows the opportunity of low-timeout partial batches.
Our method uses that opportunity to recover important information without increasing
latency.

## 4. What we still must show (the evidence gaps)

Steps 5–7 of the argument are currently *hypotheses*. The experiment plan
(README "Next steps") exists to close exactly these gaps:

1. **Semantic loss is real.** Nothing measured so far shows that dropped/delayed frames
   contain critical events — all existing metrics (fill, latency, throughput) are
   frame-agnostic. Needed: the dynamic-vs-fixed detection comparison at 5/10/20/40 ms
   against a long-timeout completeness baseline, then event-labeled replay data showing
   "conventional batching missed event X."
2. **Where frames actually die.** A partial batch under sync-off does not by itself
   drop a frame (the frame often lands in the next batch). Loss enters through specific
   mechanisms — queue/backpressure drops, `max-same-source-frames`, the camera-skip
   valves (−59…−66% compute at −50% throughput). The paper must pinpoint the drop
   mechanism the scheduler rescues frames from.
3. **A regime where waiting is genuinely unaffordable.** With YOLO11n and 4 cameras, a
   33.3 ms timeout already processes nearly everything — a reviewer will ask "why not
   just wait one frame period?" Hence the **heavier-model experiment** (YOLO11m/l, or
   more cameras / sub-frame deadline): make the low-timeout constraint real, then show
   the trade-off.
4. **"No added latency" must be measured.** Recovered frames enlarge batches and add
   inference time. Target from the 07-06 meeting — recover ~30% of lost detections
   within a ~20 ms window at no added latency — is a goal, not yet a result.

## 5. Design constraints imposed by the measurements

- **Freshness must come from kernel capture stamps (upstream of jpegparse) or local
  arrival time — never PTS.** Downstream PTS is synthetic and per-camera offset by up
  to ~1.5 s; a freshness score built on it inherits the fiction. This constraint
  *strengthens* the framing: the method needs only local arrival clocks, no sensor
  timestamps.
- **Run with `sync-inputs=0`.** The scheduler replaces alignment; it does not sit on
  top of it.
- **The offset-injection replay experiments must inject offsets measured from kernel
  stamps**, or they simulate the fiction instead of reality.

## 6. Evaluation sketch

- **Baselines:** fixed-timeout batching, FIFO/standard dynamic batching, (optionally)
  camera-skip variants.
- **Conditions:** timeouts 5/10/20/40 ms × {YOLO11n, heavier model}; replayed clips
  with measured inter-camera offsets injected (reproducible; controls variance).
- **Metrics:** important-frame preservation / event recall (primary), detection
  count/accuracy vs. long-timeout completeness baseline, end-to-end latency, GPU
  utilization, per-camera staleness & fairness (staleness spread across cameras).

## 7. Candidate titles

- *Low-Timeout Semantic Adaptive Batching for Commodity Multi-Camera Edge Inference*
- *Sync-Free Event-Preserving Adaptive Batching for Commodity Multi-Camera Edge
  Inference* (stronger)
