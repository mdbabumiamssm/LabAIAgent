"""Run-record provenance.

A run record is the single, self-contained, integrity-protected artifact that
answers "what exactly happened in this run?" months later: the protocol as
executed (per-step arguments, results, errors, timings), the instruments it
ran on (down to driver versions), the software stack, the actor, and the
contiguous audit slice covering the run -- checksummed, and signed when the
lab has an HMAC key configured.

This is the reproducibility substrate for publication (methods sections cite
a record, not a memory) and the batch-record substrate for regulated work.
"""

from .records import RunRecordStore, build_run_record

__all__ = ["RunRecordStore", "build_run_record"]
