"""Integrating instrument N+1, start to finish.

This is the answer to "can instruments be added one after another?" -- written
as executable code rather than prose.

We add a Coulter-principle cell counter that nothing in the framework has ever
heard of. The complete integration is ~60 lines of driver. Afterwards it is
indistinguishable from the built-in instruments: it appears in the manifest,
its limits are enforced, its actions are audited, an agent can discover and
operate it over MCP, and it can participate in protocols alongside everything
else.

Nothing in labaiagent/ is modified. No registry is edited. No tool schema changes.

Run:  python examples/03_add_new_instrument.py
"""

from __future__ import annotations

import random
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from labaiagent import Device, LabSession, Param, Range, Risk, procedure, read, write
from labaiagent.core.conformance import verify_driver
from labaiagent.core.errors import PhysicalError
from labaiagent.core.registry import register_driver
from labaiagent.drivers.simulated import SimulatedLiquidHandler, World

# ==========================================================================
# STEP 1 -- write the driver.  This is the entire integration.
# ==========================================================================

@register_driver("acme.cellcounter")
class AcmeCellCounter(Device):
    """Coulter-principle automated cell counter with trypan blue viability."""

    vendor = "Acme"
    model = "CellCount-3000"
    category = "cell_counter"
    driver_version = "1.0.0"

    # Notes are not decoration. This is what an agent reads before it touches
    # the instrument, and it is the only place tacit bench knowledge -- the
    # things the vendor manual does not say -- actually gets written down.
    notes = """
    Coulter-principle counter, 10-60 um aperture, trypan blue viability.
    Linear range is 5e4 to 1e7 cells/mL. Above that, coincidence counting
    undercounts badly -- dilute rather than trusting the number.
    Trypan blue is cytotoxic: read within 5 minutes of staining or viability
    reads artificially low.
    The aperture clogs. If counts drop suddenly across consecutive samples,
    that is a clog, not biology. Run `flush` before concluding anything.
    """

    def __init__(self, device_id: str, *, world: World | None = None,
                 **kw: Any) -> None:
        self.world = world
        self._rng = random.Random(42)
        self._aperture_um = 30.0
        self._clog_index = 0.0        # 0 clean, 1 fully blocked
        self._counts_since_flush = 0
        super().__init__(device_id, simulated=True, **kw)

    # -- lifecycle: three methods -----------------------------------------

    def _connect(self) -> None:
        self._clog_index = 0.0

    def _disconnect(self) -> None:
        pass

    def _self_test(self) -> bool:
        return self._aperture_um > 0

    def _halt(self) -> None:
        # Anything that moves fluid must implement this, or the lab-wide
        # e-stop is a no-op on this instrument specifically.
        self._counts_since_flush = 0

    # -- capabilities ------------------------------------------------------

    @read("aperture", unit="um", description="Installed aperture diameter")
    def get_aperture(self) -> float:
        return self._aperture_um

    @read("clog_index", unit="percent",
          description="Estimated aperture obstruction, 0 clean to 100 blocked")
    def get_clog_index(self) -> float:
        return round(self._clog_index * 100.0, 1)

    @read("counts_since_flush", unit="count",
          description="Samples counted since the last flush")
    def get_counts_since_flush(self) -> int:
        return self._counts_since_flush

    @write("aperture", risk=Risk.MEDIUM, unit="um",
           description="Select the aperture. Must match the expected cell diameter.",
           params=[Param("value", float, "um", "Aperture diameter",
                         limits=Range(10.0, 60.0, "um"))])
    def set_aperture(self, value: float) -> float:
        self._aperture_um = value
        return value

    @procedure(
        "count",
        risk=Risk.MEDIUM,
        reversible=False,
        description="Aspirate a sample and return concentration and viability",
        params=[
            Param("barcode", str, description="Source labware barcode"),
            Param("well", str, description="Source well"),
            Param("volume", float, "uL", "Sample volume to aspirate",
                  default=50.0, limits=Range(20.0, 500.0, "uL")),
            Param("dilution_factor", float, "count", "Pre-dilution applied to the sample",
                  default=1.0, limits=Range(1.0, 1000.0)),
        ],
        consumes=("sample",),
        est_duration_s=45.0,
    )
    def count(self, barcode: str, well: str, volume: float = 50.0,
              dilution_factor: float = 1.0) -> dict[str, Any]:
        if self._clog_index > 0.8:
            raise PhysicalError(
                f"{self.id}: aperture is {self._clog_index * 100:.0f}% obstructed. "
                f"Counts would be meaningless. Run flush before continuing.")

        true_conc = 2.4e6 * self._rng.uniform(0.85, 1.15)
        measured = true_conc * (1.0 - self._clog_index) * dilution_factor
        viability = min(99.0, max(40.0, self._rng.gauss(92.0, 3.5)))

        self._counts_since_flush += 1
        self._clog_index = min(1.0, self._clog_index + 0.06)

        return {
            "barcode": barcode, "well": well,
            "concentration_cells_per_mL": round(measured, 0),
            "viability_percent": round(viability, 1),
            "aperture_um": self._aperture_um,
            "in_linear_range": 5e4 <= measured <= 1e7,
        }

    @procedure("flush", risk=Risk.LOW,
               description="Back-flush the aperture to clear obstruction",
               params=[Param("cycles", int, description="Flush cycles",
                             default=3, limits=Range(1, 10))],
               est_duration_s=90.0)
    def flush(self, cycles: int = 3) -> dict[str, Any]:
        before = self._clog_index
        self._clog_index = max(0.0, self._clog_index - 0.35 * cycles)
        self._counts_since_flush = 0
        return {"clog_before_percent": round(before * 100, 1),
                "clog_after_percent": round(self._clog_index * 100, 1)}


