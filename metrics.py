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
import os
import time
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
        self._mux_batch: int = num_cams

        # {batch PTS -> mux-src monotonic time}. Keyed by PTS (not a FIFO) so a
        # dropped buffer between mux and tracker can't permanently desync the
        # compute-latency pairing (the FIFO version was one-off-forever fragile).
        self._mux_pts = {}
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
            ["batch_idx", "t_mono", "n_in_batch", "n_real", "n_active", "active_mask",
             "timeout_us", "mux_batch", "compute_ms", "e2e_ms", "total_dets", "new_ids_cum"]
            + [f"dets_cam{i}" for i in range(num_cams)]
        )
        self._t_start = time.monotonic()

        # Opt-in diagnostic (default OFF; no effect unless DIAG_SOURCES is set):
        # per batch, dump which camera source_ids landed together and their PTS
        # skew (ms). Used to explain why sync-inputs caps n_in_batch below num_cams.
        self._diag = None
        diag_path = os.environ.get("DIAG_SOURCES")
        if diag_path:
            self._diag = open(diag_path, "w", newline="")
            self._diag.write("batch_idx,t_mono,n,source_ids,pts_skew_ms,per_cam_offset_ms\n")

    # ---- called by the controllers each control tick --------------------- #
    def set_active_cameras(self, active: Set[int]) -> None:
        self._active = set(active)

    def set_timeout_us(self, us: int) -> None:
        self._timeout_us = int(us)

    def set_mux_batch(self, bs: int) -> None:
        self._mux_batch = int(bs)

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
        buf = info.get_buffer()
        if buf is not None and buf.pts != Gst.CLOCK_TIME_NONE:
            self._mux_pts[buf.pts] = time.monotonic()
            if len(self._mux_pts) > self._src_pts_cap:  # bound memory: drop oldest
                del self._mux_pts[next(iter(self._mux_pts))]
        return Gst.PadProbeReturn.OK

    def _tracker_probe(self, pad, info, _u) -> Gst.PadProbeReturn:
        buf = info.get_buffer()
        if buf is None:
            return Gst.PadProbeReturn.OK
        now = time.monotonic()

        # compute latency (mux src -> tracker src): inference + tracking only.
        # Matched by batch PTS -> robust to any dropped buffer between the two pads.
        t_mux = self._mux_pts.pop(buf.pts, None) if buf.pts != Gst.CLOCK_TIME_NONE else None
        compute_ms = (now - t_mux) * 1e3 if t_mux is not None else -1.0

        # detections per camera + true source->output latency + tracking proxy.
        batch_meta = pyds.gst_buffer_get_nvds_batch_meta(hash(buf))
        per_cam = [0] * self.num_cams
        total = 0
        max_e2e = -1.0
        n_real = 0  # frames that genuinely arrived from a live camera this cycle
        diag_srcs = []  # (source_id, buf_pts) present this batch — for DIAG_SOURCES
        if batch_meta is not None:
            for frame in parse_batch_meta(batch_meta):
                cid = frame.camera_id
                if self._diag is not None:
                    diag_srcs.append((cid, frame.buf_pts))
                total += len(frame.detections)
                if 0 <= cid < self.num_cams:
                    per_cam[cid] = len(frame.detections)
                    # A frame is REAL iff its PTS matches a source arrival stamp.
                    # The legacy mux pads a skipped batch with REPEATED frames whose
                    # PTS was already consumed -> they won't match -> excluded. This
                    # gives an honest frame count (num_frames_in_batch overcounts the
                    # phantom repeats), and the frame's true source->output latency.
                    arrival = self._src_pts[cid].pop(frame.buf_pts, None)
                    if arrival is not None:
                        n_real += 1
                        lat_ms = (now - arrival) * 1e3
                        if lat_ms > max_e2e:
                            max_e2e = lat_ms
                for det in frame.detections:
                    if det.track_id >= 0:
                        key = (cid, det.track_id)
                        if key not in self._seen_ids:
                            self._seen_ids.add(key)
                            self._new_ids_cum += 1
        # a batch is only as fresh as its OLDEST frame -> worst-case in-batch e2e
        e2e_ms = max_e2e

        # n_in_batch = what the mux REPORTS (may include phantom repeats when
        # skipping); n_real = frames that actually arrived from a camera.
        n_in_batch = batch_meta.num_frames_in_batch if batch_meta is not None else 0

        # per-camera active bitmask, e.g. "1100" = cameras 0,1 active (2,3 skipped)
        active_mask = "".join("1" if i in self._active else "0" for i in range(self.num_cams))
        self._writer.writerow(
            [self._batch_idx, f"{now - self._t_start:.4f}", n_in_batch, n_real,
             len(self._active), active_mask, self._timeout_us, self._mux_batch,
             f"{compute_ms:.3f}", f"{e2e_ms:.3f}", total, self._new_ids_cum]
            + per_cam
        )
        if self._diag is not None and diag_srcs:
            ids = sorted(s for s, _ in diag_srcs)
            pts = [p for _, p in diag_srcs if p]
            skew_ms = (max(pts) - min(pts)) / 1e6 if len(pts) > 1 else 0.0
            # per-camera PTS offset from the batch's min PTS (ms), so we can SEE the
            # actual inter-camera timestamp structure: e.g. "0=+0.0|1=+8.3|2=+16.9".
            base = min(pts) if pts else 0
            per = "|".join(f"{s}=+{(p - base)/1e6:.1f}" for s, p in sorted(diag_srcs))
            self._diag.write(f"{self._batch_idx},{now - self._t_start:.4f},"
                             f"{len(ids)},{'|'.join(map(str, ids))},{skew_ms:.2f},{per}\n")
        self._batch_idx += 1
        return Gst.PadProbeReturn.OK

    def close(self) -> None:
        """Flush the CSV and print a one-line summary."""
        try:
            self._file.flush()
            self._file.close()
            if self._diag is not None:
                self._diag.flush()
                self._diag.close()
        except Exception:
            pass
        dur = max(1e-6, time.monotonic() - self._t_start)
        print(
            f"[metrics] wrote {self._batch_idx} batches to {self._csv_path} "
            f"({self._batch_idx / dur:.1f} batches/s over {dur:.1f}s; "
            f"{self._new_ids_cum} distinct tracks).",
            flush=True,
        )
