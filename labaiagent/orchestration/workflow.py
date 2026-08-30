"""Declarative protocol engine.

A ``Protocol`` is a DAG of steps. It exists because the alternative -- an agent
emitting one tool call at a time in a reasoning loop -- is too slow for
anything with a duty cycle, and leaves no artifact you can review, diff,
version or hand to a colleague.

The intended division of labour, and the one Anthropic's own MHS pilots
converged on: let the model explore interactively, then have it *emit a
Protocol*, review the Protocol, and run the Protocol deterministically. The
model writes the controller; it is not the controller.

Steps checkpoint after every completion, so a run interrupted at step 14 of 30
resumes at 15 rather than restarting -- which matters when steps consume
irreplaceable sample.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from ..core.errors import PhysicalError, SafetyViolation, WorkflowError
from .session import LabSession


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


class OnError(str, Enum):
    ABORT = "abort"        # stop the whole protocol (default -- safest)
    CONTINUE = "continue"  # log and move on
    RETRY = "retry"        # retry then abort
    RECOVER = "recover"    # run the step's recovery hook, then continue


@dataclass
class Step:
    """One action in a protocol."""

    name: str
    device: str
    capability: str
    args: dict[str, Any] = field(default_factory=dict)
    depends_on: tuple[str, ...] = ()
    on_error: OnError = OnError.ABORT
    retries: int = 0
    #: Callable(session, context) -> bool. False means skip this step.
    when: Callable[..., bool] | None = None
    #: Callable(session, context, exception) -> None, run on failure if RECOVER.
    recover: Callable[..., None] | None = None
    #: Store the result in the protocol context under this key.
    store_as: str = ""
    #: Resolve args at run time from context: {'barcode': '$plate'}
    approval: str | None = None
    note: str = ""

    status: StepStatus = StepStatus.PENDING
    result: Any = None
    error: str = ""
    duration_ms: float = 0.0
    started_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = {k: v for k, v in asdict(self).items()
             if k not in ("when", "recover")}
        d["on_error"] = self.on_error.value
        d["status"] = self.status.value
        return d


class Protocol:
    """An ordered, dependency-aware set of steps with a shared context."""

    def __init__(self, name: str, *, description: str = "",
                 steps: Iterable[Step] = (), context: dict[str, Any] | None = None,
                 checkpoint_path: str | Path | None = None) -> None:
        self.name = name
        self.description = description
        self.steps: list[Step] = list(steps)
        self.context: dict[str, Any] = dict(context or {})
        self.checkpoint_path = Path(checkpoint_path) if checkpoint_path else None
        self._actor: str | None = None
        self._state_lock = threading.RLock()   # context + checkpoint, parallel mode
        self._index: dict[str, Step] = {}
        for s in self.steps:
            self._add_index(s)

    def _add_index(self, s: Step) -> None:
        if s.name in self._index:
            raise WorkflowError(f"Duplicate step name {s.name!r} in protocol {self.name!r}")
        self._index[s.name] = s

    def step(self, name: str, device: str, capability: str, **kw: Any) -> Protocol:
        """Fluent builder: proto.step('read', 'reader', 'read_absorbance', ...)."""
        s = Step(name=name, device=device, capability=capability,
                 args=kw.pop("args", {}) or {}, **kw)
        self.steps.append(s)
        self._add_index(s)
        return self

    # -- validation -------------------------------------------------------

    def validate(self, session: LabSession, *,
                 actor: str | None = None) -> list[str]:
        """Static check before any hardware moves.

        Catches the errors that are cheap to find now and expensive to find at
        step 22 of a two-hour run: unknown device, unknown capability, bad
        argument, missing dependency, dependency cycle, and any step whose risk
        exceeds the autonomy ceiling with no approval attached.

        ``actor`` must be the same identity the protocol will RUN as, so that
        static validation applies the same *effective* ceiling (the lower of
        the session's and that actor's) as runtime enforcement.
        """
        problems: list[str] = []
        names = {s.name for s in self.steps}
        actor_id = actor or session.audit.actor

        for s in self.steps:
            dev = session.get(s.device, required=False)
            if dev is None:
                problems.append(f"[{s.name}] unknown device {s.device!r}")
                continue
            try:
                cap = dev.capability(s.capability)
            except Exception as exc:
                problems.append(f"[{s.name}] {exc}")
                continue
            # Only validate args with no unresolved context references.
            if not any(isinstance(v, str) and v.startswith("$") for v in s.args.values()):
                try:
                    cap.validate_args(dict(s.args))
                except Exception as exc:
                    problems.append(f"[{s.name}] {exc}")
            for dep in s.depends_on:
                if dep not in names:
                    problems.append(f"[{s.name}] depends on unknown step {dep!r}")
            ceiling = session.safety.effective_ceiling(actor_id)
            if (cap.risk.rank > ceiling.rank or cap.requires_confirmation) \
                    and not s.approval:
                problems.append(
                    f"[{s.name}] {s.device}.{cap.name} is risk={cap.risk.value} "
                    f"(ceiling {ceiling.value}) and has no approval token; it will "
                    f"block at run time. Attach one via step(..., approval=token)."
                )

        problems.extend(self._detect_cycles())
        return problems

    def _detect_cycles(self) -> list[str]:
        colour: dict[str, int] = {s.name: 0 for s in self.steps}

        def visit(n: str, path: list[str]) -> list[str]:
            if colour[n] == 1:
                return [f"dependency cycle: {' -> '.join(path + [n])}"]
            if colour[n] == 2:
                return []
            colour[n] = 1
            out: list[str] = []
            for dep in self._index[n].depends_on:
                if dep in colour:
                    out += visit(dep, path + [n])
            colour[n] = 2
            return out

        found: list[str] = []
        for s in self.steps:
            found += visit(s.name, [])
        return found

    # -- execution --------------------------------------------------------

    def _resolve(self, args: dict[str, Any]) -> dict[str, Any]:
        """Substitute $context references (lock-guarded for parallel mode)."""
        with self._state_lock:
            return self._resolve_locked(args)

    def _resolve_locked(self, args: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k, v in args.items():
            if isinstance(v, str) and v.startswith("$"):
                key = v[1:]
                if key not in self.context:
                    raise WorkflowError(
                        f"Step argument {k}={v!r} references context key {key!r}, "
                        f"which is not set. Available: {sorted(self.context)}")
                out[k] = self.context[key]
            else:
                out[k] = v
        return out

    def _ready(self) -> list[Step]:
        """Steps whose dependencies have all settled.

        A step that failed under on_error=CONTINUE or RECOVER counts as
        settled: the author explicitly said the protocol should proceed past
        it. A step that failed under ABORT does not, which is what stops
        downstream work running on a bad upstream result.
        """
        settled = {
            s.name for s in self.steps
            if s.status in (StepStatus.DONE, StepStatus.SKIPPED)
            or (s.status is StepStatus.FAILED
                and s.on_error in (OnError.CONTINUE, OnError.RECOVER))
        }
        return [s for s in self.steps
                if s.status is StepStatus.PENDING and set(s.depends_on) <= settled]

    def run(self, session: LabSession, *, validate: bool = True,
            progress: Callable[[Step], None] | None = None,
            should_cancel: Callable[[], bool] | None = None,
            actor: str | None = None,
            parallel: bool = False, max_workers: int = 4) -> dict[str, Any]:
        """Execute the protocol.

        ``should_cancel`` is polled *between* steps (never mid-step -- a
        physical action interrupted halfway is more dangerous than one that
        finishes). On cancellation, remaining steps are marked SKIPPED and the
        summary carries ``cancelled: True``.

        ``actor`` is the verified identity every step executes as. It reaches
        the safety engine (per-actor ceilings and rate limits) and the audit
        trail for EACH step -- a protocol must never be a way to shed the
        submitter's identity.

        ``parallel`` executes each wave of dependency-ready steps concurrently
        (bounded by ``max_workers``). Safety is unchanged: every step still
        goes through ``LabSession.call``, per-device locks still serialise
        actuation on any one instrument, and the audit log is thread-safe.
        What parallel mode buys is wall-clock: the thermocycler can cycle
        while the reader reads. Cancellation and ABORT are wave-granular --
        steps already dispatched in the current wave finish (never interrupt
        a physical action), and nothing new is dispatched after.
        """
        self._actor = actor
        if validate:
            problems = self.validate(session, actor=actor)
            if problems:
                raise WorkflowError(
                    f"Protocol {self.name!r} failed validation:\n  - "
                    + "\n  - ".join(problems))

        session.audit.record("protocol_start", reason=self.name,
                             result={"steps": len(self.steps),
                                     "description": self.description,
                                     "parallel": parallel})
        t0 = time.perf_counter()
        aborted = False
        cancelled = False

        while True:
            if should_cancel is not None and should_cancel():
                cancelled = True
                break
            ready = self._ready()
            if not ready:
                break
            if parallel and len(ready) > 1:
                # Group the wave BY DEVICE: distinct instruments run
                # concurrently; steps addressing the same instrument run in
                # order inside its lane. Racing two calls at one device would
                # trip the safety engine's state gate (BUSY -> refused), and
                # rightly so -- the gate must stay authoritative.
                #
                # Abort and cancellation are honoured INSIDE each lane, per
                # step (review finding R4-1): once any lane hits a
                # FAILED+ABORT step, or cancellation is requested, no lane
                # dispatches another step -- steps already executing finish
                # (never interrupt a physical action), the rest are SKIPPED.
                lanes: dict[str, list[Step]] = {}
                for s in ready:
                    lanes.setdefault(s.device, []).append(s)
                stop_wave = threading.Event()

                def _lane(steps: list[Step]) -> None:
                    for s in steps:
                        if stop_wave.is_set() or (
                                should_cancel is not None and should_cancel()):
                            s.status = StepStatus.SKIPPED
                            continue
                        self._run_step(session, s, progress)
                        if (s.status is StepStatus.FAILED
                                and s.on_error is OnError.ABORT):
                            stop_wave.set()

                with ThreadPoolExecutor(
                        max_workers=max(1, min(max_workers, len(lanes))),
                        thread_name_prefix=f"proto-{self.name}") as pool:
                    futures = [pool.submit(_lane, steps)
                               for steps in lanes.values()]
                    for f in futures:
                        f.result()   # _run_step never raises; surface bugs only
                if any(s.status is StepStatus.FAILED
                       and s.on_error is OnError.ABORT for s in ready):
                    aborted = True
                if should_cancel is not None and should_cancel():
                    cancelled = True
                if aborted or cancelled:
                    break
                continue
            for s in ready:
                if aborted:
                    s.status = StepStatus.SKIPPED
                    continue
                if should_cancel is not None and should_cancel():
                    cancelled = True
                    break
                self._run_step(session, s, progress)
                if s.status is StepStatus.FAILED and s.on_error is OnError.ABORT:
                    aborted = True
            if cancelled:
                break

        for s in self.steps:
            if s.status is StepStatus.PENDING:
                s.status = StepStatus.SKIPPED

        summary = self.summary()
        summary["elapsed_s"] = round(time.perf_counter() - t0, 3)
        summary["cancelled"] = cancelled
        session.audit.record(
            "protocol_cancelled" if cancelled else "protocol_end",
            reason=self.name, result=summary)
        if aborted:
            failed = [s.name for s in self.steps if s.status is StepStatus.FAILED]
            raise WorkflowError(
                f"Protocol {self.name!r} aborted at step {failed[0]!r}: "
                f"{self._index[failed[0]].error}", step=failed[0])
        return summary

    def _run_step(self, session: LabSession, s: Step,
                  progress: Callable[[Step], None] | None) -> None:
        if s.when is not None:
            try:
                if not s.when(session, self.context):
                    s.status = StepStatus.SKIPPED
                    return
            except Exception as exc:
                s.status = StepStatus.FAILED
                s.error = f"when() raised: {exc}"
                return

        s.status = StepStatus.RUNNING
        s.started_at = time.strftime("%Y-%m-%dT%H:%M:%S")
        t0 = time.perf_counter()
        try:
            args = self._resolve(s.args)
            result = session.call(s.device, s.capability, approval=s.approval,
                                  reason=f"protocol:{self.name}/{s.name}",
                                  actor=getattr(self, "_actor", None),
                                  retries=s.retries if s.on_error is OnError.RETRY else 0,
                                  **args)
            s.result = result
            s.status = StepStatus.DONE
            if s.store_as:
                with self._state_lock:
                    self.context[s.store_as] = result
        except (SafetyViolation, PhysicalError, Exception) as exc:
            s.error = f"{type(exc).__name__}: {exc}"
            s.status = StepStatus.FAILED
            if s.on_error is OnError.RECOVER and s.recover is not None:
                try:
                    s.recover(session, self.context, exc)
                    s.error += " (recovery hook ran)"
                except Exception as rexc:
                    s.error += f" (recovery ALSO failed: {rexc})"
            elif s.on_error is OnError.CONTINUE:
                pass
        finally:
            s.duration_ms = round((time.perf_counter() - t0) * 1000, 2)
            if progress:
                progress(s)
            self._checkpoint()

    def _checkpoint(self) -> None:
        if not self.checkpoint_path:
            return
        with self._state_lock:   # parallel steps checkpoint one at a time
            self.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"protocol": self.name,
                       # Full context VALUES, not just keys: a resumed run
                       # must be able to satisfy $refs into results produced
                       # before the crash (review finding R4-7). Values are
                       # JSON round-tripped (default=str for exotic types).
                       "context": self.context,
                       "context_keys": sorted(self.context),
                       "steps": [s.to_dict() for s in self.steps]}
            tmp = self.checkpoint_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2, default=str),
                           encoding="utf-8")
            tmp.replace(self.checkpoint_path)

    def resume_from_checkpoint(self) -> int:
        """Restore completed steps (status AND results) and the shared
        context from the checkpoint. Returns how many steps were skipped.

        Restoring the context is not optional politeness: a protocol whose
        later steps reference ``$key`` values stored by earlier steps would
        otherwise abort immediately on resume -- the exact runs that most
        need resuming are the ones that pass data forward.
        """
        if not self.checkpoint_path or not self.checkpoint_path.exists():
            return 0
        data = json.loads(self.checkpoint_path.read_text(encoding="utf-8"))
        by_name = {s["name"]: s for s in data["steps"]}
        n = 0
        for s in self.steps:
            saved = by_name.get(s.name)
            if saved and saved["status"] == "done":
                s.status = StepStatus.DONE
                s.result = saved.get("result")
                s.duration_ms = saved.get("duration_ms", 0.0)
                if s.store_as and s.store_as not in data.get("context", {}):
                    self.context[s.store_as] = s.result
                n += 1
        with self._state_lock:
            for k, v in (data.get("context") or {}).items():
                self.context.setdefault(k, v)
        return n

    # -- reporting --------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for s in self.steps:
            counts[s.status.value] = counts.get(s.status.value, 0) + 1
        return {
            "protocol": self.name,
            "total_steps": len(self.steps),
            "counts": counts,
            "failed": [{"step": s.name, "error": s.error}
                       for s in self.steps if s.status is StepStatus.FAILED],
            "total_device_time_ms": round(sum(s.duration_ms for s in self.steps), 2),
        }

    def report(self) -> str:
        lines = [f"PROTOCOL: {self.name}"]
        if self.description:
            lines.append(f"  {self.description}")
        lines.append("")
        icon = {StepStatus.DONE: "  ok  ", StepStatus.FAILED: " FAIL ",
                StepStatus.SKIPPED: " skip ", StepStatus.PENDING: "  --  ",
                StepStatus.RUNNING: " run  "}
        for i, s in enumerate(self.steps, 1):
            lines.append(f"{i:3d}. [{icon[s.status]}] {s.name:<28} "
                         f"{s.device}.{s.capability:<20} {s.duration_ms:8.1f} ms")
            if s.error:
                lines.append(f"          -> {s.error}")
        c = self.summary()["counts"]
        lines.append("")
        lines.append("  " + ", ".join(f"{v} {k}" for k, v in sorted(c.items())))
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description,
                "steps": [s.to_dict() for s in self.steps]}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Protocol:
        """Rehydrate a protocol from JSON/YAML -- this is the format an agent
        emits when asked to write a protocol rather than drive one live."""
        steps = []
        for sd in data.get("steps", []):
            steps.append(Step(
                name=sd["name"], device=sd["device"], capability=sd["capability"],
                args=sd.get("args", {}), depends_on=tuple(sd.get("depends_on", ())),
                on_error=OnError(sd.get("on_error", "abort")),
                retries=int(sd.get("retries", 0)),
                store_as=sd.get("store_as", ""), note=sd.get("note", ""),
                approval=sd.get("approval"),
            ))
        return cls(data["name"], description=data.get("description", ""), steps=steps)


__all__ = ["Protocol", "Step", "StepStatus", "OnError"]
