"""Consistent memory: the lab remembers across restarts.

A control system that forgets everything at restart forces humans to
reconstruct state from the audit log by hand. ``LabMemory`` is the durable
store (SQLite, WAL, stdlib-only) that survives process death and answers,
at every startup: *where did I stop, and where was I going?*

It holds four kinds of continuity:

  - **Tasks** -- the directives humans record for the lab ("run the BCA
    standards on plate P1"), with status, priority, attached protocol
    documents, and per-task protocol checkpoints, so an interrupted run
    resumes at the step it reached instead of restarting.
  - **Incidents** -- what went wrong, on which device, and whether a human
    has resolved it yet.
  - **Oversight state** -- agent suspensions and device quarantines persist:
    a restart must never amnesty a suspended agent or un-quarantine a
    jammed instrument.
  - **Key-value memory** -- durable notes any component needs to keep.

The startup report (``LabMemory.startup_report()``) is printed by the
server on boot and available to agents through the ``lab_tasks`` tool, so
the first thing any agent learns after a restart is the unfinished work.
"""

from .store import LabMemory

__all__ = ["LabMemory"]
