"""pipeline_builder.py

Constructs and wires the GStreamer / DeepStream pipeline from a parsed config
dict. Nothing here is hardcoded — every device path, resolution, fps, model
config path and tracker setting comes from ``camera_params.yaml``.

Pipeline shape (see ds_multicam_pipeline_spec.md §4):

    per camera:  v4l2src ─► capsfilter ─►[decoder]─► nvvideoconvert ─► capsfilter(NVMM,NV12)
                                                                             │
    all cameras ─────────────────────────────────────────► nvstreammux ◄────┘   (batch = N cams)
                              │
                              ▼
                          nvinfer (PGIE: YOLO11n)   — ONE inference pass over the whole batch
                              │
                              ▼
                          nvtracker                 — persistent track IDs, per camera
                              │  (◄── BUFFER pad probe attaches HERE, on the src pad)
                              ▼
                          nvvideoconvert ─► fakesink (headless; detections leave via the probe)

Two capture paths are supported, chosen per camera by ``format``:
  * ``mjpeg`` — C920x streams motion-JPEG; required for 1080p30 (USB-2 bandwidth).
                Decoded in hardware by ``nvv4l2decoder mjpeg=1`` (NVJPG engine).
  * ``raw``   — uncompressed YUY2; fine up to 720p30. No decoder needed, just convert.

The camera frames arrive in CPU/system memory; ``nvvideoconvert`` copies them
into NVMM (GPU-accessible) ``NV12`` buffers, which is what ``nvstreammux`` and
the rest of the DeepStream pipeline require.
"""

from __future__ import annotations

import math
import os
from typing import Callable, Dict, List, Optional, Tuple

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _make(factory: str, name: str) -> Gst.Element:
    """Create a GStreamer element or raise a clear error if the plugin is missing.

    Args:
        factory: element factory name, e.g. ``"nvstreammux"``.
        name: unique instance name within the pipeline.

    Returns:
        The created ``Gst.Element``.

    Raises:
        RuntimeError: if the factory is not registered (plugin not installed).
    """
    element = Gst.ElementFactory.make(factory, name)
    if element is None:
        raise RuntimeError(
            f"Failed to create GStreamer element '{factory}' (name='{name}'). "
            f"Is the plugin installed? Check `gst-inspect-1.0 {factory}`."
        )
    return element


def _request_sink_pad(streammux: Gst.Element, index: int) -> Gst.Pad:
    """Request the ``sink_<index>`` pad from nvstreammux (GStreamer 1.20 compatible)."""
    pad_name = f"sink_{index}"
    # request_pad_simple replaced the deprecated get_request_pad in GStreamer 1.20.
    request = getattr(streammux, "request_pad_simple", None) or streammux.get_request_pad
    pad = request(pad_name)
    if pad is None:
        raise RuntimeError(f"nvstreammux did not provide request pad '{pad_name}'.")
    return pad


# --------------------------------------------------------------------------- #
# Per-camera source bin
# --------------------------------------------------------------------------- #
def _build_source_bin(index: int, cam: Dict) -> Gst.Bin:
    """Build one camera's capture branch as a self-contained ``Gst.Bin``.

    The bin exposes a single ghost src pad emitting ``video/x-raw(memory:NVMM),
    NV12`` ready to feed an nvstreammux sink pad.

    Args:
        index: camera index (also its nvstreammux source_id / camera_id).
        cam: normalized per-camera config with keys device, format, width,
            height, fps.

    Returns:
        A ``Gst.Bin`` named ``source-bin-<index>``.
    """
    source_type: str = cam.get("source_type", "v4l2")
    bin_name = f"source-bin-{index}"
    nbin = Gst.Bin.new(bin_name)

    # Capture front (live v4l2 OR deterministic file replay) -> returns the last
    # element, which feeds the shared NVMM + valve tail below.
    if source_type == "v4l2":
        head_last = _build_v4l2_front(nbin, index, cam)
    elif source_type == "file":
        head_last = _build_file_front(nbin, index, cam)
    else:
        raise RuntimeError(
            f"camera {index}: unknown source_type '{source_type}' (use 'v4l2' or 'file')."
        )

    # --- Into GPU memory (NVMM NV12), then a valve that is the skip gate ---
    conv = _make("nvvideoconvert", f"cam-conv-{index}")
    nvmmcaps = _make("capsfilter", f"cam-nvmmcaps-{index}")
    nvmmcaps.set_property("caps", Gst.Caps.from_string("video/x-raw(memory:NVMM),format=NV12"))
    # The valve is the runtime camera-skip gate. valve.drop=True -> this camera
    # stops feeding nvstreammux, so after batched-push-timeout the muxer forms a
    # SMALLER batch (context-aware skipping). drop-mode=1 (forward-sticky-events)
    # keeps caps/segment alive so the camera can rejoin without renegotiation.
    valve = _make("valve", f"cam-valve-{index}")
    valve.set_property("drop-mode", 1)
    valve.set_property("drop", False)
    for el in (conv, nvmmcaps, valve):
        nbin.add(el)
    _link_chain([head_last, conv, nvmmcaps, valve])

    # Ghost the valve's src pad so the bin can be linked to the muxer.
    ghost = Gst.GhostPad.new("src", valve.get_static_pad("src"))
    ghost.set_active(True)
    nbin.add_pad(ghost)
    return nbin


