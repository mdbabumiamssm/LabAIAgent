"""Event bus: device state changes, job progress, approvals, audit appends.

Agents (and dashboards) that can *watch* the lab do not have to poll it. The
bus is deliberately simple: bounded per-subscriber queues, drop-oldest on
overflow (an observability channel must never apply backpressure to the
control path), and JSON-serialisable event dicts.

Consumed by the REST adapter's Server-Sent Events endpoint (``GET /events``)
and available in-process via ``subscribe()``.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from collections.abc import Generator
from typing import Any


class EventBus:
    def __init__(self, *, max_queue: int = 500, max_subscribers: int = 32) -> None:
        self._subs: list[queue.Queue] = []
        self._lock = threading.RLock()
        self._max_queue = max_queue
        #: Each SSE subscriber holds a server thread for the life of the
        #: connection; an unbounded count is a denial-of-service against the
        #: control API, so subscription past the cap is refused.
        self._max_subscribers = max_subscribers
        self._seq = 0

    def publish(self, event: str, **payload: Any) -> None:
        with self._lock:
            self._seq += 1
            msg = {"seq": self._seq, "event": event,
                   "ts": round(time.time(), 3), **payload}
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(msg)
            except queue.Full:
                try:                      # drop-oldest, never block publishers
                    q.get_nowait()
                    q.put_nowait(msg)
                except Exception:
                    pass

    def publish_dict(self, payload: dict[str, Any]) -> None:
        payload = dict(payload)
        self.publish(payload.pop("event", "event"), **payload)

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=self._max_queue)
        with self._lock:
            if len(self._subs) >= self._max_subscribers:
                raise RuntimeError(
                    f"Subscriber limit reached ({self._max_subscribers}). "
                    f"Close an existing event stream before opening another.")
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    # -- helpers -----------------------------------------------------------

    def sse_stream(self, *, heartbeat_s: float = 15.0) -> Generator[bytes, None, None]:
        """Yield Server-Sent Events frames forever (used by the REST adapter).

        The first frame is an immediate ``: connected`` comment, so a caller
        can prime the generator (which is also when the subscription -- and
        any capacity refusal -- happens) without waiting a heartbeat.
        """
        q = self.subscribe()
        try:
            yield b": connected\n\n"
            while True:
                try:
                    msg = q.get(timeout=heartbeat_s)
                    yield (f"event: {msg['event']}\n"
                           f"data: {json.dumps(msg, default=str)}\n\n").encode()
                except queue.Empty:
                    yield b": heartbeat\n\n"
        finally:
            self.unsubscribe(q)

    def wire_session(self, session: Any) -> None:
        """Publish device state transitions from an existing LabSession."""
        def observer(device_id: str, old: Any, new: Any) -> None:
            self.publish("device.state", device=device_id,
                         from_state=getattr(old, "value", str(old)),
                         to_state=getattr(new, "value", str(new)))
        for dev in session.devices():
            dev.observe(observer)
        session.safety.estop.on_trip(
            lambda reason: self.publish("estop.tripped", reason=reason))


__all__ = ["EventBus"]
