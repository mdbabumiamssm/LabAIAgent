# Roadmap: from validated software to an incomparable lab system

An honest gap analysis of LabAIAgent v1.2.0, written the way a program
review would put it. Tier 1 is what stands between the current release and
*daily use in a real lab*. Tier 2 is what would make it *incomparable* —
capabilities no current commercial or academic alternative combines. Tier 3
is ecosystem and assurance work that compounds the first two. Every item
has an acceptance criterion, because a roadmap without falsifiable
milestones is a wish list.

---

## Tier 1 — required for real-lab daily use

### 1.1 Physical hardware validation (the single biggest gap)
All 181 tests prove the software; zero prove the last meter of cable. The
three hardware drivers have never driven an instrument.
**Do:** a formal bench-validation study per instrument — `labaiagent
doctor` conformance on first connect; a scripted assay run readonly →
dry-run → live; gravimetric verification of delivered volumes vs. requested
(CV and bias per volume class); cross-check of every audit record against
the instrument's own log. Publish the study in `docs/` with raw data.
**Accept when:** three physical instruments (start with the Opentrons Flex
and one SCPI device and one export-folder reader) each pass a documented
IQ/OQ-style run, and the delivered-vs-requested dataset is in the repo.

### 1.2 Driver breadth for a working bench
Three hardware patterns cover connectivity classes, not a lab.
**Do:** drivers for the instruments an MPN/heme-onc bench actually runs:
QuantStudio-class qPCR (filewatch export), plate washer, Liconic/Cytomat
incubator, plate sealer/peeler, Hamilton/Tecan (vendor API or worklist
export), flow cytometer acquisition hand-off, barcode scanner. Ship
SCPI "command packs" as YAML (no Python) for the long tail.
**Accept when:** ten drivers pass `--level strict`, at least five validated
on hardware per 1.1.

### 1.3 Operator dashboard and remote approvals
Approvals, suspension reinstatement, and live state currently require the
CLI or Python. Daily use needs a phone-usable surface.
**Do:** a single-page dashboard served by the gateway (the SSE stream,
tools, and e-signature backend already exist): live device states, running
jobs with cancel, pending approval requests with e-sign entry, suspension
list with reinstate, audit tail with chain badge, and a large red e-stop.
**Accept when:** an approver can grant an e-signed token from a phone in
under 15 seconds, and reinstatement is a dashboard action recorded in the
audit chain.

### 1.4 Durable state and service operation
Jobs and approvals die with the process (fail-closed but operationally
annoying); no service packaging.
**Do:** optional SQLite state store for jobs/approvals/suspensions;
protocol auto-resume from existing checkpoints on restart; audit-log
rotation/segmenting with chained segment heads; systemd unit + container
image; documented backup/restore.
**Accept when:** `kill -9` mid-protocol, restart, and the run resumes at
the checkpoint with the chain intact and the job history present.

### 1.5 Data and provenance layer (publication-grade)
Results are returned inline and summarised into the audit log; there is no
durable dataset with provenance.
**Do:** run records — every protocol/procedure gets a `run_id`; results
written as files (CSV/Parquet + JSON sidecar) under `runs/<run_id>/`,
SHA-256 of each artifact embedded in the audit chain; Allotrope Simple
Model (ASM) export for instrument data; push connectors for Benchling /
LabArchives / generic webhook ELN.
**Accept when:** a reviewer can start from a figure value and walk —
artifact hash → audit record → actor, arguments, approvals, instrument —
with no gaps. This is the provenance walk a Nature methods reviewer will
attempt.

---

## Tier 2 — what would make it incomparable

### 2.1 Calibrated digital twins + simulate-first enforcement
The simulators are plausible, not predictive. Calibrate them per lab: fit
the pipetting CV/bias model from the 1.1 gravimetric data, reader noise
from blanks, cycler ramp from logs. Then add the policy switch nothing else
on the market has: `policy.require_simulation_pass: true` — a protocol may
not run live until the *same document* passed on the lab's own twin.
**Accept when:** twin-predicted vs. real assay outputs agree within stated
tolerances on a validation plate, and the enforcement gate is audited.

