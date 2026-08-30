"""Human-preference capture: the RLHF substrate.

Reinforcement learning from human feedback needs one thing above all: an
honest, attributable record of which agent actions humans endorsed and which
they rejected. In a lab, those judgements already happen -- an approver mints
a token (endorsement), an operator cancels a job or trips the e-stop
(rejection), oversight suspends an agent (strong rejection), a reviewer
rates a finished run (explicit label). This module captures each of them as
a structured preference record.

What this module deliberately does NOT do: train anything. Preference
optimisation (RLHF/DPO/KTO) belongs on training infrastructure with your
model provider's tooling; shipping a toy trainer inside a lab-control
process would be theatre. What ships instead is the part only the lab can
produce -- the dataset:

    store.export_jsonl()   -> raw preference records
    store.to_dpo_pairs()   -> {"prompt", "chosen", "rejected"} pairs, the
                              standard input shape for DPO-style tuning.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class FeedbackRecord:
    ts: float
    kind: str            # 'approval_granted' | 'job_cancelled' | 'estop' |
                         # 'oversight_suspension' | 'human_rating'
    decision: str        # 'approve' | 'reject'
    actor: str           # the AGENT whose behaviour is being judged
    judge: str           # the HUMAN (or subsystem) making the judgement
    context: dict[str, Any] = field(default_factory=dict)
    comment: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class FeedbackStore:
    """Append-only JSONL preference store. Thread-safe; in-memory if no path."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else None
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._memory: list[FeedbackRecord] = []

    def record(self, kind: str, *, decision: str, actor: str, judge: str,
               context: dict[str, Any] | None = None,
               comment: str = "") -> FeedbackRecord:
        if decision not in ("approve", "reject"):
            raise ValueError("decision must be 'approve' or 'reject'")
        rec = FeedbackRecord(ts=round(time.time(), 3), kind=kind,
                             decision=decision, actor=actor, judge=judge,
                             context=dict(context or {}), comment=comment)
        with self._lock:
            if self.path:
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(rec.to_dict(), default=str,
                                        sort_keys=True) + "\n")
            else:
                self._memory.append(rec)
        return rec

    # -- reading / export ----------------------------------------------------

    def records(self) -> list[FeedbackRecord]:
        if not self.path:
            with self._lock:
                return list(self._memory)
        if not self.path.exists():
            return []
        out = []
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    out.append(FeedbackRecord(**json.loads(line)))
        return out

    def export_jsonl(self) -> str:
        return "\n".join(json.dumps(r.to_dict(), default=str, sort_keys=True)
                         for r in self.records())

    def to_dpo_pairs(self) -> list[dict[str, Any]]:
        """Preference pairs for DPO-style tuning.

        The prompt is the situation (tool + device + capability); ``chosen``
        is a human-endorsed action in that situation, ``rejected`` a
        human-rejected one. Pairing is by situation key, most recent first;
        unpaired records are omitted (they remain in the raw export).
        """
        def situation(r: FeedbackRecord) -> str:
            # The situation is the physical action addressed (device +
            # capability); the judged behaviours (tool, arguments, reason)
            # live in the actions being compared.
            c = r.context
            return json.dumps({
                "device": c.get("device", ""),
                "capability": c.get("capability", "")}, sort_keys=True)

        def action(r: FeedbackRecord) -> dict[str, Any]:
            return {"tool": r.context.get("tool", ""),
                    "arguments": r.context.get("arguments", {}),
                    "reason": r.context.get("reason", ""),
                    "actor": r.actor, "kind": r.kind}

        approved: dict[str, list[FeedbackRecord]] = {}
        rejected: dict[str, list[FeedbackRecord]] = {}
        for r in self.records():
            (approved if r.decision == "approve" else rejected) \
                .setdefault(situation(r), []).append(r)

        pairs: list[dict[str, Any]] = []
        for key in approved.keys() & rejected.keys():
            for good, bad in zip(reversed(approved[key]),
                                 reversed(rejected[key]), strict=False):
                pairs.append({"prompt": json.loads(key),
                              "chosen": action(good),
                              "rejected": action(bad)})
        return pairs

    def summary(self) -> dict[str, Any]:
        recs = self.records()
        by_kind: dict[str, int] = {}
        for r in recs:
            by_kind[r.kind] = by_kind.get(r.kind, 0) + 1
        return {"records": len(recs), "by_kind": by_kind,
                "dpo_pairs": len(self.to_dpo_pairs())}


__all__ = ["FeedbackStore", "FeedbackRecord"]
