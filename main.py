#!/usr/bin/env python3
"""main.py — entrypoint for the multi-camera DeepStream detection + tracking app.

Usage:
    python3 main.py --config config/camera_params.yaml

What it does:
    1. Parses CLI args and loads the YAML config (camera list + all parameters).
    2. Fails fast with a clear message if a configured camera device is missing.
    3. Builds the GStreamer/DeepStream pipeline (pipeline_builder.build_pipeline).
    4. Attaches a pyds pad probe on nvtracker's src pad that parses detections
       and hands them to a swappable OutputWriter.
    5. Runs a GLib main loop until EOS / error / Ctrl-C, then shuts down cleanly.

No parameters are hardcoded here — everything comes from the config file.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Dict, List

import gi
import yaml

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

import pyds  # noqa: E402

from context import make_context  # noqa: E402
from controllers import BatchController, CameraGateController, TimeoutController  # noqa: E402
from detection_parser import parse_batch_meta  # noqa: E402
from fps_overlay import FpsMeter, attach_fps_overlay  # noqa: E402
from metrics import MetricsCollector  # noqa: E402
from output_writer import OutputWriter, make_writer  # noqa: E402
from pipeline_builder import attach_detection_probe, build_pipeline  # noqa: E402


# --------------------------------------------------------------------------- #
# Config loading + normalization
# --------------------------------------------------------------------------- #
def load_config(path: str, overrides: Dict = None) -> Dict:
    """Load and normalize the YAML config (with optional CLI overrides).

    Normalizes the ``cameras`` list so every entry is a dict with source_type,
    format, width, height, fps and either a ``device`` (v4l2) or a ``file`` (replay
    path). Also fills the ``source`` / ``timeout`` / ``context`` / ``control``
    sections with defaults, and resolves config-relative paths to absolute.

    Args:
        path: path to camera_params.yaml.
        overrides: optional flat dict of CLI overrides (source, replay_dir,
            timeout_policy, timeout_us, context_type, control_ms) merged in before
            normalization.

    Returns:
        The normalized config dict.

    Raises:
        RuntimeError: if the file is missing, malformed, or has no cameras.
    """
    if not os.path.isfile(path):
        raise RuntimeError(f"Config file not found: {path}")

    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    if not isinstance(cfg, dict):
        raise RuntimeError(f"Config file {path} is not a valid YAML mapping.")

    _apply_overrides(cfg, overrides or {})

    capture = cfg.get("capture") or {}
    defaults = {
        "format": str(capture.get("format", "mjpeg")).lower(),
        "width": int(capture.get("width", 1280)),
        "height": int(capture.get("height", 720)),
        "fps": int(capture.get("fps", 30)),
        "mjpeg_decoder": str(capture.get("mjpeg_decoder", "nvjpegdec")),
    }
    cfg["capture"] = dict(defaults)

    # Source: live v4l2 cameras (default) or deterministic file replay.
    source = cfg.get("source") or {}
    source_type = str(source.get("type", "v4l2")).lower()
    replay_dir = str(source.get("replay_dir", "experiments/clips"))
    cfg["source"] = {"type": source_type, "replay_dir": replay_dir}

    raw_cameras = cfg.get("cameras")
    if not raw_cameras:
        raise RuntimeError(f"Config file {path} has no 'cameras' configured.")

    normalized: List[Dict] = []
    for i, entry in enumerate(raw_cameras):
        if isinstance(entry, str):
            entry = {"device": entry}
        if not isinstance(entry, dict):
            raise RuntimeError(f"cameras[{i}] must be a string or mapping; got: {entry!r}")
        cam = {
            "source_type": source_type,
            "format": str(entry.get("format", defaults["format"])).lower(),
            "width": int(entry.get("width", defaults["width"])),
            "height": int(entry.get("height", defaults["height"])),
            "fps": int(entry.get("fps", defaults["fps"])),
            "mjpeg_decoder": str(entry.get("mjpeg_decoder", defaults["mjpeg_decoder"])),
            "device": entry.get("device"),
            "file": entry.get("file"),  # resolved below for file sources
        }
        if source_type == "v4l2" and not cam["device"]:
            raise RuntimeError(f"cameras[{i}] needs a 'device' path for v4l2 source.")
        normalized.append(cam)
    cfg["cameras"] = normalized

    # Resolve model/tracker config paths relative to the config file's directory.
    cfg_dir = os.path.dirname(os.path.abspath(path))
    cfg.setdefault("pgie", {})
    cfg["pgie"]["config_file"] = _resolve(cfg_dir, cfg["pgie"].get("config_file", "config/pgie_config.txt"))
    tcfg = cfg.setdefault("tracker", {})
    tcfg.setdefault("ll_lib_file", "/opt/nvidia/deepstream/deepstream/lib/libnvds_nvmultiobjecttracker.so")
    tcfg["ll_config_file"] = _resolve(cfg_dir, tcfg.get("ll_config_file", "config/tracker_config.yml"))

    # For file replay, resolve each camera's clip (default <replay_dir>/cam{i}.mp4).
    if source_type == "file":
        for i, cam in enumerate(cfg["cameras"]):
            clip = cam.get("file") or os.path.join(replay_dir, f"cam{i}.mp4")
            cam["file"] = _resolve(cfg_dir, clip)

    # RT experiment sections: dynamic timeout, context-aware skipping, control rate.
    tmo = cfg.get("timeout") or {}
    smux = cfg.get("streammux") or {}
    cfg["timeout"] = {
        "policy": str(tmo.get("policy", "fixed")).lower(),
        "base_us": int(tmo.get("base_us", smux.get("batched_push_timeout_us", 33333))),
        "min_us": int(tmo.get("min_us", 5000)),
        "max_us": int(tmo.get("max_us", 100000)),
    }
    cfg["context"] = cfg.get("context") or {"type": "all"}
    cfg["context"].setdefault("type", "all")
    b = cfg.get("batch") or {}
    cfg["batch"] = {"policy": str(b.get("policy", "fixed")).lower()}
    ctrl = cfg.get("control") or {}
    cfg["control"] = {"tick_ms": int(ctrl.get("tick_ms", 500))}
    return cfg


def _apply_overrides(cfg: Dict, ov: Dict) -> None:
    """Merge flat CLI overrides into the (pre-normalization) config sections."""
    if ov.get("source"):
        cfg.setdefault("source", {})["type"] = ov["source"]
    if ov.get("replay_dir"):
        cfg.setdefault("source", {})["replay_dir"] = ov["replay_dir"]
    if ov.get("timeout_policy"):
        cfg.setdefault("timeout", {})["policy"] = ov["timeout_policy"]
    if ov.get("timeout_us") is not None:
        cfg.setdefault("timeout", {})["base_us"] = ov["timeout_us"]
    if ov.get("context_type"):
        cfg.setdefault("context", {})["type"] = ov["context_type"]
    if ov.get("batch_policy"):
        cfg.setdefault("batch", {})["policy"] = ov["batch_policy"]
    if ov.get("control_ms") is not None:
        cfg.setdefault("control", {})["tick_ms"] = ov["control_ms"]


def _resolve(base_dir: str, path: str) -> str:
    """Resolve ``path`` to absolute, treating relative paths as relative to base_dir."""
    if os.path.isabs(path):
        return path
    # config paths in the yaml are written relative to the project root (parent of config/).
    project_root = os.path.dirname(base_dir)
    return os.path.normpath(os.path.join(project_root, path))


def validate_cameras(cameras: List[Dict]) -> None:
    """Fail fast (before touching GStreamer) if any camera device is absent.

    Args:
        cameras: normalized camera list.

    Raises:
        RuntimeError: listing every missing device path, rather than letting the
            pipeline crash opaquely deep inside v4l2src.
    """
    # File-replay sources: check the clips exist.
    file_missing = [
        c.get("file") or "<unset replay clip>"
        for c in cameras
        if c.get("source_type") == "file" and not (c.get("file") and os.path.isfile(c["file"]))
    ]
    if file_missing:
        raise RuntimeError(
            "Replay clip(s) not found: " + ", ".join(file_missing)
            + ".\nRecord them first: python3 scripts/record_replay_clips.py"
        )

    # Live v4l2 sources: check the devices exist.
    dev_missing = [
        c["device"] for c in cameras if c.get("source_type") == "v4l2" and not os.path.exists(c["device"])
    ]
    if dev_missing:
        available = sorted(
            os.path.join("/dev", d) for d in os.listdir("/dev") if d.startswith("video")
        )
        raise RuntimeError(
            "Configured camera device(s) not found: "
            + ", ".join(dev_missing)
            + ".\nDevices present: "
            + (", ".join(available) if available else "(none)")
            + "\nCheck the cameras are plugged in and the 'cameras' list in the config."
        )


# --------------------------------------------------------------------------- #
# Pad probe
# --------------------------------------------------------------------------- #
def make_probe(writer: OutputWriter, meter: FpsMeter = None, context=None):
    """Build the nvtracker src-pad probe callback bound to an output writer.

    The probe runs on every batched buffer leaving nvtracker. It retrieves the
    NvDsBatchMeta, parses it into per-camera detections (with track IDs), and
    forwards them to the writer. If a ``FpsMeter`` is given, it ticks it once per
    camera per frame (feeds the FPS overlay). If a ``ContextProvider`` is given,
    it reports per-camera detection activity (feeds context-aware skipping).
    Returns OK so the buffer continues downstream.
    """

    def probe(pad: Gst.Pad, info: Gst.PadProbeInfo, _user_data) -> Gst.PadProbeReturn:
        gst_buffer = info.get_buffer()
        if gst_buffer is None:
            return Gst.PadProbeReturn.OK
        # pyds needs the C buffer address; hash(gst_buffer) yields it.
        batch_meta = pyds.gst_buffer_get_nvds_batch_meta(hash(gst_buffer))
        if batch_meta is None:
            return Gst.PadProbeReturn.OK
        frames = parse_batch_meta(batch_meta)
        for frame in frames:
            if meter is not None:
                meter.tick(frame.camera_id)
            if context is not None:
                context.note_detections(frame.camera_id, len(frame.detections))
        writer.write_batch(frames)
        return Gst.PadProbeReturn.OK

    return probe


# --------------------------------------------------------------------------- #
# Bus handling
# --------------------------------------------------------------------------- #
def _on_bus_message(bus: Gst.Bus, message: Gst.Message, loop: GLib.MainLoop) -> bool:
    """Handle EOS / ERROR / WARNING on the pipeline bus; quit the loop on end."""
    t = message.type
    if t == Gst.MessageType.EOS:
        print("[main] End-of-stream — shutting down.", file=sys.stderr)
        loop.quit()
    elif t == Gst.MessageType.ERROR:
        err, debug = message.parse_error()
        print(f"[main] ERROR from {message.src.name}: {err}", file=sys.stderr)
        if debug:
            print(f"[main] debug: {debug}", file=sys.stderr)
        loop.quit()
    elif t == Gst.MessageType.WARNING:
        warn, debug = message.parse_warning()
        print(f"[main] WARNING from {message.src.name}: {warn}", file=sys.stderr)
    return True


# --------------------------------------------------------------------------- #
# Entry
# --------------------------------------------------------------------------- #
def run(
    config_path: str,
    log_mode: str = "json",
    display: bool = False,
    record_path: str = None,
    overrides: Dict = None,
    metrics_csv: str = None,
    duration_s: float = None,
) -> int:
    """Load config, build the pipeline, wire the RT controllers, and run.

    Args:
        config_path: path to camera_params.yaml.
        log_mode: console output style — "json" | "human" | "none".
        display: show a live tiled window with bounding boxes (debug).
        record_path: if set, also write the annotated view to this MP4 file.
        overrides: CLI overrides merged into the config (source/timeout/context/...).
        metrics_csv: if set, write per-batch latency/throughput metrics here.
        duration_s: if set, stop cleanly after this many seconds (for benchmarks).

    Returns:
        Process exit code (0 on clean EOS/interrupt, non-zero on error).
    """
    cfg = load_config(config_path, overrides=overrides)
    validate_cameras(cfg["cameras"])

    n = len(cfg["cameras"])
    extras = []
    if display:
        extras.append("display window")
    if record_path:
        extras.append(f"recording -> {record_path}")
    if metrics_csv:
        extras.append(f"metrics -> {metrics_csv}")
    print(
        f"[main] {n} camera(s) [{cfg['source']['type']}] "
        f"{cfg['capture']['width']}x{cfg['capture']['height']}@{cfg['capture']['fps']} "
        f"({cfg['capture']['format']}); timeout={cfg['timeout']['policy']}"
        f"({cfg['timeout']['base_us']}us) batch={cfg['batch']['policy']} "
        f"context={cfg['context']['type']} log={log_mode}"
        + (f"; {', '.join(extras)}" if extras else "")
        + ".",
        file=sys.stderr,
    )

    Gst.init(None)

    pipeline, tracker = build_pipeline(cfg, display=display, record_path=record_path)
    mux = pipeline.get_by_name("stream-muxer")

    out_cfg = cfg.get("output", {}) or {}
    writer = make_writer(
        log_mode,
        only_nonempty=bool(out_cfg.get("only_nonempty", False)),
        pretty=bool(out_cfg.get("pretty", False)),
        interval=float(out_cfg.get("log_interval_s", 1.0)),
    )

    # Context-aware camera selection + optional experiment metrics.
    context = make_context(cfg["context"], n)
    metrics = MetricsCollector(metrics_csv, n) if metrics_csv else None

    # FPS overlay (only with a visual output). The detection probe feeds the
    # meter AND the context provider (per-camera activity).
    fps_meter = FpsMeter() if (display or record_path) else None
    attach_detection_probe(tracker, make_probe(writer, fps_meter, context))
    if fps_meter is not None:
        tiler = pipeline.get_by_name("tiler")
        if tiler is not None:
            attach_fps_overlay(tiler, n, fps_meter)
    if metrics is not None:
        metrics.attach(pipeline)

    # Runtime controllers: camera gate (skip) + dynamic batched-push-timeout,
    # re-evaluated every control tick.
    gate = CameraGateController(pipeline, n, context, metrics)
    batch_ctl = BatchController(
        mux, policy=cfg["batch"]["policy"], gate=gate, num_cams=n, metrics=metrics,
    )
    timeout_ctl = TimeoutController(
        mux,
        policy=cfg["timeout"]["policy"],
        base_us=cfg["timeout"]["base_us"],
        min_us=cfg["timeout"]["min_us"],
        max_us=cfg["timeout"]["max_us"],
        gate=gate,
        num_cams=n,
        metrics=metrics,
    )

    def _control_tick() -> bool:
        gate.tick()          # 1. apply active-camera set to the valves
        batch_ctl.tick()     # 2. size the batch to the active count (push without waiting)
        timeout_ctl.tick()   # 3. adapt the timeout (a backstop once batch-size is adaptive)
        return True          # keep firing

    control_id = GLib.timeout_add(int(cfg["control"]["tick_ms"]), _control_tick)

    loop = GLib.MainLoop()
    bus = pipeline.get_bus()
    bus.add_signal_watch()
    bus.connect("message", _on_bus_message, loop)

    # Optional fixed-duration run for benchmarks: EOS after N seconds so any
    # recording/metrics finalize cleanly, then the bus EOS quits the loop.
    if duration_s:
        def _stop() -> bool:
            print(f"[main] duration {duration_s}s elapsed — stopping.", file=sys.stderr)
            pipeline.send_event(Gst.Event.new_eos())
            return False
        GLib.timeout_add(int(float(duration_s) * 1000), _stop)

    ret = pipeline.set_state(Gst.State.PLAYING)
    if ret == Gst.StateChangeReturn.FAILURE:
        print("[main] Failed to set pipeline to PLAYING.", file=sys.stderr)
        pipeline.set_state(Gst.State.NULL)
        return 1

    print(
        "[main] Running. First launch may build the TensorRT engine (several "
        "minutes). Press Ctrl-C to stop.",
        file=sys.stderr,
    )
    exit_code = 0
    try:
        loop.run()
    except KeyboardInterrupt:
        print("\n[main] Interrupted — shutting down.", file=sys.stderr)
        # EOS lets the muxer finalize the MP4 (moov atom) before teardown.
        pipeline.send_event(Gst.Event.new_eos())
        bus.remove_signal_watch()
        bus.timed_pop_filtered(8 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
    finally:
        if control_id:
            GLib.source_remove(control_id)
        pipeline.set_state(Gst.State.NULL)
        writer.close()
        if metrics is not None:
            metrics.close()
    return exit_code


def parse_args(argv: List[str]) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Multi-camera DeepStream YOLO11n detection + tracking (Jetson)."
    )
    parser.add_argument(
        "--config",
        default="config/camera_params.yaml",
        help="Path to the camera/pipeline YAML config (default: config/camera_params.yaml).",
    )
    parser.add_argument(
        "--display",
        action="store_true",
        help="Show a live window tiling all cameras with bounding boxes + track IDs.",
    )
    parser.add_argument(
        "--record",
        metavar="PATH",
        default=None,
        help="Also write the annotated, tiled view to this H.264 MP4 file.",
    )
    parser.add_argument(
        "--log",
        choices=["json", "human", "none"],
        default=None,
        help="Console output: json (default), human (readable per-camera), or none.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Shorthand for --display with a human-readable per-camera log.",
    )
    # --- RT experiment knobs (override the config) ---------------------------
    parser.add_argument(
        "--source", choices=["v4l2", "file"], default=None,
        help="Input source: v4l2 live cameras or deterministic 'file' replay.",
    )
    parser.add_argument(
        "--replay-dir", metavar="DIR", default=None,
        help="Directory of per-camera replay clips (cam0.mp4 ...) for --source file.",
    )
    parser.add_argument(
        "--timeout-policy", choices=["fixed", "adaptive"], default=None,
        help="nvstreammux batched-push-timeout policy (fixed baseline vs adaptive).",
    )
    parser.add_argument(
        "--timeout-us", type=int, default=None,
        help="Base batched-push-timeout in microseconds (default 33333 ~= 1/30s).",
    )
    parser.add_argument(
        "--context", choices=["all", "activity", "scheduled"], default=None,
        help="Context-aware camera selection: all (baseline), activity, scheduled.",
    )
    parser.add_argument(
        "--batch-policy", choices=["fixed", "adaptive"], default=None,
        help="nvstreammux batch-size: fixed (=camera count) or adaptive (=active count, "
             "so a skipped-camera batch pushes immediately with no timeout wait).",
    )
    parser.add_argument(
        "--control-ms", type=int, default=None,
        help="How often (ms) the timeout/gate controllers re-evaluate (default 500).",
    )
    parser.add_argument(
        "--metrics-csv", metavar="PATH", default=None,
        help="Write per-batch latency/throughput metrics to this CSV (for experiments).",
    )
    parser.add_argument(
        "--duration", type=float, default=None,
        help="Stop cleanly after this many seconds (for reproducible benchmark runs).",
    )
    return parser.parse_args(argv)


def main() -> int:
    """CLI wrapper: parse args, run, translate exceptions into clear exit codes."""
    args = parse_args(sys.argv[1:])

    # --debug turns on the window and defaults the console to the human log.
    display = args.display or args.debug
    log_mode = args.log if args.log is not None else ("human" if args.debug else "json")

    overrides = {
        "source": args.source,
        "replay_dir": args.replay_dir,
        "timeout_policy": args.timeout_policy,
        "timeout_us": args.timeout_us,
        "context_type": args.context,
        "batch_policy": args.batch_policy,
        "control_ms": args.control_ms,
    }

    # filesink/CSV parents won't be auto-created — do it so output "just works".
    for out in (args.record, args.metrics_csv):
        if out:
            os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)

    try:
        return run(
            args.config,
            log_mode=log_mode,
            display=display,
            record_path=args.record,
            overrides=overrides,
            metrics_csv=args.metrics_csv,
            duration_s=args.duration,
        )
    except RuntimeError as exc:
        # Configuration / device / pipeline-build errors: clear message, no traceback.
        print(f"[main] ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
