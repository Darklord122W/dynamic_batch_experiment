#!/usr/bin/env bash
# run_experiment.sh — build, capture, analyze: the full frame-timing experiment.
#
#   ./run_experiment.sh [DURATION_S] [RESULTS_DIR]
#
# Defaults: 120 s per run, results into ./results/. Produces three runs:
#
#   baseline        the pipeline exactly as the production app runs it
#                   (auto-exposure free to change the frame rate; sync-inputs=0)
#   baseline_pinned exposure_auto_priority=0 -> cameras hold 30 fps
#                   regardless of scene light; sync-inputs=0
#   sync_pinned     same capture settings + nvstreammux sync-inputs=1,
#                   max-latency = 1 frame (33.33 ms)
#
# then runs analyze_timing.py on each (plus two comparison figures:
# baseline vs pinned = the auto-exposure effect; pinned vs sync = the
# sync-inputs effect). Requires the 4 C920s to be idle (no other consumer).
set -euo pipefail
cd "$(dirname "$0")"

DUR="${1:-120}"
RES="${2:-results}"
DEVS="/dev/video0,/dev/video2,/dev/video4,/dev/video6"

make -s

for d in ${DEVS//,/ }; do
  [ -e "$d" ] || { echo "missing $d — adjust DEVS in this script"; exit 1; }
  if command -v fuser >/dev/null && fuser "$d" >/dev/null 2>&1; then
    echo "$d is busy — stop the other consumer first"; exit 1
  fi
done

# Back-to-back 4-camera sessions need generous settle time: uvcvideo releases
# USB bandwidth lazily, and reopening too soon yields EIO ("Internal data
# stream error") on a random camera. 10 s + one retry absorbs that.
capture() {  # capture <outdir> <probe args...>
  local out="$1"; shift
  for attempt in 1 2; do
    if ./frame_timing_probe --devices "$DEVS" --duration "$DUR" \
        --out-dir "$out" "$@"; then
      return 0
    fi
    echo "capture into $out failed (attempt $attempt) — settling 10 s and retrying"
    sleep 10
  done
  echo "capture into $out failed twice — aborting"; exit 1
}

echo "== run 1/3: baseline (production-identical), ${DUR}s =="
capture "$RES/baseline"

sleep 10

echo "== run 2/3: baseline_pinned (exposure_auto_priority=0), ${DUR}s =="
capture "$RES/baseline_pinned" --extra-controls "exposure_auto_priority=0"

sleep 10

echo "== run 3/3: sync_pinned (sync-inputs=1, max-latency 33ms), ${DUR}s =="
capture "$RES/sync_pinned" --extra-controls "exposure_auto_priority=0" \
    --sync --max-latency-ms 33.333

echo "== analysis =="
python3 analyze_timing.py "$RES/baseline"        --compare "$RES/baseline_pinned"
python3 analyze_timing.py "$RES/baseline_pinned" --compare "$RES/sync_pinned"
python3 analyze_timing.py "$RES/sync_pinned"

echo "done. figures in $RES/*/figures/, numbers in $RES/*/figures/summary.md"
