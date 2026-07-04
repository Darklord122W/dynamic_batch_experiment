"""fps_overlay.py

Draws a live per-camera FPS readout in the top-left corner of each tile in the
debug display (and in any recording), using nvdsosd display metadata.

How it fits together:
  * ``FpsMeter`` measures a rolling frames-per-second for each camera. It is
    ticked once per frame per camera from the detection probe on nvtracker's src
    pad — a point where the batch reliably carries one frame_meta per camera.
  * ``attach_fps_overlay`` adds a second probe on nvmultistreamtiler's src pad
    that, once per composited buffer, writes one text label per camera at that
    camera's tile corner. Measuring before the tiler and drawing after it keeps
    both steps on solid ground (guaranteed per-source frames upstream; a single
    known composite coordinate space downstream).

Only used in --display / --record modes.
"""

from __future__ import annotations

import time

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

import pyds  # noqa: E402


class FpsMeter:
    """Rolling per-camera FPS estimate.

    Args:
        window_s: length of the averaging window in seconds. Every ``window_s``
            the fps for a camera is recomputed from the frames counted in that
            window (so the number is responsive but not jittery).
    """

    def __init__(self, window_s: float = 0.5) -> None:
        self._window = float(window_s)
        self._count: dict = {}   # source_id -> frames counted this window
        self._start: dict = {}   # source_id -> window start (monotonic)
        self._fps: dict = {}     # source_id -> last computed fps

    def tick(self, source_id: int) -> None:
        """Record one processed frame for ``source_id`` and refresh its fps."""
        now = time.monotonic()
        self._count[source_id] = self._count.get(source_id, 0) + 1
        start = self._start.setdefault(source_id, now)
        elapsed = now - start
        if elapsed >= self._window:
            self._fps[source_id] = self._count[source_id] / elapsed
            self._count[source_id] = 0
            self._start[source_id] = now

    def get(self, source_id: int) -> float:
        """Return the latest fps for ``source_id`` (0.0 until first window closes)."""
        return self._fps.get(source_id, 0.0)


def attach_fps_overlay(tiler: Gst.Element, num_cams: int, meter: FpsMeter) -> None:
    """Attach a probe on the tiler's src pad that draws per-tile FPS text.

    Args:
        tiler: the ``nvmultistreamtiler`` element (its rows/columns/width/height
            define the tile grid we position text into).
        num_cams: number of cameras (== number of tiles == labels to draw).
        meter: the shared ``FpsMeter`` fed by the detection probe.

    The tiler lays streams out in row-major order by source_id, so camera ``i``
    occupies tile (row = i // columns, col = i % columns). We convert that to the
    composited output's pixel coordinates and drop a label just inside the corner.
    """
    cols = int(tiler.get_property("columns"))
    rows = int(tiler.get_property("rows"))
    out_w = int(tiler.get_property("width"))
    out_h = int(tiler.get_property("height"))
    tile_w = out_w / max(cols, 1)
    tile_h = out_h / max(rows, 1)

    def probe(pad: Gst.Pad, info: Gst.PadProbeInfo, _user) -> Gst.PadProbeReturn:
        buf = info.get_buffer()
        if buf is None:
            return Gst.PadProbeReturn.OK
        batch_meta = pyds.gst_buffer_get_nvds_batch_meta(hash(buf))
        if batch_meta is None:
            return Gst.PadProbeReturn.OK
        l_frame = batch_meta.frame_meta_list
        if l_frame is None:
            return Gst.PadProbeReturn.OK
        try:
            # Display metadata is per-frame; attach our whole label set to the
            # first frame_meta. text_params x/y_offset are absolute pixels in the
            # composited surface, so one frame_meta can carry every tile's label.
            frame_meta = pyds.NvDsFrameMeta.cast(l_frame.data)
        except StopIteration:
            return Gst.PadProbeReturn.OK

        display_meta = pyds.nvds_acquire_display_meta_from_pool(batch_meta)
        n = 0
        for sid in range(num_cams):
            col = sid % cols
            row = sid // cols
            txt = display_meta.text_params[n]
            txt.display_text = f"cam{sid}  {meter.get(sid):4.1f} FPS"
            txt.x_offset = int(col * tile_w) + 12
            txt.y_offset = int(row * tile_h) + 10
            txt.font_params.font_name = "Serif"
            txt.font_params.font_size = 12
            txt.font_params.font_color.set(1.0, 1.0, 1.0, 1.0)   # white text
            txt.set_bg_clr = 1
            txt.text_bg_clr.set(0.0, 0.0, 0.0, 0.55)             # translucent black box
            n += 1
        display_meta.num_labels = n
        pyds.nvds_add_display_meta_to_frame(frame_meta, display_meta)
        return Gst.PadProbeReturn.OK

    src_pad = tiler.get_static_pad("src")
    if src_pad is None:
        raise RuntimeError("nvmultistreamtiler has no src pad for the FPS overlay probe.")
    src_pad.add_probe(Gst.PadProbeType.BUFFER, probe, None)
