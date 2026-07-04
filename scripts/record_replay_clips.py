#!/usr/bin/env python3
"""record_replay_clips.py — record per-camera clips for reproducible replay.

Experiments must compare policies on IDENTICAL input; live cameras aren't
reproducible. This records each camera's *raw* capture front to an H.264 MP4 at
<out_dir>/cam{i}.mp4. Replay them with ``--source file`` to feed identical frames
into any experiment.

The clips are always RAW (no overlays baked in) so replaying + inferring fresh is
meaningful. ``--display`` additionally shows a live, tiled detection view *while*
recording (inference runs in parallel, tee'd off the sources — it is NOT written
into the files).

Stopping:
  * ``--duration N``  : stop automatically after N seconds.
  * (no --duration)   : record until you press ENTER (or Ctrl-C).

Usage:
    python3 scripts/record_replay_clips.py                      # press ENTER to stop
    python3 scripts/record_replay_clips.py --duration 40        # fixed length
    python3 scripts/record_replay_clips.py --display            # watch inference live
"""
from __future__ import annotations

import argparse
import os
import sys

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _HERE)
import main as app          # noqa: E402
import pipeline_builder as pb  # noqa: E402


def _tee_link(tee: Gst.Element, dest: Gst.Element) -> None:
    """Request a src pad from a tee and link it to dest's sink pad."""
    request = getattr(tee, "request_pad_simple", None) or tee.get_request_pad
    srcpad = request("src_%u")
    if srcpad.link(dest.get_static_pad("sink")) != Gst.PadLinkReturn.OK:
        raise RuntimeError(f"record: tee -> {dest.name} link failed.")


def _add_record_branch(pipeline: Gst.Pipeline, index: int, out_dir: str) -> Gst.Element:
    """Build a queue->H264enc->parse->qtmux->filesink branch; return the head queue."""
    q = pb._make("queue", f"recq-{index}")
    enc = pb._make("nvv4l2h264enc", f"enc-{index}")
    parse = pb._make("h264parse", f"parse-{index}")
    qmux = pb._make("qtmux", f"recmux-{index}")
    sink = pb._make("filesink", f"filesink-{index}")
    sink.set_property("location", os.path.join(out_dir, f"cam{index}.mp4"))
    sink.set_property("sync", 0)
    # async=false so this non-live sink doesn't block the PAUSED->PLAYING preroll
    # of the live (v4l2) pipeline (otherwise it deadlocks in PAUSED).
    sink.set_property("async", 0)
    for el in (q, enc, parse, qmux, sink):
        pipeline.add(el)
    pb._link_chain([q, enc, parse, qmux, sink])
    return q


def _build_display_tail(pipeline: Gst.Pipeline, cfg: dict, n: int) -> Gst.Element:
    """Build mux->pgie->tracker->tiler->osd->nv3dsink; return the nvstreammux."""
    mux = pb._build_streammux(cfg, n)
    pgie = pb._build_pgie(cfg, n)
    tracker = pb._build_tracker(cfg)
    rows, cols = pb._tiler_grid(n)
    dcfg = cfg.get("display", {}) or {}
    tiler = pb._make("nvmultistreamtiler", "tiler")
    tiler.set_property("rows", rows)
    tiler.set_property("columns", cols)
    tiler.set_property("width", int(dcfg.get("width", 1280)))
    tiler.set_property("height", int(dcfg.get("height", 720)))
    osdconv = pb._make("nvvideoconvert", "osd-conv")
    osdcaps = pb._make("capsfilter", "osd-caps")
    osdcaps.set_property("caps", Gst.Caps.from_string("video/x-raw(memory:NVMM),format=RGBA"))
    osd = pb._make("nvdsosd", "osd")
    osd.set_property("process-mode", 1)
    osd.set_property("display-bbox", 1)
    osd.set_property("display-text", 1)
    sink = pb._make("nv3dsink", "disp-sink")
    sink.set_property("sync", 0)
    sink.set_property("async", 0)  # don't block the live pipeline's preroll
    for el in (mux, pgie, tracker, tiler, osdconv, osdcaps, osd, sink):
        pipeline.add(el)
    pb._link_chain([mux, pgie, tracker, tiler, osdconv, osdcaps, osd, sink])
    return mux


