"""The Supervisor: behavioural oversight of AI agents.

The safety engine judges each call in isolation. The Supervisor judges the
*pattern*: the characteristic failure of an LLM agent is not one forbidden
call (the engine refuses it) but arguing with the limits -- retrying
variations of a refused action in a tight loop. The Supervisor watches
refusals per verified identity and, past a threshold, suspends that identity:
reads and the e-stop keep working, actuation stops, and only a human
reinstates.

Independently, every HIGH/CRITICAL actuation can be routed through a
pre-execution **reviewer** -- a second opinion that is not the acting agent:

  - ``RuleBasedReviewer``: deterministic checks, no dependencies, on by
    default (a stated reason is mandatory for dangerous actions; raw
    passthrough commands are scrutinised).
  - ``FoundationModelReviewer``: an independent foundation model (Anthropic
    Claude or OpenAI GPT, via their official SDKs and your API key) is shown
    the call and the instrument's reference sheet and asked to ALLOW or DENY
    with a reason. Opt-in, and FAIL-CLOSED: if the reviewer is unreachable,
    misconfigured, or returns garbage, the call is denied -- an oversight
    layer that fails open is decoration.

Both reviewer verdicts and suspensions land in the audit chain and the
feedback store, so oversight itself is auditable and its judgements become
RLHF training signal.
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..core.types import Risk
from ..memory.store import LabMemory
from .feedback import FeedbackStore

#: dispatch() error names that count as safety refusals for streak detection.
SAFETY_REFUSALS = {
    "SafetyViolation", "LimitViolation", "InterlockFailure",
    "EmergencyStopActive", "ConfirmationRequired", "InvalidState",
}


@dataclass(frozen=True)
class Verdict:
    allow: bool
    reason: str
    reviewer: str = "rules"

    def to_dict(self) -> dict[str, Any]:
        return {"allow": self.allow, "reason": self.reason,
                "reviewer": self.reviewer}


class Reviewer:
    """Interface: judge one proposed actuation before it executes."""

    name = "abstract"

    def review(self, context: dict[str, Any]) -> Verdict:  # pragma: no cover
        raise NotImplementedError


class RuleBasedReviewer(Reviewer):
    """Deterministic minimum bar for dangerous actions. Zero dependencies."""

    name = "rules"

    def __init__(self, *, min_reason_len: int = 8) -> None:
        self.min_reason_len = min_reason_len

    def review(self, context: dict[str, Any]) -> Verdict:
        risk = context.get("risk", "")
        reason = (context.get("reason") or "").strip()
        capability = context.get("capability", "")

        if len(reason) < self.min_reason_len:
            return Verdict(False, (
                f"{risk.upper()}-risk action submitted without a substantive "
                f"`reason` (got {reason!r}). State what this actuation is for "
                f"-- the reason is audited and is the reviewer's evidence."),
                self.name)
        if "raw" in capability.split(":")[-1]:
            return Verdict(False, (
                "Raw passthrough commands bypass declared semantics; they are "
                "not eligible for automated review. A human must run this "
                "directly or explicitly re-approve the capability."), self.name)
        return Verdict(True, "reason stated; no rule violated", self.name)


class FoundationModelReviewer(Reviewer):
    """Second-model oversight: an independent foundation model vetoes calls.

    ``provider`` is 'anthropic' or 'openai'; the official SDK and an API key
    (ANTHROPIC_API_KEY / OPENAI_API_KEY) must be available. For testing or
    self-hosted models, pass ``complete`` -- any ``prompt -> text`` callable.

    FAIL-CLOSED: every failure mode (no SDK, no key, network error, malformed
    verdict) DENIES the call with the failure named, because an oversight
    layer that fails open is worse than none -- it manufactures false
    assurance.
    """

    name = "foundation_model"

    PROMPT = """You are the independent safety reviewer for a laboratory \
automation system. An AI agent proposes a physical action. You are NOT that \
agent. Judge only whether the action is plausibly safe and justified.

Proposed action (JSON):
{context}

Instrument reference (excerpt, treat as ground truth):
{reference}

