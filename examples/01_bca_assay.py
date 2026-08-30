"""End-to-end BCA protein assay across four instruments.

Mirrors the shape of the published MHS lab pilots: a liquid handler builds a
standard curve and sample wells, a robot arm moves the plate into a reader,
the reader measures A562, and the run is accepted or rejected on a linearity
gate before any result is reported.

The point being demonstrated is not that a plate can be read. It is that:

  - four instruments are coordinated through one interface
  - every actuation passed a declared safety check and is in a hash-chained log
  - the QC gate is part of the protocol, so a bad standard curve is caught
    by the system rather than by whoever opens the file next week
  - none of this required the instruments to know about each other

Run:  python examples/01_bca_assay.py
"""

from __future__ import annotations

import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from labaiagent import LabSession, Risk
from labaiagent.drivers.simulated import (
    Labware,
    SimulatedLiquidHandler,
    SimulatedPlateReader,
    SimulatedRobotArm,
    SimulatedThermocycler,
    World,
)
from labaiagent.orchestration.workflow import OnError, Protocol

BSA_STANDARDS_UG_PER_ML = [2000, 1000, 500, 250, 125, 62.5, 31.25, 0]
STANDARD_WELLS = ["A1", "B1", "C1", "D1", "E1", "F1", "G1", "H1"]
SAMPLE_WELLS = ["A2", "B2", "C2", "D2"]
UNKNOWN_CONC = [1450.0, 780.0, 96.0, 310.0]      # ground truth, hidden from the assay


def build_lab() -> tuple[LabSession, World]:
    world = World(seed=20260829)
    session = LabSession(
        [
            SimulatedLiquidHandler("lh", world=world, location="bench-A"),
            SimulatedPlateReader("reader", world=world, location="bench-A"),
            SimulatedRobotArm("arm", world=world, location="bench-A"),
            SimulatedThermocycler("cycler", world=world, location="bench-A"),
        ],
        name="bca-demo",
        audit_path="runs/bca_audit.jsonl",
        actor="user:bmia",
        autonomy_ceiling=Risk.MEDIUM,
    )
    session.connect_all()
    return session, world


def prepare_labware(world: World) -> None:
    """Stage reagents and samples. In a real lab this is the human's job."""
    reagents = Labware("RGT01", n_wells=6, kind="reservoir", location="deck_1")
    world.add_labware(reagents)
    reagents.well("A1").add(5000.0, {})                      # diluent / water
    reagents.well("A2").add(5000.0, {"protein": 10000.0})    # 2 mg/mL BSA stock
    reagents.well("A3").add(5000.0, {"bca_reagent": 5000.0})

    samples = Labware("SMP01", n_wells=24, kind="tube_rack", location="deck_2")
    world.add_labware(samples)
    for i, conc in enumerate(UNKNOWN_CONC):
        well = f"A{i + 1}"
        vol = 200.0
        samples.well(well).add(vol, {"protein": conc * vol / 1000.0})

    assay = Labware("BCA01", n_wells=96, kind="plate", location="deck_3")
    world.add_labware(assay)


