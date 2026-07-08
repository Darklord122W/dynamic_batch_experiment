# frame_timing_probe — run summary

- devices: ['/dev/video0', '/dev/video2', '/dev/video4', '/dev/video6']
- mode: 640x480@30 MJPG → nvjpegdec; sync-inputs=OFF; batched-push-timeout=33333 µs
- extra v4l2 controls: exposure_dynamic_framerate=0
- duration: 120.0 s (steady-state window 2.0–118.9 s used for all stats)
- pipeline clock: GstSystemClock (monotonic); base_time=3628708647170 ns
- REALTIME−MONOTONIC offset drift over the run: 0.0 µs

## Per-camera capture behaviour (kernel stamps)

| cam | frames | eff. fps | Δt p50 (ms) | Δt p99 (ms) | kernel seq gaps | capture→mux p50 (ms) | p99 (ms) |
|---|---|---|---|---|---|---|---|
| 0 | 3453 | 29.5 | 32.0 | 36.1 | 26 | 32.1 | 37.4 |
| 1 | 3454 | 29.5 | 32.0 | 36.0 | 25 | 108.6 | 176.9 |
| 2 | 3454 | 29.5 | 32.0 | 36.1 | 25 | 109.9 | 178.2 |
| 3 | 3452 | 29.5 | 32.0 | 36.1 | 27 | 43.2 | 139.5 |

## Batching

- batches (steady state): 3453; full: 3453 (100.0 %); partial: 0
- batch cadence: median 32.2 ms

## Time differences among cameras (the RT-BEV Fig. 5 numbers)

| sample definition | n | p50 (ms) | p90 (ms) | p99 (ms) | max (ms) |
|---|---|---|---|---|---|
| nearest-frame sets (true capture) | 3453 | 0.7 | 8.1 | 38.3 | 96.6 |
| mux batches (true capture) | 3453 | 200.1 | 204.2 | 276.1 | 340.1 |
| mux batches (synthetic PTS) | 3453 | 200.1 | 204.2 | 276.1 | 340.1 |

(one frame period = 32.0 ms; RT-BEV reports 39–46 ms on hardware-synced nuScenes cameras)
