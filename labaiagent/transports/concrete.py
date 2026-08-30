"""Concrete transports for the six ways lab instruments actually talk.

Optional third-party dependencies (pyserial, httpx, pywin32, sila2) are
imported lazily inside ``_do_open`` so the package installs and the simulators
run on a bare Python. A missing dependency produces an actionable message at
connect time, not an ImportError at import time.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..core.errors import TransientError, TransportError
from .base import Transport

# --------------------------------------------------------------------------
# 1. TCP line protocol -- robot arms, older controllers, GPIB-Ethernet bridges
# --------------------------------------------------------------------------

class TCPLineTransport(Transport):
    """Newline-delimited ASCII command/response over a socket."""

    scheme = "tcp"

    def __init__(self, host: str, port: int, *, timeout: float = 5.0,
                 terminator: str = "\n", encoding: str = "ascii", **kw: Any) -> None:
        super().__init__(host=host, port=port, timeout=timeout, **kw)
        self.host, self.port, self.timeout = host, port, timeout
        self.terminator, self.encoding = terminator, encoding
        self._sock: socket.socket | None = None
        self._buf = b""

    def _do_open(self) -> None:
        try:
            self._sock = socket.create_connection((self.host, self.port), self.timeout)
            self._sock.settimeout(self.timeout)
        except OSError as exc:
            raise TransportError(
                f"Cannot reach {self.host}:{self.port} -- {exc}. Check the "
                f"instrument is powered, on the same VLAN, and not already "
                f"claimed by the vendor software (most controllers accept only "
                f"one client at a time)."
            ) from exc

    def _do_close(self) -> None:
        if self._sock:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def request(self, payload: str, *, timeout: float | None = None) -> str:
        self._require_open()
        assert self._sock is not None
        with self._lock:
            self._sock.settimeout(timeout or self.timeout)
            try:
                self._sock.sendall((payload + self.terminator).encode(self.encoding))
                term = self.terminator.encode(self.encoding)
                while term not in self._buf:
                    chunk = self._sock.recv(4096)
                    if not chunk:
                        raise TransientError(f"{self.host}:{self.port} closed the connection")
                    self._buf += chunk
                line, _, self._buf = self._buf.partition(term)
                return line.decode(self.encoding, "replace").strip()
            except TimeoutError as exc:
                raise TransientError(f"timeout awaiting reply to {payload!r}") from exc
            except OSError as exc:
                raise TransientError(f"socket error on {payload!r}: {exc}") from exc


# --------------------------------------------------------------------------
# 2. Serial / RS-232 -- pumps, balances, older thermocyclers, syringe drives
# --------------------------------------------------------------------------

class SerialTransport(Transport):
    """RS-232 / USB-serial line protocol. Requires ``pyserial``."""

    scheme = "serial"

    def __init__(self, port: str, *, baudrate: int = 9600, timeout: float = 2.0,
                 terminator: str = "\r\n", bytesize: int = 8, parity: str = "N",
                 stopbits: float = 1, **kw: Any) -> None:
        super().__init__(port=port, baudrate=baudrate, timeout=timeout, **kw)
        self.port, self.baudrate, self.timeout = port, baudrate, timeout
        self.terminator = terminator
        self._extra = dict(bytesize=bytesize, parity=parity, stopbits=stopbits)
        self._ser: Any = None

    def _do_open(self) -> None:
        try:
            import serial  # type: ignore
        except ImportError as exc:
            raise TransportError(
                "SerialTransport needs pyserial. Install with: "
                "pip install 'labaiagent[serial]'"
            ) from exc
        try:
            self._ser = serial.Serial(self.port, self.baudrate,
                                      timeout=self.timeout, **self._extra)
            time.sleep(0.1)          # many devices need a settle after DTR toggle
            self._ser.reset_input_buffer()
        except Exception as exc:
            raise TransportError(
                f"Cannot open serial port {self.port}: {exc}. On Linux check the "
                f"user is in the 'dialout' group; on Windows confirm the COM "
                f"number in Device Manager."
            ) from exc

    def _do_close(self) -> None:
        if self._ser:
            try:
                self._ser.close()
            finally:
                self._ser = None

    def request(self, payload: str, *, timeout: float | None = None) -> str:
        self._require_open()
        with self._lock:
            if timeout is not None:
                self._ser.timeout = timeout
            self._ser.write((payload + self.terminator).encode("ascii"))
            self._ser.flush()
            raw = self._ser.readline()
            if not raw:
                raise TransientError(f"serial timeout awaiting reply to {payload!r}")
            return raw.decode("ascii", "replace").strip()


# --------------------------------------------------------------------------
# 3. HTTP / REST -- Opentrons Flex, modern networked instruments
# --------------------------------------------------------------------------

class HTTPTransport(Transport):
    """JSON over HTTP using stdlib urllib (no httpx dependency required)."""

    scheme = "http"

    def __init__(self, base_url: str, *, timeout: float = 30.0,
                 headers: dict[str, str] | None = None, **kw: Any) -> None:
        super().__init__(base_url=base_url, timeout=timeout, **kw)
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.headers = {"Content-Type": "application/json", **(headers or {})}

    def _do_open(self) -> None:
        return None

    def _do_close(self) -> None:
        return None

    def request(self, payload: dict[str, Any], *, timeout: float | None = None) -> Any:
        """payload = {'method': 'GET'|'POST'|..., 'path': '/runs', 'json': {...}}"""
        method = payload.get("method", "GET").upper()
        url = self.base_url + payload["path"]
        body = json.dumps(payload["json"]).encode() if payload.get("json") is not None else None
        req = urllib.request.Request(url, data=body, method=method, headers=self.headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            if exc.code in (408, 429, 502, 503, 504):
                raise TransientError(f"{method} {url} -> {exc.code}: {detail}") from exc
            raise TransportError(f"{method} {url} -> {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise TransientError(f"{method} {url} unreachable: {exc.reason}") from exc


# --------------------------------------------------------------------------
# 4. Watched folder -- the awkward, extremely common case
# --------------------------------------------------------------------------

class FileWatchTransport(Transport):
    """Drop a command file, wait for a result file to appear.

    A large fraction of installed plate readers, imagers and older qPCR
    instruments have no API at all: the vendor software writes an export file
    when a run finishes, and that is the only integration surface. This
    transport turns that into a synchronous request/response.

    ``stability_checks`` matters more than it looks -- the vendor process
    creates the file before it finishes writing it, so reading on first
    appearance yields a truncated CSV. We wait for the size to stop changing.
    """

    scheme = "filewatch"

    def __init__(self, *, watch_dir: str | Path, command_dir: str | Path | None = None,
                 pattern: str = "*.csv", timeout: float = 600.0,
                 poll_s: float = 1.0, stability_checks: int = 3,
                 archive_dir: str | Path | None = None, **kw: Any) -> None:
        super().__init__(watch_dir=str(watch_dir), pattern=pattern, **kw)
        self.watch_dir = Path(watch_dir)
        self.command_dir = Path(command_dir) if command_dir else None
        self.pattern = pattern
        self.timeout = timeout
        self.poll_s = poll_s
        self.stability_checks = stability_checks
        self.archive_dir = Path(archive_dir) if archive_dir else None
        self._seen: set[str] = set()

    def _do_open(self) -> None:
        if not self.watch_dir.exists():
            raise TransportError(
                f"Watch directory {self.watch_dir} does not exist. This is usually "
                f"the vendor software's configured export path -- check its "
                f"'Export' or 'Auto-save' settings."
            )
        # Baseline existing files so we only react to genuinely new results.
        self._seen = {p.name for p in self.watch_dir.glob(self.pattern)}
        if self.command_dir:
            self.command_dir.mkdir(parents=True, exist_ok=True)

    def _do_close(self) -> None:
        self._seen.clear()

    def write_command(self, filename: str, content: str) -> Path:
        if not self.command_dir:
            raise TransportError("No command_dir configured for this transport.")
        # Write-then-rename so the vendor watcher never sees a partial file.
        tmp = self.command_dir / (filename + ".tmp")
        final = self.command_dir / filename
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(final)
        return final

    def await_result(self, *, timeout: float | None = None) -> Path:
        deadline = time.monotonic() + (timeout or self.timeout)
        while time.monotonic() < deadline:
            candidates = [p for p in self.watch_dir.glob(self.pattern)
                          if p.name not in self._seen]
            if candidates:
                newest = max(candidates, key=lambda p: p.stat().st_mtime)
                if self._is_stable(newest):
                    self._seen.add(newest.name)
                    if self.archive_dir:
                        self.archive_dir.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(newest, self.archive_dir / newest.name)
                    return newest
            time.sleep(self.poll_s)
        # Deliberately NOT TransientError: auto-retrying would silently wait
        # another full window on a run that probably never started. A human
        # (or the agent, explicitly) should decide whether to wait again.
        raise TransportError(
            f"No new file matching {self.pattern!r} appeared in {self.watch_dir} "
            f"within {timeout or self.timeout:g}s. Check the run actually started "
            f"and that the vendor export path still points here."
        )

    def _is_stable(self, path: Path) -> bool:
        try:
            sizes = []
            for _ in range(self.stability_checks):
                sizes.append(path.stat().st_size)
                time.sleep(self.poll_s / 2)
            return len(set(sizes)) == 1 and sizes[0] > 0
        except OSError:
            return False

    def request(self, payload: dict[str, Any], *, timeout: float | None = None) -> Path:
        self._require_open()
        if payload.get("command"):
            self.write_command(payload["filename"], payload["command"])
        return self.await_result(timeout=timeout)


# --------------------------------------------------------------------------
# 5. Windows COM / ActiveX -- vendor SDKs with no network surface
# --------------------------------------------------------------------------

class COMTransport(Transport):
    """Windows COM automation. Requires ``pywin32`` on Windows.

    Many instrument SDKs (plate readers, imagers, older schedulers) ship a
    Windows-only COM object as the sole programmatic interface. COM is
    apartment-threaded, so we pin every call to a single dedicated thread --
    calling from an async worker pool otherwise fails intermittently and
    unreproducibly, which is a miserable class of bug to chase on a robot.
    """

    scheme = "com"

    def __init__(self, prog_id: str, *, timeout: float = 60.0, **kw: Any) -> None:
        super().__init__(prog_id=prog_id, **kw)
        self.prog_id = prog_id
        self.timeout = timeout
        self._q: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._err: Exception | None = None

    def _worker(self) -> None:
        try:
            import pythoncom  # type: ignore
            import win32com.client  # type: ignore
        except ImportError:
            self._err = TransportError(
                "COMTransport requires pywin32 on Windows: pip install pywin32"
            )
            self._ready.set()
            return
        try:
            pythoncom.CoInitialize()
            obj = win32com.client.Dispatch(self.prog_id)
        except Exception as exc:
            self._err = TransportError(f"Cannot create COM object {self.prog_id}: {exc}")
            self._ready.set()
            return
        self._ready.set()
        while True:
            item = self._q.get()
            if item is None:
                break
            fn, result_q = item
            try:
                result_q.put(("ok", fn(obj)))
            except Exception as exc:
                result_q.put(("err", exc))
        try:
            pythoncom.CoUninitialize()
        except Exception:
            pass

    def _do_open(self) -> None:
        self._thread = threading.Thread(target=self._worker, daemon=True,
                                        name=f"com-{self.prog_id}")
        self._thread.start()
        self._ready.wait(timeout=30)
        if self._err:
            raise self._err

    def _do_close(self) -> None:
        self._q.put(None)
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def call(self, fn: Callable[[Any], Any], *, timeout: float | None = None) -> Any:
        """Run ``fn(com_object)`` on the pinned COM thread."""
        self._require_open()
        rq: queue.Queue = queue.Queue()
        self._q.put((fn, rq))
        try:
            status, payload = rq.get(timeout=timeout or self.timeout)
        except queue.Empty:
            raise TransientError(f"COM call to {self.prog_id} timed out") from None
        if status == "err":
            raise TransportError(f"COM call failed: {payload}") from payload
        return payload

    def request(self, payload: Callable[[Any], Any], *, timeout: float | None = None) -> Any:
        return self.call(payload, timeout=timeout)


# --------------------------------------------------------------------------
# 6. SiLA 2 -- the lab-instrument standard where vendors support it
# --------------------------------------------------------------------------

class SiLA2Transport(Transport):
    """Client for SiLA 2 servers. Requires the ``sila2`` package.

    Where a vendor already ships a SiLA 2 server, use it -- LabAIAgent is
    complementary, not a replacement. This transport lets a LabAIAgent driver
    wrap a SiLA feature set so the instrument joins the same manifest, safety
    and audit fabric as everything else.
    """

    scheme = "sila2"

    def __init__(self, host: str, port: int = 50051, *, insecure: bool = True,
                 ca_cert: str | None = None, **kw: Any) -> None:
        super().__init__(host=host, port=port, **kw)
        self.host, self.port, self.insecure, self.ca_cert = host, port, insecure, ca_cert
        self._client: Any = None

    def _do_open(self) -> None:
        try:
            from sila2.client import SilaClient  # type: ignore
        except ImportError as exc:
            raise TransportError(
                "SiLA2Transport needs the sila2 package: "
                "pip install 'labaiagent[sila]'"
            ) from exc
        try:
            self._client = SilaClient(self.host, self.port, insecure=self.insecure)
        except Exception as exc:
            raise TransportError(f"SiLA 2 connect to {self.host}:{self.port} failed: {exc}") from exc

    def _do_close(self) -> None:
        self._client = None

    @property
    def client(self) -> Any:
        self._require_open()
        return self._client

    def request(self, payload: Callable[[Any], Any], *, timeout: float | None = None) -> Any:
        return payload(self.client)


# --------------------------------------------------------------------------
# 7. Subprocess -- wrap a vendor CLI or a Python SDK that must run isolated
# --------------------------------------------------------------------------

class SubprocessTransport(Transport):
    """Invoke a vendor CLI. Also the escape hatch for SDKs pinned to a
    different Python version -- run them in their own interpreter."""

    scheme = "subprocess"

    def __init__(self, executable: str, *, cwd: str | None = None,
                 env: dict[str, str] | None = None, timeout: float = 300.0,
                 **kw: Any) -> None:
        super().__init__(executable=executable, **kw)
        self.executable, self.cwd, self.timeout = executable, cwd, timeout
        self.env = {**os.environ, **(env or {})}

    def _do_open(self) -> None:
        if shutil.which(self.executable) is None and not Path(self.executable).exists():
            raise TransportError(f"Executable not found: {self.executable}")

    def _do_close(self) -> None:
        return None

    def request(self, payload: list[str], *, timeout: float | None = None) -> str:
        self._require_open()
        try:
            proc = subprocess.run(
                [self.executable, *payload], capture_output=True, text=True,
                cwd=self.cwd, env=self.env, timeout=timeout or self.timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise TransientError(f"{self.executable} timed out") from exc
        if proc.returncode != 0:
            raise TransportError(
                f"{self.executable} exited {proc.returncode}: {proc.stderr.strip()[:500]}")
        return proc.stdout.strip()


TRANSPORTS: dict[str, type[Transport]] = {
    "tcp": TCPLineTransport,
    "serial": SerialTransport,
    "http": HTTPTransport,
    "filewatch": FileWatchTransport,
    "com": COMTransport,
    "sila2": SiLA2Transport,
    "subprocess": SubprocessTransport,
}


def make_transport(spec: dict[str, Any]) -> Transport:
    """Build a transport from a config stanza: {'scheme': 'tcp', 'host': ...}."""
    spec = dict(spec)
    scheme = spec.pop("scheme", None)
    if scheme not in TRANSPORTS:
        raise TransportError(
            f"Unknown transport scheme {scheme!r}. Available: {sorted(TRANSPORTS)}")
    return TRANSPORTS[scheme](**spec)


__all__ = [
    "TCPLineTransport", "SerialTransport", "HTTPTransport", "FileWatchTransport",
    "COMTransport", "SiLA2Transport", "SubprocessTransport",
    "TRANSPORTS", "make_transport",
]
