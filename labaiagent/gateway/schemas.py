"""Tool-schema exporters: one registry, every vendor's dialect.

These are pure functions of the tool registry -- no lab, no session, no
network. They exist so that connecting a new agent runtime is a schema
export plus an HTTP call, not an integration project:

    from labaiagent.gateway import schemas
    schemas.to_openai_tools()      # OpenAI chat.completions / Responses API
    schemas.to_anthropic_tools()   # Anthropic Messages API
    schemas.to_gemini_tools()      # Google Gemini function calling
    schemas.to_openapi(...)        # OpenAPI 3.1 document for the REST gateway

The CLI mirrors them: ``labaiagent schemas --format openai``.
"""

from __future__ import annotations

import copy
from typing import Any

from .registry import TOOL_SPECS, ToolSpec


def _specs(readonly_only: bool = False) -> list[ToolSpec]:
    return [t for t in TOOL_SPECS
            if not readonly_only or t.readonly
            or getattr(t, "always_available", False)]


def _clean_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """A defensive deep copy so callers can mutate their export freely."""
    return copy.deepcopy(schema)


# --------------------------------------------------------------------------
# Vendor dialects
# --------------------------------------------------------------------------

def to_openai_tools(*, readonly_only: bool = False) -> list[dict[str, Any]]:
    """OpenAI tools array (chat.completions `tools=` / Responses API)."""
    return [{
        "type": "function",
        "function": {
            "name": t.name,
            "description": t.description,
            "parameters": _clean_schema(t.input_schema),
        },
    } for t in _specs(readonly_only)]


def to_anthropic_tools(*, readonly_only: bool = False) -> list[dict[str, Any]]:
    """Anthropic Messages API `tools=` array."""
    return [{
        "name": t.name,
        "description": t.description,
        "input_schema": _clean_schema(t.input_schema),
    } for t in _specs(readonly_only)]


def to_gemini_tools(*, readonly_only: bool = False) -> list[dict[str, Any]]:
    """Google Gemini `tools=[{function_declarations: [...]}]`.

    Gemini accepts an OpenAPI-schema subset; enum-with-default and plain JSON
    Schema types used by the registry are within it.
    """
    return [{
        "function_declarations": [{
            "name": t.name,
            "description": t.description,
            "parameters": _clean_schema(t.input_schema),
        } for t in _specs(readonly_only)]
    }]


# --------------------------------------------------------------------------
# OpenAPI 3.1 -- the REST gateway's contract, importable by anything
# --------------------------------------------------------------------------

def to_openapi(*, title: str = "LabAIAgent Gateway", version: str = "",
               server_url: str = "/",
               readonly_only: bool = False) -> dict[str, Any]:
    paths: dict[str, Any] = {
        "/health": {"get": {
            "operationId": "health", "summary": "Liveness and lab identity",
            "responses": {"200": {"description": "OK"}}}},
        "/tools": {"get": {
            "operationId": "list_tools",
            "summary": "The tool registry with input schemas",
            "responses": {"200": {"description": "Tool list"}}}},
        "/manifest": {"get": {
            "operationId": "manifest",
            "summary": "Machine-readable manifest of every device",
            "responses": {"200": {"description": "Lab manifest"}}}},
        "/reference": {"get": {
            "operationId": "reference",
            "summary": "Natural-language operating reference for the lab",
            "responses": {"200": {"description": "Reference text"}}}},
        "/events": {"get": {
            "operationId": "events",
            "summary": "Server-Sent Events stream: device state, jobs, approvals",
            "responses": {"200": {"description": "text/event-stream"}}}},
        "/openapi.json": {"get": {
            "operationId": "openapi",
            "summary": "This document",
            "responses": {"200": {"description": "OpenAPI 3.1"}}}},
    }
    for t in _specs(readonly_only):
        paths[f"/tools/{t.name}"] = {"post": {
            "operationId": t.name,
            "summary": t.description.split(". ")[0][:120],
            "description": t.description,
            "requestBody": {
                "required": bool(t.input_schema.get("required")),
                "content": {"application/json": {
                    "schema": _clean_schema(t.input_schema)}},
            },
            "responses": {
                "200": {"description":
                        "Structured result. `ok: false` payloads name the "
                        "violated constraint and the permitted range -- they "
                        "are repair instructions, not opaque failures."},
                "401": {"description": "Missing or invalid API key"},
                "403": {"description": "Principal's role does not allow this tool"},
            },
        }}
    if not version:
        from .. import __version__
        version = __version__
    return {
        "openapi": "3.1.0",
        "info": {
            "title": title,
            "version": version,
            "description":
                "Vendor-neutral control, safety and audit gateway for "
                "laboratory instruments. Every actuation passes a fail-closed "
                "safety engine and lands in a hash-chained audit log.",
        },
        "servers": [{"url": server_url}],
        "components": {"securitySchemes": {
            "bearer": {"type": "http", "scheme": "bearer"},
            "apiKey": {"type": "apiKey", "in": "header", "name": "X-API-Key"},
        }},
        "security": [{"bearer": []}, {"apiKey": []}],
        "paths": paths,
    }


__all__ = ["to_openai_tools", "to_anthropic_tools", "to_gemini_tools",
           "to_openapi"]