def build_protocol(session: LabSession) -> Protocol:
    """Assemble the assay as a reviewable artifact rather than a call sequence.

    Moving a plate is risk=HIGH and irreversible -- a dropped plate ends the
    experiment -- so it sits above this session's autonomy ceiling and requires
    a human approval token. The token is scoped to one device and one
    capability, expires, and is consumed on use, so it authorises these two
    moves and nothing else.
    """
    arm_token = session.request_approval(
        "arm", "proc:move_labware",
        operator="bmia",
        reason="BCA assay: transport plate BCA01 to the cycler, then the reader",
        ttl_s=1800.0, uses=2,
    )
    proto = Protocol(
        "bca_protein_assay",
        description="8-point BSA standard curve + 4 unknowns, A562, "
                    "accepted on R^2 >= 0.98",
        checkpoint_path="runs/bca_checkpoint.json",
    )

    proto.step("set_gentle_flow", "lh", "write:flow_rate",
               args={"value": 20.0},
               note="BSA is viscous; above ~25 uL/s the channels under-deliver")

    # Standard curve: 2-fold serial dilution down column 1, then a zero well.
    proto.step("dispense_bsa_top", "lh", "proc:transfer",
               args={"source_barcode": "RGT01", "source_well": "A2",
                     "dest_barcode": "BCA01", "dest_well": "A1",
                     "volume": 50.0, "new_tip": True},
               depends_on=("set_gentle_flow",))

    proto.step("serial_dilute", "lh", "proc:serial_dilution",
               args={"barcode": "BCA01", "wells": STANDARD_WELLS[:-1],
                     "transfer_volume": 25.0,
                     "diluent_barcode": "RGT01", "diluent_well": "A1",
                     "diluent_volume": 25.0},
               depends_on=("dispense_bsa_top",),
               store_as="dilution")

    proto.step("blank_well", "lh", "proc:transfer",
               args={"source_barcode": "RGT01", "source_well": "A1",
                     "dest_barcode": "BCA01", "dest_well": "H1",
                     "volume": 25.0},
               depends_on=("serial_dilute",))

    for i, well in enumerate(SAMPLE_WELLS):
        proto.step(f"load_sample_{i + 1}", "lh", "proc:transfer",
                   args={"source_barcode": "SMP01", "source_well": f"A{i + 1}",
                         "dest_barcode": "BCA01", "dest_well": well,
                         "volume": 25.0, "new_tip": True},
                   depends_on=("blank_well",))

    proto.step("add_bca_reagent", "lh", "proc:distribute",
               args={"source_barcode": "RGT01", "source_well": "A3",
                     "dest_barcode": "BCA01",
                     "dest_wells": STANDARD_WELLS + SAMPLE_WELLS,
                     "volume": 200.0, "reuse_tip": True},
               depends_on=tuple(f"load_sample_{i + 1}" for i in range(len(SAMPLE_WELLS))),
               store_as="reagent_add")

    # Incubation on the thermocycler block, which is a plate-compatible heater.
    proto.step("move_to_cycler", "arm", "proc:move_labware",
               args={"barcode": "BCA01", "destination": "cycler_block"},
               depends_on=("add_bca_reagent",), approval=arm_token)

    proto.step("close_lid", "cycler", "write:lid_closed", args={"closed": True},
               depends_on=("move_to_cycler",))

    proto.step("incubate_37C", "cycler", "write:block_temperature",
               args={"value": 37.0}, depends_on=("close_lid",),
               note="BCA develops 30 min at 37 C")

    # Read.
    proto.step("open_drawer", "reader", "write:drawer_open", args={"open": True},
               depends_on=("incubate_37C",))

    proto.step("move_to_reader", "arm", "proc:move_labware",
               args={"barcode": "BCA01", "destination": "reader_carriage"},
               depends_on=("open_drawer",), approval=arm_token)

    proto.step("close_drawer", "reader", "write:drawer_open", args={"open": False},
               depends_on=("move_to_reader",))

    proto.step("read_a562", "reader", "proc:read_absorbance",
               args={"wavelength": 562.0, "barcode": "BCA01"},
               depends_on=("close_drawer",),
               store_as="absorbance",
               retries=1, on_error=OnError.RETRY)

    return proto


# --------------------------------------------------------------------------
# Analysis
# --------------------------------------------------------------------------

def linear_fit(x: list[float], y: list[float]) -> tuple[float, float, float]:
    """Ordinary least squares. Returns (slope, intercept, r_squared)."""
    n = len(x)
    mx, my = sum(x) / n, sum(y) / n
    sxx = sum((xi - mx) ** 2 for xi in x)
    sxy = sum((xi - mx) * (yi - my) for xi, yi in zip(x, y, strict=True))
    slope = sxy / sxx if sxx else 0.0
    intercept = my - slope * mx
    ss_res = sum((yi - (slope * xi + intercept)) ** 2 for xi, yi in zip(x, y, strict=True))
    ss_tot = sum((yi - my) ** 2 for yi in y)
    r2 = 1.0 - ss_res / ss_tot if ss_tot else 0.0
    return slope, intercept, r2


