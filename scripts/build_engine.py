#!/usr/bin/env python3
"""build_engine.py — pre-build the TensorRT engine WITHOUT needing cameras.

nvinfer builds the engine from the ONNX on first use, which can take 10+ minutes.
Running that build the first time you plug in cameras is awkward, so this helper
triggers it up front by pushing a few synthetic frames (videotestsrc) through the
exact same nvinfer config. The resulting engine is serialized to the path in
pgie_config.txt (models/model_b4_gpu0_fp16.engine) and reused by main.py.

Because the ONNX was exported with dynamic batch (max 4), the single engine built
here serves any camera count 1..4.

Usage:
    python3 scripts/build_engine.py                 # batch 4 (default)
    python3 scripts/build_engine.py --batch 2 --config config/pgie_config.txt
"""
from __future__ import annotations

import argparse
import os
import sys

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402


def build(config_path: str, batch: int) -> int:
    """Run videotestsrc → nvstreammux → nvinfer → fakesink to force an engine build."""
    Gst.init(None)
    config_abs = os.path.abspath(config_path)
    if not os.path.isfile(config_abs):
        print(f"[build_engine] config not found: {config_abs}", file=sys.stderr)
        return 2

    pipeline = Gst.Pipeline.new("build-engine")
    mux = Gst.ElementFactory.make("nvstreammux", "mux")
    mux.set_property("batch-size", batch)
    mux.set_property("width", 640)
    mux.set_property("height", 480)
    mux.set_property("batched-push-timeout", 33333)
    pgie = Gst.ElementFactory.make("nvinfer", "pgie")
    pgie.set_property("config-file-path", config_abs)
    pgie.set_property("batch-size", batch)
    sink = Gst.ElementFactory.make("fakesink", "sink")
    sink.set_property("sync", 0)
    for e in (mux, pgie, sink):
        pipeline.add(e)
    mux.link(pgie)
    pgie.link(sink)

    for i in range(batch):
        src = Gst.ElementFactory.make("videotestsrc", f"src{i}")
        src.set_property("num-buffers", 10)
        src.set_property("is-live", 0)
        cf = Gst.ElementFactory.make("capsfilter", f"cf{i}")
        cf.set_property("caps", Gst.Caps.from_string("video/x-raw,width=640,height=480,framerate=30/1"))
        conv = Gst.ElementFactory.make("nvvideoconvert", f"conv{i}")
        ncf = Gst.ElementFactory.make("capsfilter", f"ncf{i}")
        ncf.set_property("caps", Gst.Caps.from_string("video/x-raw(memory:NVMM),format=NV12"))
        for e in (src, cf, conv, ncf):
            pipeline.add(e)
        src.link(cf)
        cf.link(conv)
        conv.link(ncf)
        req = getattr(mux, "request_pad_simple", None) or mux.get_request_pad
        ncf.get_static_pad("src").link(req(f"sink_{i}"))

    loop = GLib.MainLoop()
    status = {"error": None}

    def on_msg(_bus, msg, _loop):
        if msg.type == Gst.MessageType.EOS:
            loop.quit()
        elif msg.type == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            status["error"] = f"{err} | {dbg}"
            loop.quit()
        return True

    bus = pipeline.get_bus()
    bus.add_signal_watch()
    bus.connect("message", on_msg, loop)

    print(f"[build_engine] building engine (batch={batch}); first build can take 10+ min ...",
          flush=True)
    pipeline.set_state(Gst.State.PLAYING)
    loop.run()
    pipeline.set_state(Gst.State.NULL)
    if status["error"]:
        print(f"[build_engine] ERROR: {status['error']}", file=sys.stderr)
        return 1
    print("[build_engine] done — engine is cached; main.py will load it instantly.")
    return 0


def main() -> int:
    """Parse args and run the engine build."""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    p = argparse.ArgumentParser(description="Pre-build the YOLO11n TensorRT engine (no cameras needed).")
    p.add_argument("--config", default=os.path.join(here, "config", "pgie_config.txt"))
    p.add_argument("--batch", type=int, default=4, help="max batch to build for (default 4)")
    args = p.parse_args()
    return build(args.config, args.batch)


if __name__ == "__main__":
    raise SystemExit(main())
