"""Test: how does a STATIC batch-4 TensorRT engine handle a partial batch?

4 videotestsrc -> valves -> nvstreammux(bs4) -> nvinfer(static-4 engine) -> fakesink.
Phase 1 (0-4s): all 4 active   -> batches of 4  (confirms the static engine loads/runs)
Phase 2 (4-7s): drop valve 3   -> batches of 3  (partial!)
Phase 3 (7-9s): drop valve 2   -> batches of 2  (more partial)

We log, per phase: what the MUX produced (num_frames_in_batch) and what came OUT of
nvinfer (did buffers keep flowing? object counts?), plus any pipeline/TRT errors.
"""
import time
from collections import Counter, defaultdict

import gi
gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst
Gst.init(None)
import pyds

CONFIG = "/home/darklord01/Documents/deepstream_batch/multicam_perception_rt/config/_pgie_static.txt"
N = 4
p = Gst.Pipeline.new("static-test")
mux = Gst.ElementFactory.make("nvstreammux", "mux")
for k, v in [("batch-size", N), ("width", 640), ("height", 480), ("live-source", 1),
             ("batched-push-timeout", 33333)]:
    mux.set_property(k, v)
pgie = Gst.ElementFactory.make("nvinfer", "pgie")
pgie.set_property("config-file-path", CONFIG)
pgie.set_property("batch-size", N)
sink = Gst.ElementFactory.make("fakesink", "sink")
sink.set_property("sync", 0)
for e in (mux, pgie, sink):
    p.add(e)
mux.link(pgie)
pgie.link(sink)
valves = []
for i in range(N):
    src = Gst.ElementFactory.make("videotestsrc", f"src{i}")
    src.set_property("is-live", 1)
    src.set_property("pattern", i)
    cf = Gst.ElementFactory.make("capsfilter", f"cf{i}")
    cf.set_property("caps", Gst.Caps.from_string("video/x-raw,width=640,height=480,framerate=30/1"))
    conv = Gst.ElementFactory.make("nvvideoconvert", f"cv{i}")
    ncf = Gst.ElementFactory.make("capsfilter", f"ncf{i}")
    ncf.set_property("caps", Gst.Caps.from_string("video/x-raw(memory:NVMM),format=NV12"))
    valve = Gst.ElementFactory.make("valve", f"v{i}")
    valve.set_property("drop-mode", 1)
    for e in (src, cf, conv, ncf, valve):
        p.add(e)
    src.link(cf); cf.link(conv); conv.link(ncf); ncf.link(valve)
    valve.get_static_pad("src").link(mux.request_pad_simple(f"sink_{i}"))
    valves.append(valve)

state = {"phase": "1_all4"}
mux_c, pgie_c = [], []

def probe_mux(pad, info, u):
    b = info.get_buffer()
    bm = pyds.gst_buffer_get_nvds_batch_meta(hash(b)) if b else None
    if bm:
        mux_c.append((state["phase"], bm.num_frames_in_batch))
    return Gst.PadProbeReturn.OK

def probe_pgie(pad, info, u):
    b = info.get_buffer()
    bm = pyds.gst_buffer_get_nvds_batch_meta(hash(b)) if b else None
    if bm:
        nobj = 0
        l = bm.frame_meta_list
        while l:
            fm = pyds.NvDsFrameMeta.cast(l.data)
            o = fm.obj_meta_list
            while o:
                nobj += 1
                o = o.next
            l = l.next
        pgie_c.append((state["phase"], bm.num_frames_in_batch, nobj))
    return Gst.PadProbeReturn.OK

mux.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, probe_mux, None)
pgie.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, probe_pgie, None)

loop = GLib.MainLoop()
errors = []
def bus_cb(bus, msg, loop):
    if msg.type == Gst.MessageType.ERROR:
        err, dbg = msg.parse_error()
        errors.append(f"{err} | {dbg}")
        loop.quit()
    return True
bus = p.get_bus(); bus.add_signal_watch(); bus.connect("message", bus_cb, loop)

t0 = time.monotonic()
def phase():
    dt = time.monotonic() - t0
    if 4.0 < dt < 4.1 and state["phase"] == "1_all4":
        print(">> t4: drop valve 3 -> 3-frame batches", flush=True)
        valves[3].set_property("drop", True); state["phase"] = "2_three"
    if 7.0 < dt < 7.1 and state["phase"] == "2_three":
        print(">> t7: drop valve 2 -> 2-frame batches", flush=True)
        valves[2].set_property("drop", True); state["phase"] = "3_two"
    if dt > 9:
        loop.quit(); return False
    return True
GLib.timeout_add(50, phase)
print(">> setting PLAYING (nvinfer will load the static engine) ...", flush=True)
p.set_state(Gst.State.PLAYING)
loop.run()
p.set_state(Gst.State.NULL)

def summ(counts, label, has_obj=False):
    byphase = defaultdict(list)
    for c in counts:
        byphase[c[0]].append(c)
    for ph in ("1_all4", "2_three", "3_two"):
        items = byphase[ph]
        if not items:
            print(f"  [{label}] {ph}: *** NO buffers ***")
            continue
        nf = dict(Counter(x[1] for x in items))
        extra = f", total_objs={sum(x[2] for x in items)}" if has_obj else ""
        print(f"  [{label}] {ph}: {len(items)} buffers, num_frames_in_batch={nf}{extra}")

print("\n===== MUX src (what the muxer produced) =====")
summ(mux_c, "mux")
print("===== NVINFER src (what came OUT of nvinfer) =====")
summ(pgie_c, "pgie", has_obj=True)
print("===== pipeline (bus) errors =====")
print("  " + ("\n  ".join(errors) if errors else "(none)"))