Reply with ONLY a JSON object: {{"allow": true|false, "reason": "<one \
sentence>"}}. Deny if the stated reason does not plausibly justify the \
action, if arguments look inconsistent with the reference, or if you are \
uncertain -- a wrong denial costs minutes; a wrong approval can cost the \
experiment or the instrument."""

    def __init__(self, provider: str = "anthropic", *, model: str = "",
                 complete: Callable[[str], str] | None = None,
                 timeout_s: float = 30.0) -> None:
        self.provider = provider
        self.model = model
        self.timeout_s = timeout_s
        self._complete = complete

    def _call_model(self, prompt: str) -> str:
        if self._complete is not None:
            return self._complete(prompt)
        if self.provider == "anthropic":
            import anthropic  # lazy; official SDK
            client = anthropic.Anthropic(timeout=self.timeout_s)
            msg = client.messages.create(
                model=self.model or "claude-sonnet-4-5",
                max_tokens=200,
                messages=[{"role": "user", "content": prompt}])
            return "".join(getattr(b, "text", "") for b in msg.content)
        if self.provider == "openai":
            import openai  # lazy; official SDK
            client = openai.OpenAI(timeout=self.timeout_s)
            resp = client.chat.completions.create(
                model=self.model or "gpt-4o",
                max_tokens=200,
                messages=[{"role": "user", "content": prompt}])
            return resp.choices[0].message.content or ""
        raise ValueError(f"Unknown reviewer provider {self.provider!r}")

    def review(self, context: dict[str, Any]) -> Verdict:
        prompt = self.PROMPT.format(
            context=json.dumps(context, indent=2, default=str)[:4000],
            reference=str(context.get("reference", ""))[:3000])
        try:
            text = self._call_model(prompt)
            start, end = text.find("{"), text.rfind("}")
            verdict = json.loads(text[start:end + 1])
            allow = bool(verdict["allow"])
            reason = str(verdict.get("reason", ""))[:400]
        except Exception as exc:
            return Verdict(False, (
                f"Reviewer unavailable or returned an unusable verdict "
                f"({type(exc).__name__}); denying fail-closed. Fix the "
                f"reviewer configuration or switch policy.oversight.reviewer "
                f"to 'rules'."), self.name)
        return Verdict(allow, reason or "no reason given", self.name)


# ==========================================================================
# The Supervisor
# ==========================================================================

@dataclass
class _Suspension:
    actor: str
    reason: str
    at: float


class Supervisor:
    """Behavioural oversight attached to a gateway context.

    ``max_refusals`` safety refusals by one identity within ``window_s``
    triggers automatic suspension. Reviews run for actuation at or above
    ``review_risk`` (default HIGH). All interventions are audited and fed to
    the FeedbackStore.
    """

    def __init__(self, *, reviewer: Reviewer | None = None,
                 max_refusals: int = 5, window_s: float = 120.0,
                 review_risk: Risk = Risk.HIGH,
                 feedback: FeedbackStore | None = None,
                 memory: LabMemory | None = None) -> None:
        self.reviewer = reviewer or RuleBasedReviewer()
        self.max_refusals = max_refusals
        self.window_s = window_s
        self.review_risk = review_risk
        self.feedback = feedback or FeedbackStore()
        #: Durable memory. When present, suspensions and device quarantines
        #: PERSIST: a restart never amnesties a suspended agent or frees a
        #: jammed instrument.
        self.memory = memory
        self._refusals: dict[str, deque[float]] = {}
        self._suspended: dict[str, _Suspension] = {}
        self._quarantined: dict[str, str] = {}          # device -> reason
        self._lock = threading.Lock()
        if memory is not None:
            for s_ in memory.suspensions():
                self._suspended[s_["actor"]] = _Suspension(
                    s_["actor"], s_["reason"], s_["since"])
            for q in memory.quarantines():
                self._quarantined[q["device"]] = q["reason"]

    # -- configuration --------------------------------------------------------

    @classmethod
    def from_config(cls, cfg: dict[str, Any] | None, *,
                    feedback_path: str | None = None,
                    memory: LabMemory | None = None) -> Supervisor:
        cfg = dict(cfg or {})
        reviewer_kind = str(cfg.get("reviewer", "rules"))
        reviewer: Reviewer
        if reviewer_kind in ("anthropic", "openai"):
            reviewer = FoundationModelReviewer(
                reviewer_kind, model=str(cfg.get("reviewer_model", "")))
        else:
            reviewer = RuleBasedReviewer()
        return cls(
            reviewer=reviewer,
            max_refusals=int(cfg.get("max_refusals", 5)),
            window_s=float(cfg.get("window_s", 120.0)),
            review_risk=Risk(cfg.get("review_risk", "high")),
            feedback=FeedbackStore(cfg.get("feedback_path", feedback_path)),
            memory=memory,
        )

    # -- suspension ------------------------------------------------------------

    def is_suspended(self, actor: str) -> bool:
        with self._lock:
            return actor in self._suspended

    def suspension(self, actor: str) -> dict[str, Any] | None:
        with self._lock:
            s = self._suspended.get(actor)
            return ({"actor": s.actor, "reason": s.reason, "since": s.at}
                    if s else None)

    def suspend(self, actor: str, reason: str, *, session: Any = None) -> None:
        with self._lock:
            self._suspended[actor] = _Suspension(actor, reason, time.time())
        if self.memory is not None:
            self.memory.save_suspension(actor, reason)
        if session is not None:
            session.audit.record("oversight_suspend", actor=actor,
                                 reason=reason)
        self.feedback.record("oversight_suspension", decision="reject",
                             actor=actor, judge="oversight:supervisor",
                             context={"reason": reason})

    def reinstate(self, actor: str, *, operator: str,
                  session: Any = None) -> None:
        """Only a human lifts a suspension -- there is no tool for this."""
        if not operator:
            raise ValueError("Reinstatement requires a named human operator.")
        with self._lock:
            self._suspended.pop(actor, None)
        if self.memory is not None:
            self.memory.clear_suspension(actor)
        if session is not None:
            session.audit.record("oversight_reinstate", actor=actor,
                                 reason=f"reinstated by {operator}")

    # -- device quarantine -------------------------------------------------------
    #
    # A PhysicalError means the hardware, not the request, is wrong -- a
    # clogged aperture, a dropped plate, a jammed drawer. The intelligent
    # response is the one a good tech gives: STOP USING THAT INSTRUMENT,
    # write up what happened, and hand a human a diagnosis. Quarantine does
    # exactly that; it persists across restarts and only a named human
    # (resolving the incident) lifts it.

    def is_quarantined(self, device: str) -> bool:
        with self._lock:
            return device in self._quarantined

    def quarantine_reason(self, device: str) -> str:
        with self._lock:
            return self._quarantined.get(device, "")

    def quarantine(self, device: str, reason: str, *,
                   session: Any = None) -> None:
        with self._lock:
            self._quarantined[device] = reason
        if self.memory is not None:
            self.memory.set_quarantine(device, reason)
        if session is not None:
            session.audit.record("device_quarantined", device=device,
                                 reason=reason)

    def release_quarantine(self, device: str, *, operator: str,
                           session: Any = None) -> None:
        if not operator:
            raise ValueError("Releasing a quarantine requires a named human.")
        with self._lock:
            self._quarantined.pop(device, None)
        if self.memory is not None:
            self.memory.clear_quarantine(device)
        if session is not None:
            session.audit.record("device_quarantine_released", device=device,
                                 reason=f"released by {operator}")

    # -- incident intelligence -----------------------------------------------------

    def diagnose(self, session: Any, device_id: str,
                 error_message: str) -> dict[str, Any]:
        """A structured first-responder diagnosis: what failed, what the
        driver's own operating notes say about it, what the instrument was
        doing, and what to do next. Rule-based and deterministic -- the
        point is a correct checklist, not eloquence."""
        dev = session.get(device_id, required=False)
        recent = [
            {"event": r.event, "capability": r.capability,
             "error": r.error, "t": r.timestamp}
            for r in session.audit.filter(device=device_id)[-5:]]
        actions = [
            "Do NOT retry the failed call; a physical fault does not clear "
            "by repetition.",
            f"Inspect {device_id} physically before any further use.",
        ]
        notes = ""
        if dev is not None:
            notes = (dev.notes or "").strip()
            if notes:
                actions.append(
                    "Check the operating notes below -- driver authors "
                    "record exactly these failure modes and their fixes.")
            if dev.state.value == "error":
                actions.append(
                    f"{device_id} is in ERROR state; reconnect and re-run "
                    f"its self-test after the physical cause is cleared.")
        actions.append(
            "When the cause is fixed, a human resolves the incident "
            "(lab_tasks action=resolve_incident), which lifts the "
            "quarantine.")
        return {
            "device": device_id,
            "state": dev.state.value if dev is not None else "unknown",
            "error": error_message[:500],
            "operating_notes": notes[:1500],
            "recent_audit": recent,
            "recommended_actions": actions,
        }

    def handle_physical_failure(self, session: Any, *, actor: str,
                                device_id: str, capability: str,
                                error_message: str,
                                task_id: str = "") -> dict[str, Any]:
        """Quarantine the device, open a persistent incident, and produce a
        diagnosis. Returns {incident, diagnosis} for the caller to attach to
        its payload so the agent sees WHAT to do next, not just a failure."""
        self.quarantine(device_id,
                        f"PhysicalError during {capability}: "
                        f"{error_message[:200]}", session=session)
        incident = None
        if self.memory is not None:
            incident = self.memory.open_incident(
                device=device_id, capability=capability,
                error_type="PhysicalError", message=error_message,
                task_id=task_id)
        self.feedback.record(
            "estop" if "estop" in error_message.lower() else "job_cancelled",
            decision="reject", actor=actor or "unknown",
            judge="oversight:incident",
            context={"tool": "incident", "device": device_id,
                     "capability": capability, "reason": error_message[:200]})
        return {"incident": incident,
                "diagnosis": self.diagnose(session, device_id, error_message)}

    def handle_job_failure(self, session: Any, job: dict[str, Any]) -> None:
        """Route async-job physical failures through the same intelligence."""
        error = job.get("error") or ""
        if not error.startswith("PhysicalError"):
            return
        device, _, cap = (job.get("label") or "").partition(".")
        if device:
            self.handle_physical_failure(
                session, actor=job.get("actor", ""), device_id=device,
                capability=cap, error_message=error)

    # -- streak detection --------------------------------------------------------

    def observe(self, actor: str, tool: str, payload: dict[str, Any], *,
                session: Any = None, arguments: dict[str, Any] | None = None,
                ) -> None:
        """Feed one dispatch outcome; may trip an automatic suspension, and
        routes physical failures into quarantine + incident + diagnosis
        (attached to the payload so the agent receives a repair plan)."""
        if not actor or payload.get("ok", False):
            return
        if (payload.get("error") == "PhysicalError" and session is not None
                and arguments and arguments.get("device_id")):
            info = self.handle_physical_failure(
                session, actor=actor,
                device_id=str(arguments.get("device_id")),
                capability=str(arguments.get("capability", tool)),
                error_message=str(payload.get("message", "")),
                task_id=str(arguments.get("task_id", "") or ""))
            payload["incident"] = info["incident"]
            payload["diagnosis"] = info["diagnosis"]
            payload["guidance"] = (
                "PHYSICAL fault: the device has been quarantined and an "
                "incident opened. Follow diagnosis.recommended_actions; do "
                "not retry on this device.")
            return
        if payload.get("error") not in SAFETY_REFUSALS:
            return
        now = time.monotonic()
        with self._lock:
            q = self._refusals.setdefault(actor, deque())
            while q and now - q[0] > self.window_s:
                q.popleft()
            q.append(now)
            tripped = len(q) >= self.max_refusals
            if tripped:
                q.clear()
        if tripped and not self.is_suspended(actor):
            self.suspend(
                actor,
                f"{self.max_refusals} safety refusals within "
                f"{self.window_s:g}s (last: {tool} -> "
                f"{payload.get('error')}). The agent is arguing with the "
                f"limits; a human must review before it actuates again.",
                session=session)

    # -- pre-execution review ------------------------------------------------------

    def review_call(self, session: Any, actor: str, role: str, tool: str,
                    arguments: dict[str, Any]) -> Verdict | None:
        """Second-opinion review for dangerous actuation. Returns None when
        no review is required (low risk, read, or validation-only)."""
        if tool not in ("write_state", "run_procedure", "run_protocol"):
            return None
        # Quarantine gate: a device under an open incident does not actuate,
        # whatever the risk class of the request.
        if tool in ("write_state", "run_procedure"):
            qdev = str(arguments.get("device_id", ""))
            if qdev and self.is_quarantined(qdev):
                return Verdict(False, (
                    f"{qdev!r} is QUARANTINED: {self.quarantine_reason(qdev)} "
                    f"A human must resolve the incident "
                    f"(lab_tasks action=resolve_incident) before this device "
                    f"actuates again."), "quarantine")
        else:
            for step in (arguments.get("protocol") or {}).get("steps", []):
                sdev = str(step.get("device", ""))
                if sdev and self.is_quarantined(sdev):
                    return Verdict(False, (
                        f"Protocol step {step.get('name')!r} targets "
                        f"quarantined device {sdev!r}: "
                        f"{self.quarantine_reason(sdev)}"), "quarantine")
        if tool == "run_protocol":
            if arguments.get("validate_only"):
                return None
            # Protocols are reviewed step-wise at validation; the reviewer
            # sees the document as a whole.
            context = {"actor": actor, "role": role, "tool": tool,
                       "risk": "high",
                       "protocol": arguments.get("protocol", {}),
                       "reason": (arguments.get("protocol", {}) or {}
                                  ).get("description", "")}
            steps = (arguments.get("protocol") or {}).get("steps", [])
            risky = self._max_risk_of_steps(session, steps)
            if risky.rank < self.review_risk.rank:
                return None
            return self.reviewer.review(context)

        device_id = arguments.get("device_id", "")
        capability = arguments.get("capability", "")
        dev = session.get(device_id, required=False)
        if dev is None:
            return None                     # dispatch will refuse anyway
        try:
            cap = dev.capability(capability)
        except Exception:
            return None
        if cap.risk.rank < self.review_risk.rank:
            # Below the review threshold the safety engine's own layers are
            # the control; demanding reviewer sign-off on every reversible
            # MEDIUM action would train operators to rubber-stamp.
            return None
        context = {
            "actor": actor, "role": role, "tool": tool, "device": device_id,
            "capability": f"{cap.kind.value}:{cap.name}",
            "risk": cap.risk.value, "reversible": cap.reversible,
            "arguments": arguments.get("arguments", {}),
            "reason": arguments.get("reason", ""),
            "reference": dev.reference_sheet(),
        }
        return self.reviewer.review(context)

    def _max_risk_of_steps(self, session: Any, steps: list) -> Risk:
        worst = Risk.NONE
        for s in steps or []:
            dev = session.get(s.get("device", ""), required=False)
            if dev is None:
                continue
            try:
                cap = dev.capability(s.get("capability", ""))
            except Exception:
                continue
            if cap.risk.rank > worst.rank:
                worst = cap.risk
        return worst

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "reviewer": self.reviewer.name,
                "max_refusals": self.max_refusals,
                "window_s": self.window_s,
                "review_risk": self.review_risk.value,
                "suspended": [
                    {"actor": s.actor, "reason": s.reason, "since": s.at}
                    for s in self._suspended.values()],
                "quarantined": dict(self._quarantined),
                "feedback": self.feedback.summary(),
            }


__all__ = ["Supervisor", "Verdict", "Reviewer", "RuleBasedReviewer",
           "FoundationModelReviewer", "SAFETY_REFUSALS"]
