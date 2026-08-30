"""Python client for a LabAIAgent gateway.

Stdlib-only, synchronous, typed at the payload level. This is the SDK an
agent harness (or a plain script) uses to drive a remote lab:

    from labaiagent.client import LabClient

    lab = LabClient("http://lab-pc:8859", api_key="lak_...")
    lab.tools()                                   # discover
    lab.call("read_state", device_id="reader", capability="read_count")
    job = lab.call("run_procedure", device_id="cycler",
                   capability="run_qpcr", arguments={"cycles": 40},
                   mode="async")
    lab.wait_job(job["result"]["job_id"], timeout=7200)

Tool failures are returned, not raised (they are repair instructions for the
agent); transport failures raise ``GatewayUnreachable``.
"""

from __future__ import annotations

import json
import ssl
import time
import urllib.error
import urllib.request
from typing import Any


class GatewayUnreachable(ConnectionError):
    pass


class ToolFailed(RuntimeError):
    """Raised only by the ``raise_on_error=True`` convenience paths."""

    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__(payload.get("message", str(payload)))
        self.payload = payload


class LabClient:
    def __init__(self, base_url: str, *, api_key: str | None = None,
                 timeout: float = 60.0, ca_file: str | None = None,
                 verify_tls: bool = True) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        if base_url.startswith("https"):
            ctx = ssl.create_default_context(cafile=ca_file)
            if not verify_tls:
                # Explicit opt-out for bench setups with self-signed certs.
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            self._ssl_ctx: ssl.SSLContext | None = ctx
        else:
            self._ssl_ctx = None

    # -- HTTP plumbing -----------------------------------------------------

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def _request(self, method: str, path: str,
                 body: dict[str, Any] | None = None, *,
                 timeout: float | None = None) -> Any:
        req = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(body).encode() if body is not None else None,
            method=method, headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout,
                                        context=self._ssl_ctx) as r:
                raw = r.read().decode("utf-8")
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                raise GatewayUnreachable(
                    f"{method} {path} -> HTTP {exc.code}: {raw[:300]}") from exc
        except urllib.error.URLError as exc:
            raise GatewayUnreachable(
                f"Gateway at {self.base_url} unreachable: {exc.reason}") from exc

    # -- discovery -----------------------------------------------------------

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/health")

    def tools(self) -> list[dict[str, Any]]:
        return self._request("GET", "/tools")["tools"]

    def openapi(self) -> dict[str, Any]:
        return self._request("GET", "/openapi.json")

    def manifest(self) -> dict[str, Any]:
        return self._request("GET", "/manifest")

    # -- invocation ------------------------------------------------------------

    def call(self, tool: str, *, timeout: float | None = None,
             **arguments: Any) -> dict[str, Any]:
        """Invoke one tool. Returns the structured payload (ok True/False)."""
        return self._request("POST", f"/tools/{tool}", arguments,
                             timeout=timeout)

    def call_or_raise(self, tool: str, **arguments: Any) -> Any:
        out = self.call(tool, **arguments)
        if not out.get("ok"):
            raise ToolFailed(out)
        return out["result"]

    # -- conveniences mirroring LabSession -----------------------------------

    def read(self, device_id: str, capability: str, **arguments: Any) -> Any:
        return self.call_or_raise("read_state", device_id=device_id,
                                  capability=capability,
                                  arguments=arguments or None)["value"]

    def write(self, device_id: str, capability: str, *, reason: str = "",
              approval: str = "", **arguments: Any) -> Any:
        return self.call_or_raise("write_state", device_id=device_id,
                                  capability=capability, reason=reason,
                                  approval=approval, arguments=arguments)

    def run(self, device_id: str, capability: str, *, reason: str = "",
            approval: str = "", mode: str = "sync", **arguments: Any) -> Any:
        return self.call_or_raise("run_procedure", device_id=device_id,
                                  capability=capability, reason=reason,
                                  approval=approval, mode=mode,
                                  arguments=arguments)

    def snapshot(self) -> dict[str, Any]:
        return self.call_or_raise("snapshot")

    # -- jobs -------------------------------------------------------------------

    def get_job(self, job_id: str) -> dict[str, Any]:
        return self.call_or_raise("get_job", job_id=job_id)

    def cancel_job(self, job_id: str, reason: str = "") -> dict[str, Any]:
        return self.call_or_raise("cancel_job", job_id=job_id, reason=reason)

    def wait_job(self, job_id: str, *, timeout: float = 3600.0,
                 poll_s: float = 1.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            job = self.get_job(job_id)
            if job["state"] in ("succeeded", "failed", "cancelled"):
                return job
            if time.monotonic() >= deadline:
                return job
            time.sleep(poll_s)


__all__ = ["LabClient", "GatewayUnreachable", "ToolFailed"]
