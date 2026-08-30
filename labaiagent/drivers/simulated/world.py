"""A shared simulated physical world.

The simulators are only useful for validating a protocol if they are coupled:
what the liquid handler dispenses must be what the reader measures, and a
plate the arm has moved must no longer be where the handler expects it.

So all simulated devices bind to one ``World``. It tracks labware, well
contents, plate locations and tip inventory, and applies plausible physics --
pipetting error that scales with viscosity, absorbance from Beer-Lambert,
qPCR amplification from starting copy number, evaporation over time.

Nothing here is a digital twin in any rigorous sense. It exists so a protocol
can be debugged, and its failure branches exercised, before it touches real
sample.
"""

from __future__ import annotations

import math
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any

# Standard SLAS/ANSI microplate geometries.
PLATE_FORMATS: dict[int, tuple[int, int, float]] = {
    #  wells: (rows, cols, max working volume uL)
    6:   (2, 3, 3000.0),
    24:  (4, 6, 1700.0),
    96:  (8, 12, 300.0),
    384: (16, 24, 90.0),
    1536: (32, 48, 12.0),
}

ROW_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def well_ids(n_wells: int) -> list[str]:
    rows, cols, _ = PLATE_FORMATS[n_wells]
    return [f"{ROW_LETTERS[r]}{c + 1}" for r in range(rows) for c in range(cols)]


@dataclass
class Well:
    """Contents of one well. ``species`` maps name -> amount in ug (or copies)."""

    volume_uL: float = 0.0
    species: dict[str, float] = field(default_factory=dict)

    def concentration(self, name: str) -> float:
        """ug/mL for protein species (amount is in ug, volume in uL)."""
        if self.volume_uL <= 0:
            return 0.0
        return self.species.get(name, 0.0) / (self.volume_uL / 1000.0)

    def add(self, volume_uL: float, species: dict[str, float] | None = None) -> None:
        self.volume_uL += volume_uL
        for k, v in (species or {}).items():
            self.species[k] = self.species.get(k, 0.0) + v

    def remove(self, volume_uL: float) -> dict[str, float]:
        """Withdraw volume; species come out proportionally (well-mixed assumption)."""
        if volume_uL <= 0:
            return {}
        taken = min(volume_uL, self.volume_uL)
        frac = taken / self.volume_uL if self.volume_uL > 0 else 0.0
        out = {k: v * frac for k, v in self.species.items()}
        for k in list(self.species):
            self.species[k] -= out[k]
            if self.species[k] < 1e-15:
                del self.species[k]
        self.volume_uL -= taken
        return out


@dataclass
class Labware:
    """A plate, reservoir or tube rack sitting somewhere in the lab."""

    barcode: str
    n_wells: int = 96
    kind: str = "plate"
    location: str = "hotel_1"
    lid: bool = False
    created_at: float = field(default_factory=time.time)
    wells: dict[str, Well] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.n_wells not in PLATE_FORMATS:
            raise ValueError(f"Unsupported plate format: {self.n_wells}")
        if not self.wells:
            self.wells = {w: Well() for w in well_ids(self.n_wells)}

    @property
    def max_volume_uL(self) -> float:
        return PLATE_FORMATS[self.n_wells][2]

    @property
    def rows(self) -> int:
        return PLATE_FORMATS[self.n_wells][0]

    @property
    def cols(self) -> int:
        return PLATE_FORMATS[self.n_wells][1]

    def well(self, wid: str) -> Well:
        try:
            return self.wells[wid.upper()]
        except KeyError:
            raise ValueError(
                f"Well {wid!r} does not exist on a {self.n_wells}-well plate "
                f"(valid: A1..{ROW_LETTERS[self.rows - 1]}{self.cols})"
            ) from None

    def total_volume(self) -> float:
        return sum(w.volume_uL for w in self.wells.values())


