"""Simulated plate reader, qPCR thermocycler, robot arm, incubator, centrifuge.

All bind to the shared ``World``, so a plate the arm moved is genuinely
somewhere else, and a plate the handler filled genuinely reads out.
"""

from __future__ import annotations

import math
from typing import Any

from ...core.capability import procedure, read, write
from ...core.device import Device
from ...core.errors import PhysicalError
from ...core.registry import register_driver
from ...core.types import Param, Range, Risk
from .world import World, bca_absorbance, compute_ct, default_world, qpcr_curve

# ==========================================================================
# Plate reader
# ==========================================================================

@register_driver("sim.plate_reader", "sim.reader")
class SimulatedPlateReader(Device):
    """Monochromator absorbance / fluorescence microplate reader."""

    vendor = "LabAIAgent"
    model = "SimRead-M"
    category = "plate_reader"
    driver_version = "1.0.0"
    notes = """
    Absorbance 230-1000 nm, fluorescence with selectable ex/em.
    The read carriage must be empty before the robot arm places a plate --
    call eject() first or the arm will collide with the drawer.
    Path length is ~0.55 cm for 200 uL in a standard 96-well flat-bottom
    plate; absorbance values are NOT normalised to 1 cm.
    A562 is the standard BCA wavelength.
    """

    def __init__(self, device_id: str, *, world: World | None = None, **kw: Any) -> None:
        self.world = world or default_world()
        self._drawer_open = False
        self._reads = 0
        kw.setdefault("simulated", True)
        super().__init__(device_id, **kw)

    def _connect(self) -> None:
        self.world.define_position(f"{self.id}_carriage")

    def _disconnect(self) -> None:
        self._drawer_open = False

    def _halt(self) -> None:
        self.world.log(f"{self.id}: HALT - lamp off, carriage stopped")

    @read("drawer_open", description="Is the plate carriage extended?")
    def get_drawer_open(self) -> bool:
        return self._drawer_open

    @read("carriage_occupied", description="Is a plate currently on the carriage?")
    def get_carriage_occupied(self) -> bool:
        return self.world.at(f"{self.id}_carriage") is not None

    @read("read_count", unit="count", description="Reads performed since connect")
    def get_read_count(self) -> int:
        return self._reads

    @write("drawer_open", risk=Risk.LOW,
           description="Extend or retract the plate carriage",
           params=[Param("open", bool, description="True to extend")])
    def set_drawer(self, open: bool) -> bool:
        if not open and self.get_carriage_occupied():
            pass  # retracting with a plate is normal; that is how you read
        self._drawer_open = open
        self.world.log(f"{self.id}: drawer {'open' if open else 'closed'}")
        return open

    @procedure(
        "read_absorbance",
        risk=Risk.LOW,
        description="Measure absorbance of every well at one wavelength",
        params=[
            Param("wavelength", float, "nm", "Measurement wavelength",
                  limits=Range(230.0, 1000.0, "nm")),
            Param("barcode", str, description="Plate barcode; must be on the carriage",
                  default=""),
            Param("wells", list, description="Subset of wells; empty = all", default=[]),
        ],
        returns="dict of well -> absorbance (AU)",
        est_duration_s=25.0,
        interlocks=("reader_carriage_loaded",),
    )
    def read_absorbance(self, wavelength: float, barcode: str = "",
                        wells: list | None = None) -> dict[str, Any]:
        plate = self._plate_on_carriage(barcode)
        targets = [str(w) for w in (wells or plate.wells.keys())]
        rng = self.world.rng
        # Path length scales with fill volume in a flat-bottom 96-well plate.
        data: dict[str, float] = {}
        for wid in targets:
            w = plate.well(wid)
            path_cm = 0.55 * (w.volume_uL / 200.0) if w.volume_uL > 0 else 0.0
            if abs(wavelength - 562.0) < 15.0:
                conc = w.concentration("protein")
                data[wid] = round(bca_absorbance(conc, path_cm=max(path_cm, 0.01), rng=rng), 4)
            else:
                data[wid] = round(0.04 + 0.002 * rng.random(), 4)
        self._reads += 1
        self.world.log(f"{self.id}: A{wavelength:.0f} read of {plate.barcode} "
                       f"({len(data)} wells)")
        return {"barcode": plate.barcode, "wavelength_nm": wavelength,
                "unit": "AU", "n_wells": len(data), "data": data}

    def _plate_on_carriage(self, barcode: str):
        plate = self.world.at(f"{self.id}_carriage")
        if plate is None:
            raise PhysicalError(
                f"{self.id}: no plate on the carriage. Move one there with the "
                f"robot arm before reading."
            )
        if barcode and plate.barcode != barcode:
            raise PhysicalError(
                f"{self.id}: carriage holds {plate.barcode!r}, not {barcode!r}. "
                f"Refusing to read the wrong plate."
            )
        return plate