def _build_v4l2_front(nbin: Gst.Bin, index: int, cam: Dict) -> Gst.Element:
    """Build the live-capture front (v4l2src -> caps -> optional JPEG decode) inside
    ``nbin`` and return the last element (to be linked to the NVMM converter)."""
    device = cam.get("device")
    if not device:
        raise RuntimeError(f"camera {index}: source_type 'v4l2' needs a 'device' path.")
    fmt = cam["format"]
    width, height, fps = cam["width"], cam["height"], cam["fps"]
    mjpeg_decoder = cam.get("mjpeg_decoder", "nvjpegdec")

    src = _make("v4l2src", f"cam-src-{index}")
    src.set_property("device", device)
    src.set_property("io-mode", 2)  # MMAP: robust default for UVC webcams
    elements: List[Gst.Element] = [src]

    if fmt == "mjpeg":
        srccaps = _make("capsfilter", f"cam-srccaps-{index}")
        srccaps.set_property(
            "caps",
            Gst.Caps.from_string(f"image/jpeg,width={width},height={height},framerate={fps}/1"),
        )
        elements.append(srccaps)
        # C920 MJPEG is YUV 4:2:2 -> jpegparse ! nvjpegdec (HW) or jpegdec (SW).
        # NOT nvv4l2decoder mjpeg=1 (4:2:0 only). See the main README for detail.
        if mjpeg_decoder in ("nvjpegdec", "jpegdec"):
            elements += [
                _make("jpegparse", f"cam-jparse-{index}"),
                _make(mjpeg_decoder, f"cam-jpegdec-{index}"),
            ]
        elif mjpeg_decoder in ("nvv4l2", "nvv4l2decoder"):
            decoder = _make("nvv4l2decoder", f"cam-jpegdec-{index}")
            decoder.set_property("mjpeg", 1)
            elements.append(decoder)
        else:
            raise RuntimeError(
                f"camera {index}: unknown mjpeg_decoder '{mjpeg_decoder}' "
                f"(use 'nvjpegdec', 'jpegdec', or 'nvv4l2')."
            )
    elif fmt in ("raw", "yuyv", "yuy2"):
        srccaps = _make("capsfilter", f"cam-srccaps-{index}")
        srccaps.set_property(
            "caps",
            Gst.Caps.from_string(
                f"video/x-raw,format=YUY2,width={width},height={height},framerate={fps}/1"
            ),
        )
        elements += [srccaps, _make("videoconvert", f"cam-swconv-{index}")]
    else:
        raise RuntimeError(f"camera {index}: unknown capture format '{fmt}' (use 'mjpeg'/'raw').")

    for el in elements:
        nbin.add(el)
    _link_chain(elements)
    return elements[-1]


def _build_file_front(nbin: Gst.Bin, index: int, cam: Dict) -> Gst.Element:
    """Build the deterministic file-replay front (filesrc -> qtdemux -> h264parse
    -> nvv4l2decoder) inside ``nbin`` and return the decoder.

    Used for reproducible experiments: replaying identical recorded clips makes
    runs comparable (see scripts/record_replay_clips.py). Clips are H.264 MP4.
    """
    path = cam.get("file")
    if not path or not os.path.isfile(path):
        raise RuntimeError(f"camera {index}: replay file not found: {path!r}")
    src = _make("filesrc", f"cam-src-{index}")
    src.set_property("location", path)
    demux = _make("qtdemux", f"cam-demux-{index}")
    parse = _make("h264parse", f"cam-h264parse-{index}")
    dec = _make("nvv4l2decoder", f"cam-dec-{index}")
    for el in (src, demux, parse, dec):
        nbin.add(el)
    if not src.link(demux):
        raise RuntimeError(f"camera {index}: filesrc -> qtdemux link failed.")
    _link_chain([parse, dec])

    # qtdemux exposes its video pad dynamically; link it to h264parse on pad-added.
    def _on_pad(_demux: Gst.Element, pad: Gst.Pad) -> None:
        if not pad.get_name().startswith("video"):
            return
        sinkpad = parse.get_static_pad("sink")
        if not sinkpad.is_linked():
            pad.link(sinkpad)

    demux.connect("pad-added", _on_pad)
    return dec


