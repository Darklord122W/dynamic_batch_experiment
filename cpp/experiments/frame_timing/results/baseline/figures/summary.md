# frame_timing_probe — run summary

- devices: ['/dev/video0', '/dev/video2', '/dev/video4', '/dev/video6']
- mode: 640x480@30 MJPG → nvjpegdec; sync-inputs=OFF; batched-push-timeout=33333 µs
- extra v4l2 controls: (none)
- duration: 120.0 s (steady-state window 2.0–118.9 s used for all stats)
- pipeline clock: GstSystemClock (monotonic); base_time=352877422148886 ns
- REALTIME−MONOTONIC offset drift over the run: 0.0 µs

## Per-camera capture behaviour (kernel stamps)

| cam | frames | eff. fps | Δt p50 (ms) | Δt p99 (ms) | kernel seq gaps | capture→mux p50 (ms) | p99 (ms) |
|---|---|---|---|---|---|---|---|
| 0 | 3364 | 28.8 | 32.0 | 100.0 | 80 | 103.9 | 173.3 |
| 1 | 3358 | 28.7 | 32.0 | 104.0 | 71 | 30.7 | 74.9 |
| 2 | 3367 | 28.8 | 32.0 | 108.0 | 67 | 41.6 | 176.1 |
| 3 | 3364 | 28.8 | 32.0 | 100.0 | 71 | 109.6 | 181.2 |

## Batching

- batches (steady state): 3363; full: 3363 (100.0 %); partial: 0
- batch cadence: median 32.5 ms

## Time differences among cameras (the RT-BEV Fig. 5 numbers)

| sample definition | n | p50 (ms) | p90 (ms) | p99 (ms) | max (ms) |
|---|---|---|---|---|---|
| nearest-frame sets (true capture) | 3364 | 8.2 | 29.6 | 44.5 | 76.5 |
| mux batches (true capture) | 3363 | 205.9 | 271.5 | 277.9 | 297.8 |
| mux batches (synthetic PTS) | 3363 | 1468.5 | 1468.5 | 1468.5 | 1468.5 |

(one frame period = 32.0 ms; RT-BEV reports 39–46 ms on hardware-synced nuScenes cameras)
