"""Device heartbeat watchdog.

A lab that runs unattended overnight needs something that notices when an
instrument stops answering -- BEFORE the next protocol step trusts it. The
watchdog polls each connected device's own ``_self_test`` (the driver's
declared liveness check) on a fixed cadence and:

  - after ``failures_to_trip`` consecutive failures, marks the device ERROR,
    publishes ``device.heartbeat_lost``, and writes an audit record -- so the
    safety engine's state gate refuses new actuation on it;
  - when a tripped device answers again, restores it to IDLE, publishes
    ``device.heartbeat_recovered``, and audits the recovery.

Deliberate non-goals: the watchdog never probes a BUSY device (a self-test
mid-actuation can interleave traffic on half-duplex serial links), never
probes during an e-stop (the latch already owns the lab), and never quarantines
(quarantine is incident-driven and human-released; a heartbeat loss is often
just a cable). It is a sensor, not a judge.
"""

from __future__ import annotations

import threading
from typing import Any

from ..core.types import DeviceState


class Watchdog:
    """Background heartbeat monitor over a LabSession's devices."""

    #: Error prefix stamped on devices the watchdog trips. Recovery restores
    #: ONLY devices whose current error carries this prefix -- an ERROR set
    #: by a physical fault, an incident, or an operator is never ours to
    #: clear (review finding R4-5).
    TRIP_PREFIX = "watchdog:"

    def __init__(self, session: Any, *, interval_s: float = 15.0,
                 failures_to_trip: int = 3,
                 events: Any = None, supervisor: Any = None) -> None:
        if interval_s <= 0:
            raise ValueError("interval_s must be positive")
        self.session = session
        self.interval_s = float(interval_s)
        self.failures_to_trip = max(1, int(failures_to_trip))
        self.events = events
        #: Optional oversight supervisor: a quarantined device is never
        #: auto-restored, whatever its heartbeat says -- quarantine release
        #: is a named-human act.
        self.supervisor = supervisor
        self._failures: dict[str, int] = {}
        self._tripped: set[str] = set()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> Watchdog:
        if self._thread is not None:
            return self
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="labaiagent-watchdog")
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval_s + 2.0)
            self._thread = None

    # -- one pass (public so tests and CLI can drive it synchronously) ------

    def check_once(self) -> dict[str, str]:
        """Probe every eligible device once. Returns device -> outcome.

        Each probe holds the SESSION'S per-device lock (the same one
        ``LabSession.call`` actuates under), so a probe can never interleave
        traffic with an in-flight actuation, and the state re-check inside
        the lock closes the check-then-probe race (review finding R4-4). A
        device whose lock is busy is simply skipped this round -- the agent
        traffic on it is heartbeat enough.
        """
        out: dict[str, str] = {}
        if self.session.safety.estop.active:
            return out
        for dev in list(self.session):
            dev_lock = self.session._locks.get(dev.id)
            if dev_lock is None:
                continue
            if not dev_lock.acquire(blocking=False):
                out[dev.id] = "skipped (device lock busy)"
                continue
            try:
                if self.session.safety.estop.active:
                    return out
                if dev.state in (DeviceState.BUSY, DeviceState.DISCONNECTED,
                                 DeviceState.CONNECTING, DeviceState.ESTOPPED):
                    out[dev.id] = f"skipped ({dev.state.value})"
                    continue
                ok = False
                try:
                    ok = bool(dev._self_test())
                except Exception:
                    ok = False
                out[dev.id] = self._settle(dev, ok)
            finally:
                dev_lock.release()
        return out

    def _settle(self, dev: Any, ok: bool) -> str:
        """Update trip/recovery bookkeeping for one probed device.

        Called with the device lock held.
        """
        with self._lock:
            if ok:
                self._failures[dev.id] = 0
                if dev.id not in self._tripped:
                    return "ok"
                self._tripped.discard(dev.id)
                # Restore ONLY what we broke: the error must still be the
                # watchdog's own trip, and the device must not be under
                # incident quarantine (human-released, never auto).
                ours = str(getattr(dev, "_last_error", "")).startswith(
                    self.TRIP_PREFIX)
                quarantined = bool(
                    self.supervisor is not None
                    and self.supervisor.is_quarantined(dev.id))
                if ours and not quarantined \
                        and dev.state is DeviceState.ERROR:
                    dev._set_state(DeviceState.IDLE)
                    self.session.audit.record(
                        "heartbeat_recovered", device=dev.id,
                        reason="watchdog: self-test passing again")
                    self._publish("device.heartbeat_recovered", dev.id)
                    return "recovered"
                self._publish("device.heartbeat_recovered", dev.id,
                              restored=False)
                return ("answering (left in current state: "
                        + ("quarantined)" if quarantined else
                           "error not set by watchdog)"))
            n = self._failures.get(dev.id, 0) + 1
            self._failures[dev.id] = n
            if n >= self.failures_to_trip and dev.id not in self._tripped:
                self._tripped.add(dev.id)
                dev._set_state(
                    DeviceState.ERROR,
                    error=f"{self.TRIP_PREFIX} {n} consecutive heartbeat "
                          f"failures")
                self.session.audit.record(
                    "heartbeat_lost", device=dev.id,
                    reason=f"watchdog: {n} consecutive self-test failures",
                    state_after=DeviceState.ERROR.value)
                self._publish("device.heartbeat_lost", dev.id, failures=n)
                return "tripped"
            return f"failing ({n}/{self.failures_to_trip})"

    @property
    def tripped(self) -> set[str]:
        with self._lock:
            return set(self._tripped)

    # -- internals -----------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_s):
            try:
                self.check_once()
            except Exception:   # pragma: no cover - the watchdog must not die
                pass

    def _publish(self, event: str, device_id: str, **fields: Any) -> None:
        if self.events is not None:
            try:
                self.events.publish(event, device=device_id, **fields)
            except Exception:
                pass


__all__ = ["Watchdog"]
