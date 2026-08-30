"""Core value types for LabAIAgent.

Deliberately stdlib-only (dataclasses + enums) so the package installs on
locked-down instrument PCs with no build toolchain.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any, cast


class Risk(str, Enum):
    """Risk tier for a capability.

    Drives whether the safety engine requires confirmation, whether the
    capability is exposed to an autonomous agent, and how loudly it is audited.
    """

    NONE = "none"          # pure read, no physical effect
    LOW = "low"            # reversible, no sample/hardware risk (e.g. set LED)
    MEDIUM = "medium"      # consumes sample/reagent or moves an axis
    HIGH = "high"          # irreversible, can destroy sample or damage hardware
    CRITICAL = "critical"  # can injure a person; always requires human sign-off

    @property
    def rank(self) -> int:
        return {"none": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}[self.value]


class Kind(str, Enum):
    """The three MHS-style primitives."""

    READ = "read"
    WRITE = "write"
    PROCEDURE = "procedure"


class DeviceState(str, Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    IDLE = "idle"
    BUSY = "busy"
    ERROR = "error"
    ESTOPPED = "estopped"
    MAINTENANCE = "maintenance"

    @property
    def can_actuate(self) -> bool:
        return self is DeviceState.IDLE


# --------------------------------------------------------------------------
# Units
# --------------------------------------------------------------------------

# Minimal unit registry. We do not pull in `pint`: instrument work needs a
# small, predictable set of units, and a hard failure on an unknown unit is
# safer than a silent coercion.
_UNIT_DIMENSIONS: dict[str, str] = {
    # volume
    "uL": "volume", "mL": "volume", "L": "volume", "nL": "volume",
    # mass / amount
    "ug": "mass", "mg": "mass", "g": "mass", "ng": "mass",
    "ug/mL": "concentration", "mg/mL": "concentration", "ng/uL": "concentration",
    "nM": "concentration", "uM": "concentration", "mM": "concentration", "M": "concentration",
    # temperature
    "degC": "temperature", "K": "temperature",
    # time
    "s": "time", "ms": "time", "min": "time", "h": "time",
    # rotation / speed
    "rpm": "angular_velocity", "g_force": "acceleration",
    "uL/s": "flow_rate", "mL/min": "flow_rate",
    # optical
    "AU": "absorbance", "RFU": "fluorescence", "nm": "wavelength",
    # geometry / electrical
    "mm": "length", "um": "length", "deg": "angle", "rad": "angle",
    "mV": "voltage", "V": "voltage", "mW": "power", "W": "power",
    "Pa": "pressure", "kPa": "pressure", "bar": "pressure",
    "percent": "dimensionless", "count": "dimensionless", "": "dimensionless",
}

_TO_BASE: dict[str, tuple[str, float]] = {
    "nL": ("uL", 1e-3), "uL": ("uL", 1.0), "mL": ("uL", 1e3), "L": ("uL", 1e6),
    "ng": ("ug", 1e-3), "ug": ("ug", 1.0), "mg": ("ug", 1e3), "g": ("ug", 1e6),
    "ms": ("s", 1e-3), "s": ("s", 1.0), "min": ("s", 60.0), "h": ("s", 3600.0),
    "um": ("mm", 1e-3), "mm": ("mm", 1.0),
    "mV": ("V", 1e-3), "V": ("V", 1.0),
    "mW": ("W", 1e-3), "W": ("W", 1.0),
    # Mass concentration family (base ug/mL). 1 ng/uL == 1 ug/mL.
    "ug/mL": ("ug/mL", 1.0), "mg/mL": ("ug/mL", 1e3), "ng/uL": ("ug/mL", 1.0),
    # Molar concentration family (base nM). Deliberately a DIFFERENT base from
    # mass concentration: converting between them needs a molar mass, so a
    # cross-family convert() raises instead of guessing.
    "nM": ("nM", 1.0), "uM": ("nM", 1e3), "mM": ("nM", 1e6), "M": ("nM", 1e9),
    # Flow (base uL/s).
    "uL/s": ("uL/s", 1.0), "mL/min": ("uL/s", 1e3 / 60.0),
    # Pressure (base Pa).
    "Pa": ("Pa", 1.0), "kPa": ("Pa", 1e3), "bar": ("Pa", 1e5),
}


def dimension_of(unit: str) -> str:
    """Return the physical dimension of ``unit``; raise on unknown units."""
    try:
        return _UNIT_DIMENSIONS[unit]
    except KeyError:
        raise ValueError(
            f"Unknown unit {unit!r}. Add it to labaiagent.core.types._UNIT_DIMENSIONS "
            f"rather than passing an unvalidated string -- silent unit errors are "
            f"the single most common cause of ruined automated runs."
        ) from None


def convert(value: float, frm: str, to: str) -> float:
    """Convert between commensurable units. Raises on dimension mismatch."""
    if frm == to:
        return value
    if dimension_of(frm) != dimension_of(to):
        raise ValueError(f"Cannot convert {frm} -> {to}: different dimensions.")
    if frm == "degC" and to == "K":
        return value + 273.15
    if frm == "K" and to == "degC":
        return value - 273.15
    if frm not in _TO_BASE or to not in _TO_BASE:
        raise ValueError(f"No conversion factor registered for {frm} -> {to}.")
    base_unit_a, fa = _TO_BASE[frm]
    base_unit_b, fb = _TO_BASE[to]
    if base_unit_a != base_unit_b:
        raise ValueError(f"Cannot convert {frm} -> {to}.")
    return value * fa / fb


@dataclass(frozen=True, slots=True)
class Quantity:
    """A value with an attached unit. Comparisons are unit-aware."""

    value: float
    unit: str = ""

    def __post_init__(self) -> None:
        dimension_of(self.unit)  # validate eagerly
        if isinstance(self.value, (int, float)) and not math.isfinite(self.value):
            raise ValueError(f"Non-finite quantity: {self.value}")

    def to(self, unit: str) -> Quantity:
        return Quantity(convert(self.value, self.unit, unit), unit)

    def __str__(self) -> str:
        return f"{self.value:g}{(' ' + self.unit) if self.unit else ''}"


# --------------------------------------------------------------------------
# Limits
# --------------------------------------------------------------------------

class Limit:
    """Base class for a declarative constraint on a parameter value."""

    def check(self, value: Any) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def describe(self) -> str:  # pragma: no cover - abstract
        raise NotImplementedError

    def to_dict(self) -> dict[str, Any]:
        return {"type": type(self).__name__, "description": self.describe()}


@dataclass(frozen=True)
class Range(Limit):
    """Inclusive numeric range, optionally unit-aware."""

    low: float
    high: float
    unit: str = ""

    def check(self, value: Any) -> None:
        v = value.to(self.unit).value if isinstance(value, Quantity) and self.unit else (
            value.value if isinstance(value, Quantity) else value
        )
        if not isinstance(v, (int, float)):
            raise TypeError(f"Range limit needs a number, got {type(v).__name__}")
        if not (self.low <= v <= self.high):
            u = f" {self.unit}" if self.unit else ""
            raise ValueError(
                f"value {v:g}{u} outside permitted range "
                f"[{self.low:g}, {self.high:g}]{u}"
            )

    def describe(self) -> str:
        u = f" {self.unit}" if self.unit else ""
        return f"between {self.low:g} and {self.high:g}{u}"

    def to_dict(self) -> dict[str, Any]:
        return {"type": "Range", "low": self.low, "high": self.high,
                "unit": self.unit, "description": self.describe()}


@dataclass(frozen=True)
class OneOf(Limit):
    """Value must be drawn from an explicit allow-list."""

    options: tuple[Any, ...]

    def __init__(self, *options: Any) -> None:
        object.__setattr__(self, "options", tuple(options))

    def check(self, value: Any) -> None:
        if value not in self.options:
            raise ValueError(f"value {value!r} not one of {list(self.options)}")

    def describe(self) -> str:
        return "one of " + ", ".join(repr(o) for o in self.options)

    def to_dict(self) -> dict[str, Any]:
        return {"type": "OneOf", "options": list(self.options),
                "description": self.describe()}


@dataclass(frozen=True)
class Pattern(Limit):
    """String must match a regular expression (e.g. well IDs like 'A1')."""

    regex: str

    def check(self, value: Any) -> None:
        if not isinstance(value, str) or not re.fullmatch(self.regex, value):
            raise ValueError(f"value {value!r} does not match pattern {self.regex}")

    def describe(self) -> str:
        return f"matching /{self.regex}/"

    def to_dict(self) -> dict[str, Any]:
        return {"type": "Pattern", "regex": self.regex, "description": self.describe()}


@dataclass(frozen=True)
class Length(Limit):
    """Constrain the length of a sequence argument."""

    minimum: int = 0
    maximum: int = 1_000_000

    def check(self, value: Any) -> None:
        if not isinstance(value, Sequence):
            raise TypeError(f"Length limit needs a sequence, got {type(value).__name__}")
        if not (self.minimum <= len(value) <= self.maximum):
            raise ValueError(
                f"sequence of length {len(value)} outside [{self.minimum}, {self.maximum}]"
            )

    def describe(self) -> str:
        return f"between {self.minimum} and {self.maximum} items"

    def to_dict(self) -> dict[str, Any]:
        return {"type": "Length", "minimum": self.minimum, "maximum": self.maximum,
                "description": self.describe()}


@dataclass(frozen=True)
class Predicate(Limit):
    """Escape hatch: an arbitrary callable plus a human-readable description.

    Use sparingly -- a Predicate is opaque to the manifest generator, so an
    agent cannot reason about it the way it can about a Range.
    """

    fn: Callable[[Any], bool]
    description: str

    def check(self, value: Any) -> None:
        if not self.fn(value):
            raise ValueError(f"value {value!r} fails constraint: {self.description}")

    def describe(self) -> str:
        return self.description


# --------------------------------------------------------------------------
# Parameter specification
# --------------------------------------------------------------------------

class _Missing:
    """Singleton marking 'no default supplied'.

    Identity-stable across copy, deepcopy and pickle. A plain ``object()``
    sentinel silently breaks the moment a Param is deepcopied -- the clone
    compares unequal to the original and every required argument starts
    looking optional, which is the sort of bug that only surfaces when a
    protocol runs with a missing volume.
    """

    _instance: _Missing | None = None

    def __new__(cls) -> _Missing:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __copy__(self) -> _Missing:
        return self

    def __deepcopy__(self, memo: dict) -> _Missing:
        return self

    def __reduce__(self) -> tuple:
        return (_Missing, ())

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:
        return "<required>"


MISSING = _Missing()
_MISSING = MISSING  # backwards-compatible alias


@dataclass
class Param:
    """Declarative spec for one capability argument.

    ``limits`` accepts a single Limit or any sequence of Limits; it is
    normalised to a tuple in ``__post_init__``.
    """

    name: str
    type: type = float
    unit: str = ""
    description: str = ""
    default: Any = _MISSING
    limits: Limit | Sequence[Limit] = ()

    def __post_init__(self) -> None:
        if isinstance(self.limits, Limit):
            self.limits = (self.limits,)
        else:
            self.limits = tuple(self.limits)
        if self.unit:
            dimension_of(self.unit)

    @property
    def required(self) -> bool:
        return self.default is _MISSING

    def validate(self, value: Any) -> Any:
        """Coerce and range-check a single argument. Returns the coerced value."""
        if value is None and not self.required:
            return self.default

        # Accept a Quantity for a unit-bearing numeric param, converting as needed.
        if isinstance(value, Quantity):
            if not self.unit:
                raise ValueError(f"{self.name}: unitless parameter given a Quantity")
            value = value.to(self.unit).value

        if self.type in (float, int) and isinstance(value, bool):
            raise TypeError(f"{self.name}: refusing to coerce bool to {self.type.__name__}")

        try:
            if self.type is float and isinstance(value, (int, float)):
                value = float(value)
            elif self.type is int and isinstance(value, float) and value.is_integer():
                value = int(value)
        except (TypeError, ValueError):
            pass

        origin_ok = isinstance(value, self.type) if isinstance(self.type, type) else True
        if not origin_ok:
            raise TypeError(
                f"{self.name}: expected {self.type.__name__}, got {type(value).__name__}"
            )
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"{self.name}: non-finite value {value}")

        for lim in cast("tuple[Limit, ...]", self.limits):
            try:
                lim.check(value)
            except (ValueError, TypeError) as exc:
                raise ValueError(f"{self.name}: {exc}") from None
        return value

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "name": self.name,
            "type": getattr(self.type, "__name__", str(self.type)),
            "unit": self.unit,
            "description": self.description,
            "required": self.required,
            "limits": [lim.to_dict()
                       for lim in cast("tuple[Limit, ...]", self.limits)],
        }
        if not self.required:
            d["default"] = self.default
        return d

    def describe(self) -> str:
        bits = [f"{self.name} ({getattr(self.type, '__name__', self.type)}"
                + (f", {self.unit}" if self.unit else "") + ")"]
        if self.description:
            bits.append("- " + self.description)
        if self.limits:
            bits.append("[" + "; ".join(
                lim.describe()
                for lim in cast("tuple[Limit, ...]", self.limits)) + "]")
        if not self.required:
            bits.append(f"(default {self.default!r})")
        return " ".join(bits)


__all__ = [
    "Risk", "Kind", "DeviceState", "Quantity", "convert", "dimension_of",
    "Limit", "Range", "OneOf", "Pattern", "Length", "Predicate", "Param",
    "MISSING",
]
