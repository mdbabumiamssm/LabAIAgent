"""Anthropic Messages API tool-use adapter.

    from labaiagent.integrations import anthropic_tools as lab_an

    tools = lab_an.get_tools()                      # pass as tools=...
    ...model returns tool_use blocks...
    messages.append({"role": "user",
                     "content": lab_an.execute_tool_uses(session, response)})

(When the agent side is Claude Desktop / Claude Code, prefer the MCP server:
``labaiagent serve --lab lab.yaml``. This adapter is for direct API loops.)
"""

from __future__ import annotations

import json
from typing import Any

from ..gateway.registry import dispatch
from ..gateway.schemas import to_anthropic_tools
from .base import Target


def get_tools(*, readonly_only: bool = False) -> list[dict[str, Any]]:
    return to_anthropic_tools(readonly_only=readonly_only)


def execute_tool_use(target: Target, block: Any) -> dict[str, Any]:
    """Execute ONE tool_use block and return the tool_result block."""
    if isinstance(block, dict):
        use_id, name = block.get("id", ""), block.get("name", "")
        arguments = block.get("input", {}) or {}
    else:  # SDK object
        use_id, name, arguments = block.id, block.name, dict(block.input or {})
    out = dispatch(target, name, arguments)
    return {"type": "tool_result", "tool_use_id": use_id,
            "content": json.dumps(out, default=str),
            "is_error": not out.get("ok", False)}


def execute_tool_uses(target: Target, response: Any) -> list[dict[str, Any]]:
    content = (response.content if hasattr(response, "content")
               else response.get("content", []))
    results = []
    for block in content or []:
        btype = block.get("type") if isinstance(block, dict) else block.type
        if btype == "tool_use":
            results.append(execute_tool_use(target, block))
    return results


__all__ = ["get_tools", "execute_tool_use", "execute_tool_uses"]
