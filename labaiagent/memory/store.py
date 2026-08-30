"""The durable store behind LabMemory. SQLite (WAL), stdlib-only,
thread-safe, and honest about what durability means: every write is
committed before the call returns; a ``kill -9`` loses at most the
operation in flight, never history.

Paths are server-derived, never caller-supplied: protocol checkpoints live
under ``<db dir>/checkpoints/task_<id>.json``, so no agent-controlled path
ever reaches the filesystem.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from ..core.errors import LabAIAgentError

TASK_STATUSES = ("pending", "in_progress", "done", "failed", "cancelled")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    ns TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
    updated_at REAL NOT NULL, PRIMARY KEY (ns, key));
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, instructions TEXT NOT NULL,
    protocol TEXT, status TEXT NOT NULL, priority INTEGER NOT NULL,
    created_by TEXT NOT NULL, owner TEXT DEFAULT '',
    created_at REAL NOT NULL, updated_at REAL NOT NULL,
    notes TEXT NOT NULL DEFAULT '[]', result TEXT);
CREATE TABLE IF NOT EXISTS incidents (
    id TEXT PRIMARY KEY, ts REAL NOT NULL, device TEXT NOT NULL,
    capability TEXT NOT NULL, error_type TEXT NOT NULL,
    message TEXT NOT NULL, task_id TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open',
    resolution TEXT DEFAULT '', resolved_by TEXT DEFAULT '',
    resolved_at REAL);
CREATE TABLE IF NOT EXISTS suspensions (
    actor TEXT PRIMARY KEY, reason TEXT NOT NULL, since REAL NOT NULL);
CREATE TABLE IF NOT EXISTS quarantines (
    device TEXT PRIMARY KEY, reason TEXT NOT NULL, since REAL NOT NULL);
CREATE TABLE IF NOT EXISTS startups (
    ts REAL NOT NULL, session_id TEXT NOT NULL);
"""


def _now() -> float:
    return round(time.time(), 3)