# --------------------------------------------------------------------------- #
# nvstreammux / nvinfer / nvtracker
# --------------------------------------------------------------------------- #
def _build_streammux(cfg: Dict, num_cams: int) -> Gst.Element:
    """Create and configure nvstreammux for ``num_cams`` live USB sources."""
    mux = _make("nvstreammux", "stream-muxer")
    mux_cfg = cfg.get("streammux", {}) or {}
    capture = cfg["capture"]

    # batch-size MUST equal the camera count so one batched buffer holds all frames.
    mux.set_property("batch-size", num_cams)
    # Output resolution == capture resolution, so bbox pixel coords map 1:1 to the
    # original image (no rescaling in detection_parser). Overridable in config.
    mux.set_property("width", int(mux_cfg.get("width", capture["width"])))
    mux.set_property("height", int(mux_cfg.get("height", capture["height"])))
    # live-source=1 for live USB cameras; 0 for file replay (so the muxer honours
    # timestamps and the sink paces playback to real time instead of rushing).
    is_file = (cfg.get("source") or {}).get("type") == "file"
    mux.set_property("live-source", 0 if is_file else int(mux_cfg.get("live_source", 1)))
    # batched-push-timeout (µs) = 1e6 / max_fps. 33333 ≈ 1/30s: don't block the
    # batch indefinitely if one live camera hiccups.
    mux.set_property(
        "batched-push-timeout", int(mux_cfg.get("batched_push_timeout_us", 33333))
    )
    # Jetson: 0 = default NVMM surface array allocator.
    mux.set_property("nvbuf-memory-type", int(mux_cfg.get("nvbuf_memory_type", 0)))

    # Input time-synchronization (RT-BEV-style). OFF by default: our per-camera
    # detection never fuses cameras, so aligning frames by timestamp only adds
    # latency (and drops) for no accuracy gain. Turn on to experiment, or if you
    # add a fusion/stitching stage. max-latency (ns) is the extra time the muxer
    # waits to align a late frame; it is applied ONLY when sync is on, so the
    # default (sync off) path keeps its low latency with no change.
    sync_inputs = int(mux_cfg.get("sync_inputs", 0))
    mux.set_property("sync-inputs", sync_inputs)
    if sync_inputs:
        mux.set_property("max-latency", int(mux_cfg.get("max_latency_ns", 33333333)))
    return mux


def _build_pgie(cfg: Dict, num_cams: int) -> Gst.Element:
    """Create nvinfer (PGIE) from its config file, batch-size = camera count."""
    pgie = _make("nvinfer", "primary-inference")
    pgie.set_property("config-file-path", cfg["pgie"]["config_file"])
    # Override the config's batch-size so the engine is built/run for the actual
    # number of cameras (the ONNX was exported with --dynamic to allow this).
    pgie.set_property("batch-size", num_cams)
    return pgie


