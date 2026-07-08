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

# Live-measured parameters (from results/baseline_pinned_rerun, 2026-07-07,
# corrected pinning control exposure_dynamic_framerate=0):
#   startup stagger ms per camera; per-camera modal step 32.026 ms with
#   per-camera crystal differences (rate = modal step / 33.333); GAP=44 makes
#   the DELIVERED frame rate match the live mean (~29.8 fps) — required for
#   restamp-world sync fidelity (grid anchors persist only when the grid step
#   matches the mean delivered cadence). Historic 2026-07-06 values were
#   SKEW="568,0,1217,1137" RATE="0.9608,..." GAP=70.
SKEW="0,1134.8,1702.1,567.2"
RATE="0.96063,0.96099,0.96087,0.96128"
GAP=44

make -s

echo "== replay 1/2: skewed (simulates live baseline_pinned), ${DUR}s =="
./frame_timing_probe --replay-dir clips --num-cams 4 --duration "$DUR" \
    --skew-ms "$SKEW" --rate "$RATE" --gap-every $GAP --ring 4 \
    --out-dir "$RES/replay_skewed"

echo "== replay 2/2: ideal (no injected imperfections), ${DUR}s =="
./frame_timing_probe --replay-dir clips --num-cams 4 --duration "$DUR" \
    --out-dir "$RES/replay_ideal"

echo "== analysis =="
python3 analyze_timing.py "$RES/replay_skewed" --compare "$RES/baseline_pinned" || \
python3 analyze_timing.py "$RES/replay_skewed"   # live run may not exist
python3 analyze_timing.py "$RES/replay_ideal"

echo "done. figures in $RES/replay_*/figures/"