class LabMemory:
    """Durable, restart-surviving lab state. ``path=None`` gives an
    in-memory store with identical semantics (used by tests and ephemeral
    sessions)."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else None
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.checkpoint_dir = self.path.parent / "checkpoints"
        else:
            import tempfile
            self.checkpoint_dir = Path(tempfile.mkdtemp(prefix="labaiagent_cp_"))
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.path) if self.path else ":memory:",
                                   check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            if self.path:
                self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.executescript(_SCHEMA)
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._db.execute(sql, params)
            self._db.commit()
            return cur

    def _rows(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, params).fetchall()]

    # -- key-value memory ---------------------------------------------------

    def remember(self, ns: str, key: str, value: Any) -> None:
        self._exec(
            "INSERT INTO kv (ns, key, value, updated_at) VALUES (?,?,?,?) "
            "ON CONFLICT(ns, key) DO UPDATE SET value=excluded.value, "
            "updated_at=excluded.updated_at",
            (ns, key, json.dumps(value, default=str), _now()))

    def recall(self, ns: str, key: str, default: Any = None) -> Any:
        rows = self._rows("SELECT value FROM kv WHERE ns=? AND key=?", (ns, key))
        return json.loads(rows[0]["value"]) if rows else default

    def forget(self, ns: str, key: str) -> None:
        self._exec("DELETE FROM kv WHERE ns=? AND key=?", (ns, key))

    def namespace(self, ns: str) -> dict[str, Any]:
        return {r["key"]: json.loads(r["value"])
                for r in self._rows("SELECT key, value FROM kv WHERE ns=?",
                                    (ns,))}

    # -- tasks ----------------------------------------------------------------

    def add_task(self, title: str, instructions: str, *, created_by: str,
                 priority: int = 5,
                 protocol: dict[str, Any] | None = None) -> dict[str, Any]:
        if not title.strip():
            raise LabAIAgentError("A task needs a title.")
        tid = uuid.uuid4().hex[:10]
        now = _now()
        self._exec(
            "INSERT INTO tasks (id, title, instructions, protocol, status, "
            "priority, created_by, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (tid, title.strip(), instructions,
             json.dumps(protocol, default=str) if protocol else None,
             "pending", int(priority), created_by, now, now))
        return self.get_task(tid)

    def get_task(self, task_id: str) -> dict[str, Any]:
        rows = self._rows("SELECT * FROM tasks WHERE id=?", (task_id,))
        if not rows:
            raise LabAIAgentError(
                f"No task {task_id!r}. Known ids: "
                f"{[t['id'] for t in self.list_tasks()][:10]}")
        return self._task_out(rows[0])

    def _task_out(self, row: dict[str, Any]) -> dict[str, Any]:
        out = dict(row)
        out["notes"] = json.loads(out.get("notes") or "[]")
        out["protocol"] = (json.loads(out["protocol"])
                           if out.get("protocol") else None)
        out["result"] = json.loads(out["result"]) if out.get("result") else None
        out["has_checkpoint"] = self.checkpoint_path(out["id"]).exists()
        return out

    def list_tasks(self, status: str | None = None) -> list[dict[str, Any]]:
        if status:
            rows = self._rows(
                "SELECT * FROM tasks WHERE status=? "
                "ORDER BY priority ASC, created_at ASC", (status,))
        else:
            rows = self._rows(
                "SELECT * FROM tasks ORDER BY "
                "CASE status WHEN 'in_progress' THEN 0 WHEN 'pending' THEN 1 "
                "ELSE 2 END, priority ASC, created_at ASC")
        return [self._task_out(r) for r in rows]

    def next_task(self) -> dict[str, Any] | None:
        """The work the lab should pick up: an interrupted task first
        (it holds a checkpoint), otherwise the highest-priority pending one."""
        for status in ("in_progress", "pending"):
            rows = self.list_tasks(status)
            if rows:
                return rows[0]
        return None

    def update_task(self, task_id: str, *, status: str | None = None,
                    note: str = "", owner: str | None = None,
                    result: Any = None, by: str = "") -> dict[str, Any]:
        task = self.get_task(task_id)
        if status is not None:
            if status not in TASK_STATUSES:
                raise LabAIAgentError(
                    f"status must be one of {TASK_STATUSES}, not {status!r}")
            self._exec("UPDATE tasks SET status=?, updated_at=? WHERE id=?",
                       (status, _now(), task_id))
        if note:
            notes = task["notes"]
            notes.append({"ts": _now(), "by": by, "note": note[:2000]})
            self._exec("UPDATE tasks SET notes=?, updated_at=? WHERE id=?",
                       (json.dumps(notes, default=str), _now(), task_id))
        if owner is not None:
            self._exec("UPDATE tasks SET owner=?, updated_at=? WHERE id=?",
                       (owner, _now(), task_id))
        if result is not None:
            self._exec("UPDATE tasks SET result=?, updated_at=? WHERE id=?",
                       (json.dumps(result, default=str), _now(), task_id))
        return self.get_task(task_id)

    def checkpoint_path(self, task_id: str) -> Path:
        """Server-derived checkpoint location; never caller-supplied."""
        safe = "".join(c for c in task_id if c.isalnum())
        return self.checkpoint_dir / f"task_{safe}.json"

    # -- incidents --------------------------------------------------------------

    def open_incident(self, *, device: str, capability: str, error_type: str,
                      message: str, task_id: str = "") -> dict[str, Any]:
        iid = uuid.uuid4().hex[:10]
        self._exec(
            "INSERT INTO incidents (id, ts, device, capability, error_type, "
            "message, task_id) VALUES (?,?,?,?,?,?,?)",
            (iid, _now(), device, capability, error_type, message[:2000],
             task_id))
        return self.get_incident(iid)

    def get_incident(self, incident_id: str) -> dict[str, Any]:
        rows = self._rows("SELECT * FROM incidents WHERE id=?", (incident_id,))
        if not rows:
            raise LabAIAgentError(f"No incident {incident_id!r}.")
        return rows[0]

    def resolve_incident(self, incident_id: str, *, resolution: str,
                         by: str) -> dict[str, Any]:
        if not by:
            raise LabAIAgentError("Resolving an incident requires a named "
                                  "human (`by`).")
        self.get_incident(incident_id)
        self._exec(
            "UPDATE incidents SET status='resolved', resolution=?, "
            "resolved_by=?, resolved_at=? WHERE id=?",
            (resolution[:2000], by, _now(), incident_id))
        return self.get_incident(incident_id)

    def list_incidents(self, status: str = "open") -> list[dict[str, Any]]:
        if status == "all":
            return self._rows("SELECT * FROM incidents ORDER BY ts DESC")
        return self._rows(
            "SELECT * FROM incidents WHERE status=? ORDER BY ts DESC",
            (status,))

    # -- oversight persistence ------------------------------------------------------

    def save_suspension(self, actor: str, reason: str) -> None:
        self._exec(
            "INSERT INTO suspensions (actor, reason, since) VALUES (?,?,?) "
            "ON CONFLICT(actor) DO UPDATE SET reason=excluded.reason",
            (actor, reason, _now()))

    def clear_suspension(self, actor: str) -> None:
        self._exec("DELETE FROM suspensions WHERE actor=?", (actor,))

    def suspensions(self) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM suspensions")

    def set_quarantine(self, device: str, reason: str) -> None:
        self._exec(
            "INSERT INTO quarantines (device, reason, since) VALUES (?,?,?) "
            "ON CONFLICT(device) DO UPDATE SET reason=excluded.reason",
            (device, reason, _now()))

    def clear_quarantine(self, device: str) -> None:
        self._exec("DELETE FROM quarantines WHERE device=?", (device,))

    def quarantines(self) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM quarantines")

    # -- continuity -------------------------------------------------------------------

    def log_startup(self, session_id: str) -> None:
        self._exec("INSERT INTO startups (ts, session_id) VALUES (?,?)",
                   (_now(), session_id))

    def startup_report(self) -> dict[str, Any]:
        """Where the lab stopped, and where it was going. Computed fresh so
        it is equally valid at boot or mid-session."""
        interrupted = self.list_tasks("in_progress")
        pending = self.list_tasks("pending")
        prev = self._rows(
            "SELECT ts, session_id FROM startups ORDER BY ts DESC LIMIT 2")
        return {
            "previous_startup": prev[1] if len(prev) > 1 else None,
            "interrupted_tasks": [
                {"id": t["id"], "title": t["title"],
                 "has_checkpoint": t["has_checkpoint"],
                 "owner": t["owner"]} for t in interrupted],
            "pending_tasks": [
                {"id": t["id"], "title": t["title"], "priority": t["priority"]}
                for t in pending],
            "open_incidents": self.list_incidents("open"),
            "quarantined_devices": self.quarantines(),
            "suspended_agents": self.suspensions(),
            "hint": ("Resume interrupted tasks first: run_protocol with "
                     "task_id and resume=true continues from the checkpoint. "
                     "Quarantined devices need a human to resolve the "
                     "incident before they actuate again."),
        }


__all__ = ["LabMemory", "TASK_STATUSES"]
