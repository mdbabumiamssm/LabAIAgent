"""LabSession -- the single object application and agent code talks to.

Every call funnels through ``LabSession.call``, which is the one place where
safety evaluation, device locking, execution, retry and audit are applied. No
code path reaches an instrument without passing through it. That is deliberate:
the moment there are two ways to actuate hardware, one of them will not be
audited, and the audit trail is the whole argument for letting an agent near a
bench in the first place.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Any, Literal, overload

from ..core.audit import AuditLog
from ..core.capability import qualified
from ..core.device import Device
from ..core.errors import (
    DeviceNotFound,
    LabAIAgentError,
    TransientError,
)
from ..core.registry import build_device, devices_from_config, load_lab_config
from ..core.safety import SafetyEngine, standard_interlocks
from ..core.types import DeviceState, Kind, Risk


class LabSession:
    """A connected set of instruments plus the policy that governs them."""

    def __init__(
        self,
        devices: Iterable[Device] = (),
        *,
        audit_path: str | Path | None = None,
        audit_hmac_key: bytes | str | None = None,
        actor: str = "",
        autonomy_ceiling: Risk | str = Risk.MEDIUM,
        dry_run: bool = False,
        echo_audit: bool = False,
        name: str = "lab",
    ) -> None:
        self.name = name
        self._devices: dict[str, Device] = {}
        self._locks: dict[str, threading.RLock] = {}
        self.audit = AuditLog(audit_path, actor=actor, echo=echo_audit,
                      hmac_key=audit_hmac_key)
        self.safety = SafetyEngine(
            interlocks=standard_interlocks(),
            autonomy_ceiling=Risk(autonomy_ceiling) if isinstance(autonomy_ceiling, str)
            else autonomy_ceiling,
            dry_run=dry_run,
        )
        self.dry_run = dry_run
        self._register_builtin_interlocks()
        self.safety.estop.on_trip(self._on_estop)
        for d in devices:
            self.add(d)

    # -- construction helpers ---------------------------------------------

    @classmethod
    def from_config(cls, path: str | Path, **kw: Any) -> LabSession:
        """Build a session from a lab YAML file. This is how you add
        instrument N+1 without writing code."""
        cfg = load_lab_config(path)
        sess = cls(devices_from_config(path), name=cfg.get("name", "lab"), **kw)
        pol = cfg.get("policy", {}) or {}
        if "autonomy_ceiling" in pol:
            sess.safety.autonomy_ceiling = Risk(pol["autonomy_ceiling"])
        for key, spec in (pol.get("rate_limits") or {}).items():
            sess.safety.set_rate_limit(key, spec["max_calls"], spec["window_s"])
        return sess

    # -- device management ------------------------------------------------

    def add(self, device: Device) -> Device:
        if device.id in self._devices:
            raise LabAIAgentError(f"Device id {device.id!r} already in this session.")
        self._devices[device.id] = device
        self._locks[device.id] = threading.RLock()
        self.audit.record("register", device=device.id,
                          result={"driver": type(device).__name__,
                                  "model": f"{device.vendor} {device.model}"})
        return device

    def add_from_spec(self, spec: dict[str, Any]) -> Device:
        """Add one instrument from a config stanza at runtime."""
        return self.add(build_device(spec))

    def remove(self, device_id: str) -> None:
        dev = self.get(device_id)
        dev.disconnect()
        self._devices.pop(device_id, None)
        self._locks.pop(device_id, None)
        self.audit.record("unregister", device=device_id)

    @overload
    def get(self, device_id: str) -> Device: ...

    @overload
    def get(self, device_id: str, *, required: Literal[True]) -> Device: ...

    @overload
    def get(self, device_id: str, *,
            required: Literal[False]) -> Device | None: ...

    def get(self, device_id: str, *, required: bool = True) -> Device | None:
        dev = self._devices.get(device_id)
        if dev is None and required:
            near = [d for d in self._devices if device_id.lower() in d.lower()]
            raise DeviceNotFound(
                f"No device {device_id!r} in session {self.name!r}."
                + (f" Did you mean {near}?" if near else
                   f" Known: {sorted(self._devices)}")
            )
        return dev

    def __getitem__(self, device_id: str) -> Device:
        return self.get(device_id)  # type: ignore[return-value]

    def __contains__(self, device_id: object) -> bool:
        return device_id in self._devices

    def __iter__(self) -> Iterator[Device]:
        return iter(self._devices.values())

    def devices(self, *, category: str | None = None,
                tag: str | None = None) -> list[Device]:
        out = list(self._devices.values())
        if category:
            out = [d for d in out if d.category == category]
        if tag:
            out = [d for d in out if tag in d.tags]
        return out

    def occupied_positions(self) -> set[str]:
        """Union of occupied positions across every simulated world present."""
        seen: set[str] = set()
        for d in self._devices.values():
            w = getattr(d, "world", None)
            if w is not None and hasattr(w, "occupied_positions"):
                seen |= w.occupied_positions()
        return seen

    # -- connection -------------------------------------------------------

    def connect_all(self, *, fail_fast: bool = False) -> dict[str, str]:
        """Connect every device. Returns id -> 'ok' or the error string.

        ``fail_fast=False`` by default because a lab where one instrument is
        powered off should still let you work with the other nine.
        """
        results: dict[str, str] = {}
        for dev in self._devices.values():
            try:
                dev.connect()
                results[dev.id] = "ok"
                self.audit.record("connect", device=dev.id, state_after=dev.state.value)
            except Exception as exc:
                results[dev.id] = f"{type(exc).__name__}: {exc}"
                self.audit.record("connect", device=dev.id, error=str(exc),
                                  state_after=dev.state.value)
                if fail_fast:
                    raise
        return results

    def disconnect_all(self) -> None:
        for dev in self._devices.values():
            try:
                dev.disconnect()
            except Exception:
                pass
        self.audit.record("shutdown", result=self.audit.summary()["by_event"])

    def __enter__(self) -> LabSession:
        self.connect_all()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.disconnect_all()

    # -- the single invocation path ---------------------------------------

    def call(
        self,
        device_id: str,
        capability: str,
        *,
        approval: str | None = None,
        reason: str = "",
        actor: str | None = None,
        retries: int = 1,
        **kwargs: Any,
    ) -> Any:
        """Invoke a capability with full safety, locking and audit.

        Retries apply only to ``TransientError``. A ``SafetyViolation`` or a
        ``PhysicalError`` is never retried -- repeating a physical action that
        just failed is how a recoverable fault becomes a broken instrument.
        """
        dev = self.get(device_id)
        cap = dev.capability(capability)
        cap_key = qualified(cap.kind, cap.name)
        started = time.perf_counter()
        state_before = dev.state

        try:
            decision = self.safety.evaluate(
                session=self, device_id=device_id, device_state=state_before,
                cap=cap, kwargs=kwargs, approval_token=approval,
                actor=actor or self.audit.actor,
            )
        except Exception as exc:
            # A refused action is the single most important kind of audit
            # record: it is the evidence the controls were live and working.
            # Recording only successes would produce a log that looks clean
            # precisely because nothing was ever checked.
            self.audit.record(
                "refused", device=device_id, capability=cap.name,
                kind=cap.kind.value, risk=cap.risk.value, arguments=_safe_args(kwargs),
                error=f"{type(exc).__name__}: {exc}", reason=reason, actor=actor,
                state_before=state_before.value,
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
            )
            raise
        args = decision.coerced_args

        self.audit.record(
            "invoke", device=device_id, capability=cap.name, kind=cap.kind.value,
            risk=cap.risk.value, arguments=args, reason=reason,
            state_before=state_before.value, actor=actor,
            approval=decision.approval_operator,
        )

        if self.dry_run and cap.kind is not Kind.READ:
            self.audit.record("dry_run", device=device_id, capability=cap.name,
                              arguments=args, reason="dry_run mode")
            return {"dry_run": True, "device": device_id, "capability": cap.name,
                    "arguments": args,
                    "would_take_s": cap.est_duration_s}

        lock = self._locks[device_id] if cap.blocking else _NullLock()
        last_exc: Exception | None = None
        with lock:
            for attempt in range(retries + 1):
                try:
                    result = dev.invoke(cap_key, **args)
                    self.audit.record(
                        "result", device=device_id, capability=cap.name,
                        kind=cap.kind.value, risk=cap.risk.value, arguments=args,
                        result=_summarise(result), actor=actor,
                        duration_ms=round((time.perf_counter() - started) * 1000, 2),
                        state_before=state_before.value, state_after=dev.state.value,
                    )
                    return result
                except TransientError as exc:
                    last_exc = exc
                    if attempt < retries:
                        self.audit.record("retry", device=device_id,
                                          capability=cap.name, error=str(exc))
                        time.sleep(0.3 * (2 ** attempt))
                        continue
                    break
                except Exception as exc:
                    last_exc = exc
                    break

        self.audit.record(
            "error", device=device_id, capability=cap.name, kind=cap.kind.value,
            risk=cap.risk.value, arguments=args, error=f"{type(last_exc).__name__}: {last_exc}",
            actor=actor, duration_ms=round((time.perf_counter() - started) * 1000, 2),
            state_before=state_before.value, state_after=dev.state.value,
        )
        assert last_exc is not None
        raise last_exc

    def read(self, device_id: str, capability: str, **kwargs: Any) -> Any:
        """Invoke a READ capability. Never actuates."""
        return self.call(device_id, f"read:{_plain(capability)}", **kwargs)

    def write(self, device_id: str, capability: str, **kwargs: Any) -> Any:
        """Invoke a WRITE capability -- a setpoint change or discrete actuation."""
        return self.call(device_id, f"write:{_plain(capability)}", **kwargs)

    def run(self, device_id: str, capability: str, **kwargs: Any) -> Any:
        """Invoke a PROCEDURE -- a multi-step operation that occupies the device."""
        return self.call(device_id, f"proc:{_plain(capability)}", **kwargs)

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """Read every zero-argument READ capability across the whole lab.

        The cheap, safe way to answer 'what is the lab doing right now' -- and
        the first thing to put in front of an agent before it plans anything.

        Reads here take the documented read-only fast path (``dev.invoke`` on
        READ capabilities only, no safety evaluation needed because reads are
        risk=NONE by contract), but the snapshot itself is still an audit
        event so the trail shows the lab was observed.
        """
        self.audit.record("snapshot", result={"devices": sorted(self._devices)})
        out: dict[str, dict[str, Any]] = {}
        for dev in self._devices.values():
            if not dev.connected:
                out[dev.id] = {"_state": dev.state.value}
                continue
            vals: dict[str, Any] = {"_state": dev.state.value}
            for key, cap in dev.capabilities.items():
                if cap.kind is not Kind.READ or any(p.required for p in cap.params):
                    continue
                try:
                    vals[cap.name] = dev.invoke(key)
                except Exception as exc:
                    vals[cap.name] = f"<error: {type(exc).__name__}>"
            out[dev.id] = vals
        return out

    # -- approvals --------------------------------------------------------

    def request_approval(self, device_id: str, capability: str, *,
                         operator: str, reason: str, ttl_s: float = 300.0,
                         uses: int = 1) -> str:
        """Mint a scoped, expiring human approval token.

        Scoped to one device and one capability, expiring, and consumed on use.
        Scoping is what stops an approval for 'move the arm to the reader'
        being replayed as 'move the arm anywhere, forever'.
        """
        dev = self.get(device_id)
        cap = dev.capability(capability)
        token = self.safety.approvals.issue(
            operator=operator, reason=reason, device=device_id,
            capability=cap.name, ttl_s=ttl_s, uses=uses,
        )
        self.audit.record("approval_issued", device=device_id, capability=cap.name,
                          risk=cap.risk.value, actor=f"user:{operator}", reason=reason,
                          result={"ttl_s": ttl_s, "uses": uses})
        return token

    # -- emergency stop ---------------------------------------------------

    def emergency_stop(self, reason: str = "operator request") -> dict[str, Any]:
        """Latch the stop and halt every device. Always succeeds."""
        self.safety.estop.trip(reason)
        self.audit.record("estop", reason=reason,
                          result={"devices": sorted(self._devices)})
        return {"stopped": True, "reason": reason,
                "devices": {d.id: d.state.value for d in self._devices.values()}}

    def _on_estop(self, reason: str) -> None:
        for dev in self._devices.values():
            try:
                dev.halt()
            except Exception:
                pass

    def reset_emergency_stop(self, *, operator: str, reason: str) -> None:
        """Clear the latch, then re-verify each halted device before trusting it.

        A device halted mid-motion may need homing or reconnection; returning
        it straight to IDLE on say-so is how a post-e-stop run crashes into
        whatever the stop left behind. Each ESTOPPED device is re-checked with
        its own ``_self_test``; failures land in ERROR, not IDLE.
        """
        self.safety.estop.reset(operator=operator, reason=reason)
        recovered, failed = [], []
        for dev in self._devices.values():
            if dev.state is DeviceState.ESTOPPED:
                try:
                    ok = dev._self_test()
                except Exception as exc:
                    ok = False
                    dev._last_error = f"post-estop self-test raised: {exc}"
                if ok:
                    dev._set_state(DeviceState.IDLE)
                    recovered.append(dev.id)
                else:
                    dev._set_state(DeviceState.ERROR,
                                   error="failed post-e-stop self-test")
                    failed.append(dev.id)
        self.audit.record("estop_reset", actor=f"user:{operator}", reason=reason,
                          result={"recovered": recovered, "failed_self_test": failed})

    # -- introspection ----------------------------------------------------

    def manifest(self) -> dict[str, Any]:
        return {
            "session": self.name,
            "session_id": self.audit.session_id,
            "policy": {
                "autonomy_ceiling": self.safety.autonomy_ceiling.value,
                "dry_run": self.dry_run,
                "emergency_stop_active": self.safety.estop.active,
                "interlocks": self.safety.interlocks.describe(),
            },
            "devices": [d.manifest() for d in self._devices.values()],
        }

    def reference_sheets(self) -> str:
        """Concatenated natural-language reference for the whole lab.

        This is the text an agent is given as context. It is deliberately
        prose, not JSON: the model reasons better about 'do not exceed 25 uL/s
        for viscous liquids' than about a numeric bound with no rationale.
        """
        parts = [
            f"LABORATORY: {self.name}",
            f"Policy: autonomous actions permitted up to risk="
            f"{self.safety.autonomy_ceiling.value}; anything higher needs a human "
            f"approval token." + (" DRY RUN MODE -- no actuation will occur."
                                  if self.dry_run else ""),
            "",
            "Registered interlocks (checked automatically before each action):",
        ]
        for i in self.safety.interlocks.describe():
            parts.append(f"  - {i['name']}: {i['description']}")
        parts.append("")
        parts.append("=" * 70)
        for dev in self._devices.values():
            parts.append("")
            parts.append(dev.reference_sheet())
            parts.append("-" * 70)
        return "\n".join(parts)

    def status(self) -> dict[str, Any]:
        return {
            "session": self.name,
            "emergency_stop": {"active": self.safety.estop.active,
                               "reason": self.safety.estop.reason},
            "devices": {d.id: d.status() for d in self._devices.values()},
            "audit": self.audit.summary(),
        }

    # -- interlocks -------------------------------------------------------

    def define_interlock(self, name: str, description: str,
                         severity: Risk = Risk.HIGH) -> Callable:
        return self.safety.interlocks.define(name, description, severity)

    def _register_builtin_interlocks(self) -> None:
        reg = self.safety.interlocks

        @reg.define("lid_closed", "The target thermocycler's heated lid is closed.")
        def _lid(session: LabSession, ctx: dict[str, Any]) -> bool:
            dev = session.get(ctx["device"], required=False)
            return bool(dev and dev.invoke("read:lid_closed"))

        @reg.define("block_loaded", "A plate is present in the thermocycler block.")
        def _block(session: LabSession, ctx: dict[str, Any]) -> bool:
            dev = session.get(ctx["device"], required=False)
            return bool(dev and dev.invoke("read:block_occupied"))

        @reg.define("reader_carriage_loaded",
                    "A plate is present on the plate reader carriage.")
        def _carriage(session: LabSession, ctx: dict[str, Any]) -> bool:
            dev = session.get(ctx["device"], required=False)
            return bool(dev and dev.invoke("read:carriage_occupied"))

        @reg.define("destination_free",
                    "The destination position for a plate move is unoccupied.")
        def _dest(session: LabSession, ctx: dict[str, Any]) -> bool:
            dest = ctx["args"].get("destination")
            if not dest:
                return True
            return dest not in session.occupied_positions()

        @reg.define("centrifuge_balanced",
                    "Centrifuge buckets are loaded in opposing pairs.")
        def _bal(session: LabSession, ctx: dict[str, Any]) -> bool:
            dev = session.get(ctx["device"], required=False)
            return bool(dev and dev.invoke("read:balanced"))


def _safe_args(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Arguments as supplied, for the refusal record. Never coerced, because
    the whole point is to capture what was actually asked for."""
    out = {}
    for k, v in kwargs.items():
        out[k] = v if isinstance(v, (str, int, float, bool, type(None))) else repr(v)[:200]
    return out


def _plain(name: str) -> str:
    """Strip any kind prefix so read('lh', 'write:x') cannot smuggle a write."""
    return name.split(":", 1)[1] if ":" in name else name


class _NullLock:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: Any) -> None:
        return None


def _summarise(result: Any, *, max_items: int = 12) -> Any:
    """Keep the audit trail readable when a call returns 96 wells of data."""
    if isinstance(result, dict):
        out: dict[str, Any] = {}
        for k, v in result.items():
            if isinstance(v, dict) and len(v) > max_items:
                keys = list(v)
                out[k] = {"_summary": f"{len(v)} entries",
                          "_first": {kk: v[kk] for kk in keys[:3]},
                          "_last": {kk: v[kk] for kk in keys[-2:]}}
            elif isinstance(v, list) and len(v) > max_items:
                out[k] = {"_summary": f"{len(v)} items", "_first": v[:3],
                          "_last": v[-2:]}
            else:
                out[k] = v
        return out
    if isinstance(result, list) and len(result) > max_items:
        return {"_summary": f"{len(result)} items", "_first": result[:3]}
    return result


__all__ = ["LabSession"]