class World:
    """Shared mutable state for all simulated instruments."""

    def __init__(self, seed: int = 0xC0FFEE) -> None:
        self.rng = random.Random(seed)
        self.lock = threading.RLock()
        self.labware: dict[str, Labware] = {}
        #: location name -> barcode currently there (None = empty)
        self.positions: dict[str, str | None] = {}
        self.tips_available: int = 96 * 10
        self.tips_used: int = 0
        self.ambient_C: float = 22.0
        self.event_log: list[str] = []
        #: Fault injection: set e.g. {'tip_pickup_fail': 0.05}
        self.fault_rates: dict[str, float] = {}
        self.t0 = time.time()

    # -- labware management -----------------------------------------------

    def define_position(self, name: str, occupant: str | None = None) -> None:
        with self.lock:
            self.positions[name] = occupant

    def add_labware(self, lw: Labware) -> Labware:
        with self.lock:
            self.labware[lw.barcode] = lw
            self.positions.setdefault(lw.location, None)
            if self.positions.get(lw.location) not in (None, lw.barcode):
                raise ValueError(
                    f"Position {lw.location} already holds "
                    f"{self.positions[lw.location]!r}"
                )
            self.positions[lw.location] = lw.barcode
            self.log(f"labware {lw.barcode} placed at {lw.location}")
            return lw

    def get(self, barcode: str) -> Labware:
        try:
            return self.labware[barcode]
        except KeyError:
            raise ValueError(
                f"No labware with barcode {barcode!r}. Known: {sorted(self.labware)}"
            ) from None

    def at(self, location: str) -> Labware | None:
        bc = self.positions.get(location)
        return self.labware.get(bc) if bc else None

    def move(self, barcode: str, to: str) -> None:
        with self.lock:
            lw = self.get(barcode)
            if self.positions.get(to) not in (None, barcode):
                raise ValueError(
                    f"Cannot move {barcode} to {to}: occupied by "
                    f"{self.positions[to]!r}"
                )
            self.positions[lw.location] = None
            lw.location = to
            self.positions[to] = barcode
            self.log(f"labware {barcode} -> {to}")

    def occupied_positions(self) -> set[str]:
        return {k for k, v in self.positions.items() if v is not None}

    # -- consumables ------------------------------------------------------

    def consume_tips(self, n: int) -> None:
        with self.lock:
            if self.tips_available < n:
                raise RuntimeError(
                    f"Out of pipette tips: need {n}, {self.tips_available} left. "
                    f"Reload the tip carrier."
                )
            self.tips_available -= n
            self.tips_used += n

    # -- faults -----------------------------------------------------------

    def maybe_fault(self, name: str) -> bool:
        rate = self.fault_rates.get(name, 0.0)
        return rate > 0 and self.rng.random() < rate

    def log(self, msg: str) -> None:
        self.event_log.append(f"[{time.time() - self.t0:8.2f}s] {msg}")

    # -- physics ----------------------------------------------------------

    def evaporate(self, lw: Labware, seconds: float, temp_C: float | None = None) -> None:
        """Crude evaporation: ~0.4 uL/h/well at 22 C, doubling per 10 C."""
        t = self.ambient_C if temp_C is None else temp_C
        rate_uL_per_s = 0.4 / 3600.0 * (2 ** ((t - 22.0) / 10.0))
        if lw.lid:
            rate_uL_per_s *= 0.05
        for w in lw.wells.values():
            if w.volume_uL > 0:
                w.volume_uL = max(0.0, w.volume_uL - rate_uL_per_s * seconds)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "labware": {
                    bc: {
                        "kind": lw.kind, "n_wells": lw.n_wells,
                        "location": lw.location, "lid": lw.lid,
                        "total_volume_uL": round(lw.total_volume(), 2),
                        "nonempty_wells": sum(1 for w in lw.wells.values()
                                              if w.volume_uL > 0),
                    }
                    for bc, lw in self.labware.items()
                },
                "positions": dict(self.positions),
                "tips_available": self.tips_available,
                "tips_used": self.tips_used,
            }


# --------------------------------------------------------------------------
# Assay physics
# --------------------------------------------------------------------------

