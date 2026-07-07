#!/usr/bin/env bash
# run_replay.sh — the recorded-video (replay) variant of the timing experiment.
#
#   ./run_replay.sh [DURATION_S] [RESULTS_DIR]
#
# Replays clips/cam{0..3}.mp4 through the same mux + probes as the live
# experiment, with the live-measured imperfections INJECTED (see
# REPLAY_SKEW.md). Needs no cameras. Duration is bounded by the shortest
# clip (cam0: 45.7 s -> default 42 s).
#
#   replay_skewed    the closest simulation of the live sync-off
#                    baseline_pinned run: startup stagger + true 32.026 ms
#                    cadence + kernel-style capture gaps + bounded ring
#   replay_ideal     ablation: same clips, NO injected imperfections —
#                    what a naively-replayed "perfect" 4-camera rig looks like
set -euo pipefail
cd "$(dirname "$0")"

DUR="${1:-42}"
RES="${2:-results}"

# Live-measured parameters (from results/baseline_pinned, 2026-07-06):
#   startup stagger ms per camera, true period 32.026 ms (rate 32.026/33.333),
#   one 2-frame capture gap per ~70 frames, v4l2 ring ~4 buffers.
SKEW="568,0,1217,1137"
RATE="0.9608,0.9608,0.9608,0.9608"

make -s

echo "== replay 1/2: skewed (simulates live baseline_pinned), ${DUR}s =="
./frame_timing_probe --replay-dir clips --num-cams 4 --duration "$DUR" \
    --skew-ms "$SKEW" --rate "$RATE" --gap-every 70 --ring 4 \
    --out-dir "$RES/replay_skewed"

echo "== replay 2/2: ideal (no injected imperfections), ${DUR}s =="
./frame_timing_probe --replay-dir clips --num-cams 4 --duration "$DUR" \
    --out-dir "$RES/replay_ideal"

echo "== analysis =="
python3 analyze_timing.py "$RES/replay_skewed" --compare "$RES/baseline_pinned" || \
python3 analyze_timing.py "$RES/replay_skewed"   # live run may not exist
python3 analyze_timing.py "$RES/replay_ideal"

echo "done. figures in $RES/replay_*/figures/"
