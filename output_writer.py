"""output_writer.py

The single, swappable exit point for parsed detections.

The spec deliberately routes every detection through ONE writer instead of
scattering ``print()`` calls through the probe, so that swapping the consumer
later (a socket, a message queue, ROS 2 again, a database) only touches this
file. The pad probe calls ``writer.write(frame_detections)`` and nothing else.

Writers provided:
  * ``OutputWriter``    — one JSON object per camera per frame (default, machine-readable)
  * ``HumanLogWriter``  — compact, throttled, colorized per-camera terminal log (debug)
  * ``NullOutputWriter``— discards everything (benchmarking / display-only runs)

Pick one with ``make_writer(mode, ...)``.
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import List, TextIO

from detection_parser import FrameDetections


class OutputWriter:
    """Base sink. Emits one JSON line per camera per processed frame.

    Args:
        stream: where JSON lines go (default ``sys.stdout``).
        only_nonempty: if True, frames with zero detections are skipped so the
            log isn't flooded with empty records for idle cameras.
        pretty: if True, pretty-print the JSON (multi-line) instead of one line.
    """

    def __init__(
        self,
        stream: TextIO = sys.stdout,
        only_nonempty: bool = False,
        pretty: bool = False,
    ) -> None:
        self._stream = stream
        self._only_nonempty = only_nonempty
        self._pretty = pretty

    def write(self, frame: FrameDetections) -> None:
        """Emit the detections for one camera/frame.

        Args:
            frame: a ``FrameDetections`` (camera_id, frame_num, [Detection,...]).
        """
        if self._only_nonempty and not frame.detections:
            return
        record = frame.as_dict()
        if self._pretty:
            line = json.dumps(record, indent=2)
        else:
            line = json.dumps(record, separators=(",", ":"))
        self._stream.write(line + "\n")
        self._stream.flush()

    def write_batch(self, frames: List[FrameDetections]) -> None:
        """Convenience: emit every camera's ``FrameDetections`` from one batch."""
        for frame in frames:
            self.write(frame)

    def close(self) -> None:
        """Release any resources. No-op for the stdout writer; override as needed."""
        try:
            self._stream.flush()
        except Exception:
            pass


class NullOutputWriter(OutputWriter):
    """Discards everything. Useful for benchmarking or display-only runs."""

    def write(self, frame: FrameDetections) -> None:  # noqa: D401
        return


# ANSI colors, one per camera, so a human can tell the streams apart at a glance.
_CAM_COLORS = [36, 32, 33, 35, 34, 91, 92, 93]  # cyan, green, yellow, magenta, blue, ...
_DIM = "\033[2m"
_BOLD = "\033[1m"
_RESET = "\033[0m"


class HumanLogWriter(OutputWriter):
    """Compact, throttled, colorized per-camera terminal log for debugging.

    Prints at most once per ``interval`` seconds *per camera* (so a 30 fps ×
    4-camera firehose becomes a few readable lines a second), summarizing the
    objects currently tracked in that camera's latest frame, plus a measured fps.

    Example line:
        [cam0 f=312  29.7fps]  3 obj: person#7 0.91 | laptop#3 0.66 | mouse#12 0.55

    Args:
        interval: minimum seconds between printed lines for a given camera.
        stream: destination (default ``sys.stdout``).
        max_objects: cap objects shown per line (rest summarized as "+N more").
    """

    def __init__(
        self,
        interval: float = 1.0,
        stream: TextIO = sys.stdout,
        max_objects: int = 8,
    ) -> None:
        self._interval = float(interval)
        self._stream = stream
        self._max = int(max_objects)
        self._color = getattr(stream, "isatty", lambda: False)()
        self._last_t: dict = {}     # camera_id -> monotonic time of last print
        self._frames: dict = {}     # camera_id -> frames seen since last print

    def _c(self, text: str, code: int) -> str:
        """Wrap text in an ANSI color if the stream is a TTY, else return as-is."""
        return f"\033[{code}m{text}{_RESET}" if self._color else text

    def write(self, frame: FrameDetections) -> None:
        """Throttle per camera, then print a one-line summary for that camera."""
        cid = frame.camera_id
        self._frames[cid] = self._frames.get(cid, 0) + 1

        now = time.monotonic()
        last = self._last_t.get(cid)
        if last is not None and (now - last) < self._interval:
            return

        n = self._frames[cid]
        fps = (n / (now - last)) if last and now > last else 0.0
        self._last_t[cid] = now
        self._frames[cid] = 0

        dets = frame.detections
        cam_col = _CAM_COLORS[cid % len(_CAM_COLORS)]
        tag = self._c(f"cam{cid}", cam_col)
        meta = self._c(f"f={frame.frame_num} {fps:5.1f}fps", 90) if self._color \
            else f"f={frame.frame_num} {fps:5.1f}fps"

        if dets:
            shown = dets[: self._max]
            parts = " | ".join(
                f"{self._c(d.class_name, cam_col)}"
                f"{self._c('#' + str(d.track_id), 90) if self._color else '#' + str(d.track_id)}"
                f" {d.confidence:.2f}"
                for d in shown
            )
            more = "" if len(dets) <= self._max else f"  {self._c(f'+{len(dets) - self._max} more', 90)}"
            body = f"{self._c(str(len(dets)) + ' obj', 1)}: {parts}{more}"
        else:
            body = self._c("no objects", 90) if self._color else "no objects"

        self._stream.write(f"[{tag} {meta}]  {body}\n")
        self._stream.flush()


def make_writer(mode: str, only_nonempty: bool = False, pretty: bool = False,
                interval: float = 1.0) -> OutputWriter:
    """Factory: build the writer for a ``--log`` mode.

    Args:
        mode: "json" | "human" | "none".
        only_nonempty / pretty: forwarded to the JSON writer.
        interval: throttle seconds for the human writer.

    Returns:
        An ``OutputWriter`` instance.
    """
    mode = (mode or "json").lower()
    if mode == "json":
        return OutputWriter(only_nonempty=only_nonempty, pretty=pretty)
    if mode == "human":
        return HumanLogWriter(interval=interval)
    if mode == "none":
        return NullOutputWriter()
    raise ValueError(f"unknown log mode '{mode}' (use json | human | none)")
