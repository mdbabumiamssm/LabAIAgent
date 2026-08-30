"""The driver contract.

To integrate instrument N+1 you subclass ``Device``, implement three lifecycle
hooks, and decorate your methods. You do not touch the registry, the safety
engine, the scheduler, the audit log, or the MCP server -- those consume the
manifest your decorators already produced.

    class MyPump(Device):
        vendor, model, category = "Acme", "P-200", "pump"

        def _connect(self):  self.link = open_serial(self.config["port"])
        def _disconnect(self): self.link.close()
        def _self_test(self): return self.link.query("*IDN?").startswith("ACME")

        @read("flow_rate", unit="uL/s", description="Current flow rate")
        def get_flow(self) -> float:
            return float(self.link.query("FLOW?"))

        @write("flow_rate", risk=Risk.MEDIUM,
               params=[Param("value", float, "uL/s", limits=Range(0, 200, "uL/s"))])
        def set_flow(self, value: float) -> None:
            self.link.write(f"FLOW {value:.2f}")

That is the whole integration. Everything else is inherited.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable
from typing import Any, cast

from .capability import Capability, collect_capabilities, qualified, split_key
from .errors import (
    CapabilityNotFound,
    DriverError,
    PhysicalError,
    TransientError,
    TransportError,
)
from .types import DeviceState, Kind, Range, Risk


class Device:
    """Base class for every instrument driver.

    Subclasses set the class-level descriptors and implement ``_connect``,
    ``_disconnect`` and (optionally) ``_self_test`` / ``_halt``.
    """

    # -- class-level identity (override in every driver) -------------------
    vendor: str = "unknown"
    model: str = "unknown"
    category: str = "generic"          # pump | reader | thermocycler | arm | ...
    driver_version: str = "0.1.0"
    #: Free-text notes that go into the natural-language reference sheet.
    #: This is where tacit bench knowledge lives -- quirks, gotchas, the thing
    #: the vendor manual does not say. Agents read it before acting.
    notes: str = ""

    def __init__(
        self,
        device_id: str,
        *,
        config: dict[str, Any] | None = None,
        location: str = "",
        tags: Iterable[str] = (),
        simulated: bool = False,
    ) -> None:
        self.id = device_id
        self.config = dict(config or {})
        self.location = location
        self.tags = tuple(tags)
        self.simulated = simulated

        self._state = DeviceState.DISCONNECTED
        self._state_lock = threading.RLock()
        self._last_error: str = ""
        self._connected_at: float | None = None
        self._invocations: int = 0
        self._capabilities: dict[str, Capability] = collect_capabilities(self)
        self._refine_from_config()
        self._validate_contract()
        self._observers: list[Callable[[str, DeviceState, DeviceState], None]] = []

    # -- per-instance refinement -------------------------------------------

    def _refine_from_config(self) -> None:
        """Tighten declared limits for this specific instrument from config.

        A generic driver class cannot know that *this* temperature controller
        tops out at 80 C while its identical sibling downstairs is rated to
        150 C. Rather than fork the driver, the lab config narrows the limits
        per instance:

            config:
              limits:
                "write:value":
                  value: {low: 4.0, high: 80.0, unit: degC}
              risk:
                "write:raw": critical

        The invariant is one-directional: config may only make a capability
        *more* restrictive. A config that tried to widen a driver-declared
        range would be the exact mechanism by which a safety limit quietly
        disappears, so it is rejected.
        """
        import copy

        limits_cfg = self.config.get("limits") or {}
        risk_cfg = self.config.get("risk") or {}
        confirm_cfg = self.config.get("requires_confirmation") or {}

        for key, per_param in limits_cfg.items():
            cap = self._capabilities.get(key)
            if cap is None:
                raise DriverError(
                    f"{self.id}: config.limits refers to unknown capability "
                    f"{key!r}. Known: {sorted(self._capabilities)}")
            cap = copy.deepcopy(cap)
            new_params = []
            for p in cap.params:
                spec = per_param.get(p.name)
                if not spec:
                    new_params.append(p)
                    continue
                p = copy.deepcopy(p)
                low = float(spec["low"])
                high = float(spec["high"])
                unit = spec.get("unit", p.unit)
                plims = cast("tuple[Range, ...]", p.limits)  # normalised post-init
                existing = next((lim for lim in plims if isinstance(lim, Range)), None)
                if existing is not None:
                    if low < existing.low or high > existing.high:
                        raise DriverError(
                            f"{self.id}.{key}.{p.name}: config range "
                            f"[{low:g}, {high:g}] is wider than the driver's "
                            f"[{existing.low:g}, {existing.high:g}]. Config may "
                            f"only narrow a safety limit, never widen it.")
                    p.limits = tuple(lim for lim in plims
                                     if not isinstance(lim, Range))
                if unit and not p.unit:
                    p.unit = unit
                p.limits = (*cast("tuple[Range, ...]", p.limits),
                            Range(low, high, unit or p.unit))
                new_params.append(p)
            cap.params = tuple(new_params)
            self._capabilities[key] = cap

        for key, level in risk_cfg.items():
            cap = self._capabilities.get(key)
            if cap is None:
                raise DriverError(f"{self.id}: config.risk refers to unknown "
                                  f"capability {key!r}")
            new_risk = Risk(level)
            if new_risk.rank < cap.risk.rank:
                raise DriverError(
                    f"{self.id}.{key}: config cannot lower risk from "
                    f"{cap.risk.value} to {new_risk.value}.")
            cap = copy.deepcopy(cap)
            cap.risk = new_risk
            self._capabilities[key] = cap

        for key, flag in confirm_cfg.items():
            cap = self._capabilities.get(key)
            if cap is None:
                raise DriverError(f"{self.id}: config.requires_confirmation refers "
                                  f"to unknown capability {key!r}")
            if not flag and cap.requires_confirmation:
                raise DriverError(
                    f"{self.id}.{key}: config cannot switch off a "
                    f"driver-declared confirmation requirement.")
            cap = copy.deepcopy(cap)
            cap.requires_confirmation = bool(flag)
            self._capabilities[key] = cap

    # -- contract validation ----------------------------------------------

    def _validate_contract(self) -> None:
        if self.vendor == "unknown" or self.model == "unknown":
            raise DriverError(
                f"{type(self).__name__} must set class attributes `vendor` and "
                f"`model`; they appear in the manifest and the audit trail."
            )
        if not self._capabilities:
            raise DriverError(
                f"{type(self).__name__} declares no capabilities. Decorate at "
                f"least one method with @read/@write/@procedure."
            )
        for key, cap in self._capabilities.items():
            if not hasattr(self, cap.method_name):
                raise DriverError(
                    f"{type(self).__name__}.{key} points at missing method "
                    f"{cap.method_name!r}"
                )
            if cap.kind is Kind.READ and cap.risk is not Risk.NONE:
                raise DriverError(
                    f"{type(self).__name__}.{cap.name}: a @read must be risk=NONE. "
                    f"If it actuates, declare it as @write."
                )

    # -- state ------------------------------------------------------------

    @property
    def state(self) -> DeviceState:
        with self._state_lock:
            return self._state

    def _set_state(self, new: DeviceState, error: str = "") -> None:
        with self._state_lock:
            old, self._state = self._state, new
            if error:
                self._last_error = error
            elif new is DeviceState.IDLE:
                self._last_error = ""
        if old is not new:
            for obs in list(self._observers):
                try:
                    obs(self.id, old, new)
                except Exception:
                    pass

    def observe(self, callback: Callable[[str, DeviceState, DeviceState], None]) -> None:
        self._observers.append(callback)

    @property
    def connected(self) -> bool:
        return self.state not in (DeviceState.DISCONNECTED, DeviceState.CONNECTING)

    # -- lifecycle hooks (subclass implements) -----------------------------

    def _connect(self) -> None:
        raise NotImplementedError(f"{type(self).__name__} must implement _connect()")

    def _disconnect(self) -> None:
        raise NotImplementedError(f"{type(self).__name__} must implement _disconnect()")

    def _self_test(self) -> bool:
        """Cheap liveness/identity check run right after connect. Default: pass."""
        return True

    def _halt(self) -> None:
        """Best-effort immediate stop. Called on e-stop. Must not raise.

        Override this for anything that moves. The default is a no-op, which is
        correct for a passive sensor and dangerously wrong for a robot arm --
        the conformance suite warns when a driver with motion capabilities
        leaves it unimplemented.
        """

    # -- lifecycle --------------------------------------------------------

    def connect(self, *, retries: int = 2, backoff_s: float = 0.5) -> None:
        if self.connected:
            return
        self._set_state(DeviceState.CONNECTING)
        last: Exception | None = None
        for attempt in range(retries + 1):
            try:
                self._connect()
                if not self._self_test():
                    raise TransportError(f"{self.id}: self-test failed after connect")
                self._connected_at = time.time()
                self._set_state(DeviceState.IDLE)
                return
            except TransientError as exc:
                last = exc
                if attempt < retries:
                    time.sleep(backoff_s * (2 ** attempt))
            except Exception as exc:
                last = exc
                break
        self._set_state(DeviceState.ERROR, error=str(last))
        raise TransportError(f"{self.id}: connect failed: {last}") from last

    def disconnect(self) -> None:
        try:
            if self.connected:
                self._disconnect()
        finally:
            self._set_state(DeviceState.DISCONNECTED)

    def halt(self) -> None:
        try:
            self._halt()
        except Exception:
            pass
        finally:
            self._set_state(DeviceState.ESTOPPED, error="emergency stop")

    def __enter__(self) -> Device:
        self.connect()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.disconnect()

    # -- capabilities -----------------------------------------------------

    @property
    def capabilities(self) -> dict[str, Capability]:
        """Qualified key (``'write:flow_rate'``) -> Capability."""
        return dict(self._capabilities)

    def capability(self, name: str, *, kind: Kind | None = None) -> Capability:
        """Resolve a capability from a qualified key or a plain name.

        A plain name that maps to exactly one capability resolves silently. A
        plain name that maps to both a read and a write raises rather than
        guessing -- picking wrong here means actuating hardware when the caller
        meant to observe it, which is not a mistake worth being clever about.
        """
        want_kind, plain = split_key(name)
        kind = kind or want_kind

        if kind is not None:
            key = qualified(kind, plain)
            if key in self._capabilities:
                return self._capabilities[key]
            raise CapabilityNotFound(
                f"{self.id} has no {kind.value} capability {plain!r}. "
                f"Available {kind.value}s: {sorted(self.names(kind))}")

        matches = {k: c for k, c in self._capabilities.items() if c.name == plain}
        if len(matches) == 1:
            return next(iter(matches.values()))
        if len(matches) > 1:
            raise CapabilityNotFound(
                f"{self.id}.{plain!r} is ambiguous -- it exists as "
                f"{sorted(matches)}. Qualify it explicitly, or use "
                f"session.read()/session.write()/session.run().")

        close = sorted({c.name for c in self._capabilities.values()
                        if plain.lower() in c.name.lower()})
        hint = (f" Did you mean: {close}?" if close
                else f" Available: {sorted(self._capabilities)}")
        raise CapabilityNotFound(f"{self.id} has no capability {plain!r}.{hint}")

    def names(self, kind: Kind | None = None) -> list[str]:
        """Plain capability names, optionally filtered by kind."""
        return sorted(c.name for c in self._capabilities.values()
                      if kind is None or c.kind is kind)

    def has(self, name: str) -> bool:
        try:
            self.capability(name)
            return True
        except CapabilityNotFound:
            return False

    def invoke(self, capability: str, **kwargs: Any) -> Any:
        """Direct invocation, bypassing the session's safety engine.

        Only used by the session (which has already run safety) and by driver
        unit tests. Application and agent code should always go through
        ``LabSession.call`` so that limits, interlocks and audit apply.
        """
        cap = self.capability(capability)
        method = getattr(self, cap.method_name)
        self._invocations += 1
        blocking = cap.blocking and cap.kind is not Kind.READ
        if blocking:
            self._set_state(DeviceState.BUSY)
        try:
            result = method(**kwargs)
            if blocking:
                self._set_state(DeviceState.IDLE)
            return result
        except PhysicalError as exc:
            self._set_state(DeviceState.ERROR, error=str(exc))
            raise
        except Exception as exc:
            self._set_state(DeviceState.ERROR if blocking else self.state, error=str(exc))
            raise

    # -- introspection ----------------------------------------------------

    def status(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "state": self.state.value,
            "vendor": self.vendor,
            "model": self.model,
            "category": self.category,
            "location": self.location,
            "simulated": self.simulated,
            "connected_at": self._connected_at,
            "uptime_s": (time.time() - self._connected_at) if self._connected_at else None,
            "invocations": self._invocations,
            "last_error": self._last_error,
            "tags": list(self.tags),
        }

    def manifest(self) -> dict[str, Any]:
        """Machine-readable description: identity + every capability + limits."""
        return {
            "id": self.id,
            "vendor": self.vendor,
            "model": self.model,
            "category": self.category,
            "driver": type(self).__name__,
            "driver_version": self.driver_version,
            "location": self.location,
            "simulated": self.simulated,
            "tags": list(self.tags),
            "notes": self.notes,
            "state": self.state.value,
            "capabilities": [{"key": k, **c.to_dict()}
                             for k, c in sorted(self._capabilities.items())],
        }

    def reference_sheet(self) -> str:
        """Natural-language operating reference.

        This is what an agent reads before touching an instrument it has never
        seen. It replaces the PDF manual and, more importantly, the tacit
        knowledge that normally lives only in the postdoc who built the rig.
        """
        head = [
            f"INSTRUMENT: {self.id}",
            f"  {self.vendor} {self.model} ({self.category})"
            + (f", located at {self.location}" if self.location else ""),
            f"  Driver {type(self).__name__} v{self.driver_version}"
            + ("  [SIMULATED]" if self.simulated else ""),
            f"  Current state: {self.state.value}",
        ]
        if self.tags:
            head.append(f"  Tags: {', '.join(self.tags)}")
        if self.notes:
            import textwrap
            head.append("")
            head.append("OPERATING NOTES:")
            head.extend("  " + ln for ln in
                        textwrap.dedent(self.notes).strip().splitlines())

        by_kind: dict[Kind, list[Capability]] = {}
        for cap in self._capabilities.values():
            by_kind.setdefault(cap.kind, []).append(cap)

        body: list[str] = []
        for kind, title in ((Kind.READ, "READABLE STATE"),
                            (Kind.WRITE, "WRITABLE SETPOINTS"),
                            (Kind.PROCEDURE, "PROCEDURES")):
            caps = sorted(by_kind.get(kind, []), key=lambda c: c.name)
            if not caps:
                continue
            body.append("")
            body.append(f"{title}:")
            for cap in caps:
                body.extend("  " + ln for ln in cap.describe().splitlines())

        risky = [c.name for c in self._capabilities.values()
                 if c.risk.rank >= Risk.HIGH.rank or not c.reversible]
        if risky:
            body.append("")
            body.append("SAFETY: the following require human approval and cannot be "
                        "undone once started:")
            body.append("  " + ", ".join(sorted(risky)))

        return "\n".join(head + body)

    def __repr__(self) -> str:
        return (f"<{type(self).__name__} id={self.id!r} {self.vendor} {self.model} "
                f"state={self.state.value}>")


__all__ = ["Device"]
