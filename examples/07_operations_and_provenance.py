"""Industrial operations -- parallel runs, signed provenance, the watchdog.

Offline demo of the v1.4.0 layer:

  A. A two-instrument protocol runs with parallel=true: independent steps
     overlap across devices while same-device steps stay ordered.
  B. The run leaves a SIGNED RUN RECORD: protocol as executed, devices and
     driver versions, software stack, and the run's audit slice -- then we
     tamper with a copy and watch verification catch it.
  C. The heartbeat watchdog notices a dead instrument, marks it ERROR (so
     the state gate refuses new actuation), and clears it on recovery.

Run:  python examples/07_operations_and_provenance.py
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from labaiagent import LabSession
from labaiagent.drivers.simulated import (
    Labware,
    SimulatedLiquidHandler,
    SimulatedPlateReader,
    World,
)
from labaiagent.gateway.registry import GatewayContext, dispatch
from labaiagent.orchestration.watchdog import Watchdog
from labaiagent.provenance.records import RunRecordStore


def rule(title: str) -> None:
    print("\n" + "=" * 74 + f"\n{title}\n" + "=" * 74)


def main() -> int:
    state = Path(tempfile.mkdtemp(prefix="labaiagent_ops_"))
    world = World(seed=11)
    session = LabSession(
        [SimulatedLiquidHandler("lh", world=world),
         SimulatedPlateReader("reader", world=world)],
        name="ops-demo", audit_path=state / "audit.jsonl",
        audit_hmac_key=b"demo-key")
    session.connect_all()
    world.add_labware(Labware("P1", n_wells=96, location="deck_1"))
    for well in ("A1", "B1"):
        world.get("P1").well(well).add(150.0, {"analyte": 10.0})
    ctx = GatewayContext.for_session(session)
    ctx.records = RunRecordStore(state / "run_records", hmac_key=b"demo-key")

    # ------------------------------------------------------------------ A
    rule("A. Parallel protocol: two instruments, overlapping waves")
    proto = {"name": "ops", "steps": [
        {"name": "tips", "device": "lh", "capability": "read:tips_available"},
        {"name": "drawer", "device": "reader", "capability": "read:drawer_open"},
        {"name": "mix", "device": "lh", "capability": "proc:transfer",
         "args": {"source_barcode": "P1", "source_well": "A1",
                  "dest_barcode": "P1", "dest_well": "B1", "volume": 25.0},
         "depends_on": ["tips", "drawer"]}]}
    t0 = time.perf_counter()
    out = dispatch(ctx, "run_protocol", {"protocol": proto, "parallel": True})
    took = time.perf_counter() - t0
    print(f"  counts: {out['result']['summary']['counts']}  "
          f"({took*1000:.0f} ms, waves grouped by device)")

    # ------------------------------------------------------------------ B
    rule("B. The signed run record -- and tamper detection")
    rid = out["result"]["record_id"]
    ver = dispatch(ctx, "run_records", {"action": "verify", "record_id": rid})
    body = ctx.records.get(rid)["body"]
    print(f"  record {rid}: status={body['status']}, "
          f"labaiagent={body['software']['labaiagent']}, "
          f"devices={[d['id'] for d in body['devices']]}")
    print(f"  audit slice: {body['audit_slice']['n_records']} chained records")
    print(f"  verify -> {ver['result']['detail']}")
    path = ctx.records._path(rid)
    sealed = json.loads(path.read_text())
    sealed["body"]["actor"] = "user:mallory"       # the tamper
    path.write_text(json.dumps(sealed))
    ok, msg = ctx.records.verify(rid)
    print(f"  after tampering with the actor field -> valid={ok} ({msg})")

    # ------------------------------------------------------------------ C
    rule("C. Watchdog: heartbeat lost -> ERROR -> recovered")
    wd = Watchdog(session, interval_s=999, failures_to_trip=3)
    reader = session.get("reader")
    reader._self_test = lambda: False           # unplug the reader
    for _ in range(3):
        outcome = wd.check_once()["reader"]
    print(f"  after 3 failed heartbeats: {outcome}; "
          f"reader state = {reader.state.value}")
    reader._self_test = lambda: True            # plug it back in
    print(f"  next heartbeat: {wd.check_once()['reader']}; "
          f"reader state = {reader.state.value}")

    ok, msg = session.audit.verify()
    print(f"\n  audit chain: {msg}")
    session.disconnect_all()
    shutil.rmtree(state, ignore_errors=True)
    print("\nParallel where safe. Evidence you can verify. "
          "A lab that notices when something dies.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