def build_record_pipeline(cfg: dict, out_dir: str, display: bool) -> Gst.Pipeline:
    """Build the record pipeline (raw per-camera clips, + optional live inference)."""
    Gst.init(None)
    pipeline = Gst.Pipeline.new("record-clips")
    n = len(cfg["cameras"])
    mux = _build_display_tail(pipeline, cfg, n) if display else None

    for i, cam in enumerate(cfg["cameras"]):
        source_bin = pb._build_source_bin(i, cam)   # capture -> NVMM NV12 -> valve -> ghost
        pipeline.add(source_bin)
        rec_head = _add_record_branch(pipeline, i, out_dir)

        if display:
            # tee the source: one branch records raw, one runs inference for display.
            tee = pb._make("tee", f"tee-{i}")
            infq = pb._make("queue", f"infq-{i}")
            # LEAKY: the display branch may drop frames but must NEVER back-pressure
            # the tee — otherwise it stalls the recording branch and blocks EOS
            # (qtmux then never finalizes and the clip is 0 bytes).
            infq.set_property("leaky", 2)            # 2 = drop old (downstream)
            infq.set_property("max-size-buffers", 5)
            infq.set_property("max-size-time", 0)
            infq.set_property("max-size-bytes", 0)
            pipeline.add(tee)
            pipeline.add(infq)
            if not source_bin.link(tee):
                raise RuntimeError(f"record: source-bin-{i} -> tee link failed.")
            _tee_link(tee, rec_head)   # -> recording branch
            _tee_link(tee, infq)       # -> inference branch
            sinkpad = pb._request_sink_pad(mux, i)
            if infq.get_static_pad("src").link(sinkpad) != Gst.PadLinkReturn.OK:
                raise RuntimeError(f"record: inference queue {i} -> mux link failed.")
        else:
            if not source_bin.link(rec_head):
                raise RuntimeError(f"record: source-bin-{i} -> recorder link failed.")
    return pipeline


def main() -> int:
    ap = argparse.ArgumentParser(description="Record per-camera replay clips.")
    ap.add_argument("--config", default="config/camera_params.yaml")
    ap.add_argument("--out-dir", default="experiments/clips")
    ap.add_argument("--duration", type=float, default=None,
                    help="seconds to record (omit to stop manually with ENTER/Ctrl-C)")
    ap.add_argument("--display", action="store_true",
                    help="show a live tiled inference view while recording (not baked into clips)")
    args = ap.parse_args()

    cfg = app.load_config(args.config, overrides={"source": "v4l2"})  # recording is from live cams
    app.validate_cameras(cfg["cameras"])
    out_dir = os.path.abspath(args.out_dir)
    os.makedirs(out_dir, exist_ok=True)
    n = len(cfg["cameras"])
    stop_desc = f"{args.duration}s" if args.duration else "until ENTER/Ctrl-C"
    print(f"[record] {n} camera(s) -> {out_dir}/cam0..{n-1}.mp4 ({stop_desc}"
          f"{'; live inference view' if args.display else ''})", file=sys.stderr)

    pipeline = build_record_pipeline(cfg, out_dir, args.display)
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

    def stop(_reason: str) -> bool:
        print(f"[record] stopping ({_reason}) — EOS to finalize MP4s...", file=sys.stderr)
        pipeline.send_event(Gst.Event.new_eos())
        # Fallback: quit even if the (display-branch) bus EOS is slow/stalls; the
        # fragmented clips are already written, so we lose nothing by moving on.
        GLib.timeout_add(3000, loop.quit)
        return False

    # Choose the stop trigger: fixed duration, ENTER key, or (fallback) just signals.
    if args.duration:
        GLib.timeout_add(int(args.duration * 1000), lambda: stop("duration elapsed"))
    elif sys.stdin.isatty():
        print("[record] Recording... press ENTER to stop.", file=sys.stderr)

        def on_enter(_ch, _cond):
            try:
                sys.stdin.readline()
            except Exception:
                pass
            stop("ENTER pressed")
            return False

        ch = GLib.IOChannel.unix_new(sys.stdin.fileno())
        GLib.io_add_watch(ch, GLib.IOCondition.IN, on_enter)
    else:
        print("[record] No --duration and stdin is not a TTY — stop with Ctrl-C.", file=sys.stderr)

    pipeline.set_state(Gst.State.PLAYING)
    try:
        loop.run()
    except KeyboardInterrupt:
        stop("Ctrl-C")
        bus.timed_pop_filtered(8 * Gst.SECOND, Gst.MessageType.EOS)
    finally:
        pipeline.set_state(Gst.State.NULL)
    print(f"[record] done. Replay with: --source file --replay-dir {args.out_dir}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
