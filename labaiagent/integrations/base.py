"""Framework-neutral building blocks shared by every adapter."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from ..gateway.registry import TOOL_SPECS, GatewayContext, ToolSpec, dispatch
from ..orchestration.session import LabSession

Target = LabSession | GatewayContext

_JSON_TO_PY = {"string": str, "integer": int, "number": float,
               "boolean": bool, "object": dict, "array": list}


def specs(*, readonly_only: bool = False) -> list[ToolSpec]:
    return [t for t in TOOL_SPECS if t.readonly or not readonly_only]


def make_callables(target: Target, *, readonly_only: bool = False,
                   as_json: bool = False) -> list[Callable[..., Any]]:
    """Plain Python callables, one per tool -- the lowest common denominator
    that AutoGen, custom loops, and REPL use all accept.

    Each callable takes the tool's declared keyword arguments, carries the
    tool description as its docstring, and returns the structured payload
    (or its JSON string with ``as_json=True``, for frameworks that require
    string tool outputs).
    """
    out: list[Callable[..., Any]] = []
    for spec in specs(readonly_only=readonly_only):
        out.append(_make_callable(target, spec, as_json))
    return out


def _make_callable(target: Target, spec: ToolSpec, as_json: bool):
    def fn(**kwargs: Any) -> Any:
        result = dispatch(target, spec.name, kwargs)
        return json.dumps(result, default=str) if as_json else result

    fn.__name__ = spec.name
    fn.__qualname__ = spec.name
    fn.__doc__ = spec.description
    fn.__labaiagent_schema__ = spec.input_schema  # type: ignore[attr-defined]
    return fn


def pydantic_model_for(spec: ToolSpec):
    """Build a pydantic model from a tool's JSON Schema (LangChain / CrewAI
    want one as ``args_schema``). Requires pydantic v2."""
    from pydantic import Field, create_model  # lazy

    props = spec.input_schema.get("properties", {}) or {}
    required = set(spec.input_schema.get("required", []) or [])
    fields: dict[str, Any] = {}
    for pname, pschema in props.items():
        ptype = _JSON_TO_PY.get(pschema.get("type", "string"), str)
        desc = pschema.get("description", "")
        if pname in required:
            fields[pname] = (ptype, Field(..., description=desc))
        else:
            default = pschema.get("default", None)
            fields[pname] = (ptype | None if default is None else ptype,
                             Field(default, description=desc))
    return create_model(f"{spec.name}_args", **fields)


__all__ = ["Target", "specs", "make_callables", "pydantic_model_for"]