# ==========================================================================
# Thermocycler / qPCR
# ==========================================================================

@register_driver("sim.thermocycler", "sim.qpcr")
class SimulatedThermocycler(Device):
    """96-well qPCR thermocycler with optical detection."""

    vendor = "LabAIAgent"
    model = "SimCycler-96"
    category = "thermocycler"
    driver_version = "1.0.0"
    notes = """
    Peltier block, 4-105 C, heated lid to 105 C.
    The lid MUST be closed and heated before cycling or condensation on the
    seal ruins the optics and the run is unusable. The lid_closed interlock
    enforces this.
    Ramp rate ~3.5 C/s block, 2.2 C/s in-sample.
    A run is irreversible once started -- stopping mid-run wastes the plate.
    """

    def __init__(self, device_id: str, *, world: World | None = None, **kw: Any) -> None:
        self.world = world or default_world()
        self._block_C = 22.0
        self._lid_C = 22.0
        self._lid_closed = True
        self._last_run: dict[str, Any] | None = None
        kw.setdefault("simulated", True)
        super().__init__(device_id, **kw)

    def _connect(self) -> None:
        self.world.define_position(f"{self.id}_block")

    def _disconnect(self) -> None:
        pass

    def _halt(self) -> None:
        self.world.log(f"{self.id}: HALT - heaters off, block cooling")

    @read("block_temperature", unit="degC", description="Current block temperature")
    def get_block_temp(self) -> float:
        return round(self._block_C, 2)

    @read("lid_temperature", unit="degC", description="Current heated-lid temperature")
    def get_lid_temp(self) -> float:
        return round(self._lid_C, 2)

    @read("lid_closed", description="Is the heated lid closed and latched?")
    def get_lid_closed(self) -> bool:
        return self._lid_closed

    @read("block_occupied", description="Is a plate loaded in the block?")
    def get_block_occupied(self) -> bool:
        return self.world.at(f"{self.id}_block") is not None

    @write("lid_closed", risk=Risk.LOW, description="Close (True) or open (False) the heated lid",
           params=[Param("closed", bool)])
    def set_lid(self, closed: bool) -> bool:
        self._lid_closed = closed
        return closed

    @write("block_temperature", risk=Risk.MEDIUM, unit="degC",
           description="Hold the block at a temperature",
           params=[Param("value", float, "degC", limits=Range(4.0, 105.0, "degC"))],
           est_duration_s=30.0)
    def set_block_temp(self, value: float) -> float:
        self._block_C = value
        return value

    @procedure(
        "run_qpcr",
        risk=Risk.HIGH,
        reversible=False,
        description="Run a quantitative PCR program with per-cycle optical reads",
        params=[
            Param("barcode", str, description="Plate barcode loaded in the block", default=""),
            Param("cycles", int, "count", "Number of amplification cycles",
                  default=40, limits=Range(10, 50)),
            Param("anneal_temp", float, "degC", "Annealing temperature",
                  default=60.0, limits=Range(45.0, 72.0, "degC")),
            Param("target", str, description="Target species name in the wells",
                  default="template"),
        ],
        interlocks=("lid_closed", "block_loaded"),
        consumes=("sample", "reagent"),
        est_duration_s=5400.0,
        returns="per-well amplification traces and Ct values",
    )
    def run_qpcr(self, barcode: str = "", cycles: int = 40,
                 anneal_temp: float = 60.0, target: str = "template") -> dict[str, Any]:
        plate = self.world.at(f"{self.id}_block")
        if plate is None:
            raise PhysicalError(f"{self.id}: no plate in the block.")
        if barcode and plate.barcode != barcode:
            raise PhysicalError(
                f"{self.id}: block holds {plate.barcode!r}, not {barcode!r}.")
        if not self._lid_closed:
            raise PhysicalError(f"{self.id}: refusing to cycle with the lid open.")

        rng = self.world.rng
        # Annealing away from optimum costs efficiency -- a real, teachable effect.
        eff = 0.95 * math.exp(-((anneal_temp - 60.0) ** 2) / (2 * 6.0 ** 2))
        traces: dict[str, list[float]] = {}
        cts: dict[str, float | None] = {}
        for wid, w in plate.wells.items():
            copies = w.species.get(target, 0.0)
            if w.volume_uL <= 0:
                continue
            tr = qpcr_curve(copies, n_cycles=cycles, efficiency=eff, rng=rng)
            traces[wid] = [round(v, 1) for v in tr]
            ct = compute_ct(tr)
            cts[wid] = round(ct, 3) if ct is not None else None

        self._lid_C = 105.0
        self._block_C = anneal_temp
        self._last_run = {"barcode": plate.barcode, "cycles": cycles,
                          "anneal_temp": anneal_temp, "efficiency": round(eff, 4)}
        self.world.log(f"{self.id}: qPCR {plate.barcode} {cycles} cycles @ "
                       f"{anneal_temp} C, {len(cts)} wells")
        return {**self._last_run, "n_wells": len(cts), "ct": cts, "traces": traces}