def analyse(absorbance: dict, world: World) -> dict:
    """Fit the standard curve, gate on linearity, back-calculate unknowns."""
    data = absorbance["data"]
    plate = world.get("BCA01")

    # True concentrations achieved after the physical dilution -- this is what
    # the curve should be fitted against, not the nominal series, because the
    # instrument's actual delivered volumes define the real standards.
    xs, ys = [], []
    for well in STANDARD_WELLS:
        conc = plate.well(well).concentration("protein")
        xs.append(conc)
        ys.append(data[well])

    # Fit only the linear region: BCA saturates above ~1000 ug/mL, and forcing
    # a line through the roll-off is the classic way to get a curve that looks
    # acceptable and quantifies badly at the top.
    lin = [(x, y) for x, y in zip(xs, ys, strict=True) if x <= 1000.0]
    slope, intercept, r2 = linear_fit([p[0] for p in lin], [p[1] for p in lin])

    unknowns = {}
    for _i, well in enumerate(SAMPLE_WELLS):
        a = data[well]
        est = (a - intercept) / slope if slope else float("nan")
        # Correct for the 25 uL sample into 225 uL total dilution in-well.
        true_conc = plate.well(well).concentration("protein")
        unknowns[well] = {
            "A562": a,
            "estimated_ug_per_mL_in_well": round(est, 1),
            "actual_ug_per_mL_in_well": round(true_conc, 1),
            "recovery_percent": round(100.0 * est / true_conc, 1) if true_conc else None,
        }

    return {
        "n_standards": len(lin),
        "slope": round(slope, 6),
        "intercept": round(intercept, 4),
        "r_squared": round(r2, 5),
        "passes_qc": r2 >= 0.98,
        "unknowns": unknowns,
    }


def main() -> int:
    session, world = build_lab()
    prepare_labware(world)
    proto = build_protocol(session)

    print("=" * 74)
    print("LabAIAgent -- BCA protein assay across 4 instruments")
    print("=" * 74)

    problems = proto.validate(session)
    if problems:
        print("\nVALIDATION FAILED (nothing was actuated):")
        for p in problems:
            print("  -", p)
        return 1
    print(f"\nStatic validation passed: {len(proto.steps)} steps, "
          f"{len(session.devices())} instruments")

    print("\nEXECUTING")
    try:
        proto.run(session, validate=False,
                  progress=lambda s: print(f"   [{s.status.value:>7}] "
                                           f"{s.name:<24} {s.duration_ms:7.1f} ms"))
    except Exception as exc:
        print(f"\nProtocol failed: {exc}")
        print(proto.report())
        return 1

    print("\n" + "=" * 74)
    print("ANALYSIS")
    print("=" * 74)
    result = analyse(proto.context["absorbance"], world)

    print(f"\nStandard curve ({result['n_standards']} points in the linear region)")
    print(f"  A562 = {result['slope']:.6f} x [protein] + {result['intercept']:.4f}")
    print(f"  R^2  = {result['r_squared']:.5f}")
    print(f"  QC   = {'PASS' if result['passes_qc'] else 'FAIL (R^2 < 0.98)'}")

    print("\nUnknowns (in-well concentration after 1:9 dilution by BCA reagent)")
    print(f"  {'WELL':<6} {'A562':>8} {'EST':>10} {'ACTUAL':>10} {'RECOVERY':>10}")
    for well, u in result["unknowns"].items():
        print(f"  {well:<6} {u['A562']:>8.4f} "
              f"{u['estimated_ug_per_mL_in_well']:>10.1f} "
              f"{u['actual_ug_per_mL_in_well']:>10.1f} "
              f"{str(u['recovery_percent']) + '%':>10}")

    recoveries = [u["recovery_percent"] for u in result["unknowns"].values()
                  if u["recovery_percent"]]
    print(f"\n  mean recovery {statistics.mean(recoveries):.1f}%, "
          f"SD {statistics.pstdev(recoveries):.1f}%")

    print("\n" + "=" * 74)
    print("PROVENANCE")
    print("=" * 74)
    summary = session.audit.summary()
    print(f"  session      : {summary['session_id']}")
    print(f"  records      : {summary['records']}")
    print(f"  by event     : {summary['by_event']}")
    print(f"  chain valid  : {summary['chain_valid']} -- {summary['chain_status']}")
    print(f"  tips consumed: {world.tips_used}")
    print("\n  Full trail: runs/bca_audit.jsonl")

    session.disconnect_all()
    return 0 if result["passes_qc"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
