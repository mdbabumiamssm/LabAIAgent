"""OpenAI tool-calling adapter (chat.completions and the Responses API).

    from labaiagent.integrations import openai_tools as lab_oa

    tools = lab_oa.get_tools()                       # pass as tools=...
    ...model returns tool calls...
    messages += lab_oa.execute_tool_calls(session, response)

No OpenAI SDK import is required to *serve* tools -- only the shapes matter.
"""

from __future__ import annotations

import json
from typing import Any

from ..gateway.registry import dispatch
from ..gateway.schemas import to_openai_tools
from .base import Target


def get_tools(*, readonly_only: bool = False) -> list[dict[str, Any]]:
    return to_openai_tools(readonly_only=readonly_only)


def execute_tool_call(target: Target, tool_call: Any) -> dict[str, Any]:
    """Execute ONE tool call object/dict from a chat.completions response and
    return the ``role="tool"`` message to append."""
    if isinstance(tool_call, dict):
        call_id = tool_call.get("id", "")
        fn = tool_call.get("function", {}) or {}
        name, raw_args = fn.get("name", ""), fn.get("arguments", "{}")
    else:  # SDK object
        call_id = tool_call.id
        name, raw_args = tool_call.function.name, tool_call.function.arguments
    try:
        arguments = json.loads(raw_args or "{}")
    except json.JSONDecodeError:
        arguments = {}
    out = dispatch(target, name, arguments)
    return {"role": "tool", "tool_call_id": call_id,
            "content": json.dumps(out, default=str)}


def execute_tool_calls(target: Target, response: Any) -> list[dict[str, Any]]:
    """Execute every tool call in a chat.completions response message."""
    message = (response.choices[0].message
               if hasattr(response, "choices") else response)
    calls = getattr(message, "tool_calls", None) or (
        message.get("tool_calls", []) if isinstance(message, dict) else [])
    return [execute_tool_call(target, c) for c in calls or []]


__all__ = ["get_tools", "execute_tool_call", "execute_tool_calls"]
