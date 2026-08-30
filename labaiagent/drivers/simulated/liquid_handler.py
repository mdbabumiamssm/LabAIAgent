"""Simulated 8-channel liquid handler."""

from __future__ import annotations

import math
import time
from typing import Any

from ...core.capability import procedure, read, write
from ...core.device import Device
from ...core.errors import PhysicalError
from ...core.registry import register_driver
from ...core.types import Param, Pattern, Range, Risk
from .world import World, default_world


@register_driver("sim.liquid_handler", "sim.lh")
class SimulatedLiquidHandler(Device):
    """8-channel air-displacement pipettor over a deck of positions.

    The error model is the point of this simulator. Pipetting CV is not
    constant: it rises steeply below ~10 uL, and it degrades for viscous
    liquids aspirated at speed. Protocols that look fine on paper and fail on
    the bench usually fail for exactly those two reasons, so the simulator
    reproduces them and a well-written protocol will trip over them here
    instead of on real sample.
    """

    vendor = "LabAIAgent"
    model = "SimLH-8"
    category = "liquid_handler"
    driver_version = "1.0.0"
    notes = """
    Eight independent channels, air displacement, 1000 uL tips.
    Aspirating below 5 uL is possible but CV exceeds 5% -- prefer an
    intermediate dilution instead of a sub-5 uL transfer.
    Viscous liquids (glycerol, BSA >5 mg/mL, DMSO) need flow_rate <= 25 uL/s or
    the channel under-delivers; the simulator penalises this the same way the
    real instrument does.
    Tips are consumed one per channel per transfer unless reuse_tips is set.
    Deck positions are named deck_1 .. deck_12.
    """

    def __init__(self, device_id: str, *, world: World | None = None, **kw: Any) -> None:
        self.world = world or default_world()
        self._flow_rate = 100.0
        self._current_tips = 0
        kw.setdefault("simulated", True)
        super().__init__(device_id, **kw)

    # -- lifecycle --------------------------------------------------------

    def _connect(self) -> None:
        time.sleep(0.01)
        for i in range(1, 13):
            self.world.define_position(f"deck_{i}")

    def _disconnect(self) -> None:
        self._current_tips = 0

    def _self_test(self) -> bool:
        return True

    def _halt(self) -> None:
        self._current_tips = 0
        self.world.log(f"{self.id}: HALT - channels parked, tips ejected")

    # -- reads ------------------------------------------------------------

    @read("tips_available", unit="count", description="Tips remaining on the carrier")
    def get_tips_available(self) -> int:
        return self.world.tips_available

    @read("flow_rate", unit="uL/s", description="Current aspirate/dispense flow rate")
    def get_flow_rate(self) -> float:
        return self._flow_rate

    @read("deck_layout", description="Which labware barcode sits at each deck position")
    def get_deck_layout(self) -> dict[str, str | None]:
        return {k: v for k, v in self.world.positions.items() if k.startswith("deck_")}

    @read("well_volume", unit="uL",
          description="Liquid volume currently in one well (simulator ground truth)",
          params=[Param("barcode", str, description="Plate barcode"),
                  Param("well", str, description="Well ID e.g. A1",
                        limits=Pattern(r"[A-P](?:[1-9]|1[0-9]|2[0-4])"))])
    def get_well_volume(self, barcode: str, well: str) -> float:
        return round(self.world.get(barcode).well(well).volume_uL, 3)

    # -- writes -----------------------------------------------------------

    @write("flow_rate", risk=Risk.LOW, unit="uL/s",
           description="Set aspirate/dispense flow rate for subsequent transfers",
           params=[Param("value", float, "uL/s", "Flow rate",
                         limits=Range(1.0, 300.0, "uL/s"))])
    def set_flow_rate(self, value: float) -> float:
        self._flow_rate = value
        return value

    # -- procedures -------------------------------------------------------

    @procedure(
        "transfer",
        risk=Risk.MEDIUM,
        description="Aspirate from a source well and dispense into a destination well",
        params=[
            Param("source_barcode", str, description="Source labware barcode"),
            Param("source_well", str, description="Source well",
                  limits=Pattern(r"[A-P](?:[1-9]|1[0-9]|2[0-4])")),
            Param("dest_barcode", str, description="Destination labware barcode"),
            Param("dest_well", str, description="Destination well",
                  limits=Pattern(r"[A-P](?:[1-9]|1[0-9]|2[0-4])")),
            Param("volume", float, "uL", "Volume to transfer",
                  limits=Range(0.5, 1000.0, "uL")),
            Param("new_tip", bool, description="Use a fresh tip", default=True),
            Param("mix_cycles", int, description="Post-dispense mix cycles",
                  default=0, limits=Range(0, 20)),
        ],
        consumes=("tip", "sample"),
        reversible=False,
        est_duration_s=4.0,
    )
    def transfer(self, source_barcode: str, source_well: str, dest_barcode: str,
                 dest_well: str, volume: float, new_tip: bool = True,
                 mix_cycles: int = 0) -> dict[str, Any]:
        w = self.world
        src = w.get(source_barcode)
        dst = w.get(dest_barcode)
        src_w = src.well(source_well)
        dst_w = dst.well(dest_well)

        if new_tip:
            if w.maybe_fault("tip_pickup_fail"):
                raise PhysicalError(
                    f"{self.id}: tip pickup failed at the tip carrier. The channel "
                    f"is now unarmed. Check tip seating and re-run this step."
                )
            w.consume_tips(1)
            self._current_tips = 1

        if src_w.volume_uL < volume:
            raise PhysicalError(
                f"{self.id}: liquid-level detection reports {src_w.volume_uL:.1f} uL "
                f"in {source_barcode}:{source_well}, need {volume:.1f} uL. "
                f"Aborting rather than aspirating air."
            )
        if dst_w.volume_uL + volume > dst.max_volume_uL:
            raise PhysicalError(
                f"{self.id}: {dest_barcode}:{dest_well} would overflow "
                f"({dst_w.volume_uL + volume:.1f} uL > {dst.max_volume_uL:.0f} uL max)."
            )

        delivered = self._delivered_volume(volume, src_w)
        species = src_w.remove(delivered)
        dst_w.add(delivered, species)
        if mix_cycles:
            time.sleep(0.001 * mix_cycles)

        w.log(f"{self.id}: {volume:.1f} uL {source_barcode}:{source_well} -> "
              f"{dest_barcode}:{dest_well} (delivered {delivered:.2f})")
        return {
            "requested_uL": volume,
            "delivered_uL": round(delivered, 3),
            "error_uL": round(delivered - volume, 3),
            "relative_error": round((delivered - volume) / volume, 5),
            "tip_used": bool(new_tip),
        }

    @procedure(
        "distribute",
        risk=Risk.MEDIUM,
        description="Dispense the same volume from one source into many destination wells",
        params=[
            Param("source_barcode", str),
            Param("source_well", str, limits=Pattern(r"[A-P](?:[1-9]|1[0-9]|2[0-4])")),
            Param("dest_barcode", str),
            Param("dest_wells", list, description="List of destination well IDs"),
            Param("volume", float, "uL", limits=Range(0.5, 1000.0, "uL")),
            Param("reuse_tip", bool, default=True,
                  description="Keep one tip for the whole distribution"),
        ],
        consumes=("tip", "reagent"),
        reversible=False,
        est_duration_s=1.5,
    )
    def distribute(self, source_barcode: str, source_well: str, dest_barcode: str,
                   dest_wells: list, volume: float, reuse_tip: bool = True) -> dict[str, Any]:
        results = []
        for i, dw in enumerate(dest_wells):
            r = self.transfer(source_barcode, source_well, dest_barcode, str(dw),
                              volume, new_tip=(not reuse_tip) or i == 0)
            results.append(r)
        total = sum(r["delivered_uL"] for r in results)
        errs = [r["relative_error"] for r in results]
        return {
            "wells": len(results),
            "total_delivered_uL": round(total, 2),
            "mean_relative_error": round(sum(errs) / len(errs), 5) if errs else 0.0,
            "max_abs_relative_error": round(max(abs(e) for e in errs), 5) if errs else 0.0,
        }

    @procedure(
        "serial_dilution",
        risk=Risk.MEDIUM,
        description="Build a serial dilution series across a row or column",
        params=[
            Param("barcode", str),
            Param("wells", list, description="Ordered well IDs, most concentrated first"),
            Param("transfer_volume", float, "uL", limits=Range(1.0, 500.0, "uL")),
            Param("diluent_barcode", str),
            Param("diluent_well", str, limits=Pattern(r"[A-P](?:[1-9]|1[0-9]|2[0-4])")),
            Param("diluent_volume", float, "uL", limits=Range(1.0, 500.0, "uL")),
        ],
        consumes=("tip", "sample", "reagent"),
        reversible=False,
        est_duration_s=30.0,
    )
    def serial_dilution(self, barcode: str, wells: list, transfer_volume: float,
                        diluent_barcode: str, diluent_well: str,
                        diluent_volume: float) -> dict[str, Any]:
        # Pre-fill every well after the first with diluent.
        self.distribute(diluent_barcode, diluent_well, barcode,
                        [str(w) for w in wells[1:]], diluent_volume, reuse_tip=True)
        steps = []
        for i in range(len(wells) - 1):
            r = self.transfer(barcode, str(wells[i]), barcode, str(wells[i + 1]),
                              transfer_volume, new_tip=True, mix_cycles=3)
            steps.append(r)
        plate = self.world.get(barcode)
        return {
            "series_length": len(wells),
            "nominal_fold": round((transfer_volume + diluent_volume) / transfer_volume, 3),
            "final_volumes_uL": [round(plate.well(str(w)).volume_uL, 2) for w in wells],
            "steps": steps,
        }

    # -- internals --------------------------------------------------------

    def _delivered_volume(self, requested: float, src_well) -> float:
        """Pipetting error: volume-dependent CV plus a viscosity/speed penalty."""
        rng = self.world.rng
        # CV floor ~0.6% at large volumes, rising sharply below 10 uL.
        cv = 0.006 + 0.045 * math.exp(-requested / 6.0)
        # Viscosity proxy: total dissolved species per unit volume.
        conc = (sum(src_well.species.values()) / max(src_well.volume_uL, 1e-9)) * 1000.0
        viscous = conc > 4.0
        bias = 0.0
        if viscous and self._flow_rate > 25.0:
            # Under-delivery grows with flow rate above the viscous threshold.
            bias = -0.012 * (self._flow_rate - 25.0) / 25.0
            cv *= 1.8
        delivered = requested * (1.0 + bias + rng.gauss(0.0, cv))
        return max(0.0, min(delivered, src_well.volume_uL))


__all__ = ["SimulatedLiquidHandler"]
