# frame_timing_probe — run summary

- devices: ['/dev/video0', '/dev/video2', '/dev/video4', '/dev/video6']
- mode: 640x480@30 MJPG → nvjpegdec; sync-inputs=ON; batched-push-timeout=33333 µs; max-latency=33 ms
- extra v4l2 controls: exposure_auto_priority=0
- duration: 120.0 s (steady-state window 2.0–118.9 s used for all stats)
- pipeline clock: GstSystemClock (monotonic); base_time=353323028853726 ns
- REALTIME−MONOTONIC offset drift over the run: 0.0 µs

## Per-camera capture behaviour (kernel stamps)

| cam | frames | eff. fps | Δt p50 (ms) | Δt p99 (ms) | kernel seq gaps | capture→mux p50 (ms) | p99 (ms) |
|---|---|---|---|---|---|---|---|
| 0 | 3496 | 29.9 | 32.0 | 36.1 | 2 | 35.6 | 79.1 |
| 1 | 3465 | 29.6 | 32.0 | 36.1 | 17 | 31.7 | 38.9 |
| 2 | 3398 | 29.1 | 32.0 | 108.0 | 49 | 28.2 | 127.7 |
| 3 | 3498 | 29.9 | 32.0 | 36.1 | 1 | 33.4 | 76.3 |

## Batching

- batches (steady state): 388; full: 61 (15.7 %); partial: 327
- batch cadence: median 201.0 ms

## Time differences among cameras (the RT-BEV Fig. 5 numbers)

| sample definition | n | p50 (ms) | p90 (ms) | p99 (ms) | max (ms) |
|---|---|---|---|---|---|
| nearest-frame sets (true capture) | 3496 | 9.9 | 14.3 | 45.9 | 81.9 |
| mux batches (true capture) | 348 | 4.5 | 185.6 | 189.7 | 294.6 |
| mux batches (synthetic PTS) | 348 | 1.8 | 239.6 | 239.6 | 239.6 |

(one frame period = 32.0 ms; RT-BEV reports 39–46 ms on hardware-synced nuScenes cameras)