def bca_absorbance(protein_ug_per_mL: float, *, path_cm: float = 0.55,
                   noise_sd: float = 0.004, rng: random.Random | None = None) -> float:
    """A562 for a BCA protein assay.

    Empirically the BCA response is near-linear to ~1000 ug/mL then rolls off.
    We model it as a saturating hyperbola plus a reagent blank, which
    reproduces the shape people actually fit (and the reason a standard curve
    run too high fails linearity).
    """
    rng = rng or random.Random()
    blank = 0.075
    a_max, k_half = 1.85, 1250.0
    signal = a_max * protein_ug_per_mL / (k_half + protein_ug_per_mL)
    signal *= path_cm / 0.55
    return max(0.0, blank + signal + rng.gauss(0.0, noise_sd))


def qpcr_curve(starting_copies: float, n_cycles: int = 40, *,
               efficiency: float = 0.95, plateau_rfu: float = 42000.0,
               baseline_rfu: float = 350.0, noise_sd: float = 45.0,
               rng: random.Random | None = None) -> list[float]:
    """Simulate one amplification trace.

    Exponential growth at (1+E) per cycle, saturating into a plateau as
    reagents deplete -- the standard sigmoid. Ct falls out of it naturally,
    which is what lets the demo compute a real standard curve and efficiency.
    """
    rng = rng or random.Random()
    if starting_copies <= 0:
        return [baseline_rfu + rng.gauss(0, noise_sd) for _ in range(n_cycles)]
    # Scale so that ~1e6 starting copies crosses threshold near cycle 15 and
    # ~1e1 near cycle 32, i.e. a realistic 5-log dynamic range. Getting this
    # wrong in the other direction saturates the trace before cycle 1 and
    # inverts the standard curve, which is a instructive failure in itself:
    # the Ct algorithm is correct, the photometry model was not.
    rfu_per_copy = 2.0e-8
    out: list[float] = []
    for c in range(1, n_cycles + 1):
        copies = starting_copies * ((1.0 + efficiency) ** c)
        raw = rfu_per_copy * copies
        signal = plateau_rfu * raw / (plateau_rfu + raw)   # saturation
        out.append(baseline_rfu + signal + rng.gauss(0, noise_sd))
    return out


def compute_ct(trace: list[float], threshold: float | None = None,
               baseline_cycles: tuple[int, int] = (3, 15)) -> float | None:
    """Threshold-crossing Ct with linear interpolation between cycles.

    Threshold defaults to baseline mean + 10 SD, which is the conventional
    automatic setting on most instruments.
    """
    lo, hi = baseline_cycles
    base = trace[lo - 1:hi]
    if not base:
        return None
    mean = sum(base) / len(base)
    var = sum((x - mean) ** 2 for x in base) / max(1, len(base) - 1)
    sd = math.sqrt(var)
    thr = threshold if threshold is not None else mean + 10.0 * sd
    for i in range(1, len(trace)):
        if trace[i] >= thr > trace[i - 1]:
            span = trace[i] - trace[i - 1]
            frac = (thr - trace[i - 1]) / span if span > 0 else 0.0
            return (i) + frac          # 1-indexed cycle number
    return None


_DEFAULT_WORLD: World | None = None


def default_world() -> World:
    """The shared world used by simulators built from a YAML lab config.

    Devices constructed in Python can each be handed an explicit ``World``.
    Devices built from config cannot, so they fall back to this singleton --
    otherwise every simulated instrument in a configured lab would inhabit its
    own private universe and a plate moved by the arm would never arrive at
    the reader.
    """
    global _DEFAULT_WORLD
    if _DEFAULT_WORLD is None:
        _DEFAULT_WORLD = World()
    return _DEFAULT_WORLD


def reset_default_world(seed: int = 0xC0FFEE) -> World:
    global _DEFAULT_WORLD
    _DEFAULT_WORLD = World(seed=seed)
    return _DEFAULT_WORLD


__all__ = [
    "World", "Labware", "Well", "PLATE_FORMATS", "well_ids",
    "bca_absorbance", "qpcr_curve", "compute_ct",
    "default_world", "reset_default_world",
]
