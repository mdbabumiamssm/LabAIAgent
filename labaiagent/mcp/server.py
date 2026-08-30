"""MCP adapter -- the Model Context Protocol rendering of the tool registry.

The tools themselves live in ``labaiagent.gateway.registry``; this module is
one of several mechanical renderings of that single registry (REST, OpenAI
schemas, LangChain, ... are the others). Design decisions worth restating:

1. **The tool surface is small and fixed** -- twenty tools regardless of
   whether the lab has three instruments or ninety. Adding instrument N+1
   changes no tool schema.
2. **Reads and writes are different tools.** ``read_state`` cannot actuate,
   ever, and a read-only agent can be handed the read tools alone.
3. **Errors are repair instructions** -- structured payloads naming the
   violated constraint and the permitted range.
4. **The dangerous tools are gated by the session, not by the server.**
   There is exactly one policy point.

Serving modes:
  - stdio with the official ``mcp`` SDK when installed (``--sdk``)
  - a dependency-free stdio JSON-RPC loop otherwise -- instrument PCs are
    often machines you cannot freely ``pip install`` on
  - HTTP: ``POST /mcp`` on the REST gateway speaks the same JSON-RPC, so
    remote MCP clients connect over the network (see gateway/rest.py)

Beyond tools, the stdio loop also serves MCP **resources**, so clients can
load lab context without spending tool calls:

  lab://manifest         machine-readable manifest of every device
  lab://reference        the whole-lab natural-language operating reference
  lab://audit/tail       the most recent audit records
"""

from __future__ import annotations

import json
import sys
from typing import Any

from ..gateway.auth import Principal
from ..gateway.registry import (
    TOOL_SPECS,
    TOOLS,  # re-exported for backwards compatibility
    GatewayContext,
    dispatch,  # re-exported: the single dispatch every adapter uses
    tool_index,
)
from ..orchestration.session import LabSession

PROTOCOL_VERSION = "2025-06-18"


def _pkg_version() -> str:
    from .. import __version__
    return __version__

RESOURCES: list[dict[str, str]] = [
    {"uri": "lab://manifest", "name": "Lab manifest",
     "description": "Machine-readable manifest: every device, capability and limit.",
     "mimeType": "application/json"},
    {"uri": "lab://reference", "name": "Lab operating reference",
     "description": "Natural-language operating reference for the whole lab.",
     "mimeType": "text/plain"},
    {"uri": "lab://audit/tail", "name": "Recent audit records",
     "description": "The most recent entries of the tamper-evident audit log.",
     "mimeType": "application/json"},
]


def read_resource(session: LabSession, uri: str) -> tuple[str, str]:
    """Return (mimeType, text) for a lab:// resource URI."""
    if uri == "lab://manifest":
        return "application/json", json.dumps(session.manifest(), indent=2,
                                              default=str)
    if uri == "lab://reference":
        return "text/plain", session.reference_sheets()
    if uri == "lab://audit/tail":
        return "application/json", json.dumps(session.audit.tail(50), indent=2,
                                              default=str)
    raise KeyError(uri)


# --------------------------------------------------------------------------
# Official MCP SDK path
# --------------------------------------------------------------------------

def build_fastmcp_server(session: LabSession, *, name: str = "labaiagent",
                         readonly: bool = False,
                         principal: Principal | None = None):
    """Build a FastMCP server if the ``mcp`` package is available."""
    try:
        from mcp.server.fastmcp import FastMCP  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "The official MCP SDK is not installed. Either "
            "`pip install 'labaiagent[mcp]'`, or use serve_stdio() which "
            "needs no dependencies."
        ) from exc

    ctx = GatewayContext.for_session(session)
    server = FastMCP(name)
    for spec in tool_index(readonly_only=readonly).values():
        _bind_fastmcp_tool(server, ctx, spec, principal)
    return server


def _bind_fastmcp_tool(server: Any, ctx: GatewayContext, spec: Any,
                       principal: Principal | None) -> None:
    tname = spec.name

    async def _handler(**kwargs: Any) -> str:
        out = dispatch(ctx, tname, kwargs, principal=principal)
        return json.dumps(out, indent=2, default=str)

    _handler.__name__ = tname
    _handler.__doc__ = spec.description
    server.tool(name=tname, description=spec.description)(_handler)


# --------------------------------------------------------------------------
# Dependency-free stdio JSON-RPC loop
# --------------------------------------------------------------------------

def handle_jsonrpc(session: LabSession, req: dict[str, Any], *,
                   name: str = "labaiagent", readonly: bool = False,
                   principal: Principal | None = None) -> dict[str, Any] | None:
    """Handle one MCP JSON-RPC message; returns the response (or None for
    notifications). Shared by the stdio loop and the HTTP ``POST /mcp``
    endpoint, so both transports behave identically."""
    rid = req.get("id")
    method = req.get("method", "")
    params = req.get("params", {}) or {}

    if method == "initialize":
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}, "resources": {}},
            "serverInfo": {"name": name, "version": _pkg_version()},
            "instructions": session.reference_sheets()[:4000],
        }}
    if method in ("notifications/initialized", "initialized"):
        return None
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "tools": [{"name": t.name, "description": t.description,
                       "inputSchema": t.input_schema}
                      for t in tool_index(readonly_only=readonly).values()]
        }}
    if method == "tools/call":
        out = dispatch(session, params.get("name", ""),
                       params.get("arguments", {}) or {},
                       readonly=readonly, principal=principal)
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "content": [{"type": "text",
                         "text": json.dumps(out, indent=2, default=str)}],
            "isError": not out.get("ok", False),
        }}
    if method == "resources/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"resources": RESOURCES}}
    if method == "resources/read":
        uri = params.get("uri", "")
        try:
            mime, text = read_resource(session, uri)
        except KeyError:
            return {"jsonrpc": "2.0", "id": rid,
                    "error": {"code": -32002, "message": f"unknown resource {uri}"}}
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "contents": [{"uri": uri, "mimeType": mime, "text": text}]}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    return {"jsonrpc": "2.0", "id": rid,
            "error": {"code": -32601, "message": f"unknown method {method}"}}


def serve_stdio(session: LabSession, *, name: str = "labaiagent",
                readonly: bool = False, stream_in: Any = None,
                stream_out: Any = None,
                principal: Principal | None = None) -> None:
    """Minimal MCP-shaped JSON-RPC server over stdio.

    Implements initialize / tools/list / tools/call / resources/*, which is
    the subset a client needs to drive a lab. Written against stdlib only so
    it runs on a locked-down instrument workstation where installing the SDK
    is not an option.
    """
    fin = stream_in or sys.stdin
    fout = stream_out or sys.stdout

    for line in fin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            fout.write(json.dumps({"jsonrpc": "2.0", "id": None,
                                   "error": {"code": -32700,
                                             "message": "parse error"}}) + "\n")
            fout.flush()
            continue
        resp = handle_jsonrpc(session, req, name=name, readonly=readonly,
                              principal=principal)
        if resp is not None:
            fout.write(json.dumps(resp, default=str) + "\n")
            fout.flush()


__all__ = ["TOOLS", "TOOL_SPECS", "dispatch", "build_fastmcp_server",
           "serve_stdio", "handle_jsonrpc", "read_resource", "RESOURCES",
           "PROTOCOL_VERSION"]
