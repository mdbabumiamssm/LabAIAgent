"""Consistent memory and intelligent recovery -- the continuity demo.

Simulates the full lifecycle the lab actually lives:

  A. A human records tasks on the durable board (one carries a protocol).
  B. An agent executes the task; progress checkpoints to disk.
  C. The process "crashes" mid-mission (we drop every in-memory object and
     rebuild from the same files -- exactly what a restart does).
  D. On startup the lab REMEMBERS: which task was interrupted, that a
     checkpoint exists, which incidents are open, which devices are
     quarantined, which agents are suspended.
  E. The agent resumes the interrupted task from its checkpoint -- completed
     steps are not re-run.
  F. Something goes physically wrong (aspirating from an empty well): the
     system responds intelligently -- quarantines the instrument, opens an
     incident, and hands the agent a diagnosis built from the driver's own
     operating notes instead of a bare error.
  G. A human resolves the incident; the quarantine lifts; work continues.

Run:  python examples/06_memory_and_recovery.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from labaiagent import LabSession
from labaiagent.drivers.simulated import (
    Labware,
    SimulatedLiquidHandler,
    SimulatedPlateReader,
    World,
)
from labaiagent.gateway.auth import Principal, Role
from labaiagent.gateway.registry import GatewayContext, dispatch
from labaiagent.memory.store import LabMemory
from labaiagent.oversight.supervisor import Supervisor

AGENT = Principal(id="agent:claude", role=Role.OPERATOR)
DR_BABU = Principal(id="user:babu", kind="human", role=Role.APPROVER)


def rule(title: str) -> None:
    print("\n" + "=" * 74 + f"\n{title}\n" + "=" * 74)


def build_lab(state_dir: Path) -> GatewayContext:
    """Everything a restart rebuilds: session, memory, supervisor."""
    world = World(seed=33)
    session = LabSession(
        [SimulatedLiquidHandler("lh", world=world),
         SimulatedPlateReader("reader", world=world)],
        name="continuity-demo", audit_path=state_dir / "audit.jsonl")
    session.connect_all()
    world.add_labware(Labware("P1", n_wells=96, location="deck_1"))
    world.get("P1").well("A1").add(200.0, {"protein": 100.0})
    world.add_labware(Labware("DIL", n_wells=6, kind="reservoir",
                              location="deck_2"))
    world.get("DIL").well("A1").add(3000.0, {})
    ctx = GatewayContext.for_session(session)
    ctx.memory = LabMemory(state_dir / "state.db")
    ctx.supervisor = Supervisor(memory=ctx.memory)
    ctx.memory.log_startup(session.audit.session_id)
    return ctx


def main() -> int:
    state_dir = Path(tempfile.mkdtemp(prefix="labaiagent_demo_"))
    ctx = build_lab(state_dir)

    # ------------------------------------------------------------------ A
    rule("A. Human records the day's directives (they persist)")
    proto = {"name": "dilution", "steps": [
        {"name": "prep", "device": "lh", "capability": "read:tips_available"},
        {"name": "dilute", "device": "lh", "capability": "proc:serial_dilution",
         "args": {"barcode": "P1", "wells": ["A1", "B1", "C1", "D1"],
                  "transfer_volume": 50.0, "diluent_barcode": "DIL",
                  "diluent_well": "A1", "diluent_volume": 50.0},
         "depends_on": ["prep"]}]}
    t1 = dispatch(ctx, "lab_tasks",
                  {"action": "add", "title": "Serial dilution on P1",
                   "instructions": "Standards prep per SOP-041", "priority": 1,
                   "protocol": proto}, principal=DR_BABU)["result"]["task"]
    dispatch(ctx, "lab_tasks",
             {"action": "add", "title": "Read A562 after dilution",
              "priority": 2}, principal=DR_BABU)
    print(f"  recorded: {t1['id']} \"Serial dilution on P1\" + 1 more")

    # ------------------------------------------------------------------ B
    rule("B. Agent starts the task; progress checkpoints to disk")
    run = dispatch(ctx, "run_protocol", {"task_id": t1["id"]}, principal=AGENT)
    print(f"  run: {run['result']['summary']['counts']}, "
          f"checkpoint on disk: "
          f"{ctx.memory.checkpoint_path(t1['id']).exists()}")
    # Simulate the crash arriving before the task was marked settled:
    ctx.memory.update_task(t1["id"], status="in_progress",
                           note="power loss mid-settlement", by="demo")

    # ------------------------------------------------------------------ C+D
    rule("C+D. CRASH. Restart. The lab remembers where it stopped")
    del ctx
    ctx = build_lab(state_dir)          # fresh process, same files
    report = ctx.memory.startup_report()
    it = report["interrupted_tasks"][0]
    print(f"  interrupted: {it['id']} \"{it['title']}\" "
          f"(checkpoint: {it['has_checkpoint']})")
    print(f"  pending    : "
          f"{[t['title'] for t in report['pending_tasks']]}")

    # ------------------------------------------------------------------ E
    rule("E. Resume from the checkpoint: finished steps are NOT re-run")
    resumed = dispatch(ctx, "run_protocol",
                       {"task_id": it["id"], "resume": True}, principal=AGENT)
    print(f"  resumed_steps skipped: {resumed['result']['resumed_steps']} "
          f"| task now: {ctx.memory.get_task(it['id'])['status']}")

    # ------------------------------------------------------------------ F
    rule("F. Physical fault -> quarantine + incident + diagnosis")
    out = dispatch(ctx, "run_procedure",
                   {"device_id": "lh", "capability": "transfer",
                    "arguments": {"source_barcode": "P1", "source_well": "H12",
                                  "dest_barcode": "P1", "dest_well": "A2",
                                  "volume": 100.0},
                    "reason": "transfer from (empty) well H12"},
                   principal=AGENT)
    print(f"  error       : {out['error']} -> incident "
          f"{out['incident']['id']} opened")
    print(f"  first advice: {out['diagnosis']['recommended_actions'][0]}")
    blocked = dispatch(ctx, "write_state",
                       {"device_id": "lh", "capability": "flow_rate",
                        "arguments": {"value": 50}, "reason": "should block"},
                       principal=AGENT)
    print(f"  lh actuation now: [{blocked['error']}] (quarantined; reads and "
          f"other devices unaffected)")

    # Quarantine also survives a restart:
    del ctx
    ctx = build_lab(state_dir)
    print(f"  after ANOTHER restart, still quarantined: "
          f"{ctx.supervisor.is_quarantined('lh')}")

    # ------------------------------------------------------------------ G
    rule("G. Human resolves the incident; the lab goes back to work")
    inc = ctx.memory.list_incidents("open")[0]
    res = dispatch(ctx, "lab_tasks",
                   {"action": "resolve_incident", "incident_id": inc["id"],
                    "resolution": "Well map corrected; tips verified"},
                   principal=DR_BABU)
    print(f"  resolved by user:babu -> quarantine released: "
          f"{res['result']['quarantine_released']}")
    again = dispatch(ctx, "write_state",
                     {"device_id": "lh", "capability": "flow_rate",
                      "arguments": {"value": 60}, "reason": "back to work"},
                     principal=AGENT)
    print(f"  lh actuates again: {again['ok']}")

    ok, msg = ctx.session.audit.verify()
    print(f"\n  audit chain across all of it: {msg}")
    ctx.session.disconnect_all()
    shutil.rmtree(state_dir, ignore_errors=True)
    print("\nThe lab remembers. The lab recovers. Humans stay in charge.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
