# frame_timing_probe — run summary

- devices: ['/dev/video0', '/dev/video2', '/dev/video4', '/dev/video6']
- mode: 640x480@30 MJPG → nvjpegdec; sync-inputs=ON; batched-push-timeout=33333 µs; max-latency=33 ms; jpegparse pts-fix=off
- extra v4l2 controls: exposure_dynamic_framerate=0
- duration: requested 120.0 s, captured 119.4 s (steady-state window 2.0–118.9 s used for all stats)
- pipeline clock: GstSystemClock (monotonic); base_time=4586328844236 ns
- REALTIME−MONOTONIC offset drift over the run: 0.0 µs

## Per-camera capture behaviour (kernel stamps)

| cam | frames | eff. fps | Δt p50 (ms) | Δt p99 (ms) | kernel seq gaps | capture→mux p50 (ms) | p99 (ms) |
|---|---|---|---|---|---|---|---|
| 0 | 3491 | 29.9 | 32.0 | 36.1 | 6 | 36.1 | 84.9 |
| 1 | 3498 | 29.9 | 32.0 | 36.0 | 0 | 33.6 | 38.7 |
| 2 | 3498 | 29.9 | 32.0 | 36.0 | 0 | 35.8 | 80.8 |
| 3 | 3495 | 29.9 | 32.0 | 36.0 | 3 | 37.7 | 82.2 |

## Batching

- batches (steady state): 639; full: 258 (40.4 %); partial: 381
- batch cadence: median 200.3 ms

## Time differences among cameras (the RT-BEV Fig. 5 numbers)

| sample definition | n | p50 (ms) | p90 (ms) | p99 (ms) | max (ms) |
|---|---|---|---|---|---|
| nearest-frame sets (true capture) | 3491 | 1.0 | 4.4 | 5.0 | 32.6 |
| mux batches (true capture) | 612 | 4.4 | 169.0 | 301.0 | 369.0 |
| mux batches (synthetic PTS) | 612 | 2.3 | 236.0 | 236.0 | 236.0 |

(one frame period = 32.0 ms; RT-BEV reports 39–46 ms on hardware-synced nuScenes cameras)
