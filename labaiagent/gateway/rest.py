"""REST + MCP-over-HTTP adapter -- stdlib only, so it runs anywhere the
package installs, including locked-down instrument workstations.

Surface (see /openapi.json for the machine-readable contract):

    GET  /                    operator dashboard (self-contained HTML console)
    GET  /health              liveness + lab identity
    GET  /metrics             Prometheus text-format operational metrics
    GET  /openapi.json        OpenAPI 3.1 document
    GET  /tools               tool registry with input schemas
    GET  /manifest            machine-readable device manifest
    GET  /reference           whole-lab operating reference (text)
    GET  /events              Server-Sent Events: device state, jobs, approvals
    POST /tools/{name}        invoke one tool; body = JSON arguments
    POST /mcp                 MCP JSON-RPC over HTTP (initialize, tools/*,
                              resources/*) -- remote MCP clients connect here

Authentication: ``Authorization: Bearer <key>`` or ``X-API-Key: <key>``,
resolved against the principals file. Without an authenticator the server
refuses to bind to non-loopback interfaces -- an unauthenticated lab control
API reachable from the network is not a configuration, it is an incident.

This adapter is what makes the lab reachable by *every* HTTP-capable agent
runtime: OpenAI tool calling, Gemini, LangChain/LlamaIndex via the OpenAPI
document, custom orchestrators, and remote MCP clients -- all against the
same twenty tools and the same safety engine.
"""

from __future__ import annotations

import json
import ssl
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from ..core.errors import ConfigurationError
from ..mcp.server import handle_jsonrpc
from ..memory.store import LabMemory
from ..orchestration.session import LabSession
from ..orchestration.watchdog import Watchdog
from ..oversight.supervisor import Supervisor
from ..provenance.records import RunRecordStore
from .auth import Authenticator, Principal
from .dashboard import DASHBOARD_CSP, DASHBOARD_HTML
from .metrics import render_metrics
from .registry import GatewayContext, dispatch, tool_index
from .schemas import to_openapi

MAX_BODY = 2 * 1024 * 1024  # 2 MiB is generous for a protocol document

#: Response headers on every reply. `nosniff` stops content-type confusion;
#: `no-store` keeps tool results (which can contain experimental data) out
#: of shared caches and proxy disks.
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
}


class AuthThrottle:
    """Per-source-IP lockout against API-key brute force.

    After ``max_failures`` bad keys inside ``window_s``, ALL further requests
    from that address are refused with 429 for ``cooldown_s`` -- before any
    key comparison runs, so a guessing loop gets neither an oracle nor CPU.
    The direct peer address is used; ``X-Forwarded-For`` is deliberately NOT
    trusted (any client can forge it). Behind a reverse proxy, enforce
    throttling at the proxy as well.
    """

    def __init__(self, *, max_failures: int = 10, window_s: float = 60.0,
                 cooldown_s: float = 60.0) -> None:
        self.max_failures = max_failures
        self.window_s = window_s
        self.cooldown_s = cooldown_s
        self._failures: dict[str, deque[float]] = {}
        self._locked_until: dict[str, float] = {}
        self._lock = threading.Lock()

    def locked(self, addr: str) -> bool:
        with self._lock:
            until = self._locked_until.get(addr, 0.0)
            if until and time.monotonic() < until:
                return True
            self._locked_until.pop(addr, None)
            return False

    def record_failure(self, addr: str) -> None:
        now = time.monotonic()
        with self._lock:
            q = self._failures.setdefault(addr, deque())
            while q and now - q[0] > self.window_s:
                q.popleft()
            q.append(now)
            if len(q) >= self.max_failures:
                self._locked_until[addr] = now + self.cooldown_s
                q.clear()

    def record_success(self, addr: str) -> None:
        with self._lock:
            self._failures.pop(addr, None)


