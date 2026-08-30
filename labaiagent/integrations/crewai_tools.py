"""CrewAI adapter.

    from labaiagent.integrations.crewai_tools import get_tools
    agent = Agent(role="lab tech", tools=get_tools(session), ...)
"""

from __future__ import annotations

import json
from typing import Any

from ..gateway.registry import dispatch
from .base import Target, pydantic_model_for, specs


def get_tools(target: Target, *, readonly_only: bool = False) -> list[Any]:
    try:
        from crewai.tools import BaseTool  # lazy
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "CrewAI adapter needs crewai: pip install crewai") from exc

    tools: list[Any] = []
    for spec in specs(readonly_only=readonly_only):
        args_model = pydantic_model_for(spec)

        def make(spec=spec):
            def _run(self: Any, **kwargs: Any) -> str:
                return json.dumps(dispatch(target, spec.name, kwargs),
                                  default=str)
            return _run

        tool_cls = type(
            f"LabAIAgent_{spec.name}", (BaseTool,),
            {"name": spec.name, "description": spec.description,
             "args_schema": args_model, "_run": make()},
        )
        tools.append(tool_cls())
    return tools


__all__ = ["get_tools"]
