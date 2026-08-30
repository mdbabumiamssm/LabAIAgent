"""Hugging Face smolagents adapter.

    from labaiagent.integrations.smolagents_tools import get_tools
    agent = ToolCallingAgent(tools=get_tools(session), model=model)
"""

from __future__ import annotations

import json
from typing import Any

from ..gateway.registry import dispatch
from .base import Target, specs

_JSON_TO_SMOL = {"string": "string", "integer": "integer", "number": "number",
                 "boolean": "boolean", "object": "object", "array": "array"}


def get_tools(target: Target, *, readonly_only: bool = False) -> list[Any]:
    try:
        from smolagents import Tool  # lazy
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "smolagents adapter needs smolagents: "
            "pip install smolagents") from exc

    tools: list[Any] = []
    for spec in specs(readonly_only=readonly_only):
        props = spec.input_schema.get("properties", {}) or {}
        required = set(spec.input_schema.get("required", []) or [])
        inputs = {
            name: {"type": _JSON_TO_SMOL.get(p.get("type", "string"), "string"),
                   "description": p.get("description", ""),
                   **({} if name in required else {"nullable": True})}
            for name, p in props.items()
        }

        def make(spec=spec):
            def forward(self: Any, **kwargs: Any) -> str:
                clean = {k: v for k, v in kwargs.items() if v is not None}
                return json.dumps(dispatch(target, spec.name, clean),
                                  default=str)
            return forward

        tool_cls = type(
            f"LabAIAgent_{spec.name}", (Tool,),
            {"name": spec.name, "description": spec.description,
             "inputs": inputs, "output_type": "string", "forward": make()},
        )
        tools.append(tool_cls())
    return tools


__all__ = ["get_tools"]
