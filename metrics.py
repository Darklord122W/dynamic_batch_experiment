"""metrics.py — latency / throughput / batch instrumentation for experiments.

Attaches two pad probes and writes one CSV row per processed batch:

  * nvstreammux **src** pad  -> stamps a monotonic time when a batch is pushed.
  * nvtracker  **src** pad  -> the batch has been inferred + tracked; here we
      - pop the mux stamp (FIFO; the pipeline is linear so order is preserved)
        to get the compute latency (mux -> tracker),
      - use the buffer PTS vs the pipeline running clock for an e2e latency
        (capture -> here),
      - count how many frames were actually in the batch (== the timeout / skip
        effect) and how many detections each camera produced,
      - track newly-appearing track IDs as a cheap tracking-stability proxy.

Aggregation (mean / p50 / p99 / FES) is done offline by scripts/analyze.py so the
hot path stays cheap. What we log is raw per-batch rows.

No ground truth is available for live webcams, so "accuracy" here is a *proxy*
(track-ID stability). Swap in real mAP if you replay a labelled dataset.
"""

from __future__ import annotations

import csv
import time
from collections import deque
from typing import Optional, Set

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

import pyds  # noqa: E402

from detection_parser import parse_batch_meta


class MetricsCollector:
    """Collects per-batch metrics and streams them to a CSV file."""

    def __init__(self, csv_path: str, num_cams: int) -> None:
        self.num_cams = num_cams
        self._csv_path = csv_path
        self._pipeline: Optional[Gst.Pipeline] = None

        # State updated by the controllers each tick.
        self._active: Set[int] = set(range(num_cams))
        self._timeout_us: int = 0

        # FIFO of mux-src timestamps, matched to tracker-src buffers in order.
        self._mux_ts: deque = deque()
        # Per-camera map {frame PTS -> source arrival time}. Correlating by PTS
        # (not FIFO) survives nvstreammux dropping frames in live mode. Lets us
        # measure the TRUE latency a frame experiences (batch-wait + compute),
        # which the batch's own PTS misses because nvstreammux re-timestamps it.
        self._src_pts = {i: {} for i in range(num_cams)}
        self._src_pts_cap = 600
        self._batch_idx = 0

        # Tracking-stability proxy: distinct (camera, track_id) ever seen.
        self._seen_ids: Set = set()
        self._new_ids_cum = 0

        self._file = open(csv_path, "w", newline="")
        self._writer = csv.writer(self._file)
        self._writer.writerow(
            ["batch_idx", "t_mono", "n_in_batch", "n_active", "timeout_us",
             "compute_ms", "e2e_ms", "total_dets", "new_ids_cum"]
            + [f"dets_cam{i}" for i in range(num_cams)]
        )
        self._t_start = time.monotonic()

    # ---- called by the controllers each control tick --------------------- #
    def set_active_cameras(self, active: Set[int]) -> None:
        self._active = set(active)

    def set_timeout_us(self, us: int) -> None:
        self._timeout_us = int(us)

    # ---- probe wiring ---------------------------------------------------- #
    def attach(self, pipeline: Gst.Pipeline) -> None:
        """Attach the mux-src and tracker-src probes."""
        self._pipeline = pipeline
        mux = pipeline.get_by_name("stream-muxer")
        tracker = pipeline.get_by_name("tracker")
        if mux is None or tracker is None:
            raise RuntimeError("MetricsCollector: could not find stream-muxer / tracker.")
        mux.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, self._mux_probe, None)
        tracker.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, self._tracker_probe, None)

        # Per-camera arrival stamps at each source bin's (post-valve) src pad.
        for i in range(self.num_cams):
            src_bin = pipeline.get_by_name(f"source-bin-{i}")
            pad = src_bin.get_static_pad("src") if src_bin is not None else None
            if pad is not None:
                pad.add_probe(Gst.PadProbeType.BUFFER, self._make_src_probe(i), None)

    def _make_src_probe(self, cam_id: int):
        def _probe(pad, info, _u) -> Gst.PadProbeReturn:
            buf = info.get_buffer()
            if buf is not None and buf.pts != Gst.CLOCK_TIME_NONE:
                d = self._src_pts[cam_id]
                d[buf.pts] = time.monotonic()
                if len(d) > self._src_pts_cap:  # bound memory: drop oldest
                    del d[next(iter(d))]
            return Gst.PadProbeReturn.OK
        return _probe

    def _mux_probe(self, pad, info, _u) -> Gst.PadProbeReturn:
        if info.get_buffer() is not None:
            self._mux_ts.append(time.monotonic())
        return Gst.PadProbeReturn.OK

    def _tracker_probe(self, pad, info, _u) -> Gst.PadProbeReturn:
        buf = info.get_buffer()
        if buf is None:
            return Gst.PadProbeReturn.OK
        now = time.monotonic()

        # compute latency (mux src -> tracker src): inference + tracking only.
        t_mux = self._mux_ts.popleft() if self._mux_ts else None
        compute_ms = (now - t_mux) * 1e3 if t_mux is not None else -1.0

        # detections per camera + true source->output latency + tracking proxy.
        batch_meta = pyds.gst_buffer_get_nvds_batch_meta(hash(buf))
        per_cam = [0] * self.num_cams
        total = 0
        max_e2e = -1.0
        if batch_meta is not None:
            for frame in parse_batch_meta(batch_meta):
                cid = frame.camera_id
                if 0 <= cid < self.num_cams:
                    per_cam[cid] = len(frame.detections)
                    # true latency: this frame's source arrival -> now (wait + compute)
                    arrival = self._src_pts[cid].pop(frame.buf_pts, None)
                    if arrival is not None:
                        lat_ms = (now - arrival) * 1e3
                        if lat_ms > max_e2e:
                            max_e2e = lat_ms
                total += len(frame.detections)
                for det in frame.detections:
                    if det.track_id >= 0:
                        key = (cid, det.track_id)
                        if key not in self._seen_ids:
                            self._seen_ids.add(key)
                            self._new_ids_cum += 1
        # a batch is only as fresh as its OLDEST frame -> worst-case in-batch e2e
        e2e_ms = max_e2e

        # How many camera frames were actually in this batch — the direct measure
        # of the timeout / skip effect (4 = full; fewer = timed out or skipped).
        n_in_batch = batch_meta.num_frames_in_batch if batch_meta is not None else 0

        self._writer.writerow(
            [self._batch_idx, f"{now - self._t_start:.4f}", n_in_batch, len(self._active),
             self._timeout_us, f"{compute_ms:.3f}", f"{e2e_ms:.3f}", total, self._new_ids_cum]
            + per_cam
        )
        self._batch_idx += 1
        return Gst.PadProbeReturn.OK

    def close(self) -> None:
        """Flush the CSV and print a one-line summary."""
        try:
            self._file.flush()
            self._file.close()
        except Exception:
            pass
        dur = max(1e-6, time.monotonic() - self._t_start)
        print(
            f"[metrics] wrote {self._batch_idx} batches to {self._csv_path} "
            f"({self._batch_idx / dur:.1f} batches/s over {dur:.1f}s; "
            f"{self._new_ids_cum} distinct tracks).",
            flush=True,
        )
