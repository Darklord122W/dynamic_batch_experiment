"""STATIC engine + SYNC-ON: partial batches come from dropped late frames.
3 live cameras -> mux(sync-inputs=1) -> nvinfer(STATIC-4 engine) -> fakesink.
Does the static engine handle sync-induced 1-2 frame batches?"""
import time
from collections import Counter, defaultdict
import gi; gi.require_version("Gst","1.0")
from gi.repository import GLib, Gst
Gst.init(None); import pyds
import sys; sys.path.insert(0, ".")
import pipeline_builder as pb

CONFIG="/home/darklord01/Documents/deepstream_batch/multicam_perception_rt/config/_pgie_static.txt"
devs=["/dev/video2","/dev/video4","/dev/video6"]
p=Gst.Pipeline.new("sync-static")
mux=Gst.ElementFactory.make("nvstreammux","stream-muxer")
for k,v in [("batch-size",4),("width",640),("height",480),("live-source",1),
            ("batched-push-timeout",33333),("sync-inputs",1),("max-latency",33333333)]:
    mux.set_property(k,v)
pgie=Gst.ElementFactory.make("nvinfer","pgie"); pgie.set_property("config-file-path",CONFIG); pgie.set_property("batch-size",4)
sink=Gst.ElementFactory.make("fakesink","sink"); sink.set_property("sync",0)
for e in (mux,pgie,sink): p.add(e)
mux.link(pgie); pgie.link(sink)
for i,d in enumerate(devs):
    cam={"source_type":"v4l2","device":d,"format":"mjpeg","width":640,"height":480,"fps":30,"mjpeg_decoder":"nvjpegdec"}
    sb=pb._build_source_bin(i,cam); p.add(sb)
    sb.get_static_pad("src").link(mux.request_pad_simple(f"sink_{i}"))
c=[]  # (num_frames, num_objs) at nvinfer src
def probe(pad,info,u):
    b=info.get_buffer(); bm=pyds.gst_buffer_get_nvds_batch_meta(hash(b)) if b else None
    if bm:
        n=0; l=bm.frame_meta_list
        while l:
            fm=pyds.NvDsFrameMeta.cast(l.data); o=fm.obj_meta_list
            while o: n+=1; o=o.next
            l=l.next
        c.append((bm.num_frames_in_batch, n))
    return Gst.PadProbeReturn.OK
pgie.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, probe, None)
loop=GLib.MainLoop(); errs=[]
def bus(b,m,l):
    if m.type==Gst.MessageType.ERROR: e,dg=m.parse_error(); errs.append(f"{e}|{dg}"); l.quit()
    return True
bs=p.get_bus(); bs.add_signal_watch(); bs.connect("message",bus,loop)
GLib.timeout_add(10000, lambda: loop.quit() or False)
p.set_state(Gst.State.PLAYING); loop.run(); p.set_state(Gst.State.NULL)
c=c[20:]  # skip warmup
print("  NVINFER src under sync-on: num_frames_in_batch dist =", dict(sorted(Counter(x[0] for x in c).items())))
print(f"  buffers out of nvinfer: {len(c)} | total detections: {sum(x[1] for x in c)}")
print("  pipeline errors:", errs if errs else "(none)")