# ==========================================================================
# Robot arm
# ==========================================================================

@register_driver("sim.robot_arm", "sim.arm")
class SimulatedRobotArm(Device):
    """6-axis plate-handling arm serving hotels and instrument nests."""

    vendor = "LabAIAgent"
    model = "SimArm-6"
    category = "robot_arm"
    driver_version = "1.0.0"
    notes = """
    Payload 1.2 kg, reach 850 mm. Serves hotel_1..hotel_6, deck_1..deck_12 and
    instrument nests named <device_id>_carriage / <device_id>_block.
    A move is only safe if the destination nest is EMPTY and, for instrument
    nests, the instrument's drawer is OPEN. Both are enforced by interlocks.
    The gripper cannot regrip mid-move: a dropped plate is a hard stop
    requiring a human.
    """

    def __init__(self, device_id: str, *, world: World | None = None,
                 speed: float = 50.0, **kw: Any) -> None:
        self.world = world or default_world()
        self._speed = speed
        self._holding: str | None = None
        self._moves = 0
        kw.setdefault("simulated", True)
        super().__init__(device_id, **kw)

    def _connect(self) -> None:
        for i in range(1, 7):
            self.world.define_position(f"hotel_{i}")

    def _disconnect(self) -> None:
        pass

    def _halt(self) -> None:
        self.world.log(f"{self.id}: HALT - servos disabled mid-trajectory"
                       + (f", STILL HOLDING {self._holding}" if self._holding else ""))

    @read("holding", description="Barcode of the labware currently gripped, or empty")
    def get_holding(self) -> str:
        return self._holding or ""

    @read("speed", unit="percent", description="Motion speed as a percentage of maximum")
    def get_speed(self) -> float:
        return self._speed

    @read("move_count", unit="count", description="Moves completed since connect")
    def get_move_count(self) -> int:
        return self._moves

    @write("speed", risk=Risk.MEDIUM, unit="percent",
           description="Set motion speed. Above 75% the gripper can lose a wet plate.",
           params=[Param("value", float, "percent", limits=Range(5.0, 100.0, "percent"))])
    def set_speed(self, value: float) -> float:
        self._speed = value
        return value

    @procedure(
        "move_labware",
        risk=Risk.HIGH,
        reversible=False,
        description="Move a plate from its current position to a destination nest",
        params=[
            Param("barcode", str, description="Labware to move"),
            Param("destination", str, description="Destination position name"),
        ],
        interlocks=("destination_free", "no_device_in_error"),
        est_duration_s=12.0,
    )
    def move_labware(self, barcode: str, destination: str) -> dict[str, Any]:
        w = self.world
        lw = w.get(barcode)
        origin = lw.location
        if w.maybe_fault("gripper_drop"):
            raise PhysicalError(
                f"{self.id}: gripper lost {barcode} in transit between {origin} "
                f"and {destination}. STOP. A human must clear the deck and "
                f"inspect for spillage before any further motion."
            )
        if self._speed > 75.0 and lw.total_volume() > 0:
            w.log(f"{self.id}: WARNING moving a filled plate at {self._speed:.0f}% speed")
        w.move(barcode, destination)
        self._moves += 1
        return {"barcode": barcode, "from": origin, "to": destination,
                "speed_percent": self._speed}


# ==========================================================================
# Incubator
# ==========================================================================

