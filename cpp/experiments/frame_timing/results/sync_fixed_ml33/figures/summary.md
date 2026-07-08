# frame_timing_probe — run summary

- devices: ['/dev/video0', '/dev/video2', '/dev/video4', '/dev/video6']
- mode: 640x480@30 MJPG → nvjpegdec; sync-inputs=ON; batched-push-timeout=33333 µs; max-latency=33 ms; jpegparse pts-fix=ON
- extra v4l2 controls: exposure_dynamic_framerate=0
- duration: requested 120.0 s, captured 119.4 s (steady-state window 2.0–118.9 s used for all stats)
- pipeline clock: GstSystemClock (monotonic); base_time=4449410603063 ns
- REALTIME−MONOTONIC offset drift over the run: 0.0 µs

## Per-camera capture behaviour (kernel stamps)

| cam | frames | eff. fps | Δt p50 (ms) | Δt p99 (ms) | kernel seq gaps | capture→mux p50 (ms) | p99 (ms) |
|---|---|---|---|---|---|---|---|
| 0 | 2397 | 20.5 | 35.9 | 104.0 | 549 | 123.3 | 195.8 |
| 1 | 2396 | 20.5 | 35.9 | 105.7 | 549 | 124.6 | 213.8 |
| 2 | 2395 | 20.5 | 34.0 | 104.0 | 550 | 124.7 | 191.2 |
| 3 | 2395 | 20.5 | 35.9 | 104.0 | 551 | 127.0 | 198.5 |

## Batching

- batches (steady state): 2395; full: 2395 (100.0 %); partial: 0
- batch cadence: median 0.3 ms

## Time differences among cameras (the RT-BEV Fig. 5 numbers)

| sample definition | n | p50 (ms) | p90 (ms) | p99 (ms) | max (ms) |
|---|---|---|---|---|---|
| nearest-frame sets (true capture) | 2397 | 2.1 | 6.1 | 36.7 | 100.8 |
| mux batches (true capture) | 2395 | 2.1 | 32.7 | 70.1 | 166.1 |
| mux batches (synthetic PTS) | 2395 | 2.1 | 32.7 | 70.1 | 166.1 |

(one frame period = 35.9 ms; RT-BEV reports 39–46 ms on hardware-synced nuScenes cameras)