# ==========================================================================
# STEP 2 onwards -- everything below is the framework, not integration work
# ==========================================================================

def rule(title: str) -> None:
    print("\n" + "=" * 74)
    print(title)
    print("=" * 74)


def main() -> int:
    rule("STEP 1 -- driver written (60 lines above). Nothing else was edited.")

    # ---------------------------------------------------------------- step 2
    rule("STEP 2 -- conformance gate")
    counter = AcmeCellCounter("counter")
    report = verify_driver(counter)
    print(report.render())
    if not report.passed:
        print("Driver rejected. It does not go in a lab config until this passes.")
        return 1

    # ---------------------------------------------------------------- step 3
    rule("STEP 3 -- it is now discoverable, with no registry edit")
    from labaiagent.core.registry import list_drivers
    drivers = list_drivers()
    print(f"  {len(drivers)} drivers registered; the new one is among them:")
    d = drivers["acme.cellcounter"]
    print(f"    acme.cellcounter -> {d['class']} ({d['vendor']} {d['model']})")

    # ---------------------------------------------------------------- step 4
    rule("STEP 4 -- add it to a running lab alongside existing instruments")
    world = World(seed=11)
    session = LabSession(
        [SimulatedLiquidHandler("lh", world=world)],
        name="growing-lab", audit_path="runs/onboard_audit.jsonl",
        actor="user:bmia",
    )
    session.connect_all()
    print(f"  Lab starts with: {[d.id for d in session.devices()]}")

    session.add(counter)
    counter.connect()
    print(f"  After adding one instrument: {[d.id for d in session.devices()]}")
    print("\n  Equivalently, without any Python at all -- a stanza in lab.yaml:")
    print("      - id: counter")
    print("        driver: acme.cellcounter")
    print("        location: bench-B")

    # ---------------------------------------------------------------- step 5
    rule("STEP 5 -- the agent-facing reference generated itself")
    print(counter.reference_sheet())

    # ---------------------------------------------------------------- step 6
    rule("STEP 6 -- limits are enforced without the driver checking anything")
    from labaiagent.core.errors import SafetyViolation
    try:
        session.write("counter", "aperture", value=150.0)
    except SafetyViolation as exc:
        print(f"  Rejected: {exc}")
    print("\n  The driver's set_aperture() contains no validation code. The"
          "\n  Range on its Param did the work, and the same declaration"
          "\n  produced the manifest entry an agent reads.")

    # ---------------------------------------------------------------- step 7
    rule("STEP 7 -- it works over MCP with no new tools")
    from labaiagent.mcp.server import TOOLS, dispatch
    print(f"  Tool count before and after adding the instrument: {len(TOOLS)}")
    out = dispatch(session, "run_procedure", {
        "device_id": "counter", "capability": "count",
        "arguments": {"barcode": "SMP01", "well": "A1", "volume": 50.0},
        "reason": "viability check before sorting"})
    r = out["result"]["result"]
    print(f"  count -> {r['concentration_cells_per_mL']:.0f} cells/mL, "
          f"{r['viability_percent']}% viable, "
          f"in linear range: {r['in_linear_range']}")

    # ---------------------------------------------------------------- step 8
    rule("STEP 8 -- it participates in protocols like anything else")
    from labaiagent.orchestration.workflow import Protocol
    proto = Protocol("viability_screen",
                     description="Count four wells, flush between plates")
    for i, well in enumerate(["A1", "A2", "A3", "A4"], start=1):
        proto.step(f"count_{i}", "counter", "proc:count",
                   args={"barcode": "SMP01", "well": well, "volume": 50.0},
                   store_as=f"count_{i}",
                   depends_on=(f"count_{i - 1}",) if i > 1 else ())
    proto.step("flush", "counter", "proc:flush", args={"cycles": 3},
               depends_on=("count_4",))

    problems = proto.validate(session)
    print(f"  Static validation: {'passed' if not problems else problems}")
    proto.run(session, validate=False)
    print()
    print(proto.report())

    concs = [proto.context[f"count_{i}"]["concentration_cells_per_mL"]
             for i in range(1, 5)]
    print(f"\n  Concentrations across the run: "
          f"{', '.join(f'{c:.2e}' for c in concs)}")
    print("  Note the downward drift -- that is the simulated aperture clogging,")
    print("  which is exactly the artifact the driver's notes warn an agent about.")

    # ---------------------------------------------------------------- step 9
    rule("STEP 9 -- provenance, automatically")
    s = session.audit.summary()
    print(f"  records {s['records']}, events {s['by_event']}")
    print(f"  chain   {s['chain_status']}")
    print(f"  counter actions logged: {s['by_device'].get('counter', 0)}")

    session.disconnect_all()
    print("\n" + "=" * 74)
    print("Total integration cost for instrument N+1: one driver class.")
    print("Repeat per instrument. Nothing already working had to change.")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
