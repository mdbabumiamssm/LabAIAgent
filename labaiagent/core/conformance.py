"""Driver conformance suite.

The onboarding gate. When you integrate instrument N+1 you run this against the
new driver; it either passes or tells you precisely what is wrong. Nothing goes
into a lab config until it passes at level ``strict``.

This exists because the failure mode of a plugin architecture is silent
divergence: driver 12 declares a limit it does not enforce, driver 13 returns
a string where the manifest promises a float, driver 14 has no ``_halt`` so the
e-stop is decorative on that instrument alone. Each is invisible until the run
that matters. A conformance suite converts all of them into a red line at
integration time.

Checks are graded:
  ERROR   -- contract violation; the driver is not safe to register
  WARN    -- works, but will bite you (missing notes, no halt on a mover)
  INFO    -- observations worth reading once
"""

from __future__ import annotations

import inspect
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, cast

from .device import Device
from .errors import ConformanceError
from .types import DeviceState, Kind, Param, Range, Risk

MOTION_CATEGORIES = {"robot_arm", "liquid_handler", "centrifuge", "stage",
                     "gantry", "shaker", "thermocycler"}


@dataclass
class Finding:
    level: str          # ERROR | WARN | INFO
    check: str
    message: str
    capability: str = ""

    def __str__(self) -> str:
        loc = f" [{self.capability}]" if self.capability else ""
        return f"{self.level:<5} {self.check}{loc}: {self.message}"


@dataclass
class ConformanceReport:
    driver: str
    device_id: str
    findings: list[Finding] = field(default_factory=list)
    checks_run: int = 0
    elapsed_s: float = 0.0

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "ERROR"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "WARN"]

    @property
    def passed(self) -> bool:
        return not self.errors

    def passed_at(self, level: str = "strict") -> bool:
        if level == "strict":
            return not self.errors and not self.warnings
        return not self.errors

    def render(self) -> str:
        head = [
            f"CONFORMANCE REPORT -- {self.driver} (device id {self.device_id!r})",
            f"  {self.checks_run} checks in {self.elapsed_s * 1000:.0f} ms",
            f"  {len(self.errors)} error(s), {len(self.warnings)} warning(s), "
            f"{len([f for f in self.findings if f.level == 'INFO'])} note(s)",
            "",
        ]
        if not self.findings:
            head.append("  All checks passed cleanly.")
        for lvl in ("ERROR", "WARN", "INFO"):
            group = [f for f in self.findings if f.level == lvl]
            if group:
                head.append(f"{lvl}:")
                head.extend("  " + str(f).split(" ", 1)[1] for f in group)
                head.append("")
        verdict = "PASS" if self.passed else "FAIL"
        if self.passed and self.warnings:
            verdict = "PASS (with warnings -- not strict-clean)"
        head.append(f"VERDICT: {verdict}")
        return "\n".join(head)


