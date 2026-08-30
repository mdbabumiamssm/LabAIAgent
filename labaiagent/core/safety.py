"""Safety engine.

Everything here runs *before* a single byte reaches the instrument. The design
premise is the one thing that separates lab actuation from software tool calls:
a physical action is not idempotent and cannot be rolled back. So the cost of a
false negative is unbounded, and the engine is deliberately fail-closed --
if a check cannot be evaluated, the call is refused.

Layers, in evaluation order:
  1. Emergency-stop latch      (global, cannot be bypassed by any token)
  2. Device state gate         (is the instrument in a legal state?)
  3. Parameter limits          (declared Range/OneOf/Pattern on each Param)
  4. Named interlocks          (cross-device preconditions, evaluated live)
  5. Rate limits               (guards runaway agent loops)
  6. Confirmation tokens       (human sign-off for HIGH/CRITICAL risk)
"""

from __future__ import annotations

import secrets
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .capability import Capability
from .errors import (
    ConfirmationRequired,
    EmergencyStopActive,
    InterlockFailure,
    InvalidState,
    SafetyViolation,
)
from .types import DeviceState, Risk

# --------------------------------------------------------------------------
# Emergency stop
# --------------------------------------------------------------------------

class EmergencyStop:
    """Process-wide latching stop.

    Latching is the important property: once tripped it stays tripped until a
    human calls ``reset()`` with a reason. Nothing an agent can emit will clear
    it, because the agent is exactly the thing you may be stopping.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._active = False
        self._reason = ""
        self._tripped_at: float | None = None
        self._listeners: list[Callable[[str], None]] = []

    @property
    def active(self) -> bool:
        with self._lock:
            return self._active

    @property
    def reason(self) -> str:
        with self._lock:
            return self._reason

    def trip(self, reason: str = "manual") -> None:
        with self._lock:
            if self._active:
                return
            self._active = True
            self._reason = reason
            self._tripped_at = time.time()
            listeners = list(self._listeners)
        for cb in listeners:
            try:
                cb(reason)
            except Exception:
                pass  # a failing listener must never block the stop

    def reset(self, *, operator: str, reason: str) -> None:
        if not operator or not reason:
            raise ValueError("E-stop reset requires both an operator and a reason.")
        with self._lock:
            self._active = False
            self._reason = ""
            self._tripped_at = None

    def on_trip(self, callback: Callable[[str], None]) -> None:
        """Register a hook -- drivers use this to halt motion immediately."""
        with self._lock:
            self._listeners.append(callback)

    def assert_clear(self, device: str = "", capability: str = "") -> None:
        if self.active:
            raise EmergencyStopActive(
                f"Emergency stop is latched (reason: {self.reason!r}). "
                f"Refusing {device}.{capability}. A human must call reset().",
                device=device, capability=capability, constraint="emergency_stop",
            )


# --------------------------------------------------------------------------
# Interlocks
# --------------------------------------------------------------------------

@dataclass
class Interlock:
    """A named precondition evaluated live against the lab session.

    ``check`` receives the session and the invocation context and returns True
    if it is safe to proceed. An exception inside a check counts as a failure
    (fail-closed), never as a pass.
    """

    name: str
    description: str
    check: Callable[..., bool]
    severity: Risk = Risk.HIGH

    def evaluate(self, session: Any, ctx: dict[str, Any]) -> tuple[bool, str]:
        try:
            ok = bool(self.check(session, ctx))
            return ok, "" if ok else f"interlock {self.name!r} not satisfied"
        except Exception as exc:
            return False, f"interlock {self.name!r} raised {type(exc).__name__}: {exc}"


class InterlockRegistry:
    def __init__(self) -> None:
        self._items: dict[str, Interlock] = {}

    def add(self, interlock: Interlock) -> None:
        self._items[interlock.name] = interlock

    def define(self, name: str, description: str, severity: Risk = Risk.HIGH):
        """Decorator form: @interlocks.define('lid_closed', '...')"""
        def deco(fn: Callable[..., bool]) -> Callable[..., bool]:
            self.add(Interlock(name=name, description=description, check=fn,
                               severity=severity))
            return fn
        return deco

    def get(self, name: str) -> Interlock | None:
        return self._items.get(name)

    def names(self) -> list[str]:
        return sorted(self._items)

    def describe(self) -> list[dict[str, str]]:
        return [{"name": i.name, "description": i.description,
                 "severity": i.severity.value} for i in self._items.values()]


# --------------------------------------------------------------------------
# Rate limiting
# --------------------------------------------------------------------------

@dataclass
class RateLimit:
    """Sliding-window cap on invocations, tracked *per actor*.

    Exists because the characteristic agent failure mode is not one bad
    command but the same marginal command a thousand times in a loop --
    aspirating a plate dry, or cycling a shutter until it fails.

    Windows are keyed by actor so one runaway agent exhausts its own budget,
    not every other caller's. An empty actor shares one anonymous window.
    """

    max_calls: int
    window_s: float
    scope: str = "capability"  # 'capability' | 'device' | 'global'
    _events: dict = field(default_factory=dict, repr=False)  # actor -> Deque

    def check_and_record(self, key: str, actor: str = "") -> None:
        q: deque[float] = self._events.setdefault(actor, deque())
        now = time.monotonic()
        while q and now - q[0] > self.window_s:
            q.popleft()
        if len(q) >= self.max_calls:
            raise SafetyViolation(
                f"Rate limit exceeded for {key}"
                + (f" (actor {actor!r})" if actor else "")
                + f": {self.max_calls} calls per {self.window_s:g}s. This "
                f"usually means a control loop is stuck; investigate before "
                f"raising the limit.",
                constraint="rate_limit",
                detail={"max_calls": self.max_calls, "window_s": self.window_s,
                        "actor": actor},
            )
        q.append(now)


# --------------------------------------------------------------------------
# Approval tokens
# --------------------------------------------------------------------------

@dataclass
class Approval:
    token: str
    operator: str
    reason: str
    device: str
    capability: str
    issued_at: float
    ttl_s: float
    uses_remaining: int

    @property
    def expired(self) -> bool:
        return (time.time() - self.issued_at) > self.ttl_s or self.uses_remaining <= 0


class ApprovalBroker:
    """Issues single-use, time-boxed, scoped human approvals.

    Scoping is what stops an approval for 'move the arm to position 3' being
    replayed as 'move the arm anywhere'. Tokens are bound to a device and a
    capability, expire, and are consumed on use.
    """

    def __init__(self, default_ttl_s: float = 300.0) -> None:
        self._tokens: dict[str, Approval] = {}
        self._lock = threading.RLock()
        self.default_ttl_s = default_ttl_s

    def issue(self, *, operator: str, reason: str, device: str, capability: str,
              ttl_s: float | None = None, uses: int = 1) -> str:
        if not operator or not reason:
            raise ValueError("Approvals require an operator identity and a reason.")
        token = secrets.token_urlsafe(16)
        with self._lock:
            self._tokens[token] = Approval(
                token=token, operator=operator, reason=reason, device=device,
                capability=capability, issued_at=time.time(),
                ttl_s=ttl_s if ttl_s is not None else self.default_ttl_s,
                uses_remaining=uses,
            )
        return token

    def consume(self, token: str, device: str, capability: str) -> Approval:
        with self._lock:
            appr = self._tokens.get(token)
            if appr is None:
                raise ConfirmationRequired("Unknown approval token.",
                                           device=device, capability=capability,
                                           constraint="approval")
            if appr.expired:
                self._tokens.pop(token, None)
                raise ConfirmationRequired("Approval token expired or exhausted.",
                                           device=device, capability=capability,
                                           constraint="approval")
            if appr.device != device or appr.capability != capability:
                raise ConfirmationRequired(
                    f"Approval token is scoped to {appr.device}.{appr.capability}, "
                    f"not {device}.{capability}.",
                    device=device, capability=capability, constraint="approval")
            appr.uses_remaining -= 1
            if appr.uses_remaining <= 0:
                self._tokens.pop(token, None)
            return appr


# --------------------------------------------------------------------------
# The engine
# --------------------------------------------------------------------------

@dataclass
class SafetyDecision:
    allowed: bool
    coerced_args: dict[str, Any] = field(default_factory=dict)
    violations: list[str] = field(default_factory=list)
    approval_operator: str | None = None


class SafetyEngine:
    """Evaluates every invocation before it reaches a driver."""

    def __init__(
        self,
        *,
        estop: EmergencyStop | None = None,
        interlocks: InterlockRegistry | None = None,
        approvals: ApprovalBroker | None = None,
        autonomy_ceiling: Risk = Risk.MEDIUM,
        dry_run: bool = False,
    ) -> None:
        self.estop = estop or EmergencyStop()
        self.interlocks = interlocks or InterlockRegistry()
        self.approvals = approvals or ApprovalBroker()
        #: Highest risk tier an unattended agent may invoke without a token.
        self.autonomy_ceiling = autonomy_ceiling
        #: Per-actor ceilings (verified principal id -> Risk). The effective
        #: ceiling for a call is the LOWER of the session ceiling and the
        #: actor's own -- an identity can only be more restricted, never less.
        self.actor_ceilings: dict[str, Risk] = {}
        self.dry_run = dry_run
        self._rate_limits: dict[str, RateLimit] = {}

    def effective_ceiling(self, actor: str = "") -> Risk:
        ceiling = self.autonomy_ceiling
        actor_ceiling = self.actor_ceilings.get(actor)
        if actor_ceiling is not None and actor_ceiling.rank < ceiling.rank:
            ceiling = actor_ceiling
        return ceiling

    def set_rate_limit(self, key: str, max_calls: int, window_s: float) -> None:
        """key is 'device.capability', 'device.*', or '*'."""
        self._rate_limits[key] = RateLimit(max_calls, window_s)

    def _applicable_rate_limits(self, device: str, capability: str) -> list[tuple[str, RateLimit]]:
        keys = [f"{device}.{capability}", f"{device}.*", "*"]
        return [(k, self._rate_limits[k]) for k in keys if k in self._rate_limits]

    def evaluate(
        self,
        *,
        session: Any,
        device_id: str,
        device_state: DeviceState,
        cap: Capability,
        kwargs: dict[str, Any],
        approval_token: str | None = None,
        actor: str = "",
    ) -> SafetyDecision:
        violations: list[str] = []
        operator: str | None = None

        # 1. E-stop -- checked first and never bypassable.
        if cap.kind.value != "read":
            self.estop.assert_clear(device_id, cap.name)

        # 2. State gate.
        if cap.requires_states and device_state.value not in cap.requires_states:
            raise InvalidState(
                f"{device_id}.{cap.name} requires state in {list(cap.requires_states)}, "
                f"device is {device_state.value!r}."
            )

        # 3. Parameter limits (raises with a precise message on failure).
        coerced = cap.validate_args(kwargs)

        # 4. Named interlocks.
        ctx = {"device": device_id, "capability": cap.name, "args": coerced,
               "actor": actor, "state": device_state}
        for name in cap.interlocks:
            lock = self.interlocks.get(name)
            if lock is None:
                # Fail closed: a capability declaring an interlock we cannot
                # evaluate is more dangerous than one declaring none.
                raise InterlockFailure(
                    f"{device_id}.{cap.name} declares interlock {name!r}, which is "
                    f"not registered in this session. Refusing to proceed.",
                    device=device_id, capability=cap.name, constraint=name)
            ok, msg = lock.evaluate(session, ctx)
            if not ok:
                violations.append(msg)

        if violations:
            raise InterlockFailure(
                f"{device_id}.{cap.name} blocked: " + "; ".join(violations),
                device=device_id, capability=cap.name,
                constraint="interlock", detail=violations)

        # 5. Rate limits (windows are per actor).
        for key, rl in self._applicable_rate_limits(device_id, cap.name):
            rl.check_and_record(key, actor=actor)

        # 6. Confirmation for high-risk actuation. The effective ceiling is
        #    the lower of the session ceiling and this actor's own ceiling.
        ceiling = self.effective_ceiling(actor)
        needs_token = (
            cap.requires_confirmation
            or cap.risk.rank > ceiling.rank
        )
        if needs_token and cap.kind.value != "read":
            if not approval_token:
                raise ConfirmationRequired(
                    f"{device_id}.{cap.name} is risk={cap.risk.value} "
                    f"(ceiling={ceiling.value})"
                    + (", irreversible" if not cap.reversible else "")
                    + ". A human approval token is required. Call "
                      f"session.request_approval('{device_id}', '{cap.name}', "
                      f"operator=..., reason=...).",
                    device=device_id, capability=cap.name, constraint="confirmation")
            appr = self.approvals.consume(approval_token, device_id, cap.name)
            operator = appr.operator

        return SafetyDecision(allowed=True, coerced_args=coerced,
                              approval_operator=operator)


# --------------------------------------------------------------------------
# Stock interlocks useful in most labs
# --------------------------------------------------------------------------

def standard_interlocks() -> InterlockRegistry:
    """A small library of cross-cutting interlocks worth having on day one."""
    reg = InterlockRegistry()

    @reg.define("no_device_in_error",
                "No instrument in the session is currently in an ERROR state.")
    def _no_error(session: Any, ctx: dict[str, Any]) -> bool:
        return not any(d.state is DeviceState.ERROR for d in session.devices())

    @reg.define("target_device_idle",
                "The instrument named in args['target'] is idle and free.")
    def _target_idle(session: Any, ctx: dict[str, Any]) -> bool:
        target = ctx["args"].get("target") or ctx["args"].get("destination")
        if not target:
            return True
        dev = session.get(str(target), required=False)
        return dev is None or dev.state is DeviceState.IDLE

    @reg.define("deck_position_free",
                "The destination deck position is not already occupied.")
    def _deck_free(session: Any, ctx: dict[str, Any]) -> bool:
        pos = ctx["args"].get("position") or ctx["args"].get("to_position")
        if pos is None:
            return True
        occupied: set[str] = getattr(session, "occupied_positions", set)()
        return pos not in occupied

    return reg


__all__ = [
    "EmergencyStop", "Interlock", "InterlockRegistry", "RateLimit",
    "Approval", "ApprovalBroker", "SafetyEngine", "SafetyDecision",
    "standard_interlocks",
]
