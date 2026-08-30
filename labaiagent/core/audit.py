"""Tamper-evident audit trail.

Every actuation is appended to a hash-chained JSONL file. Each record embeds
the SHA-256 of the previous record, so any edit or deletion anywhere in the
history invalidates every subsequent link and is detectable by ``verify()``.

**Threat-model honesty:** an unkeyed hash chain detects *modification* of the
file, but an adversary with write access can rewrite the ENTIRE file and
recompute every hash -- the chain proves internal consistency, not
authenticity. For that stronger property, supply an ``hmac_key``: every link
is then computed with HMAC-SHA256 under the key, so a whole-file rewrite by
anyone without the key is detectable. Keep the key out of the log host's
reach (an env var injected at service start, a KMS, an operator prompt) and
anchor the current head hash somewhere external periodically.

This is the piece that makes agent-driven instrumentation defensible under
ALCOA+ expectations (Attributable, Legible, Contemporaneous, Original,
Accurate, plus Complete/Consistent/Enduring/Available) and gives you the
records-and-signatures substrate 21 CFR Part 11 asks for. It is not by itself
a Part 11 compliance claim -- that requires validation, access control, and
SOPs around this file -- but it removes the usual blocker, which is that
LLM-driven actions leave no reconstructible trail.
"""

from __future__ import annotations

import getpass
import hashlib
import hmac as _hmac
import json
import os
import socket
import threading
import uuid
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

GENESIS = "0" * 64


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _json_default(o: Any) -> Any:
    if hasattr(o, "to_dict"):
        return o.to_dict()
    if hasattr(o, "isoformat"):
        return o.isoformat()
    if isinstance(o, (set, frozenset, tuple)):
        return list(o)
    if isinstance(o, bytes):
        return o.decode("utf-8", "replace")
    return repr(o)