class GatewayServer:
    """Owns the HTTP server, the shared GatewayContext, and auth."""

    def __init__(self, session: LabSession, *,
                 host: str = "127.0.0.1", port: int = 8859,
                 auth: Authenticator | None = None,
                 readonly: bool = False,
                 tls_cert: str | None = None, tls_key: str | None = None,
                 throttle: AuthThrottle | None = None,
                 supervisor: Supervisor | None = None,
                 watchdog_interval_s: float = 0.0) -> None:
        loopback = host in ("127.0.0.1", "localhost", "::1")
        if auth is None and not loopback:
            raise ConfigurationError(
                f"Refusing to serve on {host!r} without an authenticator. "
                f"Provide a principals file (labaiagent serve --auth "
                f"principals.yaml) or bind to 127.0.0.1.")
        if bool(tls_cert) != bool(tls_key):
            raise ConfigurationError(
                "TLS needs BOTH --tls-cert and --tls-key.")
        self.session = session
        self.ctx = GatewayContext.for_session(session)
        self.ctx.events.wire_session(session)
        # Behavioural oversight is ON by default for networked servers: a
        # rule-based reviewer plus refusal-streak suspension, with the RLHF
        # feedback store beside the audit log. Pass an explicit Supervisor
        # (e.g. with a FoundationModelReviewer) to upgrade it.
        # Durable lab memory lives beside the audit log: tasks, incidents,
        # quarantines and suspensions all survive restarts.
        if self.ctx.memory is None and session.audit.path:
            self.ctx.memory = LabMemory(
                session.audit.path.parent / "labaiagent_state.db")
        if self.ctx.memory is not None:
            self.ctx.memory.log_startup(session.audit.session_id)
        # Run-record provenance lives beside the audit log, sealed with the
        # same HMAC key so one secret roots both trust chains.
        if self.ctx.records is None and session.audit.path:
            self.ctx.records = RunRecordStore(
                session.audit.path.parent / "run_records",
                hmac_key=session.audit._hmac_key)
        if supervisor is not None:
            self.ctx.supervisor = supervisor
        elif self.ctx.supervisor is None:
            fb = (str(session.audit.path.parent / "feedback.jsonl")
                  if session.audit.path else None)
            self.ctx.supervisor = Supervisor.from_config(
                {}, feedback_path=fb, memory=self.ctx.memory)
        # Optional heartbeat watchdog (off unless an interval is configured).
        # Created AFTER the supervisor so recovery can honour quarantines.
        self.watchdog: Watchdog | None = None
        if watchdog_interval_s > 0:
            self.watchdog = Watchdog(session,
                                     interval_s=watchdog_interval_s,
                                     events=self.ctx.events,
                                     supervisor=self.ctx.supervisor).start()
        self.auth = auth
        self.readonly = readonly
        self.host, self.port = host, port
        self.tls_cert, self.tls_key = tls_cert, tls_key
        self.throttle = throttle or AuthThrottle()
        if auth is not None:
            auth.apply_to_session(session)

        gateway = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "LabAIAgent/1.0"
            protocol_version = "HTTP/1.1"

            # -- plumbing ---------------------------------------------------

            def log_message(self, fmt: str, *args: Any) -> None:  # quiet
                pass

            def _send(self, code: int, payload: Any,
                      content_type: str = "application/json") -> None:
                body = (payload if isinstance(payload, bytes) else
                        json.dumps(payload, indent=2, default=str).encode()
                        if content_type == "application/json"
                        else str(payload).encode())
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                for hname, hval in SECURITY_HEADERS.items():
                    self.send_header(hname, hval)
                if code == 429:
                    self.send_header(
                        "Retry-After", str(int(gateway.throttle.cooldown_s)))
                self.end_headers()
                self.wfile.write(body)

            def _peer(self) -> str:
                # Direct peer only; X-Forwarded-For is client-controlled.
                return self.client_address[0] if self.client_address else "?"

            def _throttled(self) -> bool:
                """429 (before any key comparison) while the peer is locked
                out for brute-forcing keys."""
                if gateway.auth is not None and gateway.throttle.locked(self._peer()):
                    self._send(429, {
                        "ok": False, "error": "too_many_auth_failures",
                        "message": "Too many failed authentication attempts "
                                   "from this address; wait and retry."})
                    return True
                return False

            def _principal(self) -> Principal | None:
                if gateway.auth is None:
                    # Loopback-only, unauthenticated: local operator trust,
                    # same model as the stdio pipe.
                    return None
                key = ""
                bearer = self.headers.get("Authorization", "")
                if bearer.lower().startswith("bearer "):
                    key = bearer[7:].strip()
                key = key or self.headers.get("X-API-Key", "")
                principal = gateway.auth.authenticate(key or None)
                peer = self._peer()
                if principal is None and key:
                    gateway.throttle.record_failure(peer)
                elif principal is not None:
                    gateway.throttle.record_success(peer)
                return principal

            # -- GET --------------------------------------------------------

            def do_GET(self) -> None:  # noqa: N802
                if self._throttled():
                    return
                path = self.path.split("?", 1)[0].rstrip("/") or "/"
                principal = self._principal()
                if path in ("/", "/dashboard"):
                    # The shell carries NO data: every byte of lab state the
                    # page shows travels through the authenticated /tools/*
                    # endpoints below, so serving the shell itself is safe.
                    body = DASHBOARD_HTML.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type",
                                     "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Content-Security-Policy", DASHBOARD_CSP)
                    for hname, hval in SECURITY_HEADERS.items():
                        self.send_header(hname, hval)
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if path == "/health":
                    # Liveness must work for load balancers and probes even
                    # without a key -- but an unauthenticated caller gets a
                    # bare pulse, not lab details.
                    if gateway.auth is not None and principal is None:
                        self._send(200, {"ok": True})
                        return
                    self._send(200, {
                        "ok": True, "lab": gateway.session.name,
                        "devices": len(gateway.session.devices()),
                        "emergency_stop": gateway.session.safety.estop.active,
                        "readonly": gateway.readonly})
                    return
                if gateway.auth is not None and principal is None:
                    self._send(401, {"ok": False, "error": "unauthorized",
                                     "message": "Missing or invalid API key."})
                    return
                if path == "/openapi.json":
                    self._send(200, to_openapi(
                        server_url=gateway.url,
                        readonly_only=gateway.readonly))
                elif path == "/tools":
                    self._send(200, {"tools": [
                        t.to_public() for t in
                        tool_index(readonly_only=gateway.readonly).values()]})
                elif path == "/manifest":
                    self._send(200, gateway.session.manifest())
                elif path == "/reference":
                    self._send(200, gateway.session.reference_sheets(),
                               content_type="text/plain; charset=utf-8")
                elif path == "/metrics":
                    self._send(200, render_metrics(gateway.ctx),
                               content_type="text/plain; version=0.0.4; "
                                            "charset=utf-8")
                elif path == "/events":
                    self._stream_events()
                else:
                    self._send(404, {"ok": False, "error": "not_found",
                                     "message": f"No route {path!r}."})

            def _stream_events(self) -> None:
                try:
                    stream = gateway.ctx.events.sse_stream(heartbeat_s=15.0)
                    first = next(stream)   # subscribes; raises if at capacity
                except (RuntimeError, StopIteration):
                    self._send(503, {"ok": False, "error": "too_many_streams",
                                     "message": "Event-stream subscriber limit "
                                                "reached; close one first."})
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                try:
                    self.wfile.write(first)
                    self.wfile.flush()
                    for frame in stream:
                        self.wfile.write(frame)
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    stream.close()   # runs the generator's finally -> unsubscribe

            # -- POST -------------------------------------------------------

            def _body(self) -> dict[str, Any] | None:
                # Only plain Content-Length bodies are supported. A chunked
                # body would leave its octets in the socket buffer to be
                # parsed as the NEXT request on this kept-alive connection --
                # silent argument loss at best, request smuggling behind a
                # proxy at worst. Refuse and close instead.
                if self.headers.get("Transfer-Encoding"):
                    self.close_connection = True
                    self._send(411, {"ok": False, "error": "length_required",
                                     "message": "Transfer-Encoding is not "
                                                "supported; send a JSON body "
                                                "with Content-Length."})
                    return None
                if "Content-Length" not in self.headers:
                    self.close_connection = True
                    self._send(411, {"ok": False, "error": "length_required",
                                     "message": "POST requests must carry "
                                                "Content-Length."})
                    return None
                n = int(self.headers.get("Content-Length", 0) or 0)
                if n > MAX_BODY:
                    self.close_connection = True
                    self._send(413, {"ok": False, "error": "too_large"})
                    return None
                raw = self.rfile.read(n) if n else b"{}"
                try:
                    data = json.loads(raw or b"{}")
                except json.JSONDecodeError:
                    self._send(400, {"ok": False, "error": "bad_json",
                                     "message": "Request body must be JSON."})
                    return None
                if not isinstance(data, dict):
                    self._send(400, {"ok": False, "error": "bad_json",
                                     "message": "Body must be a JSON object."})
                    return None
                return data

            def do_POST(self) -> None:  # noqa: N802
                if self._throttled():
                    return
                path = self.path.split("?", 1)[0].rstrip("/")
                principal = self._principal()
                if gateway.auth is not None and principal is None:
                    self._send(401, {"ok": False, "error": "unauthorized",
                                     "message": "Missing or invalid API key."})
                    return
                body = self._body()
                if body is None:
                    return
                if path == "/mcp":
                    resp = handle_jsonrpc(gateway.session, body,
                                          readonly=gateway.readonly,
                                          principal=principal)
                    self._send(200, resp if resp is not None else {})
                    return
                if path.startswith("/tools/"):
                    name = path[len("/tools/"):]
                    out = dispatch(gateway.ctx, name, body,
                                   readonly=gateway.readonly,
                                   principal=principal)
                    code = 200
                    if out.get("error") == "unknown_tool":
                        code = 404
                    elif out.get("error") == "forbidden":
                        code = 403
                    self._send(code, out)
                    return
                self._send(404, {"ok": False, "error": "not_found",
                                 "message": f"No route {path!r}."})

        self._handler_cls = Handler
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # -- lifecycle -------------------------------------------------------------

    def _bind(self) -> ThreadingHTTPServer:
        httpd = ThreadingHTTPServer((self.host, self.port), self._handler_cls)
        if self.tls_cert and self.tls_key:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            ctx.load_cert_chain(certfile=self.tls_cert, keyfile=self.tls_key)
            httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
        self.port = httpd.server_address[1]   # resolve port 0
        return httpd

    def start(self) -> GatewayServer:
        """Start in a background thread (used by tests and embedding code)."""
        self._httpd = self._bind()
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        daemon=True,
                                        name="labaiagent-gateway")
        self._thread.start()
        return self

    def serve_forever(self) -> None:
        self._httpd = self._bind()
        try:
            self._httpd.serve_forever()
        except KeyboardInterrupt:  # pragma: no cover
            pass
        finally:
            self._httpd.server_close()

    def stop(self, *, shutdown_jobs: bool = False) -> None:
        """Stop the HTTP server and the watchdog.

        ``shutdown_jobs=True`` also tears down the job thread pool. Default
        False, deliberately: the GatewayContext (and its JobManager) is
        session-scoped and may outlive this server -- an embedding process
        can stop the HTTP surface while letting running jobs finish. Pass
        True when this server owned the session's whole lifecycle.
        """
        if self.watchdog is not None:
            self.watchdog.stop()
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if shutdown_jobs:
            self.ctx.jobs.shutdown()

    @property
    def url(self) -> str:
        scheme = "https" if self.tls_cert else "http"
        return f"{scheme}://{self.host}:{self.port}"


def serve_http(session: LabSession, **kw: Any) -> None:
    """Blocking entry point used by the CLI."""
    server = GatewayServer(session, **kw)
    import sys
    print(f"LabAIAgent gateway on {server.host}:{server.port} "
          f"(tls={'on' if server.tls_cert else 'OFF'}, "
          f"auth={'on' if server.auth else 'off/loopback'}, "
          f"readonly={server.readonly}, "
          f"watchdog={'on' if server.watchdog else 'off'}) -- "
          f"dashboard at /, OpenAPI at /openapi.json, MCP at POST /mcp, "
          f"events at /events, metrics at /metrics",
          file=sys.stderr)
    if not server.tls_cert and server.host not in ("127.0.0.1", "localhost", "::1"):
        print("WARNING: serving PLAINTEXT HTTP on a non-loopback interface; "
              "API keys will transit in the clear. Use --tls-cert/--tls-key "
              "or terminate TLS at a reverse proxy.", file=sys.stderr)
    server.serve_forever()


__all__ = ["AuthThrottle", "GatewayServer", "serve_http", "SECURITY_HEADERS"]
