"""controllers.py — runtime controllers fired on a periodic tick.

Two controllers turn the ContextProvider's decisions into live pipeline changes:

  * ``CameraGateController`` — sets each source's ``valve.drop`` so only the
    active cameras feed nvstreammux (context-aware camera skipping).
  * ``TimeoutController`` — adapts ``nvstreammux batched-push-timeout`` at
    runtime (the "dynamic batching timeout").

Why they belong together: when a camera is valve-skipped, its nvstreammux sink
pad never receives a frame, so the muxer can NEVER assemble a full batch of
``batch-size`` and therefore waits the *entire* timeout on every batch. The
adaptive TimeoutController removes that penalty by shrinking the wait to match
the number of cameras actually expected — so skipping + adaptive timeout together
reduce both inference work and batch-wait latency. This mirrors RT-BEV shrinking
the allowable sync delay when it syncs a smaller camera group.

Both are driven by a single GLib periodic timer set up in main.py.
"""

from __future__ import annotations

from typing import Dict, Optional, Set

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402


class CameraGateController:
    """Applies a ContextProvider's active-camera set to the per-camera valves."""

    def __init__(self, pipeline: Gst.Pipeline, num_cams: int, provider, metrics=None) -> None:
        self._provider = provider
        self._num = num_cams
        self._metrics = metrics
        self._valves: Dict[int, Gst.Element] = {}
        for i in range(num_cams):
            valve = pipeline.get_by_name(f"cam-valve-{i}")
            if valve is None:
                raise RuntimeError(f"CameraGateController: valve 'cam-valve-{i}' not found.")
            self._valves[i] = valve
        self._active: Set[int] = set(range(num_cams))

    def tick(self) -> Set[int]:
        """Query the provider and toggle each valve's drop accordingly."""
        active = self._provider.active_cameras()
        for i, valve in self._valves.items():
            drop = i not in active
            if bool(valve.get_property("drop")) != drop:
                valve.set_property("drop", drop)
        self._active = set(active)
        if self._metrics is not None:
            self._metrics.set_active_cameras(self._active)
        return self._active

    @property
    def active(self) -> Set[int]:
        return self._active


class TimeoutController:
    """Adapts nvstreammux ``batched-push-timeout`` (microseconds) each tick.

    Policies:
      * ``fixed``    — never change; stays at ``base_us`` (baseline for A/B).
      * ``adaptive`` — scale the wait by the fraction of cameras currently active:
                       ``timeout = clamp(base_us * n_active/num_cams, min, max)``.
                       Fewer active cameras -> shorter wait (don't block on the
                       cameras that are being skipped).

    Args:
        mux: the nvstreammux element.
        policy: "fixed" | "adaptive".
        base_us: base timeout (default 33333 ~= 1/30s).
        min_us, max_us: clamps for the adaptive policy.
        gate: the CameraGateController (adaptive reads its active count).
        num_cams: total cameras.
        metrics: optional MetricsCollector to record the current timeout.
    """

    def __init__(
        self,
        mux: Gst.Element,
        policy: str = "fixed",
        base_us: int = 33333,
        min_us: int = 5000,
        max_us: int = 100000,
        gate: Optional[CameraGateController] = None,
        num_cams: int = 4,
        metrics=None,
    ) -> None:
        self._mux = mux
        self._policy = policy
        self._base = int(base_us)
        self._min = int(min_us)
        self._max = int(max_us)
        self._gate = gate
        self._num = max(1, int(num_cams))
        self._metrics = metrics
        self._current = int(base_us)
        mux.set_property("batched-push-timeout", self._current)
        if metrics is not None:
            metrics.set_timeout_us(self._current)

    def tick(self) -> int:
        """Recompute and, if changed, apply the batched-push-timeout."""
        if self._policy == "fixed":
            new = self._base
        elif self._policy == "adaptive":
            n_active = len(self._gate.active) if self._gate is not None else self._num
            frac = max(1, n_active) / self._num
            new = int(round(self._base * frac))
            new = max(self._min, min(self._max, new))
        else:
            raise ValueError(f"unknown timeout policy '{self._policy}' (use fixed | adaptive)")

        if new != self._current:
            self._mux.set_property("batched-push-timeout", new)
            self._current = new
        if self._metrics is not None:
            self._metrics.set_timeout_us(self._current)
        return self._current

    @property
    def current_us(self) -> int:
        return self._current


class BatchController:
    """Adapts nvstreammux ``batch-size`` to the active-camera count each tick.

    MOTIVATION (the idea): when a camera is skipped the batch never reaches the
    full ``batch-size``, so the muxer waits the whole timeout on every batch. The
    hope was that ``batch-size == n_active`` would make a short batch count as
    "full" and push immediately, removing the wait.

    EMPIRICAL RESULT (measured on this device — see experiments/README.md): with
    the LEGACY nvstreammux this does NOT work, and is slightly counterproductive.
    The legacy mux pushes when *all connected sink pads have delivered OR the
    timeout fires* — not when ``batch-size`` frames are present — so shrinking
    batch-size does not trigger an early push (isolated test: skip-2 wait was
    ~54 ms at batch-size 4 and ~90 ms at batch-size 2). Skipping a camera is
    therefore a COMPUTE/POWER win (fewer frames inferred) but adds batch-wait
    latency, and the working lever to shrink that wait is the adaptive TIMEOUT,
    not batch-size. A true early-push on skip needs the NEW nvstreammux
    (USE_NEW_NVSTREAMMUX=yes, deadline-based) or dynamically releasing the skipped
    cameras' request pads. This controller is kept as a documented experiment;
    leave ``policy: fixed`` unless you are re-measuring on the new mux.

    Only nvstreammux is touched. nvinfer's ``batch-size`` is its build-time engine
    max (== camera count) and is NULL/READY-only, but it needs no change: the
    dynamic engine (max batch N) runs any smaller batch natively.

    Policies:
      * ``fixed``    — batch-size stays at the camera count (recommended default).
      * ``adaptive`` — batch-size = active-camera count (experiment only; see above).

    Args:
        mux: the nvstreammux element.
        policy: "fixed" | "adaptive".
        gate: the CameraGateController (adaptive reads its active count).
        num_cams: total cameras (the fixed/max batch size).
        metrics: optional MetricsCollector to record the current batch size.
    """

    def __init__(
        self,
        mux: Gst.Element,
        policy: str = "fixed",
        gate: Optional[CameraGateController] = None,
        num_cams: int = 4,
        metrics=None,
    ) -> None:
        self._mux = mux
        self._policy = policy
        self._gate = gate
        self._num = max(1, int(num_cams))
        self._metrics = metrics
        self._current = self._num
        mux.set_property("batch-size", self._current)
        if metrics is not None:
            metrics.set_mux_batch(self._current)

    def tick(self) -> int:
        """Recompute and, if changed, apply the mux batch-size."""
        if self._policy == "fixed":
            new = self._num
        elif self._policy == "adaptive":
            n_active = len(self._gate.active) if self._gate is not None else self._num
            new = max(1, min(self._num, n_active))
        else:
            raise ValueError(f"unknown batch policy '{self._policy}' (use fixed | adaptive)")

        if new != self._current:
            self._mux.set_property("batch-size", new)
            self._current = new
        if self._metrics is not None:
            self._metrics.set_mux_batch(self._current)
        return self._current

    @property
    def current(self) -> int:
        return self._current
