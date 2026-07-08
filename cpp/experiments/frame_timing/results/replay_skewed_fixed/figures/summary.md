# frame_timing_probe — run summary

- replay: clips/cam*.mp4 (H.264 → nvv4l2decoder, paced by identity sync=true)
- injected: skew-ms=[0.0, 1134.8, 1702.1, 567.2]; rate=[0.96063, 0.96099, 0.96087, 0.96128]; gap-every=275; ring=4; restamp=OFF (true timestamps)
- mux: sync-inputs=OFF; batched-push-timeout=33333 µs
- duration: requested 42.0 s, captured 42.8 s (steady-state window 2.0–42.3 s used for all stats)
- pipeline clock: GstSystemClock (monotonic); base_time=3965575606009 ns
- REALTIME−MONOTONIC offset drift over the run: 0.0 µs

## Per-camera capture behaviour (kernel stamps)

| cam | frames | eff. fps | Δt p50 (ms) | Δt p99 (ms) | kernel seq gaps | capture→mux p50 (ms) | p99 (ms) |
|---|---|---|---|---|---|---|---|
| 0 | 1251 | 31.1 | 32.0 | 32.0 | n/a (replay) | 114.7 | 119.9 |
| 1 | 1251 | 31.0 | 32.0 | 32.0 | n/a (replay) | 93.0 | 95.6 |
| 2 | 1252 | 31.0 | 32.0 | 32.0 | n/a (replay) | 0.1 | 0.2 |
| 3 | 1251 | 31.0 | 32.0 | 32.0 | n/a (replay) | 107.4 | 117.9 |

## Batching

- batches (steady state): 1252; full: 1252 (100.0 %); partial: 0
- batch cadence: median 32.0 ms

## Time differences among cameras (the RT-BEV Fig. 5 numbers)

| sample definition | n | p50 (ms) | p90 (ms) | p99 (ms) | max (ms) |
|---|---|---|---|---|---|
| nearest-frame sets (true capture) | 1251 | 20.8 | 28.5 | 40.1 | 51.7 |
| mux batches (true capture) | 1252 | 299.6 | 302.6 | 341.0 | 342.1 |
| mux batches (synthetic PTS) | 1252 | 299.6 | 302.6 | 341.0 | 342.1 |

(one frame period = 32.0 ms; RT-BEV reports 39–46 ms on hardware-synced nuScenes cameras)
