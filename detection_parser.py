"""detection_parser.py

Turns DeepStream's on-buffer metadata into plain Python ``Detection`` objects.

Why this module exists
----------------------
DeepStream does not hand detections back as a normal buffer payload. After
``nvinfer`` (PGIE) and ``nvtracker`` run, each result is attached to the
GStreamer buffer as ``NvDsBatchMeta`` metadata. The only way to read it from
Python is a *pad probe* (see ``pipeline_builder.attach_detection_probe``) that
receives the buffer and walks the metadata tree with ``pyds``.

The metadata is nested because up to 4 cameras are batched together:

    NvDsBatchMeta
     └─ frame_meta_list          # ONE NvDsFrameMeta per camera in this batch
         ├─ source_id            # which camera this frame came from
         ├─ frame_num            # frame index for that camera
         └─ obj_meta_list        # ONE NvDsObjectMeta per tracked object
             ├─ obj_label        # class-name string (from the PGIE labels file)
             ├─ class_id
             ├─ confidence
             ├─ object_id        # persistent track ID assigned by nvtracker
             └─ rect_params      # .left/.top/.width/.height, pixels

``frame_meta.source_id`` maps a detection back to the right camera; ``object_id``
is the tracker's persistent ID (stable while the tracker keeps matching it — a
lightweight tracker may reassign an ID after a long occlusion, which is expected).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Dict, List

import pyds


@dataclass
class Detection:
    """A single tracked detection in one camera's frame.

    All bounding-box values are in pixels, in the coordinate space of
    ``nvstreammux``'s ``width``/``height`` — which this project sets equal to the
    camera capture resolution, so the numbers are directly meaningful in the
    original image (no rescaling needed).
    """

    camera_id: int      # nvstreammux source_id == the camera's index in camera_params.yaml
    track_id: int       # nvtracker persistent object_id
    class_name: str     # human-readable label, e.g. "person"
    confidence: float   # detector confidence in [0, 1]
    x: float            # bbox top-left x, pixels
    y: float            # bbox top-left y, pixels
    width: float        # bbox width, pixels
    height: float       # bbox height, pixels

    def as_dict(self) -> Dict:
        """Return a JSON-serializable dict of this detection."""
        return asdict(self)


@dataclass
class FrameDetections:
    """All detections for ONE camera in ONE processed frame.

    This is the natural unit of output: the spec asks for one structured record
    per camera per frame.
    """

    camera_id: int
    frame_num: int
    detections: List[Detection]
    buf_pts: int = 0   # frame PTS (ns); used to correlate with source-side timing

    def as_dict(self) -> Dict:
        """Return a JSON-serializable dict: camera + frame + list of detections."""
        return {
            "camera_id": self.camera_id,
            "frame_num": self.frame_num,
            "num_detections": len(self.detections),
            "detections": [d.as_dict() for d in self.detections],
        }


def parse_batch_meta(batch_meta: "pyds.NvDsBatchMeta") -> List[FrameDetections]:
    """Walk an ``NvDsBatchMeta`` and return one ``FrameDetections`` per camera.

    Args:
        batch_meta: the batch metadata retrieved inside a pad probe via
            ``pyds.gst_buffer_get_nvds_batch_meta(hash(gst_buffer))``.

    Returns:
        A list with one ``FrameDetections`` entry per camera present in this
        batch (typically the full camera count). Cameras with no objects still
        produce an entry with an empty ``detections`` list.

    Notes:
        The ``frame_meta_list`` / ``obj_meta_list`` are C GList linked lists.
        ``pyds.NvDsFrameMeta.cast`` / ``pyds.NvDsObjectMeta.cast`` reinterpret
        each node's ``.data`` pointer as the right struct; we advance with
        ``.next`` until the list ends (``None``).
    """
    frames: List[FrameDetections] = []

    l_frame = batch_meta.frame_meta_list
    while l_frame is not None:
        # cast() may raise StopIteration on a malformed/last node; guard it.
        try:
            frame_meta = pyds.NvDsFrameMeta.cast(l_frame.data)
        except StopIteration:
            break

        camera_id = int(frame_meta.source_id)
        frame_num = int(frame_meta.frame_num)
        buf_pts = int(frame_meta.buf_pts)  # frame PTS, for source-side latency correlation
        detections: List[Detection] = []

        l_obj = frame_meta.obj_meta_list
        while l_obj is not None:
            try:
                obj_meta = pyds.NvDsObjectMeta.cast(l_obj.data)
            except StopIteration:
                break

            rect = obj_meta.rect_params
            detections.append(
                Detection(
                    camera_id=camera_id,
                    # object_id is uint64; nvtracker sets it to the persistent ID.
                    # UNTRACKED_OBJECT_ID (2**64-1) can appear before a track is
                    # confirmed — surface it as -1 so downstream code can spot it.
                    track_id=_normalize_track_id(obj_meta.object_id),
                    class_name=obj_meta.obj_label,
                    confidence=float(obj_meta.confidence),
                    x=float(rect.left),
                    y=float(rect.top),
                    width=float(rect.width),
                    height=float(rect.height),
                )
            )
            l_obj = l_obj.next

        frames.append(FrameDetections(camera_id, frame_num, detections, buf_pts=buf_pts))
        l_frame = l_frame.next

    return frames


# nvtracker uses this sentinel for objects not (yet) assigned a persistent ID.
_UNTRACKED_OBJECT_ID = (1 << 64) - 1


def _normalize_track_id(object_id: int) -> int:
    """Map the tracker's uint64 ``object_id`` to a friendly int.

    Returns -1 for the ``UNTRACKED_OBJECT_ID`` sentinel, otherwise the ID as-is.
    """
    oid = int(object_id)
    return -1 if oid == _UNTRACKED_OBJECT_ID else oid
