# frame_timing_probe — run summary

- devices: ['/dev/video0', '/dev/video2', '/dev/video4', '/dev/video6']
- mode: 640x480@30 MJPG → nvjpegdec; sync-inputs=OFF; batched-push-timeout=33333 µs
- extra v4l2 controls: exposure_auto_priority=0
- duration: 120.0 s (steady-state window 2.0–118.4 s used for all stats)
- pipeline clock: GstSystemClock (monotonic); base_time=353190884637346 ns
- REALTIME−MONOTONIC offset drift over the run: 0.0 µs

## Per-camera capture behaviour (kernel stamps)

| cam | frames | eff. fps | Δt p50 (ms) | Δt p99 (ms) | kernel seq gaps | capture→mux p50 (ms) | p99 (ms) |
|---|---|---|---|---|---|---|---|
| 0 | 3380 | 29.1 | 32.0 | 100.0 | 52 | 110.4 | 181.6 |
| 1 | 3375 | 29.0 | 32.0 | 104.0 | 55 | 39.5 | 112.4 |
| 2 | 3379 | 29.1 | 32.0 | 108.0 | 51 | 27.2 | 35.3 |
| 3 | 3383 | 29.1 | 32.0 | 100.0 | 52 | 108.6 | 179.4 |

## Batching

- batches (steady state): 3379; full: 3379 (100.0 %); partial: 0
- batch cadence: median 32.5 ms

## Time differences among cameras (the RT-BEV Fig. 5 numbers)

| sample definition | n | p50 (ms) | p90 (ms) | p99 (ms) | max (ms) |
|---|---|---|---|---|---|
| nearest-frame sets (true capture) | 3380 | 8.9 | 13.0 | 45.0 | 68.9 |
| mux batches (true capture) | 3379 | 208.9 | 276.9 | 281.0 | 285.0 |
| mux batches (synthetic PTS) | 3379 | 1050.1 | 1050.1 | 1050.1 | 1050.1 |

(one frame period = 32.0 ms; RT-BEV reports 39–46 ms on hardware-synced nuScenes cameras)
