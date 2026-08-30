"""qPCR standard curve, plus a demonstration of every safety layer firing.

Part 1 runs a real analysis: a 6-point 10-fold standard curve, Ct calling,
and amplification efficiency from the slope -- the calculation any qPCR run is
judged on.

Part 2 is the more important half. It deliberately attempts eight unsafe or
malformed operations and shows each one being refused, because a control layer
that has never been shown failing has not been shown at all.

Run:  python examples/02_qpcr_and_safety.py
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from labaiagent import LabSession, Risk
from labaiagent.core.errors import (
    CapabilityNotFound,
    SafetyViolation,
)
from labaiagent.drivers.simulated import (
    Labware,
    SimulatedCentrifuge,
    SimulatedLiquidHandler,
    SimulatedRobotArm,
    SimulatedThermocycler,
    World,
)

STANDARD_WELLS = ["A1", "A2", "A3", "A4", "A5", "A6"]
STARTING_COPIES = [1e6, 1e5, 1e4, 1e3, 1e2, 1e1]
NTC_WELL = "A7"


def build_lab() -> tuple[LabSession, World]:
    world = World(seed=606)
    session = LabSession(
        [
            SimulatedLiquidHandler("lh", world=world),
            SimulatedThermocycler("cycler", world=world),
            SimulatedRobotArm("arm", world=world),
            SimulatedCentrifuge("spinner", world=world),
        ],
        name="qpcr-demo",
        audit_path="runs/qpcr_audit.jsonl",
        actor="user:bmia",
        autonomy_ceiling=Risk.MEDIUM,
    )
    session.connect_all()
    return session, world


def stage_plate(world: World) -> Labware:
    plate = Labware("QPCR01", n_wells=96, kind="plate", location="cycler_block")
    world.add_labware(plate)
    for well, copies in zip(STANDARD_WELLS, STARTING_COPIES, strict=True):
        plate.well(well).add(20.0, {"template": copies, "mastermix": 10.0})
    plate.well(NTC_WELL).add(20.0, {"mastermix": 10.0})   # no-template control
    return plate


# --------------------------------------------------------------------------
# Part 1 -- the analysis
# --------------------------------------------------------------------------

def linear_fit(x: list[float], y: list[float]) -> tuple[float, float, float]:
    n = len(x)
    mx, my = sum(x) / n, sum(y) / n
    sxx = sum((xi - mx) ** 2 for xi in x)
    sxy = sum((xi - mx) * (yi - my) for xi, yi in zip(x, y, strict=True))
    slope = sxy / sxx if sxx else 0.0
    intercept = my - slope * mx
    ss_res = sum((yi - (slope * xi + intercept)) ** 2 for xi, yi in zip(x, y, strict=True))
    ss_tot = sum((yi - my) ** 2 for yi in y)
    return slope, intercept, (1.0 - ss_res / ss_tot if ss_tot else 0.0)


def run_qpcr(session: LabSession) -> dict:
    print("=" * 74)
    print("PART 1 -- qPCR standard curve")
    print("=" * 74)

    token = session.request_approval(
        "cycler", "proc:run_qpcr", operator="bmia",
        reason="6-point standard curve for JAK2 V617F assay validation")
    print("\n  Approval token minted, scoped to cycler.run_qpcr, single use.")

    session.write("cycler", "lid_closed", closed=True)
    result = session.run("cycler", "run_qpcr", barcode="QPCR01", cycles=40,
                         anneal_temp=60.0, target="template",
                         approval=token, reason="standard curve")

    cts = result["ct"]
    logs, obs = [], []
    print(f"\n  {'WELL':<6} {'COPIES':>10} {'log10':>8} {'Ct':>8}")
    for well, copies in zip(STANDARD_WELLS, STARTING_COPIES, strict=True):
        ct = cts.get(well)
        print(f"  {well:<6} {copies:>10.0e} {math.log10(copies):>8.2f} "
              f"{ct if ct else 'undet':>8}")
        if ct:
            logs.append(math.log10(copies))
            obs.append(ct)
    ntc = cts.get(NTC_WELL)
    print(f"  {NTC_WELL:<6} {'NTC':>10} {'--':>8} {ntc if ntc else 'undet':>8}")

    slope, intercept, r2 = linear_fit(logs, obs)
    efficiency = (10 ** (-1.0 / slope) - 1.0) * 100.0 if slope else 0.0

    print(f"\n  Ct = {slope:.4f} x log10(copies) + {intercept:.3f}")
    print(f"  R^2         : {r2:.5f}")
    print(f"  Slope       : {slope:.4f}   (ideal -3.32)")
    print(f"  Efficiency  : {efficiency:.1f}%  (acceptable 90-110%)")
    print(f"  NTC         : {'clean' if not ntc else 'CONTAMINATED'}")

    ok = (r2 >= 0.99 and 90 <= efficiency <= 110 and not ntc)
    print(f"  QC verdict  : {'PASS' if ok else 'REVIEW'}")
    return {"r2": r2, "slope": slope, "efficiency": efficiency, "pass": ok}


# --------------------------------------------------------------------------
# Part 2 -- every safety layer, deliberately tripped
# --------------------------------------------------------------------------

def demonstrate_safety(session: LabSession, world: World) -> None:
    print("\n" + "=" * 74)
    print("PART 2 -- safety layers, deliberately tripped")
    print("=" * 74)
    print("\nEach line below is an operation that WAS REFUSED, and why.\n")

    def attempt(label: str, fn) -> None:
        try:
            fn()
            print(f"  [NOT BLOCKED] {label}  <-- this is a bug")
        except SafetyViolation as exc:
            print(f"  [BLOCKED] {label}")
            print(f"            {type(exc).__name__}: {str(exc)[:150]}")
        except CapabilityNotFound as exc:
            print(f"  [BLOCKED] {label}")
            print(f"            CapabilityNotFound: {str(exc)[:150]}")
        except Exception as exc:
            print(f"  [BLOCKED] {label}")
            print(f"            {type(exc).__name__}: {str(exc)[:150]}")

    # 1. Declared parameter range.
    attempt("arm speed set to 500% (declared max 100%)",
            lambda: session.write("arm", "speed", value=500.0))

    # 2. Pattern limit on a well ID.
    attempt("transfer to well 'Z99' (not a valid 96-well address)",
            lambda: session.run("lh", "transfer", source_barcode="QPCR01",
                                source_well="A1", dest_barcode="QPCR01",
                                dest_well="Z99", volume=10.0))

    # 3. Missing required argument.
    attempt("transfer with no volume argument",
            lambda: session.run("lh", "transfer", source_barcode="QPCR01",
                                source_well="A1", dest_barcode="QPCR01",
                                dest_well="B1"))

    # 4. Risk ceiling -- high-risk action with no approval token.
    attempt("plate move with no human approval (risk=high, ceiling=medium)",
            lambda: session.run("arm", "move_labware", barcode="QPCR01",
                                destination="hotel_1"))

    # 5. Interlock -- destination already occupied.
    world.add_labware(Labware("BLOCKER", n_wells=96, location="hotel_2"))
    tok = session.request_approval("arm", "proc:move_labware", operator="bmia",
                                   reason="safety demo")
    attempt("plate move into an occupied position (interlock: destination_free)",
            lambda: session.run("arm", "move_labware", barcode="QPCR01",
                                destination="hotel_2", approval=tok))

    # 6. Approval token replay -- scoped to a different capability.
    tok2 = session.request_approval("arm", "proc:move_labware", operator="bmia",
                                    reason="safety demo")
    attempt("reusing an arm-move token to authorise a centrifuge spin",
            lambda: session.run("spinner", "spin", rcf=1000.0, duration=60.0,
                                approval=tok2))

    # 7. Interlock -- unbalanced centrifuge, the one action that can injure.
    session.run("spinner", "load_bucket", bucket=1, barcode="QPCR01")
    tok3 = session.request_approval("spinner", "proc:spin", operator="bmia",
                                    reason="safety demo")
    attempt("spinning an unbalanced rotor (interlock: centrifuge_balanced)",
            lambda: session.run("spinner", "spin", rcf=1000.0, duration=60.0,
                                approval=tok3))

    # 8. Rate limit.
    session.safety.set_rate_limit("lh.flow_rate", max_calls=3, window_s=60)
    for _ in range(3):
        session.write("lh", "flow_rate", value=50.0)
    attempt("4th flow-rate change inside a 3-per-minute rate limit",
            lambda: session.write("lh", "flow_rate", value=50.0))

    # 9. Emergency stop latches everything.
    print("\n  --- emergency stop ---")
    stop = session.emergency_stop("safety demonstration")
    print(f"  E-STOP latched. Device states: {stop['devices']}")
    attempt("any actuation while the e-stop is latched",
            lambda: session.write("lh", "flow_rate", value=10.0))
    print("  Reads remain permitted while stopped (observing is always safe):")
    print(f"     cycler block temperature = "
          f"{session.read('cycler', 'block_temperature')} degC")

    session.reset_emergency_stop(operator="bmia", reason="demonstration complete")
    print("  E-stop cleared by a named human operator (an agent cannot clear it).")


def demonstrate_audit(session: LabSession) -> None:
    print("\n" + "=" * 74)
    print("PART 3 -- audit trail integrity")
    print("=" * 74)

    s = session.audit.summary()
    print(f"\n  records        : {s['records']}")
    print(f"  events         : {s['by_event']}")
    print(f"  chain verified : {s['chain_valid']} -- {s['chain_status']}")

    path = Path("runs/qpcr_audit.jsonl")
    lines = path.read_text(encoding="utf-8").splitlines()
    print("\n  Now tampering with the log: editing record 5 in place...")
    import json
    rec = json.loads(lines[4])
    rec["arguments"] = {"value": 999999}
    lines[4] = json.dumps(rec, sort_keys=True, separators=(",", ":"))
    tampered = path.with_suffix(".tampered.jsonl")
    tampered.write_text("\n".join(lines) + "\n", encoding="utf-8")

    from labaiagent.core.audit import AuditLog
    ok, msg = AuditLog(tampered).verify()
    print(f"  verification   : {'VALID' if ok else 'INVALID'} -- {msg}")
    print("\n  The edit is detected without needing a copy of the original: each")
    print("  record embeds the hash of its predecessor, so changing any record")
    print("  breaks every link after it.")


def main() -> int:
    session, world = build_lab()
    stage_plate(world)
    run_qpcr(session)
    demonstrate_safety(session, world)
    demonstrate_audit(session)
    session.disconnect_all()
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
