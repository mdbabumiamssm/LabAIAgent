"""LlamaIndex adapter.

    from labaiagent.integrations.llamaindex_tools import get_tools
    agent = FunctionAgent(tools=get_tools(session), llm=llm)
"""

from __future__ import annotations

from typing import Any

from .base import Target, make_callables


def get_tools(target: Target, *, readonly_only: bool = False) -> list[Any]:
    try:
        from llama_index.core.tools import FunctionTool  # lazy
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "LlamaIndex adapter needs llama-index-core: "
            "pip install llama-index-core") from exc

    return [FunctionTool.from_defaults(fn=fn, name=fn.__name__,
                                       description=fn.__doc__ or "")
            for fn in make_callables(target, readonly_only=readonly_only,
                                     as_json=True)]


__all__ = ["get_tools"]
