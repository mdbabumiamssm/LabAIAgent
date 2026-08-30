"""Citation-carrying protocol templates: published methods → executable work.

A template is the bridge between the literature and the bench: a versioned,
parameterised method whose every step is bound to a device *category* rather
than a device id, and whose provenance is a set of citations that MUST come
from the trusted-source registry (PubMed-indexed literature or an allowlisted
pharma/vendor publisher) -- a template without acceptable provenance cannot
even be constructed.

Translation is human-in-the-loop by construction:

    template.instantiate(session, parameters) -> (Protocol, report)

resolves categories against the instruments actually present, validates every
parameter against its declared limits, and returns a protocol *document* plus
a static-validation report. It never executes anything: steps flagged
``needs_approval`` still require a human approval token at run time, exactly
like any other high-risk action.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..core.errors import ConfigurationError, LabAIAgentError
from ..core.types import Param, Range
from ..orchestration.session import LabSession
from ..orchestration.workflow import Protocol, Step
from .sources import TRUSTED_SOURCES


@dataclass(frozen=True)
class Citation:
    source_key: str      # must exist in TRUSTED_SOURCES
    ref: str             # 'PMID:3843705', 'DOI:10...', or a document title
    title: str = ""

    def __post_init__(self) -> None:
        if self.source_key not in TRUSTED_SOURCES:
            raise ConfigurationError(
                f"Citation source {self.source_key!r} is not in the trusted-"
                f"source registry. Templates may only cite PubMed-indexed "
                f"literature or allowlisted publishers "
                f"({sorted(TRUSTED_SOURCES)}).")

    def to_dict(self) -> dict[str, str]:
        src = TRUSTED_SOURCES[self.source_key]
        return {"source": src.name, "kind": src.kind, "ref": self.ref,
                "title": self.title}


@dataclass(frozen=True)
class TemplateStep:
    name: str
    category: str                      # device category to bind to
    capability: str                    # qualified, e.g. 'proc:transfer'
    args: dict[str, Any] = field(default_factory=dict)   # '$param' references
    depends_on: tuple[str, ...] = ()
    store_as: str = ""
    needs_approval: bool = False       # high-risk: a human token is required
    note: str = ""


@dataclass(frozen=True)
class ProtocolTemplate:
    id: str
    name: str
    version: str
    summary: str
    citations: tuple[Citation, ...]
    parameters: tuple[Param, ...]
    steps: tuple[TemplateStep, ...]
    notes: str = ""

    def __post_init__(self) -> None:
        if not self.citations:
            raise ConfigurationError(
                f"Template {self.id!r} has no citations; provenance-free "
                f"protocols are not accepted into the library.")

    # -- description ---------------------------------------------------------

    def describe(self) -> dict[str, Any]:
        return {
            "id": self.id, "name": self.name, "version": self.version,
            "summary": self.summary,
            "citations": [c.to_dict() for c in self.citations],
            "parameters": [p.to_dict() for p in self.parameters],
            "device_categories": sorted({s.category for s in self.steps}),
            "steps": len(self.steps),
            "approval_steps": [s.name for s in self.steps if s.needs_approval],
            "notes": self.notes,
        }

    # -- translation ----------------------------------------------------------

    def instantiate(self, session: LabSession,
                    parameters: dict[str, Any] | None = None,
                    device_map: dict[str, str] | None = None,
                    ) -> tuple[Protocol, dict[str, Any]]:
        """Bind the template to THIS lab.

        Every declared parameter is validated against its limits; every
        device category is resolved to exactly one instrument (ambiguity is
        an error unless ``device_map`` decides it). Returns the Protocol and
        a report: bindings, citations, unresolved approvals, and the static
        validation problems -- so a human (or an agent under a human
        approval) sees exactly what would run before anything runs.
        """
        # 1. Parameters, limit-checked exactly like any capability argument.
        supplied = dict(parameters or {})
        values: dict[str, Any] = {}
        spec = {p.name: p for p in self.parameters}
        unknown = set(supplied) - set(spec)
        if unknown:
            raise LabAIAgentError(
                f"Template {self.id!r}: unexpected parameter(s) "
                f"{sorted(unknown)}. Accepted: {sorted(spec)}")
        for pname, p in spec.items():
            if pname in supplied:
                try:
                    values[pname] = p.validate(supplied[pname])
                except (ValueError, TypeError) as exc:
                    # Agent-facing repair signal, same contract as capability
                    # argument validation.
                    raise LabAIAgentError(
                        f"Template {self.id!r}: {exc}") from None
            elif p.required:
                raise LabAIAgentError(
                    f"Template {self.id!r}: missing required parameter "
                    f"{pname!r} ({p.describe()})")
            else:
                values[pname] = p.default

        # 2. Category -> device binding against the live lab.
        bindings: dict[str, str] = {}
        for cat in sorted({s.category for s in self.steps}):
            if device_map and cat in device_map:
                dev = session.get(device_map[cat])   # raises if unknown
                if dev.category != cat:
                    raise LabAIAgentError(
                        f"device_map binds category {cat!r} to {dev.id!r}, "
                        f"whose category is {dev.category!r}.")
                bindings[cat] = dev.id
                continue
            matches = session.devices(category=cat)
            if not matches:
                raise LabAIAgentError(
                    f"Template {self.id!r} needs a {cat!r}; this lab has "
                    f"none. Present categories: "
                    f"{sorted({d.category for d in session.devices()})}")
            if len(matches) > 1:
                raise LabAIAgentError(
                    f"Template {self.id!r}: category {cat!r} is ambiguous "
                    f"({[d.id for d in matches]}); pass device_map to choose.")
            bindings[cat] = matches[0].id

        # 3. Substitute '$param' references and build the Protocol.
        def resolve(v: Any) -> Any:
            if isinstance(v, str) and v.startswith("$"):
                key = v[1:]
                if key not in values:
                    raise LabAIAgentError(
                        f"Template step references unknown parameter {v!r}.")
                return values[key]
            return v

        steps = [Step(
            name=s.name, device=bindings[s.category], capability=s.capability,
            args={k: resolve(v) for k, v in s.args.items()},
            depends_on=s.depends_on, store_as=s.store_as, note=s.note,
        ) for s in self.steps]

        proto = Protocol(
            f"{self.id}@{self.version}",
            description=f"{self.name} -- {self.summary} "
                        f"[citations: "
                        f"{'; '.join(c.ref for c in self.citations)}]",
            steps=steps)

        report = {
            "template": self.id, "version": self.version,
            "citations": [c.to_dict() for c in self.citations],
            "parameters": values,
            "device_bindings": bindings,
            "needs_approval": [s.name for s in self.steps if s.needs_approval],
            "validation_problems": proto.validate(session),
            "hint": "Review this document, obtain approval tokens for the "
                    "steps listed under needs_approval (a human mints them), "
                    "attach them to those steps, then execute via "
                    "run_protocol.",
        }
        return proto, report


# ==========================================================================
# The shipped library. Every entry carries real, checkable provenance.
# ==========================================================================

_WELL = r"[A-P](?:[1-9]|1[0-9]|2[0-4])"

PROTOCOL_TEMPLATES: dict[str, ProtocolTemplate] = {t.id: t for t in [

    ProtocolTemplate(
        id="bca_protein_assay",
        name="BCA protein quantitation (microplate)",
        version="1.0",
        summary="Bicinchoninic-acid total-protein assay: build a BSA "
                "standard curve by serial dilution, transfer samples, read "
                "A562, quantify against the curve.",
        citations=(
            Citation("pubmed", "PMID:3843705",
                     "Smith PK et al. Measurement of protein using "
                     "bicinchoninic acid. Anal Biochem 1985;150:76-85."),
            Citation("thermofisher", "Pierce BCA Protein Assay Kit "
                     "instructions (23225/23227)"),
        ),
        parameters=(
            Param("standards_barcode", str,
                  description="Plate carrying the standard-curve wells"),
            Param("stock_well", str, description="BSA stock well (top of curve)",
                  default="A1"),
            Param("diluent_barcode", str, description="Diluent reservoir barcode"),
            Param("diluent_well", str, description="Diluent well", default="A1"),
            Param("transfer_volume", float, "uL",
                  "Serial-dilution transfer volume", default=25.0,
                  limits=Range(1.0, 200.0, "uL")),
            Param("diluent_volume", float, "uL",
                  "Diluent per dilution step", default=25.0,
                  limits=Range(1.0, 200.0, "uL")),
            Param("wavelength", float, "nm", "Read wavelength (BCA: 562)",
                  default=562.0, limits=Range(540.0, 590.0, "nm")),
        ),
        steps=(
            TemplateStep(
                "build_standard_curve", "liquid_handler",
                "proc:serial_dilution",
                args={"barcode": "$standards_barcode",
                      "wells": ["A1", "B1", "C1", "D1", "E1", "F1", "G1"],
                      "transfer_volume": "$transfer_volume",
                      "diluent_barcode": "$diluent_barcode",
                      "diluent_well": "$diluent_well",
                      "diluent_volume": "$diluent_volume"},
                note="Two-fold BSA series, most concentrated first "
                     "(working range ~20-2000 ug/mL)."),
            TemplateStep(
                "move_to_reader", "robot_arm", "proc:move_labware",
                args={"barcode": "$standards_barcode",
                      "destination": "reader_carriage"},
                depends_on=("build_standard_curve",),
                needs_approval=True,
                note="Plate transport is HIGH risk; requires a human token."),
            TemplateStep(
                "read_a562", "plate_reader", "proc:read_absorbance",
                args={"wavelength": "$wavelength",
                      "barcode": "$standards_barcode"},
                depends_on=("move_to_reader",),
                store_as="absorbance",
                note="A562; path length is fill-volume dependent -- do not "
                     "normalise to 1 cm without correction."),
        ),
        notes="Run standards and unknowns on the same plate whenever "
              "possible. The curve rolls off above ~1000 ug/mL: fitting a "
              "line through the roll-off quantifies badly at the top.",
    ),

    ProtocolTemplate(
        id="qpcr_quantification",
        name="Quantitative PCR run (MIQE-aligned)",
        version="1.0",
        summary="Close the heated lid and run a qPCR program with per-cycle "
                "optical reads; report Ct per well for standard-curve "
                "quantification.",
        citations=(
            Citation("pubmed", "PMID:19246619",
                     "Bustin SA et al. The MIQE guidelines. Clin Chem "
                     "2009;55:611-22."),
            Citation("biorad", "Real-Time PCR Applications Guide"),
        ),
        parameters=(
            Param("plate_barcode", str,
                  description="qPCR plate loaded in the block"),
            Param("cycles", int, "count", "Amplification cycles",
                  default=40, limits=Range(10, 50)),
            Param("anneal_temp", float, "degC", "Annealing temperature",
                  default=60.0, limits=Range(45.0, 72.0, "degC")),
            Param("target", str, description="Template species name",
                  default="template"),
        ),
        steps=(
            TemplateStep(
                "close_lid", "thermocycler", "write:lid_closed",
                args={"closed": True},
                note="Lid must be closed and heated or condensation ruins "
                     "the optics (enforced again by the lid interlock)."),
            TemplateStep(
                "run_qpcr", "thermocycler", "proc:run_qpcr",
                args={"barcode": "$plate_barcode", "cycles": "$cycles",
                      "anneal_temp": "$anneal_temp", "target": "$target"},
                depends_on=("close_lid",),
                store_as="qpcr_data",
                needs_approval=True,
                note="Irreversible once started (HIGH risk). Report per "
                     "MIQE: efficiency from the standard-curve slope, "
                     "not assumed."),
        ),
        notes="MIQE minimum reporting: instrument, chemistry, efficiency, "
              "NTC behaviour, and Ct determination method.",
    ),

    ProtocolTemplate(
        id="serial_dilution_series",
        name="Serial dilution series",
        version="1.0",
        summary="Standard N-fold serial dilution across a well series with "
                "post-dispense mixing.",
        citations=(
            Citation("thermofisher",
                     "Thermo Scientific: serial dilution good practice "
                     "(pipetting technical reference)"),
            Citation("protocols_io", "protocols.io serial-dilution "
                     "collection (DOI-registered)"),
        ),
        parameters=(
            Param("barcode", str, description="Plate for the series"),
            Param("wells", list,
                  description="Ordered well IDs, most concentrated first"),
            Param("transfer_volume", float, "uL", "Volume carried forward",
                  default=25.0, limits=Range(1.0, 500.0, "uL")),
            Param("diluent_barcode", str, description="Diluent labware"),
            Param("diluent_well", str, description="Diluent source well",
                  default="A1"),
            Param("diluent_volume", float, "uL", "Diluent per well",
                  default=25.0, limits=Range(1.0, 500.0, "uL")),
        ),
        steps=(
            TemplateStep(
                "serial_dilution", "liquid_handler", "proc:serial_dilution",
                args={"barcode": "$barcode", "wells": "$wells",
                      "transfer_volume": "$transfer_volume",
                      "diluent_barcode": "$diluent_barcode",
                      "diluent_well": "$diluent_well",
                      "diluent_volume": "$diluent_volume"},
                store_as="dilution_report",
                note="Sub-5 uL transfers carry >5% CV: prefer an "
                     "intermediate dilution over tiny volumes."),
        ),
        notes="Fold-change per step is (transfer+diluent)/transfer; verify "
              "the report's final volumes before consuming the series.",
    ),
]}


def get_template(template_id: str) -> ProtocolTemplate:
    try:
        return PROTOCOL_TEMPLATES[template_id]
    except KeyError:
        raise LabAIAgentError(
            f"No protocol template {template_id!r}. Available: "
            f"{sorted(PROTOCOL_TEMPLATES)}") from None


__all__ = ["Citation", "TemplateStep", "ProtocolTemplate",
           "PROTOCOL_TEMPLATES", "get_template"]
