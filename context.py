"""context.py — context-aware camera selection.

Decides, each control tick, which cameras should currently be *active* (processed)
vs. *skipped*. This is the local analogue of RT-BEV's "context filter" /
FlexibleTimeSync: instead of a driving command choosing the relevant cameras, we
pick a relevance signal appropriate to a generic multi-camera detector.

A ``ContextProvider`` returns a set of active camera ids. The
``CameraGateController`` (controllers.py) applies that set to the per-camera
valves, and the ``TimeoutController`` shrinks the batch timeout when fewer
cameras are active — so skipping cameras both cuts inference work and, with the
adaptive timeout, cuts the batch wait.

Providers:
  * ``AllActiveContext``   — baseline, everything on (equivalent to the original
                             pipeline). Use this to measure the "no-skip" case.
  * ``ScheduledContext``   — deterministic timeline of active-sets. For
                             reproducible experiments that isolate the effect of
                             skipping on latency/throughput.
  * ``ActivityContext``    — detection-driven: skip cameras with no detections
                             for a while, periodically re-probe them (like RT-BEV
                             bringing all cameras back on keyframes).

All of these are intentionally simple, swappable starting points — the point is
the scaffolding, not a tuned policy.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Dict, List, Set


class ContextProvider(ABC):
    """Base class: given the current context, return the set of active camera ids."""

    def __init__(self, num_cams: int) -> None:
        self.num_cams = num_cams

    @abstractmethod
    def active_cameras(self) -> Set[int]:
        """Return the set of camera ids that should be processed right now."""

    def note_detections(self, camera_id: int, num_detections: int) -> None:
        """Optional feedback hook: per-camera detection activity from the probe.

        Detection-driven providers use this; others ignore it.
        """

    @property
    def name(self) -> str:
        return type(self).__name__


class AllActiveContext(ContextProvider):
    """Baseline provider: all cameras always active (no skipping)."""

    def active_cameras(self) -> Set[int]:
        return set(range(self.num_cams))


class ScheduledContext(ContextProvider):
    """Deterministic active-set timeline for reproducible experiments.

    Args:
        num_cams: total cameras.
        schedule: list of steps ``{"at": seconds, "cameras": [ids]}``. Starting at
            the given elapsed time, that set becomes active. Before the first
            step's time, all cameras are active.

    Example (start with 4, drop to 2 after 10s, back to 4 after 20s):
        [{"at": 10, "cameras": [0, 1]}, {"at": 20, "cameras": [0, 1, 2, 3]}]
    """

    def __init__(self, num_cams: int, schedule: List[Dict]) -> None:
        super().__init__(num_cams)
        self._schedule = sorted(schedule or [], key=lambda s: float(s["at"]))
        self._t0 = None

    def active_cameras(self) -> Set[int]:
        now = time.monotonic()
        if self._t0 is None:
            self._t0 = now
        elapsed = now - self._t0
        active = set(range(self.num_cams))
        for step in self._schedule:
            if elapsed >= float(step["at"]):
                active = {int(c) for c in step["cameras"]}
        return active or set(range(self.num_cams))


class ActivityContext(ContextProvider):
    """Detection-driven camera selection.

    A camera whose latest frames have no detections for ``idle_secs`` is skipped.
    Skipped cameras are re-enabled for one tick every ``reprobe_secs`` so new
    activity can be picked up again (a lightweight stand-in for RT-BEV's keyframe
    "bring everything back" behaviour). At least one camera is always kept active.

    Args:
        num_cams: total cameras.
        idle_secs: no-detection duration before a camera is skipped.
        reprobe_secs: how often a skipped camera is briefly re-enabled to re-check.
    """

    def __init__(self, num_cams: int, idle_secs: float = 3.0, reprobe_secs: float = 2.0) -> None:
        super().__init__(num_cams)
        self._idle = float(idle_secs)
        self._reprobe = float(reprobe_secs)
        now = time.monotonic()
        self._last_activity: Dict[int, float] = {c: now for c in range(num_cams)}
        self._skipped_since: Dict[int, float] = {}

    def note_detections(self, camera_id: int, num_detections: int) -> None:
        if num_detections > 0:
            self._last_activity[camera_id] = time.monotonic()

    def active_cameras(self) -> Set[int]:
        now = time.monotonic()
        active: Set[int] = set()
        for c in range(self.num_cams):
            idle_for = now - self._last_activity.get(c, now)
            if idle_for < self._idle:
                active.add(c)
                self._skipped_since.pop(c, None)
            else:
                # Skipped — but re-probe periodically so it can come back.
                skipped_at = self._skipped_since.setdefault(c, now)
                if (now - skipped_at) >= self._reprobe:
                    active.add(c)
                    self._skipped_since[c] = now
        if not active:  # safety: never skip everything
            active = set(range(self.num_cams))
        return active


def make_context(spec: Dict, num_cams: int) -> ContextProvider:
    """Factory: build a ContextProvider from a small config dict.

    Args:
        spec: e.g. {"type": "all"} | {"type": "activity", "idle_secs": 3,
              "reprobe_secs": 2} | {"type": "scheduled", "schedule": [...]}.
        num_cams: total cameras.
    """
    kind = (spec or {}).get("type", "all").lower()
    if kind in ("all", "allactive", "none"):
        return AllActiveContext(num_cams)
    if kind == "scheduled":
        return ScheduledContext(num_cams, spec.get("schedule", []))
    if kind == "activity":
        return ActivityContext(
            num_cams,
            idle_secs=float(spec.get("idle_secs", 3.0)),
            reprobe_secs=float(spec.get("reprobe_secs", 2.0)),
        )
    raise ValueError(f"unknown context type '{kind}' (use all | scheduled | activity)")
