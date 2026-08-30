"""Asynchronous job engine.

Physical operations are slow: a qPCR program runs for ninety minutes, an
Opentrons run for hours. A tool call that blocks for that long times out every
agent client ever written, and holds the whole server hostage while it waits.

The job engine turns any WRITE, PROCEDURE, or protocol run into a *handle*:

    job = jobs.submit_call(session, "cycler", "proc:run_qpcr", {...})
    jobs.get(job_id)        -> {state, progress, result, error, ...}
    jobs.cancel(job_id)     -> cooperative cancel (between protocol steps)

Design rules:
  - Jobs still funnel through ``LabSession.call`` -- the single invocation
    path is preserved; this is scheduling, not a second door to hardware.
  - Every state transition is an audit event.
  - Cancellation is cooperative and never interrupts a step mid-actuation:
    interrupting a physical action halfway is more dangerous than finishing
    it. For a hard stop there is the e-stop, which latches.
"""

from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..core.errors import LabAIAgentError


class JobState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in (JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED)


@dataclass
class Job:
    id: str
    kind: str                    # 'call' | 'protocol'
    label: str                   # 'device.capability' or protocol name
    actor: str = ""
    state: JobState = JobState.QUEUED
    submitted_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    progress: dict[str, Any] = field(default_factory=dict)
    result: Any = None
    error: str | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event,
                                          repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.id,
            "kind": self.kind,
            "label": self.label,
            "actor": self.actor,
            "state": self.state.value,
            "submitted_at": self.submitted_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "elapsed_s": round((self.finished_at or time.time())
                               - (self.started_at or self.submitted_at), 3),
            "progress": dict(self.progress),
            "result": self.result if self.state is JobState.SUCCEEDED else None,
            "error": self.error,
        }