@register_driver("sim.incubator")
class SimulatedIncubator(Device):
    """CO2 incubator with a plate hotel."""

    vendor = "LabAIAgent"
    model = "SimInc-200"
    category = "incubator"
    driver_version = "1.0.0"
    notes = """
    37 C / 5% CO2 / 95% RH nominal. Door opening drops CO2 for ~4 minutes;
    batch your plate movements rather than opening repeatedly.
    """

    def __init__(self, device_id: str, *, world: World | None = None, **kw: Any) -> None:
        self.world = world or default_world()
        self._temp = 37.0
        self._co2 = 5.0
        self._door_open = False
        kw.setdefault("simulated", True)
        super().__init__(device_id, **kw)

    def _connect(self) -> None:
        for i in range(1, 21):
            self.world.define_position(f"{self.id}_slot_{i}")

    def _disconnect(self) -> None:
        pass

    @read("temperature", unit="degC", description="Chamber temperature")
    def get_temperature(self) -> float:
        return round(self._temp + self.world.rng.gauss(0, 0.05), 2)

    @read("co2", unit="percent", description="Chamber CO2 concentration")
    def get_co2(self) -> float:
        base = self._co2 * (0.6 if self._door_open else 1.0)
        return round(base + self.world.rng.gauss(0, 0.03), 2)

    @read("door_open", description="Is the chamber door open?")
    def get_door_open(self) -> bool:
        return self._door_open

    @write("temperature", risk=Risk.MEDIUM, unit="degC",
           description="Set chamber temperature setpoint",
           params=[Param("value", float, "degC", limits=Range(20.0, 50.0, "degC"))],
           est_duration_s=900.0)
    def set_temperature(self, value: float) -> float:
        self._temp = value
        return value

    @write("door_open", risk=Risk.LOW, description="Open (True) or close (False) the chamber door",
           params=[Param("open", bool)])
    def set_door(self, open: bool) -> bool:
        self._door_open = open
        return open

    @procedure("incubate", risk=Risk.MEDIUM,
               description="Hold a plate at temperature for a fixed duration",
               params=[Param("barcode", str),
                       Param("duration", float, "min", limits=Range(1.0, 2880.0, "min"))],
               est_duration_s=1800.0)
    def incubate(self, barcode: str, duration: float) -> dict[str, Any]:
        lw = self.world.get(barcode)
        self.world.evaporate(lw, duration * 60.0, temp_C=self._temp)
        self.world.log(f"{self.id}: incubated {barcode} {duration:.0f} min @ {self._temp} C")
        return {"barcode": barcode, "duration_min": duration,
                "temperature_C": self._temp,
                "residual_volume_uL": round(lw.total_volume(), 2)}


# ==========================================================================
# Centrifuge
# ==========================================================================

@register_driver("sim.centrifuge")
class SimulatedCentrifuge(Device):
    """Plate centrifuge with imbalance detection."""

    vendor = "LabAIAgent"
    model = "SimSpin-4"
    category = "centrifuge"
    driver_version = "1.0.0"
    notes = """
    Four-bucket swing-out rotor. Buckets MUST be loaded in opposing pairs;
    running unbalanced is the one action here that can injure someone, so it
    is risk=CRITICAL and always requires a human approval token.
    """

    def __init__(self, device_id: str, *, world: World | None = None, **kw: Any) -> None:
        self.world = world or default_world()
        self._buckets: dict[int, str | None] = {1: None, 2: None, 3: None, 4: None}
        self._lid_locked = False
        kw.setdefault("simulated", True)
        super().__init__(device_id, **kw)

    def _connect(self) -> None:
        pass

    def _disconnect(self) -> None:
        pass

    def _halt(self) -> None:
        self.world.log(f"{self.id}: HALT - rotor braking, lid stays locked until stopped")

    @read("buckets", description="Which barcode is in each rotor bucket")
    def get_buckets(self) -> dict[str, str]:
        return {str(k): (v or "") for k, v in self._buckets.items()}

    @read("balanced", description="Are the loaded buckets in opposing pairs?")
    def get_balanced(self) -> bool:
        loaded = {k for k, v in self._buckets.items() if v}
        if not loaded:
            return True
        return loaded in ({1, 3}, {2, 4}, {1, 2, 3, 4})

    @procedure("load_bucket", risk=Risk.MEDIUM,
               description="Place labware into a rotor bucket",
               params=[Param("bucket", int, limits=Range(1, 4)),
                       Param("barcode", str)],
               reversible=False, est_duration_s=8.0)
    def load_bucket(self, bucket: int, barcode: str) -> dict[str, Any]:
        self.world.get(barcode)
        self._buckets[bucket] = barcode
        return {"bucket": bucket, "barcode": barcode, "balanced": self.get_balanced()}

    @procedure(
        "spin",
        risk=Risk.CRITICAL,
        reversible=False,
        requires_confirmation=True,
        description="Spin the rotor. Requires human approval and a balanced load.",
        params=[Param("rcf", float, "g_force", "Relative centrifugal field",
                      limits=Range(50.0, 4000.0, "g_force")),
                Param("duration", float, "s", limits=Range(5.0, 3600.0, "s"))],
        interlocks=("centrifuge_balanced",),
        est_duration_s=300.0,
    )
    def spin(self, rcf: float, duration: float) -> dict[str, Any]:
        if not self.get_balanced():
            raise PhysicalError(
                f"{self.id}: rotor is not balanced. Refusing to spin -- an "
                f"unbalanced rotor at {rcf:.0f} g can destroy the instrument."
            )
        self.world.log(f"{self.id}: spun at {rcf:.0f} g for {duration:.0f} s")
        return {"rcf": rcf, "duration_s": duration,
                "buckets": self.get_buckets()}


__all__ = [
    "SimulatedPlateReader", "SimulatedThermocycler", "SimulatedRobotArm",
    "SimulatedIncubator", "SimulatedCentrifuge",
]
