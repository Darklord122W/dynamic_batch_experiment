# frame_timing_probe — run summary

- replay: clips/cam*.mp4 (H.264 → nvv4l2decoder, paced by identity sync=true)
- injected: skew-ms=[0.0, 1134.8, 1702.1, 567.2]; rate=[0.96063, 0.96099, 0.96087, 0.96128]; gap-every=275; ring=4; restamp=ON (jpegparse emulation)
- mux: sync-inputs=OFF; batched-push-timeout=33333 µs
- duration: requested 42.0 s, captured 42.8 s (steady-state window 2.0–42.3 s used for all stats)
- pipeline clock: GstSystemClock (monotonic); base_time=3922302924427 ns
- REALTIME−MONOTONIC offset drift over the run: 0.0 µs

## Per-camera capture behaviour (kernel stamps)

| cam | frames | eff. fps | Δt p50 (ms) | Δt p99 (ms) | kernel seq gaps | capture→mux p50 (ms) | p99 (ms) |
|---|---|---|---|---|---|---|---|
| 0 | 1251 | 31.1 | 32.0 | 32.0 | n/a (replay) | 114.8 | 119.9 |
| 1 | 1251 | 31.0 | 32.0 | 32.0 | n/a (replay) | 125.0 | 127.8 |
| 2 | 1252 | 31.0 | 32.0 | 32.0 | n/a (replay) | 0.1 | 0.3 |
| 3 | 1251 | 31.0 | 32.0 | 32.0 | n/a (replay) | 107.3 | 117.9 |

## Batching

- batches (steady state): 1252; full: 1252 (100.0 %); partial: 0
- batch cadence: median 32.0 ms

## Time differences among cameras (the RT-BEV Fig. 5 numbers)

| sample definition | n | p50 (ms) | p90 (ms) | p99 (ms) | max (ms) |
|---|---|---|---|---|---|
| nearest-frame sets (true capture) | 1251 | 20.8 | 28.5 | 40.1 | 51.7 |
| mux batches (true capture) | 1252 | 308.5 | 310.6 | 373.0 | 374.1 |
| mux batches (synthetic PTS) | 1252 | 1438.1 | 1438.1 | 1438.1 | 1438.1 |

(one frame period = 32.0 ms; RT-BEV reports 39–46 ms on hardware-synced nuScenes cameras)