class ConformanceSuite:
    """Runs every check against a driver instance."""

    def __init__(self, *, exercise: bool = True, timeout_s: float = 30.0) -> None:
        #: If True, actually connect and invoke every read capability.
        self.exercise = exercise
        self.timeout_s = timeout_s

    def run(self, device: Device) -> ConformanceReport:
        rep = ConformanceReport(driver=type(device).__name__, device_id=device.id)
        t0 = time.perf_counter()
        checks: list[Callable[[Device, ConformanceReport], None]] = [
            self._check_identity,
            self._check_capability_names,
            self._check_read_purity,
            self._check_param_specs,
            self._check_risk_coherence,
            self._check_manifest_serialisable,
            self._check_reference_sheet,
            self._check_halt_implemented,
            self._check_lifecycle_hooks,
            self._check_interlock_declarations,
            self._check_readwrite_pairs,
        ]
        if self.exercise:
            checks += [
                self._check_connect_disconnect,
                self._check_reads_execute,
                self._check_limits_enforced,
                self._check_unknown_capability,
                self._check_idempotent_connect,
            ]
        for chk in checks:
            rep.checks_run += 1
            try:
                chk(device, rep)
            except Exception as exc:
                rep.findings.append(Finding(
                    "ERROR", chk.__name__.lstrip("_"),
                    f"check itself raised {type(exc).__name__}: {exc}"))
        rep.elapsed_s = time.perf_counter() - t0
        return rep

    # -- static checks ----------------------------------------------------

    def _check_identity(self, d: Device, r: ConformanceReport) -> None:
        for attr in ("vendor", "model", "category"):
            val = getattr(d, attr, "")
            if not val or val == "unknown":
                r.findings.append(Finding("ERROR", "identity",
                                          f"class attribute {attr!r} is unset"))
        if getattr(d, "driver_version", "0.1.0") == "0.1.0":
            r.findings.append(Finding("INFO", "identity",
                                      "driver_version is still the default 0.1.0"))
        if not (d.notes or "").strip():
            r.findings.append(Finding(
                "WARN", "identity",
                "no `notes` set. This is the field an agent reads to learn the "
                "instrument's quirks; leaving it empty discards exactly the "
                "tacit knowledge this framework exists to capture."))

    def _check_capability_names(self, d: Device, r: ConformanceReport) -> None:
        for key, cap in d.capabilities.items():
            name = cap.name
            if not name.replace("_", "").isalnum():
                r.findings.append(Finding("ERROR", "naming",
                                          "capability name must be snake_case alphanumeric",
                                          key))
            if name != name.lower():
                r.findings.append(Finding("WARN", "naming",
                                          "capability names should be lowercase", key))
            if not cap.description.strip():
                r.findings.append(Finding(
                    "WARN", "naming", "no description; the agent has nothing to "
                    "reason about beyond the name", key))

    def _check_read_purity(self, d: Device, r: ConformanceReport) -> None:
        for key, cap in d.capabilities.items():
            if cap.kind is Kind.READ:
                if cap.risk is not Risk.NONE:
                    r.findings.append(Finding("ERROR", "read_purity",
                                              "a @read must be risk=NONE", key))
                if cap.consumes:
                    r.findings.append(Finding("ERROR", "read_purity",
                                              "a @read must not declare consumables", key))
                if not cap.reversible:
                    r.findings.append(Finding("ERROR", "read_purity",
                                              "a @read must be reversible", key))

    def _check_param_specs(self, d: Device, r: ConformanceReport) -> None:
        for key, cap in d.capabilities.items():
            method = getattr(d, cap.method_name)
            sig = inspect.signature(method)
            declared = {p.name for p in cap.params}
            actual = {p for p in sig.parameters if p != "self"}
            missing = declared - actual
            extra = {p for p in actual - declared
                     if sig.parameters[p].default is inspect.Parameter.empty}
            if missing:
                r.findings.append(Finding(
                    "ERROR", "param_spec",
                    f"declares parameter(s) {sorted(missing)} that the method "
                    f"{cap.method_name}{sig} does not accept", key))
            if extra:
                r.findings.append(Finding(
                    "ERROR", "param_spec",
                    f"method requires argument(s) {sorted(extra)} that are not "
                    f"declared as Param(...), so validation will never see them", key))
            for p in cap.params:
                if p.type in (float, int) and not p.limits:
                    r.findings.append(Finding(
                        "WARN", "param_spec",
                        f"numeric parameter {p.name!r} has no limits. An unbounded "
                        f"numeric input to a physical device is how you get a "
                        f"stage driven into a hard stop.", key))
                if p.type in (float, int) and not p.unit and p.name not in (
                        "cycles", "count", "n", "bucket", "mix_cycles", "retries"):
                    r.findings.append(Finding(
                        "WARN", "param_spec",
                        f"numeric parameter {p.name!r} declares no unit", key))

    def _check_risk_coherence(self, d: Device, r: ConformanceReport) -> None:
        for key, cap in d.capabilities.items():
            if not cap.reversible and cap.risk.rank < Risk.MEDIUM.rank:
                r.findings.append(Finding(
                    "ERROR", "risk", "irreversible capability declared below "
                    "risk=medium", key))
            if cap.risk is Risk.CRITICAL and not cap.requires_confirmation:
                r.findings.append(Finding(
                    "WARN", "risk", "risk=critical but requires_confirmation is "
                    "False; it will still be gated by the autonomy ceiling, but "
                    "declare the intent explicitly", key))
            if cap.consumes and cap.reversible:
                r.findings.append(Finding(
                    "WARN", "risk", f"consumes {list(cap.consumes)} but is marked "
                    f"reversible -- consumed sample cannot be un-consumed", key))

    def _check_manifest_serialisable(self, d: Device, r: ConformanceReport) -> None:
        import json
        try:
            json.dumps(d.manifest())
        except (TypeError, ValueError) as exc:
            r.findings.append(Finding(
                "ERROR", "manifest",
                f"manifest is not JSON-serialisable ({exc}); it cannot be sent "
                f"to an agent or an MCP client"))

    def _check_reference_sheet(self, d: Device, r: ConformanceReport) -> None:
        sheet = d.reference_sheet()
        if len(sheet) < 120:
            r.findings.append(Finding("WARN", "reference_sheet",
                                      "reference sheet is very short"))
        for key, cap in d.capabilities.items():
            if cap.name not in sheet:
                r.findings.append(Finding("ERROR", "reference_sheet",
                                          "capability missing from reference sheet", key))

    def _check_halt_implemented(self, d: Device, r: ConformanceReport) -> None:
        overridden = type(d)._halt is not Device._halt
        moves = d.category in MOTION_CATEGORIES or any(
            c.risk.rank >= Risk.HIGH.rank for c in d.capabilities.values())
        if moves and not overridden:
            r.findings.append(Finding(
                "ERROR", "halt",
                f"category {d.category!r} can move or has HIGH-risk capabilities "
                f"but does not override _halt(). The emergency stop would be a "
                f"no-op on this instrument."))
        elif not overridden:
            r.findings.append(Finding("INFO", "halt",
                                      "_halt() not overridden (acceptable for a passive device)"))

    def _check_lifecycle_hooks(self, d: Device, r: ConformanceReport) -> None:
        for hook in ("_connect", "_disconnect"):
            if getattr(type(d), hook) is getattr(Device, hook):
                r.findings.append(Finding("ERROR", "lifecycle",
                                          f"{hook}() is not implemented"))

    def _check_interlock_declarations(self, d: Device, r: ConformanceReport) -> None:
        for key, cap in d.capabilities.items():
            if cap.risk.rank >= Risk.HIGH.rank and not cap.interlocks:
                r.findings.append(Finding(
                    "WARN", "interlocks",
                    "HIGH/CRITICAL risk capability declares no interlocks. Consider "
                    "what physical precondition must hold before this is safe.", key))

    # -- dynamic checks ---------------------------------------------------

    def _check_connect_disconnect(self, d: Device, r: ConformanceReport) -> None:
        try:
            d.connect()
        except Exception as exc:
            r.findings.append(Finding("ERROR", "connect",
                                      f"connect() raised {type(exc).__name__}: {exc}"))
            return
        if d.state is not DeviceState.IDLE:
            r.findings.append(Finding("ERROR", "connect",
                                      f"state is {d.state.value!r} after connect, expected 'idle'"))
        d.disconnect()
        if d.state is not DeviceState.DISCONNECTED:
            r.findings.append(Finding("ERROR", "disconnect",
                                      f"state is {d.state.value!r} after disconnect"))
        d.connect()

    def _check_idempotent_connect(self, d: Device, r: ConformanceReport) -> None:
        try:
            d.connect()
            d.connect()
        except Exception as exc:
            r.findings.append(Finding("ERROR", "connect",
                                      f"a second connect() raised {exc}"))

    def _check_reads_execute(self, d: Device, r: ConformanceReport) -> None:
        if not d.connected:
            d.connect()
        for key, cap in d.capabilities.items():
            if cap.kind is not Kind.READ:
                continue
            if any(p.required for p in cap.params):
                r.findings.append(Finding(
                    "INFO", "read_exec",
                    "not exercised (requires arguments)", key))
                continue
            try:
                val = d.invoke(key)
            except Exception as exc:
                r.findings.append(Finding(
                    "ERROR", "read_exec",
                    f"raised {type(exc).__name__}: {exc}", key))
                continue
            if cap.unit and not isinstance(val, (int, float)):
                r.findings.append(Finding(
                    "ERROR", "read_exec",
                    f"declares unit {cap.unit!r} but returned "
                    f"{type(val).__name__}", key))

    def _check_unknown_capability(self, d: Device, r: ConformanceReport) -> None:
        """An unknown name must raise CapabilityNotFound, not AttributeError.

        Agents mistype capability names constantly. The error they get back is
        their only repair signal, so it has to be the typed one carrying the
        'did you mean' list -- not a bare AttributeError from deep in a driver.
        """
        from .errors import CapabilityNotFound
        try:
            d.capability("__definitely_not_a_capability__")
        except CapabilityNotFound:
            pass
        except Exception as exc:
            r.findings.append(Finding(
                "ERROR", "unknown_capability",
                f"unknown name raised {type(exc).__name__} instead of "
                f"CapabilityNotFound"))
        else:
            r.findings.append(Finding("ERROR", "unknown_capability",
                                      "unknown capability name did not raise"))

    def _check_readwrite_pairs(self, d: Device, r: ConformanceReport) -> None:
        """A writable setpoint should normally also be readable.

        Write-only state is the single most common cause of an agent losing
        track of an instrument: it sets a value, cannot confirm it, and its
        model of the device silently diverges from reality.
        """
        reads = {c.name for c in d.capabilities.values() if c.kind is Kind.READ}
        writes = {c.name for c in d.capabilities.values() if c.kind is Kind.WRITE}
        for name in sorted(writes - reads):
            r.findings.append(Finding(
                "WARN", "readback",
                "writable but not readable; an agent cannot verify this "
                "setpoint took effect", f"write:{name}"))

    def _check_limits_enforced(self, d: Device, r: ConformanceReport) -> None:
        """Probe one out-of-range value per bounded numeric parameter.

        Only ``validate_args`` is exercised -- nothing is actuated. The point is
        to confirm the declared limit is genuinely rejected rather than merely
        documented.
        """
        from .errors import LimitViolation
        from .types import OneOf as _OneOf

        for key, cap in d.capabilities.items():
            for p in cap.params:
                probes: list[Any] = []
                plims = cast("tuple[Any, ...]", p.limits)
                rng = next((lim for lim in plims if isinstance(lim, Range)), None)
                if rng is not None:
                    # Probe BOTH ends: an inverted or one-sided check passes a
                    # high-only probe and still drives a stage into a hard stop.
                    probes.append(rng.high + max(1.0, abs(rng.high) * 0.5))
                    probes.append(rng.low - max(1.0, abs(rng.low) * 0.5))
                oneof = next((lim for lim in plims if isinstance(lim, _OneOf)), None)
                if oneof is not None:
                    probes.append("__definitely_not_an_option__")
                for bad in probes:
                    probe = {q.name: (bad if q.name == p.name else _stub(q))
                             for q in cap.params}
                    try:
                        cap.validate_args(probe)
                    except LimitViolation:
                        continue  # correct: rejected with a typed, structured error
                    except Exception as exc:
                        r.findings.append(Finding(
                            "WARN", "limits",
                            f"parameter {p.name!r} was rejected with "
                            f"{type(exc).__name__} rather than LimitViolation; the "
                            f"caller loses the permitted-range hint", key))
                        continue
                    r.findings.append(Finding(
                        "ERROR", "limits",
                        f"parameter {p.name!r} accepted out-of-limit value {bad!r} "
                        f"despite its declared limits", key))


def _stub(p: Param) -> Any:
    """A plausible in-range value for an unrelated parameter during probing."""
    if not p.required:
        return p.default
    rng = next((lim for lim in cast("tuple[Any, ...]", p.limits)
                if isinstance(lim, Range)), None)
    if p.type is float:
        return (rng.low + rng.high) / 2 if rng else 1.0
    if p.type is int:
        return int((rng.low + rng.high) / 2) if rng else 1
    if p.type is bool:
        return False
    if p.type is list:
        return []
    if p.type is dict:
        return {}
    return "A1"


def verify_driver(device: Device, *, level: str = "normal",
                  exercise: bool = True, raise_on_fail: bool = False) -> ConformanceReport:
    """Convenience entry point used by the CLI and by driver test suites."""
    rep = ConformanceSuite(exercise=exercise).run(device)
    if raise_on_fail and not rep.passed_at(level):
        raise ConformanceError(rep.render())
    return rep


__all__ = ["ConformanceSuite", "ConformanceReport", "Finding", "verify_driver"]
