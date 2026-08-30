"""Transport abstraction.

Instrument connectivity in a real lab is not one protocol, it is six, and the
awkward ones dominate. A 2019 plate reader exports CSV to a watched folder. A
Windows-only SDK is reachable only through COM. A robot arm speaks a line
protocol over a socket. A modern liquid handler has a REST API. Anything from
before ~2010 is RS-232.

Separating *transport* from *driver* means a driver expresses instrument
semantics once and can be re-pointed at a different link (including a
simulator) without touching capability code. That is what makes the
"integrate one instrument at a time" story hold: the hard part becomes
picking a transport, not writing plumbing.
"""

from __future__ import annotations

import abc
import threading
import time
from typing import Any

from ..core.errors import TransientError, TransportError


class Transport(abc.ABC):
    """A byte/message pipe to an instrument."""

    #: Human label used in manifests and error messages.
    scheme: str = "abstract"

    def __init__(self, **options: Any) -> None:
        self.options = options
        self._lock = threading.RLock()
        self._open = False

    # -- lifecycle --------------------------------------------------------

    @abc.abstractmethod
    def _do_open(self) -> None: ...

    @abc.abstractmethod
    def _do_close(self) -> None: ...

    def open(self) -> None:
        with self._lock:
            if self._open:
                return
            self._do_open()
            self._open = True

    def close(self) -> None:
        with self._lock:
            if not self._open:
                return
            try:
                self._do_close()
            finally:
                self._open = False

    @property
    def is_open(self) -> bool:
        return self._open

    def __enter__(self) -> Transport:
        self.open()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- I/O --------------------------------------------------------------

    def request(self, payload: Any, *, timeout: float | None = None) -> Any:
        """Send and await a response. Subclasses override."""
        raise NotImplementedError(f"{type(self).__name__} does not support request()")

    def send(self, payload: Any) -> None:
        """Fire-and-forget write."""
        self.request(payload)

    # -- helpers ----------------------------------------------------------

    def _require_open(self) -> None:
        if not self._open:
            raise TransportError(f"{type(self).__name__} is not open; call open() first.")

    def with_retry(self, fn, *, retries: int = 2, backoff_s: float = 0.25) -> Any:
        """Retry only on TransientError -- never on a protocol or safety error."""
        last: Exception | None = None
        for attempt in range(retries + 1):
            try:
                return fn()
            except TransientError as exc:
                last = exc
                if attempt < retries:
                    time.sleep(backoff_s * (2 ** attempt))
        raise TransportError(f"exhausted {retries} retries: {last}") from last

    def describe(self) -> dict[str, Any]:
        safe = {k: ("***" if "pass" in k.lower() or "token" in k.lower() else v)
                for k, v in self.options.items()}
        return {"scheme": self.scheme, "open": self._open, "options": safe}


class LoopbackTransport(Transport):
    """In-process transport backed by a callable. Used by simulators and tests."""

    scheme = "loopback"

    def __init__(self, handler=None, **options: Any) -> None:
        super().__init__(**options)
        self.handler = handler or (lambda p: p)
        self.history: list[Any] = []

    def _do_open(self) -> None:
        return None

    def _do_close(self) -> None:
        return None

    def request(self, payload: Any, *, timeout: float | None = None) -> Any:
        self._require_open()
        self.history.append(payload)
        return self.handler(payload)


__all__ = ["Transport", "LoopbackTransport"]