def _build_tracker(cfg: Dict) -> Gst.Element:
    """Create nvtracker from the tracker section of the config."""
    tracker = _make("nvtracker", "tracker")
    tcfg = cfg["tracker"]
    # Low-level tracker plugin (shared by IOU/NvSORT/NvDCF; behaviour set by the yml).
    tracker.set_property("ll-lib-file", tcfg["ll_lib_file"])
    tracker.set_property("ll-config-file", tcfg["ll_config_file"])
    # Internal processing resolution — MUST be multiples of 32.
    tracker.set_property("tracker-width", int(tcfg.get("width", 640)))
    tracker.set_property("tracker-height", int(tcfg.get("height", 384)))
    tracker.set_property("gpu-id", int(tcfg.get("gpu_id", 0)))
    # Show the persistent track ID next to each object in the OSD (debug display).
    # OSD-only — harmless when running headless (no OSD in the pipeline).
    tracker.set_property("display-tracking-id", 1)
    # NOTE: DS7.1 nvtracker has no 'enable-batch-process'/'enable-past-frame'
    # properties (batching is on by default; verified via `gst-inspect-1.0
    # nvtracker`). Setting them would raise, so we don't.
    return tracker


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def build_pipeline(
    cfg: Dict,
    display: bool = False,
    record_path: Optional[str] = None,
) -> Tuple[Gst.Pipeline, Gst.Element]:
    """Build the full DeepStream pipeline from a parsed config dict.

    Args:
        cfg: parsed ``camera_params.yaml`` with keys: cameras, capture,
            streammux, pgie, tracker (see the sample config for the schema).
        display: if True, add a debug tail that tiles all cameras into one window
            and draws bounding boxes + labels + track IDs (``nvmultistreamtiler``
            → ``nvdsosd`` → ``nv3dsink``).
        record_path: if set, also encode that annotated, tiled view to an H.264
            MP4 file at this path. Works with or without ``display``.

    Returns:
        ``(pipeline, tracker)`` — the assembled, unstarted ``Gst.Pipeline`` and
        the ``nvtracker`` element (its src pad is where main.py attaches the
        detection probe, because track_id is only populated after the tracker).

    Raises:
        RuntimeError: on any element-creation or linking failure.
    """
    cameras: List[Dict] = cfg["cameras"]
    num_cams = len(cameras)
    if num_cams < 1:
        raise RuntimeError("No cameras configured — 'cameras' list is empty.")

    pipeline = Gst.Pipeline.new("multicam-perception")
    if pipeline is None:
        raise RuntimeError("Failed to create Gst.Pipeline.")

    # Stage 2-4: shared, single-instance elements.
    streammux = _build_streammux(cfg, num_cams)
    pgie = _build_pgie(cfg, num_cams)
    tracker = _build_tracker(cfg)
    for el in (streammux, pgie, tracker):
        pipeline.add(el)

    # Stage 1: build each camera branch and link it into a streammux sink pad.
    for index, cam in enumerate(cameras):
        source_bin = _build_source_bin(index, cam)
        pipeline.add(source_bin)
        srcpad = source_bin.get_static_pad("src")
        sinkpad = _request_sink_pad(streammux, index)
        if srcpad.link(sinkpad) != Gst.PadLinkReturn.OK:
            raise RuntimeError(f"Failed to link source-bin-{index} to nvstreammux.")

    # Shared trunk: mux -> pgie -> tracker.
    for upstream, downstream in ((streammux, pgie), (pgie, tracker)):
        if not upstream.link(downstream):
            raise RuntimeError(f"Failed to link {upstream.name} -> {downstream.name}.")

    # Tail after the tracker: headless (fakesink) or a visual (tiler + OSD) branch.
    # The detection probe on tracker's src pad runs in every mode — the tail is
    # only about what a human sees.
    _build_tail(pipeline, tracker, cfg, num_cams, display, record_path)

    return pipeline, tracker


# --------------------------------------------------------------------------- #
# Output tail: headless fakesink, or the debug display / recording branch.
# --------------------------------------------------------------------------- #
def _link_chain(elements: List[Gst.Element]) -> None:
    """Link a list of elements head-to-tail, raising on the first failure."""
    for upstream, downstream in zip(elements, elements[1:]):
        if not upstream.link(downstream):
            raise RuntimeError(f"Failed to link {upstream.name} -> {downstream.name}.")


def _tiler_grid(n: int) -> Tuple[int, int]:
    """Return (rows, columns) for an approximately square tile grid of n cameras."""
    cols = math.ceil(math.sqrt(n))
    rows = math.ceil(n / cols)
    return rows, cols


def _branch_from(head: Gst.Element, head_is_tee: bool, first: Gst.Element) -> None:
    """Link a sink branch's first element to `head` (a tee needs a request pad)."""
    if head_is_tee:
        request = getattr(head, "request_pad_simple", None) or head.get_request_pad
        teepad = request("src_%u")
        sinkpad = first.get_static_pad("sink")
        if teepad.link(sinkpad) != Gst.PadLinkReturn.OK:
            raise RuntimeError("Failed to link tee -> sink branch.")
    else:
        if not head.link(first):
            raise RuntimeError(f"Failed to link {head.name} -> {first.name}.")