def canonical(payload: dict[str, Any]) -> str:
    """Deterministic serialisation -- sorted keys, no incidental whitespace."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      default=_json_default, ensure_ascii=False)


@dataclass
class AuditRecord:
    seq: int
    timestamp: str
    actor: str                 # who/what initiated: 'agent:claude', 'user:bmia'
    host: str
    session_id: str
    event: str                 # 'invoke', 'result', 'error', 'estop', 'override'
    device: str = ""
    capability: str = ""
    kind: str = ""
    risk: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    result: Any = None
    error: str | None = None
    duration_ms: float | None = None
    state_before: str = ""
    state_after: str = ""
    reason: str = ""           # why -- free text, required for overrides
    approval: str | None = None
    prev_hash: str = GENESIS
    hash: str = ""

    def compute_hash(self, key: bytes | None = None) -> str:
        """Chain link for this record.

        Unkeyed: SHA-256 (internal-consistency evidence). Keyed: HMAC-SHA256
        (authenticity evidence -- a whole-file rewrite without the key cannot
        produce valid links). The record schema is identical in both modes;
        the mode is a property of the log, not of the record.
        """
        body = {k: v for k, v in asdict(self).items() if k != "hash"}
        data = canonical(body).encode("utf-8")
        if key:
            return _hmac.new(key, data, hashlib.sha256).hexdigest()
        return hashlib.sha256(data).hexdigest()


class AuditLog:
    """Append-only, hash-chained event log.

    Thread-safe. Writes are flushed and fsync'd by default so a power loss
    mid-run cannot silently truncate the trail -- set ``fsync=False`` only if
    you are logging to a remote store that guarantees durability itself.
    """

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        actor: str = "",
        session_id: str | None = None,
        fsync: bool = True,
        echo: bool = False,
        hmac_key: bytes | str | None = None,
    ) -> None:
        self.path = Path(path) if path else None
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        if hmac_key is None:
            env = os.environ.get("LABAIAGENT_AUDIT_HMAC_KEY", "")
            hmac_key = env or None
        self._hmac_key: bytes | None = (
            hmac_key.encode("utf-8") if isinstance(hmac_key, str) else hmac_key)
        self.actor = actor or f"user:{_safe_user()}"
        self.session_id = session_id or uuid.uuid4().hex[:12]
        self.host = socket.gethostname()
        self.fsync = fsync
        self.echo = echo
        self._lock = threading.RLock()
        self._seq = 0
        self._last_hash = GENESIS
        self._memory: list[AuditRecord] = []
        self._resume_ok = True
        self._resume_msg = ""
        if self.path and self.path.exists():
            self._resume()

    # -- writing ----------------------------------------------------------

    def _resume(self) -> None:
        """Pick up the chain from an existing file -- after verifying it.

        Resuming blindly from the last line would let tampering that happened
        while the process was down be silently extended with valid records.
        Instead the whole chain is re-walked here. A log that fails
        verification stays readable (``records``, ``verify``, ``tail`` all
        work, so the damage can be inspected), but this instance refuses to
        APPEND to it -- extending a broken chain would launder the break.
        """
        assert self.path is not None
        last: dict[str, Any] | None = None
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    last = json.loads(line)
        if last:
            self._seq = int(last["seq"])
            self._last_hash = last["hash"]
            ok, msg = self.verify()
            if not ok:
                self._resume_ok = False
                self._resume_msg = msg

    def record(self, event: str, **fields: Any) -> AuditRecord:
        if not self._resume_ok:
            raise RuntimeError(
                f"Refusing to append to {self.path}: the existing chain does "
                f"not verify ({self._resume_msg}). Investigate the tampering, "
                f"archive the file, and start a fresh log."
            )
        with self._lock:
            self._seq += 1
            rec = AuditRecord(
                seq=self._seq,
                timestamp=_utc_now(),
                actor=fields.pop("actor", None) or self.actor,
                host=self.host,
                session_id=self.session_id,
                event=event,
                prev_hash=self._last_hash,
                **fields,
            )
            rec.hash = rec.compute_hash(self._hmac_key)
            self._last_hash = rec.hash
            if self.path is None:
                # In-memory mode only: the file IS the store otherwise, and
                # duplicating every record in RAM is a leak on the busiest
                # control-path object in a long-lived server.
                self._memory.append(rec)
            if self.path:
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(canonical(asdict(rec)) + "\n")
                    fh.flush()
                    if self.fsync:
                        os.fsync(fh.fileno())
            if self.echo:
                print(f"[audit {rec.seq:05d}] {rec.event} {rec.device}."
                      f"{rec.capability} {rec.error or ''}".rstrip())
            return rec

    @property
    def seq(self) -> int:
        """Sequence number of the most recent record (0 when empty).

        Callers snapshot this before starting a bounded piece of work (a
        protocol run) so provenance can later slice out exactly the records
        the work produced.
        """
        with self._lock:
            return self._seq

    # -- reading / verification -------------------------------------------

    def __iter__(self) -> Iterator[AuditRecord]:
        return iter(self.records())

    def records(self) -> list[AuditRecord]:
        if not self.path or not self.path.exists():
            return list(self._memory)
        out: list[AuditRecord] = []
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    out.append(AuditRecord(**json.loads(line)))
        return out

    def verify(self, *, records: list[AuditRecord] | None = None) -> tuple[bool, str]:
        """Re-walk the chain. Returns (ok, human-readable message).

        Pass ``records`` (from one ``records()`` call) to avoid re-reading the
        file when the caller already holds the list.
        """
        prev = GENESIS
        expected_seq = 0
        for rec in (records if records is not None else self.records()):
            expected_seq += 1
            if rec.seq != expected_seq:
                return False, f"sequence gap at record {rec.seq} (expected {expected_seq})"
            if rec.prev_hash != prev:
                return False, f"broken chain at record {rec.seq}: prev_hash mismatch"
            if rec.compute_hash(self._hmac_key) != rec.hash:
                return False, (f"record {rec.seq} fails verification -- modified "
                               f"after writing"
                               + (" (or the wrong HMAC key was supplied)"
                                  if self._hmac_key else ""))
            prev = rec.hash
        return True, f"chain intact across {expected_seq} record(s)"

    def tail(self, n: int = 20) -> list[dict[str, Any]]:
        return [asdict(r) for r in self.records()[-n:]]

    def filter(self, *, device: str | None = None, event: str | None = None,
               since: str | None = None) -> list[AuditRecord]:
        out = self.records()
        if device:
            out = [r for r in out if r.device == device]
        if event:
            out = [r for r in out if r.event == event]
        if since:
            out = [r for r in out if r.timestamp >= since]
        return out

    def summary(self, *, records: list[AuditRecord] | None = None) -> dict[str, Any]:
        recs = records if records is not None else self.records()
        by_event: dict[str, int] = {}
        by_device: dict[str, int] = {}
        for r in recs:
            by_event[r.event] = by_event.get(r.event, 0) + 1
            if r.device:
                by_device[r.device] = by_device.get(r.device, 0) + 1
        ok, msg = self.verify(records=recs)
        return {
            "session_id": self.session_id,
            "records": len(recs),
            "first": recs[0].timestamp if recs else None,
            "last": recs[-1].timestamp if recs else None,
            "by_event": by_event,
            "by_device": by_device,
            "chain_valid": ok,
            "chain_status": msg,
        }


def _safe_user() -> str:
    try:
        return getpass.getuser()
    except Exception:  # pragma: no cover - headless containers
        return os.environ.get("USER", "unknown")


__all__ = ["AuditLog", "AuditRecord", "canonical", "GENESIS"]
