"""Signed, exportable run records.

Design constraints, in order:

1. A record must be USEFUL EVIDENCE: everything needed to reconstruct the run
   lives inside the one file -- no joins against a database that may have
   been migrated, no references to a log that may have rotated.
2. A record must be TAMPER-EVIDENT: the body is checksummed (SHA-256 over a
   canonical serialisation) and, when the lab runs with an audit HMAC key,
   signed with HMAC-SHA256 under the same key. ``verify`` recomputes both.
3. Building a record must be CHEAP AND NON-FATAL: provenance is written after
   the run settles; a failure to write a record must never fail the run
   itself (the audit trail still holds the ground truth).

What a record deliberately is NOT: it is not the audit log (that is the
append-only ground truth; the record embeds a *slice* of it), and it is not a
compliance claim by itself -- it is the technical artifact a validation
package builds on (docs/COMPLIANCE.md maps the two).
"""

from __future__ import annotations

import hashlib
import hmac as _hmac
import json
import platform
import sys
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ..core.audit import canonical
from ..core.errors import LabAIAgentError

RECORD_VERSION = "1"


def _utc_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def build_run_record(
    session: Any,
    proto: Any,
    *,
    status: str,
    actor: str = "",
    task_id: str = "",
    resumed_steps: int = 0,
    seq_before: int = 0,
) -> dict[str, Any]:
    """Assemble one run record from a settled protocol run.

    ``seq_before`` is the audit sequence number captured immediately before
    the run started; the embedded audit slice is every record after it that
    belongs to this protocol (its start/end markers and each step's calls),
    kept with hashes intact so the slice remains independently checkable
    against the full log.
    """
    from .. import __version__

    proto_doc = proto.to_dict()
    device_ids = sorted({s["device"] for s in proto_doc.get("steps", [])})
    devices = []
    for did in device_ids:
        dev = session.get(did, required=False)
        if dev is None:
            devices.append({"id": did, "missing": True})
            continue
        devices.append({
            "id": dev.id, "vendor": dev.vendor, "model": dev.model,
            "category": dev.category, "driver": type(dev).__name__,
            "driver_version": dev.driver_version,
            "simulated": dev.simulated, "location": dev.location,
        })

    marker = f"protocol:{proto.name}/"
    slice_recs = []
    for r in session.audit.records():
        if r.seq <= seq_before:
            continue
        if (r.reason or "").startswith(marker) or (
                r.event in ("protocol_start", "protocol_end",
                            "protocol_cancelled")
                and r.reason == proto.name):
            slice_recs.append(asdict(r))

    body: dict[str, Any] = {
        "record_version": RECORD_VERSION,
        "record_id": uuid.uuid4().hex[:16],
        "created_at": _utc_now(),
        "status": status,                       # done | failed | cancelled
        "lab": session.name,
        "session_id": session.audit.session_id,
        "actor": actor or session.audit.actor,
        "task_id": task_id or None,
        "resumed_steps": resumed_steps,
        "software": {
            "labaiagent": __version__,
            "python": sys.version.split()[0],
            "platform": platform.platform(),
        },
        "devices": devices,
        "protocol": proto_doc,                  # as EXECUTED: statuses, results,
                                                # errors, per-step durations
        "audit_slice": {
            "filter": f"seq > {seq_before} and (reason startswith {marker!r} "
                      f"or protocol start/end markers)",
            "n_records": len(slice_recs),
            "records": slice_recs,
        },
    }
    return body


class RunRecordStore:
    """Directory of integrity-protected run-record JSON files.

    Layout: ``<dir>/run_<record_id>.json``. Each file is
    ``{"body": {...}, "integrity": {sha256, algorithm, signature?}}``.
    The signature is HMAC-SHA256 over the canonical body, under the same key
    that keys the audit chain -- one secret to manage, one trust root.
    """

    def __init__(self, directory: str | Path,
                 hmac_key: bytes | str | None = None) -> None:
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._key: bytes | None = (
            hmac_key.encode("utf-8") if isinstance(hmac_key, str) else hmac_key)

    # -- integrity ---------------------------------------------------------

    def _seal(self, body: dict[str, Any]) -> dict[str, Any]:
        data = canonical(body).encode("utf-8")
        integrity: dict[str, Any] = {
            "algorithm": "sha256+hmac-sha256" if self._key else "sha256",
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        if self._key:
            integrity["signature"] = _hmac.new(
                self._key, data, hashlib.sha256).hexdigest()
        return {"body": body, "integrity": integrity}

    def _path(self, record_id: str) -> Path:
        safe = "".join(c for c in record_id if c.isalnum())
        if not safe:
            raise LabAIAgentError("Empty or invalid record_id.")
        return self.dir / f"run_{safe}.json"

    # -- API ----------------------------------------------------------------

    def save(self, body: dict[str, Any]) -> str:
        # Normalise BEFORE sealing (review finding R4-3): protocol step
        # results are arbitrary driver values (datetime, Path, ...), whose
        # repr-based canonical form would differ from what a JSON round-trip
        # of the saved file re-canonicalises to -- making a legitimate record
        # unverifiable. One canonical round-trip here means the bytes hashed
        # are exactly the bytes any later verify() will reconstruct.
        body = json.loads(canonical(body))
        sealed = self._seal(body)
        path = self._path(body["record_id"])
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(sealed, indent=2), encoding="utf-8")
        tmp.replace(path)
        return body["record_id"]

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        rows: list[dict[str, Any]] = []
        for p in sorted(self.dir.glob("run_*.json"),
                        key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
            try:
                sealed = json.loads(p.read_text(encoding="utf-8"))
                body = sealed["body"]
            except Exception:
                rows.append({"record_id": p.stem[4:], "unreadable": True})
                continue
            rows.append({
                "record_id": body.get("record_id"),
                "created_at": body.get("created_at"),
                "status": body.get("status"),
                "protocol": body.get("protocol", {}).get("name"),
                "actor": body.get("actor"),
                "task_id": body.get("task_id"),
                "devices": [d.get("id") for d in body.get("devices", [])],
                "signed": "signature" in sealed.get("integrity", {}),
            })
        return rows

    def get(self, record_id: str) -> dict[str, Any]:
        path = self._path(record_id)
        if not path.exists():
            raise LabAIAgentError(f"No run record {record_id!r}.")
        return json.loads(path.read_text(encoding="utf-8"))

    def verify(self, record_id: str) -> tuple[bool, str]:
        """Recompute the checksum (and signature, when keyed) of one record."""
        sealed = self.get(record_id)
        body, integrity = sealed.get("body", {}), sealed.get("integrity", {})
        data = canonical(body).encode("utf-8")
        if hashlib.sha256(data).hexdigest() != integrity.get("sha256"):
            return False, "checksum mismatch -- the record was modified"
        if self._key:
            sig = integrity.get("signature", "")
            want = _hmac.new(self._key, data, hashlib.sha256).hexdigest()
            if not sig:
                return False, ("record is unsigned but this store has a key -- "
                               "it predates the key or was written elsewhere")
            if not _hmac.compare_digest(sig, want):
                return False, "signature mismatch -- wrong key or modified record"
            return True, "checksum and signature verify"
        return True, "checksum verifies (store has no HMAC key; unsigned)"


__all__ = ["RunRecordStore", "build_run_record", "RECORD_VERSION"]
