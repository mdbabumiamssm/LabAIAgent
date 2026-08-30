"""Capability decorators.

A driver author annotates plain methods. Everything downstream -- argument
validation, safety enforcement, the JSON manifest, the natural-language
reference sheet, MCP tool exposure, and the audit trail -- is derived from
these annotations. That is the whole trick: describe the instrument once,
declaratively, and never write integration glue again.
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any, cast

from .types import Kind, Param, Risk

CAPABILITY_ATTR = "__labaiagent_capability__"


@dataclass
class Capability:
    """Metadata attached to a decorated driver method."""

    name: str
    kind: Kind
    description: str
    params: tuple[Param, ...] = ()
    unit: str = ""                      # for READ: unit of the returned value
    returns: str = ""                   # human description of the return value
    risk: Risk = Risk.NONE
    requires_confirmation: bool = False
    requires_states: tuple[str, ...] = ()   # device states in which this is legal
    interlocks: tuple[str, ...] = ()        # named interlocks that must pass
    est_duration_s: float | None = None
    blocking: bool = True               # occupies the device for its duration
    reversible: bool = True             # can the effect be undone?
    consumes: tuple[str, ...] = ()      # consumables burned (tips, reagent, sample)
    tags: tuple[str, ...] = ()
    method_name: str = ""

    def validate_args(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        """Validate and coerce a kwargs dict against the declared parameters.

        Rejections raise ``LimitViolation``, which carries the parameter name,
        the offending value and the permitted range as structured fields. That
        matters more than it looks: when the caller is a model, the error text
        is the entire repair signal, and "invalid argument" costs a turn and
        often produces a worse second guess than the first.
        """
        from .errors import LimitViolation

        spec = {p.name: p for p in self.params}
        unknown = set(kwargs) - set(spec)
        if unknown:
            raise LimitViolation(
                f"{self.name}: unexpected argument(s) {sorted(unknown)}. "
                f"Accepted: {sorted(spec)}",
                capability=self.name, constraint="unknown_argument",
                detail={"unexpected": sorted(unknown), "accepted": sorted(spec)},
            )
        out: dict[str, Any] = {}
        for pname, p in spec.items():
            if pname in kwargs:
                try:
                    out[pname] = p.validate(kwargs[pname])
                except (ValueError, TypeError) as exc:
                    permitted = ("; ".join(
                        lim.describe()
                        for lim in cast("tuple[Any, ...]", p.limits))
                        or "any value")
                    raise LimitViolation(
                        f"{self.name}: {exc}",
                        capability=self.name, constraint="parameter_limit",
                        parameter=pname, value=kwargs[pname],
                        permitted=permitted, unit=p.unit,
                        detail={"parameter": p.to_dict()},
                    ) from None
            elif p.required:
                raise LimitViolation(
                    f"{self.name}: missing required argument {pname!r} "
                    f"({p.describe()})",
                    capability=self.name, constraint="missing_argument",
                    parameter=pname,
                    detail={"required": [q.name for q in self.params if q.required]},
                )
            else:
                out[pname] = p.default
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind.value,
            "description": self.description,
            "parameters": [p.to_dict() for p in self.params],
            "unit": self.unit,
            "returns": self.returns,
            "risk": self.risk.value,
            "requires_confirmation": self.requires_confirmation,
            "requires_states": list(self.requires_states),
            "interlocks": list(self.interlocks),
            "estimated_duration_s": self.est_duration_s,
            "blocking": self.blocking,
            "reversible": self.reversible,
            "consumes": list(self.consumes),
            "tags": list(self.tags),
        }

    def describe(self) -> str:
        """Human/LLM-readable one-entry reference."""
        lines = [f"{self.name} [{self.kind.value}, risk={self.risk.value}]"]
        if self.description:
            lines.append(f"    {self.description}")
        for p in self.params:
            lines.append(f"    - {p.describe()}")
        if self.kind is Kind.READ and self.unit:
            lines.append(f"    returns: {self.returns or 'value'} in {self.unit}")
        elif self.returns:
            lines.append(f"    returns: {self.returns}")
        flags = []
        if not self.reversible:
            flags.append("IRREVERSIBLE")
        if self.requires_confirmation:
            flags.append("requires human confirmation")
        if self.consumes:
            flags.append("consumes " + ", ".join(self.consumes))
        if self.interlocks:
            flags.append("interlocks: " + ", ".join(self.interlocks))
        if self.est_duration_s:
            flags.append(f"~{self.est_duration_s:g}s")
        if flags:
            lines.append("    " + " | ".join(flags))
        return "\n".join(lines)


def _make_decorator(kind: Kind, default_risk: Risk) -> Callable[..., Any]:
    def decorator(
        name: str | None = None,
        *,
        description: str = "",
        params: Sequence[Param] | None = None,
        unit: str = "",
        returns: str = "",
        risk: Risk | str = default_risk,
        requires_confirmation: bool = False,
        requires_states: Iterable[str] = ("idle",),
        interlocks: Iterable[str] = (),
        est_duration_s: float | None = None,
        blocking: bool = True,
        reversible: bool = True,
        consumes: Iterable[str] = (),
        tags: Iterable[str] = (),
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def wrap(fn: Callable[..., Any]) -> Callable[..., Any]:
            cap_name = name or fn.__name__
            desc = description or inspect.cleandoc(fn.__doc__ or "").split("\n\n")[0]
            cap = Capability(
                name=cap_name,
                kind=kind,
                description=desc,
                params=tuple(params or ()),
                unit=unit,
                returns=returns,
                risk=Risk(risk) if not isinstance(risk, Risk) else risk,
                requires_confirmation=requires_confirmation,
                requires_states=tuple(requires_states),
                interlocks=tuple(interlocks),
                est_duration_s=est_duration_s,
                blocking=blocking,
                reversible=reversible,
                consumes=tuple(consumes),
                tags=tuple(tags),
                method_name=fn.__name__,
            )
            # Reads never mutate state, so they are legal from any live state --
            # explicitly including ESTOPPED. Observing an instrument after an
            # emergency stop is exactly when you most need to, and a control
            # layer that goes blind at the moment something went wrong is worse
            # than useless.
            if kind is Kind.READ and requires_states == ("idle",):
                cap.requires_states = ("idle", "busy", "error", "maintenance",
                                       "estopped")

            @functools.wraps(fn)
            def inner(*args: Any, **kwargs: Any) -> Any:
                return fn(*args, **kwargs)

            setattr(inner, CAPABILITY_ATTR, cap)
            return inner

        return wrap

    return decorator


#: Non-mutating sensor read. Always safe; exposed to agents unconditionally.
read = _make_decorator(Kind.READ, Risk.NONE)

#: Single setpoint change or discrete actuation.
write = _make_decorator(Kind.WRITE, Risk.MEDIUM)

#: Multi-step operation that occupies the instrument (a run, a transfer series).
procedure = _make_decorator(Kind.PROCEDURE, Risk.MEDIUM)


#: Short prefixes used in qualified capability keys.
KIND_PREFIX = {Kind.READ: "read", Kind.WRITE: "write", Kind.PROCEDURE: "proc"}
PREFIX_KIND = {v: k for k, v in KIND_PREFIX.items()}


def qualified(kind: Kind, name: str) -> str:
    """Canonical capability key, e.g. ``write:flow_rate``.

    Reads and writes deliberately share a name when they address the same
    physical state variable -- 'read temperature' / 'write temperature' is the
    natural instrument model, and forcing ``get_temperature`` /
    ``set_temperature`` into the agent-facing namespace would lose that
    symmetry. The kind prefix keeps them distinct without renaming either.
    """
    return f"{KIND_PREFIX[kind]}:{name}"


def split_key(key: str) -> tuple[Kind | None, str]:
    """Parse ``'write:flow_rate'`` -> (Kind.WRITE, 'flow_rate').

    An unqualified name yields ``(None, name)`` and is resolved later against
    the device's actual capability set.
    """
    if ":" in key:
        prefix, _, rest = key.partition(":")
        if prefix in PREFIX_KIND:
            return PREFIX_KIND[prefix], rest
    return None, key


def collect_capabilities(obj: Any) -> dict[str, Capability]:
    """Walk an instance/class and gather every decorated capability.

    Returns a mapping of qualified key -> Capability. Subclass overrides win,
    because ``dir()`` resolves through the MRO -- so a vendor-specific driver
    can refine a capability inherited from a generic base class without
    redeclaring the rest.
    """
    import copy

    found: dict[str, Capability] = {}
    for attr in dir(type(obj)):
        if attr.startswith("__"):
            continue
        try:
            member = getattr(type(obj), attr)
        except AttributeError:  # pragma: no cover - defensive
            continue
        cap = getattr(member, CAPABILITY_ATTR, None)
        if isinstance(cap, Capability):
            key = qualified(cap.kind, cap.name)
            existing = found.get(key)
            if existing is not None and existing.method_name != cap.method_name:
                raise TypeError(
                    f"Duplicate capability {key!r} on {type(obj).__name__} "
                    f"(methods {existing.method_name} and {cap.method_name}). "
                    f"Two capabilities of the same kind cannot share a name; "
                    f"a read/write pair may."
                )
            # Deep-copy per instance. The decorator attaches ONE Capability to
            # the class, so without this every instance of a driver shares the
            # same limits object -- and per-instance config refinement on one
            # device would silently rewrite the safety limits of every other
            # device using that driver.
            found[key] = copy.deepcopy(cap)
    return found


__all__ = ["Capability", "read", "write", "procedure", "collect_capabilities",
           "CAPABILITY_ATTR", "qualified", "split_key", "KIND_PREFIX", "PREFIX_KIND"]
