#!/usr/bin/env python3
"""record_replay_clips.py — record short per-camera clips for reproducible replay.

Experiments must compare policies on IDENTICAL input; live cameras aren't
reproducible. This records each camera's capture front to an H.264 MP4 at
<out_dir>/cam{i}.mp4. Then run any experiment with ``--source file`` (or set
``source.type: file`` in the config) to replay those exact frames every time.

Usage:
    python3 scripts/record_replay_clips.py --duration 30 --out-dir experiments/clips
"""
from __future__ import annotations

import argparse
import os
import sys

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

# Import the app's config loader + the (validated) capture-front builders.
_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _HERE)
import main as app          # noqa: E402
import pipeline_builder as pb  # noqa: E402


def build_record_pipeline(cfg: dict, out_dir: str) -> Gst.Pipeline:
    """Build one pipeline with a capture->H.264->MP4 branch per camera."""
    Gst.init(None)
    pipeline = Gst.Pipeline.new("record-clips")
    for i, cam in enumerate(cfg["cameras"]):
        source_bin = pb._build_source_bin(i, cam)  # capture -> NVMM NV12 -> valve -> ghost
        enc = Gst.ElementFactory.make("nvv4l2h264enc", f"enc-{i}")
        parse = Gst.ElementFactory.make("h264parse", f"parse-{i}")
        mux = Gst.ElementFactory.make("qtmux", f"mux-{i}")
        sink = Gst.ElementFactory.make("filesink", f"filesink-{i}")
        if None in (enc, parse, mux, sink):
            raise RuntimeError("record: missing an encoder/parser/mux/filesink element.")
        sink.set_property("location", os.path.join(out_dir, f"cam{i}.mp4"))
        sink.set_property("sync", 0)
        for el in (source_bin, enc, parse, mux, sink):
            pipeline.add(el)
        if not source_bin.link(enc):
            raise RuntimeError(f"record: source-bin-{i} -> encoder link failed.")
        for a, b in ((enc, parse), (parse, mux), (mux, sink)):
            if not a.link(b):
                raise RuntimeError(f"record: {a.name} -> {b.name} link failed.")
    return pipeline


def main() -> int:
    ap = argparse.ArgumentParser(description="Record per-camera replay clips.")
    ap.add_argument("--config", default="config/camera_params.yaml")
    ap.add_argument("--out-dir", default="experiments/clips")
    ap.add_argument("--duration", type=float, default=30.0, help="seconds to record")
    args = ap.parse_args()

    # Recording must come from live cameras.
    cfg = app.load_config(args.config, overrides={"source": "v4l2"})
    app.validate_cameras(cfg["cameras"])
    out_dir = os.path.abspath(args.out_dir)
    os.makedirs(out_dir, exist_ok=True)
    n = len(cfg["cameras"])
    print(f"[record] {n} camera(s) -> {out_dir}/cam0..{n-1}.mp4 for {args.duration}s", file=sys.stderr)

    pipeline = build_record_pipeline(cfg, out_dir)
    loop = GLib.MainLoop()
    bus = pipeline.get_bus()
    bus.add_signal_watch()

    def on_msg(_bus, msg, _loop):
        if msg.type == Gst.MessageType.EOS:
            loop.quit()
        elif msg.type == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            print(f"[record] ERROR: {err} | {dbg}", file=sys.stderr)
            loop.quit()
        return True

    bus.connect("message", on_msg, loop)

    def _stop():
        print("[record] stopping (EOS to finalize MP4s)...", file=sys.stderr)
        pipeline.send_event(Gst.Event.new_eos())
        return False

    GLib.timeout_add(int(args.duration * 1000), _stop)
    pipeline.set_state(Gst.State.PLAYING)
    try:
        loop.run()
    except KeyboardInterrupt:
        pipeline.send_event(Gst.Event.new_eos())
        bus.timed_pop_filtered(8 * Gst.SECOND, Gst.MessageType.EOS)
    finally:
        pipeline.set_state(Gst.State.NULL)
    print(f"[record] done. Replay with: --source file --replay-dir {args.out_dir}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
