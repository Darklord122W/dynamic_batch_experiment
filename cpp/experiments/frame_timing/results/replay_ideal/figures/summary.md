# frame_timing_probe — run summary

- devices: ['/dev/video0', '/dev/video2', '/dev/video4', '/dev/video6']
- mode: 640x480@30 MJPG → nvjpegdec; sync-inputs=OFF; batched-push-timeout=33333 µs
- extra v4l2 controls: (none)
- duration: 42.0 s (steady-state window 2.0–42.3 s used for all stats)
- pipeline clock: GstSystemClock (monotonic); base_time=408963593317145 ns
- REALTIME−MONOTONIC offset drift over the run: -0.0 µs

## Per-camera capture behaviour (kernel stamps)

| cam | frames | eff. fps | Δt p50 (ms) | Δt p99 (ms) | kernel seq gaps | capture→mux p50 (ms) | p99 (ms) |
|---|---|---|---|---|---|---|---|
| 0 | 1209 | 30.0 | 33.3 | 33.3 | 0 | 0.2 | 0.4 |
| 1 | 1209 | 30.0 | 33.3 | 33.3 | 0 | 0.2 | 0.4 |
| 2 | 1209 | 30.0 | 33.3 | 33.3 | 0 | 0.2 | 0.3 |
| 3 | 1209 | 30.0 | 33.3 | 33.3 | 0 | 0.2 | 0.4 |

## Batching

- batches (steady state): 1209; full: 1209 (100.0 %); partial: 0
- batch cadence: median 33.3 ms

## Time differences among cameras (the RT-BEV Fig. 5 numbers)

| sample definition | n | p50 (ms) | p90 (ms) | p99 (ms) | max (ms) |
|---|---|---|---|---|---|
| nearest-frame sets (true capture) | 1209 | 0.0 | 0.0 | 0.0 | 0.0 |
| mux batches (true capture) | 1209 | 0.0 | 0.0 | 0.0 | 0.0 |
| mux batches (synthetic PTS) | 1209 | 0.0 | 0.0 | 0.0 | 0.0 |

(one frame period = 33.3 ms; RT-BEV reports 39–46 ms on hardware-synced nuScenes cameras)