class JobManager:
    """Bounded thread-pool executor for long-running lab operations.

    Per-device serialisation is already guaranteed by ``LabSession``'s
    per-device locks, so two jobs targeting the same instrument queue on the
    lock rather than interleaving.
    """

    def __init__(self, *, max_workers: int = 4, keep: int = 200,
                 max_active: int = 64, on_event: Any = None) -> None:
        self._pool = ThreadPoolExecutor(max_workers=max_workers,
                                        thread_name_prefix="labaiagent-job")
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._lock = threading.RLock()
        self._keep = keep
        #: Cap on live (queued + running) jobs. Without it, an agent looping
        #: on mode="async" grows the executor queue and the job table without
        #: bound -- the characteristic runaway-agent failure, applied to RAM.
        self._max_active = max_active
        #: Optional callable(event: dict) -- wired to the gateway event bus.
        self.on_event = on_event

    # -- submission ---------------------------------------------------------

    def submit_call(self, session: Any, device_id: str, capability: str,
                    arguments: dict[str, Any] | None = None, *,
                    actor: str = "", approval: str | None = None,
                    reason: str = "") -> Job:
        job = self._new_job("call", f"{device_id}.{capability}", actor)

        def work() -> Any:
            return session.call(device_id, capability, approval=approval,
                                reason=reason or f"job:{job.id}",
                                actor=actor or None, **(arguments or {}))

        self._start(job, session, work)
        return job

    def submit_protocol(self, session: Any, protocol: Any, *,
                        actor: str = "", parallel: bool = False,
                        on_done: Any = None) -> Job:
        """``protocol`` is an orchestration.workflow.Protocol instance,
        already validated by the caller. ``on_done(job)`` runs after the
        terminal state is set (used to persist task outcomes).
        ``parallel`` is forwarded to ``Protocol.run`` (DAG-wave concurrency;
        per-device locks still serialise actuation per instrument)."""
        job = self._new_job("protocol", protocol.name, actor)
        total = len(protocol.steps)

        def progress(step: Any) -> None:
            done = sum(1 for s in protocol.steps
                       if s.status.value in ("done", "skipped", "failed"))
            job.progress.update({
                "steps_total": total, "steps_settled": done,
                "last_step": step.name, "last_status": step.status.value,
            })
            self._emit("job.progress", job)

        def work() -> Any:
            # The submitter's verified identity runs EVERY step -- a protocol
            # must never be a way to shed per-actor ceilings or rate limits.
            return protocol.run(session, validate=False, progress=progress,
                                should_cancel=job.cancel_event.is_set,
                                actor=actor or None, parallel=parallel)

        self._start(job, session, work, on_done=on_done)
        return job

    # -- lifecycle ----------------------------------------------------------

    def _new_job(self, kind: str, label: str, actor: str) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], kind=kind, label=label, actor=actor)
        with self._lock:
            active = sum(1 for j in self._jobs.values() if not j.state.terminal)
            if active >= self._max_active:
                raise LabAIAgentError(
                    f"Too many jobs in flight ({active} queued or running; "
                    f"limit {self._max_active}). Wait for existing jobs to "
                    f"finish (get_job / list_jobs) or cancel some "
                    f"(cancel_job) before submitting more.")
            self._jobs[job.id] = job
            self._order.append(job.id)
            # Evict oldest *terminal* jobs beyond the retention window.
            while len(self._order) > self._keep:
                for jid in self._order:
                    if self._jobs[jid].state.terminal:
                        self._order.remove(jid)
                        self._jobs.pop(jid, None)
                        break
                else:
                    break
        return job

    def _start(self, job: Job, session: Any, work: Any,
               on_done: Any = None) -> None:
        audit = getattr(session, "audit", None)
        if audit:
            audit.record("job_submitted", reason=job.label,
                         result={"job_id": job.id, "kind": job.kind},
                         actor=job.actor or None)
        self._emit("job.submitted", job)

        def run() -> None:
            if job.cancel_event.is_set():
                # Cancelled while still queued. This is a terminal settlement
                # like any other: it must be audited and it must fire on_done,
                # or a task-linked protocol job would leave its task stuck
                # 'in_progress' forever with no run record (review finding
                # R4-6).
                job.state = JobState.CANCELLED
                job.finished_at = time.time()
                if audit:
                    audit.record("job_finished", reason=job.label,
                                 result={"job_id": job.id,
                                         "state": job.state.value,
                                         "note": "cancelled before start"},
                                 actor=job.actor or None)
                self._emit("job.cancelled", job)
                if on_done is not None:
                    try:
                        on_done(job)
                    except Exception:
                        pass
                return
            job.state = JobState.RUNNING
            job.started_at = time.time()
            self._emit("job.started", job)
            try:
                result = work()
                cancelled = (job.kind == "protocol"
                             and isinstance(result, dict)
                             and result.get("cancelled"))
                job.result = result
                job.state = JobState.CANCELLED if cancelled else JobState.SUCCEEDED
            except Exception as exc:
                job.error = f"{type(exc).__name__}: {exc}"
                job.state = JobState.FAILED
            finally:
                job.finished_at = time.time()
                if audit:
                    audit.record("job_finished", reason=job.label,
                                 result={"job_id": job.id,
                                         "state": job.state.value},
                                 error=job.error, actor=job.actor or None)
                self._emit(f"job.{job.state.value}", job)
                if on_done is not None:
                    try:
                        on_done(job)
                    except Exception:
                        pass

        self._pool.submit(run)

    # -- inspection / control -------------------------------------------------

    def get(self, job_id: str) -> Job:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise LabAIAgentError(
                f"No job {job_id!r}. Known (most recent first): "
                f"{list(reversed(self._order))[:10]}")
        return job

    def cancel(self, job_id: str, *, reason: str = "") -> Job:
        job = self.get(job_id)
        if job.state.terminal:
            return job
        job.cancel_event.set()
        job.progress["cancel_requested"] = True
        if reason:
            job.progress["cancel_reason"] = reason
        self._emit("job.cancel_requested", job)
        return job

    def list(self, *, state: str | None = None, limit: int = 50) -> list[Job]:
        with self._lock:
            jobs = [self._jobs[j] for j in reversed(self._order)]
        if state:
            jobs = [j for j in jobs if j.state.value == state]
        return jobs[:limit]

    def wait(self, job_id: str, *, timeout: float = 60.0,
             poll_s: float = 0.05) -> Job:
        """Convenience for tests and synchronous callers."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = self.get(job_id)
            if job.state.terminal:
                return job
            time.sleep(poll_s)
        return self.get(job_id)

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

    def _emit(self, event: str, job: Job) -> None:
        if self.on_event:
            try:
                self.on_event({"event": event, **job.to_dict()})
            except Exception:
                pass


__all__ = ["Job", "JobManager", "JobState"]
