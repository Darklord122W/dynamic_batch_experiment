# frame_timing_probe — run summary

- devices: ['/dev/video0', '/dev/video2', '/dev/video4', '/dev/video6']
- mode: 640x480@30 MJPG → nvjpegdec; sync-inputs=OFF; batched-push-timeout=33333 µs
- extra v4l2 controls: (none)
- duration: 42.0 s (steady-state window 2.0–42.1 s used for all stats)
- pipeline clock: GstSystemClock (monotonic); base_time=408920316104156 ns
- REALTIME−MONOTONIC offset drift over the run: 0.0 µs

## Per-camera capture behaviour (kernel stamps)

| cam | frames | eff. fps | Δt p50 (ms) | Δt p99 (ms) | kernel seq gaps | capture→mux p50 (ms) | p99 (ms) |
|---|---|---|---|---|---|---|---|
| 0 | 1216 | 30.4 | 32.0 | 96.1 | 0 | 49.3 | 113.5 |
| 1 | 1216 | 30.4 | 32.0 | 96.1 | 0 | 104.8 | 105.2 |
| 2 | 1218 | 30.4 | 32.0 | 96.1 | 0 | 0.1 | 0.3 |
| 3 | 1216 | 30.4 | 32.0 | 96.1 | 0 | 0.1 | 0.3 |

## Batching

- batches (steady state): 1219; full: 1219 (100.0 %); partial: 0
- batch cadence: median 32.0 ms

## Time differences among cameras (the RT-BEV Fig. 5 numbers)

| sample definition | n | p50 (ms) | p90 (ms) | p99 (ms) | max (ms) |
|---|---|---|---|---|---|
| nearest-frame sets (true capture) | 1216 | 16.0 | 16.0 | 48.0 | 48.0 |
| mux batches (true capture) | 1219 | 288.2 | 296.7 | 296.7 | 360.8 |
| mux batches (synthetic PTS) | 1219 | 1117.0 | 1117.0 | 1117.0 | 1117.0 |

(one frame period = 32.0 ms; RT-BEV reports 39–46 ms on hardware-synced nuScenes cameras)
