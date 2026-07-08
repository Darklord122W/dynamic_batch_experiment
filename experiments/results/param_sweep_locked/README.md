# Parameter sweep, locked clocks — where does C++ beat Python?

**2026-07-07, campaign 3.** Follow-up to
[`../baseline_cpp_vs_py_locked/`](../baseline_cpp_vs_py_locked/README.md), which
left C++ (new mux) at e2e 158 ms vs Python (legacy mux) 133 ms, with the gap
identified as ~115 ms of new-mux hold. This sweep varies the batching knobs to
find the hold's source. Same protocol otherwise: replay clips, 25 s runs,
warmup 4 s, `jetson_clocks` on (MODE_30W), single run per config + 3 repeats
for the two winners. Exact commands: [`../BASELINE_COMMANDS.md`](../BASELINE_COMMANDS.md)
pattern + the flags below; INI variants `mux_minfps60.txt` / `mux_minfps120.txt`
kept in this directory.

## Result (e2e ms, one run per config)

| config | p50 | p99 | mean | wait | compute |
|---|---|---|---|---|---|
| Python baseline (timeout 33 ms) | 133.0 | 133.9 | 133.2 | 33.5 | 99.7 |
| Python `--timeout-us 5000` (also 10 k/20 k) | 133.0 | 200.5 | 134.7 | 34.0 | 100.7 |
| C++ shipped INI (`overall-min-fps=30`) = old baseline | 162.7 | 200.5 | 157.5 | 113.1 | 44.4 |
| C++ `--timeout-us 20000` (INI 30) | 94.9 | 103.9 | 96.4 | 67.4 | 29.1 |
| C++ `--timeout-us 10000` (INI 30) | 95.0 | 139.9 | 97.1 | 64.6 | 32.5 |
| C++ `--timeout-us 5000` (INI 30) | 131.0 | 175.3 | 133.6 | 92.3 | 41.3 |
| C++ INI `overall-min-fps=60` | 28.3 | 161.7 | 35.7 | 2.4 | 33.3 |
| C++ `--mux-config none` (built-in defaults) | 36.7 | 200.5 | 50.5 | 4.8 | 45.6 |
| C++ `--timeout-us 5000` + INI `min-fps=120` | 28.3 | 130.5 | 37.6 | 1.5 | 36.1 |
| **C++ INI `overall-min-fps=120`** | **28.3** | **109.7** | **30.5** | **2.4** | **28.1** |

(Rows for `cpp_t5000.csv` and `cpp_t5_mf120.csv` added 2026-07-07 — both were run
in this sweep but omitted from the first write-up; numbers from
`analyze.py --warmup 4` on the CSVs in this directory.)

## Findings

1. **The 115 ms hold was our own config.** `config/mux_config.txt` ships
   `overall-min-fps=30` (chosen for the sync-on experiments): the new mux's
   batching algorithm paces pushes to that cadence and holds frames ~3.5 frame
   periods. Raise the floor (`min-fps=60/120`) or drop the INI entirely and the
   hold collapses to 2–5 ms.
2. **C++ now beats Python 4.7× on median latency: 28 ms vs 133 ms.** Python
   cannot follow: its legacy mux pushes only when the batch *fills*, i.e. when
   the 4th camera's frame arrives ~33 ms after the 1st (lowering
   `--timeout-us` to 5 ms leaves p50 unchanged at 133 ms; p99 tail noise rose
   to 200.5 ms), and its booked compute
   is ~100 ms. The C++ floor = arrival stagger amortized + 28 ms compute.
3. **All frames still processed** — `mux-config none` logged
   `processed 3024 of 3024 arrived frames`; min-fps=120 pushes partial batches
   (reported fullness 3.36–3.69) but throughput stays ~121 frames/s and
   detections match (~2500/25 s). Nothing is dropped, batches are just smaller
   and more frequent.
4. **Caveat — episodic excursions.** Winner repeats hold p50 = 28.26/28.28/28.30 ms,
   but mean/p99 vary (30.5/40.0/61.8 · p99 110/160/244): raw traces show
   second-long queue-buildup bursts to 100–300 ms that come and go. NOT DVFS
   (clocks pinned) and NOT thermal (48 °C). Open question — likely metastable
   queueing in the free-paced new mux; a `leaky`/queue-tuning or
   `overall-max-fps` experiment would chase it down.
