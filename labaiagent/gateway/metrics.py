"""Prometheus text-format metrics for the gateway.

Scrape target for the ops stack a real deployment already runs (Prometheus /
Grafana / Alertmanager). Everything here is read from live objects -- no
audit-file scan per scrape, because a 1-second scrape interval against an
fsync-per-append log would be a self-inflicted denial of service.

Exposition format: version 0.0.4 text, the stable one every collector parses.
"""

from __future__ import annotations

import time
from typing import Any

_START = time.time()


def _esc(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def render_metrics(ctx: Any, *, extra: dict[str, float] | None = None) -> str:
    """Render the lab's operational state as Prometheus metrics."""
    lines: list[str] = []

    def head(name: str, help_: str, kind: str) -> None:
        lines.append(f"# HELP {name} {help_}")
        lines.append(f"# TYPE {name} {kind}")

    def metric(name: str, value: float, **labels: str) -> None:
        # Integers rendered exactly (%g would lose precision past 1e6 --
        # invocation counters and audit heads get there in a long campaign).
        text = str(int(value)) if float(value).is_integer() else f"{value:g}"
        if labels:
            lab = ",".join(f'{k}="{_esc(str(v))}"' for k, v in sorted(labels.items()))
            lines.append(f"{name}{{{lab}}} {text}")
        else:
            lines.append(f"{name} {text}")

    session = ctx.session

    head("labaiagent_up", "1 while the gateway is serving.", "gauge")
    metric("labaiagent_up", 1)

    head("labaiagent_uptime_seconds", "Seconds since process metrics init.",
         "gauge")
    metric("labaiagent_uptime_seconds", time.time() - _START)

    head("labaiagent_emergency_stop_active",
         "1 while the emergency stop is latched.", "gauge")
    metric("labaiagent_emergency_stop_active",
           1 if session.safety.estop.active else 0)

    head("labaiagent_devices", "Devices by current state.", "gauge")
    by_state: dict[str, int] = {}
    for dev in session:
        by_state[dev.state.value] = by_state.get(dev.state.value, 0) + 1
    for state in ("idle", "busy", "error", "disconnected", "estopped",
                  "connecting"):
        metric("labaiagent_devices", by_state.get(state, 0), state=state)

    head("labaiagent_device_invocations_total",
         "Capability invocations per device since connect.", "counter")
    for dev in session:
        metric("labaiagent_device_invocations_total",
               getattr(dev, "_invocations", 0), device=dev.id)

    head("labaiagent_jobs", "Async jobs by state (retained window).", "gauge")
    jobs_by: dict[str, int] = {}
    for j in ctx.jobs.list(limit=10_000):
        jobs_by[j.state.value] = jobs_by.get(j.state.value, 0) + 1
    for state in ("queued", "running", "succeeded", "failed", "cancelled"):
        metric("labaiagent_jobs", jobs_by.get(state, 0), state=state)

    head("labaiagent_audit_records_total",
         "Audit records written this session (sequence head).", "counter")
    metric("labaiagent_audit_records_total", session.audit.seq)

    if ctx.memory is not None:
        mem = ctx.memory
        head("labaiagent_tasks", "Durable task-board entries by status.",
             "gauge")
        tasks_by: dict[str, int] = {}
        for t in mem.list_tasks():
            tasks_by[t["status"]] = tasks_by.get(t["status"], 0) + 1
        for status in ("pending", "in_progress", "done", "failed",
                       "cancelled"):
            metric("labaiagent_tasks", tasks_by.get(status, 0), status=status)

        head("labaiagent_open_incidents", "Open incidents.", "gauge")
        metric("labaiagent_open_incidents", len(mem.list_incidents("open")))

        head("labaiagent_quarantined_devices",
             "Devices under incident quarantine.", "gauge")
        metric("labaiagent_quarantined_devices", len(mem.quarantines()))

    if ctx.records is not None:
        head("labaiagent_run_records", "Run records on disk.", "gauge")
        try:
            metric("labaiagent_run_records",
                   len(list(ctx.records.dir.glob("run_*.json"))))
        except OSError:  # pragma: no cover
            pass

    for name, value in (extra or {}).items():
        head(name, "Gateway-supplied metric.", "gauge")
        metric(name, value)

    return "\n".join(lines) + "\n"


__all__ = ["render_metrics"]