def _build_tail(
    pipeline: Gst.Pipeline,
    tracker: Gst.Element,
    cfg: Dict,
    num_cams: int,
    display: bool,
    record_path: Optional[str],
) -> None:
    """Attach the correct downstream tail to nvtracker's src.

    Headless (default): nvvideoconvert -> fakesink (frames discarded; detections
    leave via the probe). Debug: tile all cameras, draw boxes/labels/IDs with
    nvdsosd, then show it in a window and/or record it to MP4.
    """
    if not display and not record_path:
        # File replay: sync=1 paces playback to real time so the batching timeout
        # behaves like it does with live cameras. Live v4l2 sources are already
        # realtime-paced by capture, so sync=0 (process as fast as frames arrive).
        realtime = (cfg.get("source") or {}).get("type") == "file"
        conv = _make("nvvideoconvert", "sink-conv")
        sink = _make("fakesink", "sink")
        sink.set_property("sync", 1 if realtime else 0)
        sink.set_property("enable-last-sample", 0)
        pipeline.add(conv)
        pipeline.add(sink)
        _link_chain([tracker, conv, sink])
        return

    # --- Visual branch: composite N cameras -> draw overlays ---
    dcfg = cfg.get("display", {}) or {}
    rows, cols = _tiler_grid(num_cams)
    tiler = _make("nvmultistreamtiler", "tiler")
    tiler.set_property("rows", rows)
    tiler.set_property("columns", cols)
    tiler.set_property("width", int(dcfg.get("width", 1280)))
    tiler.set_property("height", int(dcfg.get("height", 720)))

    osd_conv = _make("nvvideoconvert", "osd-conv")
    osd_caps = _make("capsfilter", "osd-caps")
    # nvdsosd requires RGBA.
    osd_caps.set_property("caps", Gst.Caps.from_string("video/x-raw(memory:NVMM),format=RGBA"))
    osd = _make("nvdsosd", "osd")
    osd.set_property("process-mode", 1)  # GPU
    osd.set_property("display-bbox", 1)
    osd.set_property("display-text", 1)

    for el in (tiler, osd_conv, osd_caps, osd):
        pipeline.add(el)
    _link_chain([tracker, tiler, osd_conv, osd_caps, osd])

    # One sink -> link straight off the OSD; two sinks -> fan out with a tee.
    head, head_is_tee = osd, False
    if display and record_path:
        tee = _make("tee", "viz-tee")
        pipeline.add(tee)
        if not osd.link(tee):
            raise RuntimeError("Failed to link nvdsosd -> tee.")
        head, head_is_tee = tee, True

    if display:
        _attach_display_sink(pipeline, head, head_is_tee, dcfg)
    if record_path:
        _attach_record_branch(pipeline, head, head_is_tee, record_path)


def _attach_display_sink(
    pipeline: Gst.Pipeline, head: Gst.Element, head_is_tee: bool, dcfg: Dict
) -> None:
    """Add a live on-screen window (nv3dsink) fed from `head`."""
    queue = _make("queue", "disp-queue")
    sink = _make("nv3dsink", "disp-sink")
    sink.set_property("sync", 0)  # live: never wait on the clock
    if "window_width" in dcfg:
        sink.set_property("window-width", int(dcfg["window_width"]))
    if "window_height" in dcfg:
        sink.set_property("window-height", int(dcfg["window_height"]))
    pipeline.add(queue)
    pipeline.add(sink)
    _branch_from(head, head_is_tee, queue)
    _link_chain([queue, sink])


def _attach_record_branch(
    pipeline: Gst.Pipeline, head: Gst.Element, head_is_tee: bool, path: str
) -> None:
    """Add an H.264 MP4 recording branch (annotated, tiled view) fed from `head`."""
    queue = _make("queue", "rec-queue")
    conv = _make("nvvideoconvert", "rec-conv")
    caps = _make("capsfilter", "rec-caps")
    caps.set_property("caps", Gst.Caps.from_string("video/x-raw(memory:NVMM),format=NV12"))
    enc = _make("nvv4l2h264enc", "rec-enc")
    parse = _make("h264parse", "rec-parse")
    mux = _make("qtmux", "rec-mux")
    sink = _make("filesink", "rec-sink")
    sink.set_property("location", path)
    sink.set_property("sync", 0)
    for el in (queue, conv, caps, enc, parse, mux, sink):
        pipeline.add(el)
    _branch_from(head, head_is_tee, queue)
    # queue -> conv -> caps -> enc -> parse -> qtmux -> filesink
    _link_chain([queue, conv, caps, enc, parse, mux, sink])


def attach_detection_probe(
    tracker: Gst.Element, probe_fn: Callable[[Gst.Pad, Gst.PadProbeInfo], Gst.PadProbeReturn]
) -> None:
    """Attach a BUFFER probe on nvtracker's src pad.

    The probe sits on the tracker (not PGIE) because ``object_id`` (the persistent
    track ID) is only populated after nvtracker runs. PGIE's output has detections
    but no stable track IDs yet.

    Args:
        tracker: the nvtracker element returned by ``build_pipeline``.
        probe_fn: callback ``(pad, info) -> Gst.PadProbeReturn`` that reads the
            batch meta and forwards detections to the output writer.
    """
    src_pad = tracker.get_static_pad("src")
    if src_pad is None:
        raise RuntimeError("nvtracker has no src pad to attach the probe to.")
    src_pad.add_probe(Gst.PadProbeType.BUFFER, probe_fn, None)