### 2.2 Closed-loop experimentation with human gates
The self-driving-lab capability, done safely: Bayesian optimization
(Ax/BoTorch) proposing the next protocol parameters from run-record data,
each iteration passing the full stack — validation, twin, ceiling,
approvals, oversight — with a human gate between rounds by default.
**Accept when:** a seeded optimization campaign (e.g., qPCR anneal-temp /
efficiency) converges on the simulator end-to-end, every iteration fully
audited, and the campaign object is itself a reviewable artifact.

### 2.3 Opt-in parallel DAG execution
Serial execution is a defensible v1 choice; overlapping an incubation with
a read is real wall-clock in every assay.
**Do:** bounded parallel branches behind `parallelism: N`, per-device locks
already serialize hardware, deterministic *logical* order preserved in the
audit via step lineage ids.
**Accept when:** the BCA example runs measurably faster with parallelism 2
and the audit chain replays to the same causal order.

### 2.4 Vision verification of physical state
Cheap and uniquely convincing oversight: a camera snapshot before/after
HIGH/CRITICAL steps, hash-embedded in the audit record; optionally the
foundation-model reviewer sees the image ("is there actually a plate on
the carriage?").
**Accept when:** a plate-move audit record carries before/after images
whose hashes verify with the chain.

### 2.5 Oversight verdict benchmark
The LLM reviewer's *quality* is unmeasured (VALIDATION limitation 6). Build
the eval: a labeled set of proposed actions (from the feedback store plus
authored adversarial cases), scored per reviewer (rules / Claude / GPT) for
false-allow and false-deny rates. Ship the harness so every lab can
benchmark before trusting.
**Accept when:** `labaiagent bench-reviewer` prints a confusion matrix per
configured reviewer and the false-allow rate on the adversarial set is
published in VALIDATION.md.

### 2.6 Standards bridge, both directions
SiLA 2 client exists; add **SiLA 2 server mode** (expose every LabAIAgent
device as a SiLA service) and AnIML/ASM data export (1.5), making it the
interoperability hub rather than another island.
**Accept when:** a third-party SiLA client operates a simulated device
through the safety engine with the actuation audited.

### 2.7 Live protocols.io / vendor-document ingestion
The trusted-source registry gates domains; the next step is fetching: pull
a DOI'd protocols.io protocol or an allowlisted vendor PDF, parse to a
*draft* template with citations auto-attached, and require human curation
before it enters the library (the registry stays non-extensible at
runtime).
**Accept when:** a protocols.io DOI becomes a draft template with its
provenance intact and cannot be executed until a human promotes it.

---

## Tier 3 — assurance and ecosystem (compounding)

- **3.1 Formal model of the safety kernel:** TLA+/model-check the
  e-stop/state/approval/suspension state machine; publish the spec. Very
  few lab systems can say "the interlock logic is model-checked."
- **3.2 External security assessment:** fuzz the HTTP surface (the parser
  caps and 411 handling are tested but not fuzzed), commission a pen test,
  ship SBOM + signed releases (sigstore), pin dependencies.
- **3.3 Compliance binder:** IQ/OQ/PQ templates, SOP skeletons, a 21 CFR
  Part 11 / ALCOA+ control-mapping document referencing the specific
  mechanisms (keyed audit, e-signatures, roles) — what a QA office needs
  to adopt this in a regulated study.
- **3.4 Observability:** OpenTelemetry spans around `LabSession.call`,
  Prometheus `/metrics`, structured JSON logs.
- **3.5 Distribution:** PyPI release, mkdocs documentation site, driver
  authoring tutorial, community template/driver contribution pipeline with
  the conformance suite as the merge gate.
- **3.6 Code health:** split `gateway/registry.py` (~900 lines) into
  handlers/specs/dispatch modules; audit-log tail index; `LabClient`
  retry/backoff; paginated `get_audit_log`.

---

## Sequencing recommendation

1.1 → 1.3 → 1.5 first (validate hardware, give humans their surface, make
data provenance real): that combination is "ready to use in the lab."
Then 2.1 → 2.2 (calibrated twins gating closed-loop optimization): that
combination — provenance-complete, oversight-gated, simulate-first,
closed-loop — is the configuration no commercial scheduler or academic
framework currently offers together, and it is the spine of a strong
methods paper: *validated safety architecture + calibrated twin gating +
closed-loop campaign, with every byte of provenance walkable.*
