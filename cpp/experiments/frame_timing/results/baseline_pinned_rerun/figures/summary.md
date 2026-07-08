# frame_timing_probe — run summary

- devices: ['/dev/video0', '/dev/video2', '/dev/video4', '/dev/video6']
- mode: 640x480@30 MJPG → nvjpegdec; sync-inputs=OFF; batched-push-timeout=33333 µs
- extra v4l2 controls: exposure_dynamic_framerate=0
- duration: 120.0 s (steady-state window 2.0–118.9 s used for all stats)
- pipeline clock: GstSystemClock (monotonic); base_time=3413111846219 ns
- REALTIME−MONOTONIC offset drift over the run: 0.0 µs

## Per-camera capture behaviour (kernel stamps)

| cam | frames | eff. fps | Δt p50 (ms) | Δt p99 (ms) | kernel seq gaps | capture→mux p50 (ms) | p99 (ms) |
|---|---|---|---|---|---|---|---|
| 0 | 3483 | 29.8 | 32.0 | 36.0 | 9 | 101.5 | 111.9 |
| 1 | 3483 | 29.8 | 32.0 | 36.0 | 9 | 35.6 | 111.8 |
| 2 | 3482 | 29.8 | 32.0 | 36.0 | 9 | 32.2 | 36.5 |
| 3 | 3483 | 29.8 | 32.0 | 36.0 | 10 | 33.5 | 111.2 |

## Batching

- batches (steady state): 3482; full: 3482 (100.0 %); partial: 0
- batch cadence: median 32.2 ms

## Time differences among cameras (the RT-BEV Fig. 5 numbers)

| sample definition | n | p50 (ms) | p90 (ms) | p99 (ms) | max (ms) |
|---|---|---|---|---|---|
| nearest-frame sets (true capture) | 3483 | 1.6 | 7.4 | 31.3 | 48.8 |
| mux batches (true capture) | 3482 | 198.4 | 203.4 | 278.5 | 282.4 |
| mux batches (synthetic PTS) | 3482 | 1468.8 | 1468.8 | 1468.8 | 1468.8 |

(one frame period = 32.0 ms; RT-BEV reports 39–46 ms on hardware-synced nuScenes cameras)
