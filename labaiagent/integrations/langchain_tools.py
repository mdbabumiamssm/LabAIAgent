"""LangChain / LangGraph adapter.

    from labaiagent.integrations.langchain_tools import get_tools
    tools = get_tools(session)          # list[StructuredTool]
    agent = create_react_agent(model, tools)        # LangGraph
"""

from __future__ import annotations

import json
from typing import Any

from ..gateway.registry import dispatch
from .base import Target, pydantic_model_for, specs


def get_tools(target: Target, *, readonly_only: bool = False) -> list[Any]:
    try:
        from langchain_core.tools import StructuredTool  # lazy
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "LangChain adapter needs langchain-core: "
            "pip install langchain-core") from exc

    tools: list[Any] = []
    for spec in specs(readonly_only=readonly_only):
        def make(spec=spec):
            def run(**kwargs: Any) -> str:
                return json.dumps(dispatch(target, spec.name, kwargs),
                                  default=str)
            return run

        tools.append(StructuredTool.from_function(
            func=make(),
            name=spec.name,
            description=spec.description,
            args_schema=pydantic_model_for(spec),
        ))
    return tools


__all__ = ["get_tools"]
