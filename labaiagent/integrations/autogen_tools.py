"""AutoGen / AG2 adapter.

AutoGen registers plain callables with a name and description:

    from labaiagent.integrations.autogen_tools import get_functions
    for fn in get_functions(session):
        assistant.register_for_llm(name=fn.__name__,
                                   description=fn.__doc__)(fn)
        executor.register_for_execution(name=fn.__name__)(fn)
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .base import Target, make_callables


def get_functions(target: Target, *,
                  readonly_only: bool = False) -> list[Callable[..., Any]]:
    # String returns: AutoGen forwards tool output straight into chat.
    return make_callables(target, readonly_only=readonly_only, as_json=True)


__all__ = ["get_functions"]