5. `--timeout-us` under INI 30 lands mid-way (~95 ms) — the property and the
   INI floor interact; the INI is the dominant knob.
   (errata 2026-07-07: `batched-push-timeout` and the INI's `overall-min-fps`
   are the *same* internal knob (last writer wins, `nvstreammux_batch.cpp:333`),
   and the pre-2026-07-07 C++ binary set the property *before* loading the INI —
   so the shipped INI (min-fps=30) silently overrode every C++ `--timeout-us` in
   this sweep; the load order was fixed 2026-07-07 (INI first, CLI property
   wins), so future sweeps genuinely vary the knob.)

## Detection performance

No ground truth exists for these clips (so no mAP) — what we can measure is
whether the fast configs **detect as much per processed frame** as the
baselines, with the same tracking stability. They do, to within ±0.5%:

| run | frames | dets | dets/frame | cam0 | cam1 | cam2 | cam3 | new IDs |
|---|---|---|---|---|---|---|---|---|
| Python baseline | 2580 | 2454 | 0.951 | 0.78 | 1.99 | 0.73 | 0.31 | 21 |
| Python timeout 5 ms | 2575 | 2447 | 0.950 | 0.78 | 1.99 | 0.73 | 0.31 | 21 |
| C++ INI 30 (old baseline) | 2669 | 2530 | 0.948 | 0.76 | 2.05 | 0.70 | 0.27 | 22 |
| C++ timeout 20 ms | 2602 | 2491 | 0.957 | 0.76 | 2.10 | 0.72 | 0.25 | 21 |
| C++ mux defaults (r1/r2) | 2612/2652 | 2496/2520 | 0.956/0.950 | 0.76 | 2.06–2.09 | 0.71–0.72 | 0.26–0.27 | 21 |
| **C++ min-fps 120 (r1/r2/r3)** | 2609–2668 | 2492–2531 | 0.949–0.956 | 0.76 | 2.05–2.09 | 0.71–0.72 | 0.25–0.27 | 21 |

(dets/frame = total detections ÷ frames batched, post-warmup; per-camera columns
= that camera's detections ÷ (frames/4); new IDs = tracker churn over ~21.5 s.)

Read: **overall dets/frame spans 0.948–0.957 across all ten runs** — pushing
partial batches (min-fps 120, fullness ~3.4) or pushing early (mux defaults)
loses no detections, because every arriving frame is still processed, just in
smaller batches. Per-camera rates match Python within a few % (cam1, the busiest
view, reads ~2.05–2.10 on C++ vs 1.99 on Python — same engine, slightly
different frame subsets), and track churn is 21–22 new IDs everywhere, so ID
stability is unaffected. The same per-camera agreement at full precision is
plotted in campaign 1's fig6 (`../baseline_cpp_vs_py/plots/fig6_dets_per_cam.png`).

**Limit of this claim:** counts and churn are consistency checks, not accuracy —
confidences aren't logged in the metrics CSV and there are no labels. If you need
a real mAP comparison, replay a labelled dataset and pass `--accuracy` to
analyze.py (see `experiments/README.md`).

## Verdict

The "C++ slower than Python" result of campaigns 1–2 was a configuration
artifact, not a property of the code: with the mux INI floor raised (or removed),
the C++ new-mux pipeline is **4.7× faster at the median** (28 vs 133 ms) and
strictly better at p50 in every repeat (28 vs 133 ms); p99 beats Python only in
the best repeat (110 vs 134 ms) — other repeats had p99 160/244 ms (the episodic
excursions of Finding 4). Throughput and detections are equal throughout. Python's 133 ms is a hard floor built into
the legacy mux's fill-gated batching.

**Recommended default for the C++ app on replay/live baseline:**
`overall-min-fps=120` INI (or `--mux-config none` if partial batches are
unwanted — costs ~8 ms at p50). The shipped `overall-min-fps=30` should remain
only for the sync-on experiments it was written for.
