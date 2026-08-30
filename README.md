# LabAIAgent

**The universal AI-agent gateway for laboratory instruments.**

A vendor-neutral layer that makes lab instruments discoverable, safely
operable, and auditable by **any** AI agent — Claude over MCP, OpenAI and
Gemini over native tool calling, LangChain/LangGraph, LlamaIndex, CrewAI,
AutoGen, smolagents, or anything that speaks HTTP — one instrument at a
time, through one safety engine, onto one tamper-evident audit trail.

---

## The design problem

Connecting an AI agent to lab hardware is not primarily a protocol problem.
Four things make it hard, and a system that ignores any of them fails in the
same predictable way:

**Physical actions are not idempotent.** A software tool call can be retried;
an aspirate cannot be un-aspirated, and a dropped plate ends the experiment.
So the cost of a false negative in a safety check is unbounded, and every
check here is fail-closed.

**Instruments do not speak one protocol.** A 2019 plate reader exports CSV to
a watched folder. A Windows-only SDK is reachable only through COM. Anything
from before ~2010 is RS-232. A layer that only supports modern REST
instruments covers the minority of a real lab.

**Agents do not speak one protocol either.** Claude wants MCP, OpenAI wants a
functions array, LangGraph wants BaseTools, a scheduler wants plain REST. A
lab layer welded to one agent runtime is obsolete with the next model
release.

**Adding instrument N+1 — or agent framework N+1 — must not touch 1..N.**
Otherwise integration cost grows superlinearly and the project stalls. The
entire architecture is organised around this constraint, on both sides.

---

## Architecture

```
  Claude (MCP stdio / MCP over HTTP)   ┐
  OpenAI / Gemini (native tool calls)  │   ┌────────────────────────────────┐
  LangChain · LlamaIndex · CrewAI      ├──►│         AGENT GATEWAY          │
  AutoGen · smolagents · plain Python  │   │  20 fixed tools, rendered      │
  REST + OpenAPI 3.1 (anything else)   ┘   │  mechanically per runtime      │
                                           │  API keys · roles · per-actor  │
                                           │  ceilings · async jobs · SSE   │
                                           └───────────────┬────────────────┘
                                                           │
                                           ┌───────────────▼────────────────┐
                                           │           LabSession           │
                            Python code ──►│  THE single invocation path.   │
                            Protocols   ──►│  Nothing reaches hardware      │
                            CLI         ──►│  except through here.          │
                                           └───────────────┬────────────────┘
                                                           │
                             ┌─────────────────────────────┼──────────────┐
                             ▼                             ▼              ▼
                      ┌─────────────┐              ┌──────────────┐ ┌──────────────┐
                      │   Safety    │              │  Scheduler   │ │  Audit log   │
                      │  6 layers,  │              │  per-device  │ │ hash-chained │
                      │ fail-closed │              │   locking    │ │   ALCOA+     │
                      └─────────────┘              └──────────────┘ └──────────────┘
                                                           │
                                           ┌───────────────▼────────────────┐
                                           │        Driver contract         │
                                           │  @read / @write / @procedure   │
                                           │  → manifest, limits, reference │
                                           │    sheet, all from one decl.   │
                                           └───────────────┬────────────────┘
                                                           │
                        ┌──────────┬──────────┬────────────┼──────────┬──────────┐
                        ▼          ▼          ▼            ▼          ▼          ▼
                     serial      TCP        HTTP      filewatch     COM       SiLA 2
                     RS-232    sockets      REST     (no API at   Windows    where the
                                                       all)        SDKs     vendor has it
```

Two load-bearing ideas:

1. **A driver author describes the instrument once, declaratively, and
   everything else is derived** — argument validation, the JSON manifest, the
   natural-language reference an agent reads, every framework's tool schema,
   and the audit record all come from the same decorators.
2. **The agent-facing surface is one fixed registry of twenty tools**, and
   every runtime adapter is a mechanical rendering of it. Adding an
   instrument changes no tool schema; adding an agent framework is a
   ~100-line shim that funnels into the same dispatch.

---

## Quickstart

```bash
pip install -e .
python examples/01_bca_assay.py            # 4 instruments, real analysis
python examples/02_qpcr_and_safety.py      # every safety layer, deliberately tripped
python examples/03_add_new_instrument.py   # onboarding a novel instrument
python examples/04_universal_agents.py     # one lab, every agent runtime
python examples/05_knowledge_and_oversight.py  # literature → workflow, e-sign, oversight
python examples/06_memory_and_recovery.py      # restart continuity + intelligent recovery
python examples/07_operations_and_provenance.py # parallel runs, signed records, watchdog
python -m pytest tests/ -q                 # 232 tests incl. property-based
```

```python
from labaiagent import LabSession

session = LabSession.from_config("config/example_lab.yaml")
session.connect_all()

session.read("cycler", "block_temperature")             # 22.0
session.write("cycler", "block_temperature", value=95)  # checked, then actuated
session.run("lh", "transfer", source_barcode="P1", source_well="A1",
            dest_barcode="P1", dest_well="B1", volume=50.0)
```

---

## Connecting agents

### Claude (MCP)

```bash
labaiagent serve --lab config/example_lab.yaml --readonly    # start here
labaiagent serve --lab config/example_lab.yaml --dry-run     # then this
labaiagent serve --lab config/example_lab.yaml               # only then this
```

Claude Desktop / Claude Code config:

```json
{"mcpServers": {"lab": {
    "command": "labaiagent",
    "args": ["serve", "--lab", "/abs/path/config/example_lab.yaml", "--readonly"]}}}
```

Works with the official `mcp` SDK when installed (`--sdk`) and falls back to
a dependency-free stdio JSON-RPC loop when it is not — because instrument PCs
are often machines you cannot freely `pip install` on. The server also
exposes MCP **resources** (`lab://manifest`, `lab://reference`,
`lab://audit/tail`) so clients can load lab context without spending tool
calls.

### Anything over HTTP (OpenAI, Gemini, remote MCP, dashboards, cron)

```bash
labaiagent serve --lab config/example_lab.yaml --http --port 8859 \
                 --auth config/principals.yaml
```

One port serves: `POST /tools/{name}` (REST), `GET /openapi.json`
(OpenAPI 3.1 — import it into anything), `POST /mcp` (MCP JSON-RPC over
HTTP for remote MCP clients), and `GET /events` (Server-Sent Events: device
state changes, job progress, approvals).

Non-loopback binding **requires** an authenticator: an unauthenticated lab
control API on the network is not a configuration, it is an incident. Add
`--tls-cert server.pem --tls-key key.pem` for HTTPS (TLS ≥ 1.2); failed-key
brute forcing is locked out per source address before any key comparison.

### Native tool-calling (no server needed)

```python
from labaiagent.gateway import schemas
schemas.to_openai_tools()      # tools= for chat.completions / Responses
schemas.to_anthropic_tools()   # tools= for the Messages API
schemas.to_gemini_tools()      # Gemini function declarations
```

```python
# Execute what the model asked for -- one line per runtime:
from labaiagent.integrations import openai_tools, anthropic_tools
messages += openai_tools.execute_tool_calls(session, response)
blocks   = anthropic_tools.execute_tool_uses(session, response)
```

### Agent frameworks

```python
from labaiagent.integrations.langchain_tools  import get_tools   # LangChain / LangGraph
from labaiagent.integrations.llamaindex_tools import get_tools   # LlamaIndex
from labaiagent.integrations.crewai_tools     import get_tools   # CrewAI
from labaiagent.integrations.autogen_tools    import get_functions  # AutoGen / AG2
from labaiagent.integrations.smolagents_tools import get_tools   # HF smolagents
from labaiagent.integrations import make_callables               # anything else
```

Every one of these is a thin shim over the same registry and the same
dispatch. None adds a second path to hardware.

### Remote client SDK

```python
from labaiagent.client import LabClient
lab = LabClient("http://lab-pc:8859", api_key="lak_...")
lab.read("reader", "read_count")
job = lab.call("run_procedure", device_id="cycler", capability="run_qpcr",
               arguments={"cycles": 40}, mode="async")
lab.wait_job(job["result"]["job_id"], timeout=7200)
```

---

## The agent surface: 20 tools, fixed

`list_devices` · `describe_device` · `lab_reference` · `snapshot` ·
`read_state` · `write_state` · `run_procedure` · `run_protocol` ·
`emergency_stop` · `get_audit_log` · `get_job` · `cancel_job` · `list_jobs` ·
`request_approval` · `search_literature` · `list_protocol_templates` ·
`instantiate_protocol_template` · `submit_feedback` · `lab_tasks`

An agent given 400 generated tools chooses badly; one given
`list_devices → describe_device → call` navigates a lab of any size with the
same competence. Reads and writes are separate tools, so `read_state` cannot
actuate under any circumstance and a read-only agent can be handed the read
tools alone.

**Long operations run as jobs.** A qPCR program takes ninety minutes; no tool
call should. `write_state` / `run_procedure` / `run_protocol` accept
`mode: "async"` and return a `job_id` immediately; `get_job` polls state,
progress and the result; `cancel_job` requests a cooperative stop — between
steps, never mid-actuation (for a hard stop there is `emergency_stop`, which
latches). Anything estimated over 5 minutes is refused in sync mode with a
pointer to `mode: "async"`.

---

## Identity: who is actually calling?

The audit trail is only as trustworthy as its `actor` field, so networked
callers authenticate. `config/principals.yaml` maps API keys to verified
principals with four roles — `observer` (read-only), `operator` (actuate up
to the ceiling), `approver` (may mint approval tokens: the *human* role that
unlocks remote approvals from a phone or dashboard), `admin` — plus optional
**per-actor autonomy ceilings** (an identity can only be *more* restricted
than the session, never less) and **per-actor rate limits** (one runaway
agent exhausts its own budget, not everyone else's).

An agent cannot mint its own approval: `request_approval` requires the
`approver` role. Anyone, including a read-only observer, can call
`emergency_stop` — stopping is always safe.

---

## Adding instrument N+1

This is the part the whole system is built around.

```bash
labaiagent scaffold acme.pump --category pump --transport serial
# → acme_pump.py + test_acme_pump.py, with decorators, limits and tests in place
```

Fill in three lifecycle hooks and your capabilities:

```python
@register_driver("acme.pump")
class AcmePump(Device):
    vendor, model, category = "Acme", "P-200", "pump"

    notes = """
    Below 5 uL/s the check valve chatters and delivery is unreliable.
    The prime cycle must run after any reservoir change or the first
    three dispenses are short.
    """                      # ← what you'd tell a new postdoc. Agents read this.

    def _connect(self):    self._link.open()
    def _disconnect(self): self._link.close()
    def _halt(self):       self._link.request("ABORT")

    @read("flow_rate", unit="uL/s", description="Current flow rate")
    def get_flow(self) -> float:
        return float(self._link.request("FLOW?"))

    @write("flow_rate", risk=Risk.MEDIUM,
           params=[Param("value", float, "uL/s", limits=Range(5, 200, "uL/s"))])
    def set_flow(self, value: float) -> None:
        self._link.request(f"FLOW {value:.2f}")
```

Then gate it:

```bash
labaiagent verify acme_pump.py:AcmePump --level strict
```

The conformance suite runs 16 checks and either passes or tells you exactly
what is wrong — including limits that are declared but not enforced (both
ends of every range are probed), writable setpoints with no readback, and a
`_halt()` left unimplemented on something that moves.

Finally, a five-line stanza in your lab YAML:

```yaml
- id: pump_1
  driver: acme.pump
  config:
    port: /dev/ttyUSB0
    limits:
      "write:flow_rate":
        value: {low: 10.0, high: 120.0, unit: uL/s}   # narrow for THIS unit
```

**Nothing else changes.** Not the tool registry, not any framework's schema,
not the safety engine, not protocols you already wrote. Config may only ever
*tighten* a driver-declared limit, never widen one.

---

## Safety model

Six layers, evaluated in order, all fail-closed:

| # | Layer | Blocks |
|---|-------|--------|
| 1 | **Emergency stop** | Latching, process-wide. No token clears it — only a named human. Reads stay permitted. On reset, every halted device must pass its self-test before returning to service; failures land in ERROR, not IDLE. |
| 2 | **State gate** | Capability invoked while the instrument is in an incompatible state |
| 3 | **Parameter limits** | `Range` / `OneOf` / `Pattern` / `Length`, declared per argument |
| 4 | **Interlocks** | Live cross-device preconditions. An interlock that cannot be evaluated blocks. |
| 5 | **Rate limits** | Sliding window, per actor. The characteristic agent failure is the same marginal command a thousand times. |
| 6 | **Approval tokens** | Scoped to one device + capability, expiring, single-use. Required above the *effective* ceiling — the lower of the session's and the calling identity's. |

Every refusal is a structured payload naming the constraint and the permitted
range — when the caller is a model, the error text is the entire repair
signal:

```json
{
  "error": "LimitViolation",
  "message": "speed: value 500 percent outside permitted range [5, 100] percent",
  "parameter": "value", "value": 500, "permitted": "between 5 and 100 percent",
  "retryable": false,
  "guidance": "This was blocked by a safety rule, not a transient fault.
               Do not retry the same call."
}
```

---

## Audit trail

Every invocation, **every refusal**, every job transition, every snapshot,
and every approval is appended to a hash-chained JSONL log. Each record
embeds the SHA-256 of its predecessor, so editing or deleting anything
invalidates every subsequent link:

```bash
labaiagent audit runs/audit.jsonl verify
# INVALID: record 5 has been modified after writing
```

The chain is re-verified whenever an existing log is opened; a log that
fails verification stays readable for forensics but **refuses to accept new
records** — extending a broken chain would launder the break.

This gives you the records-and-signatures substrate 21 CFR Part 11 asks for
and satisfies ALCOA+ attributability. It is *not* by itself a compliance
claim — that needs validation, access control and SOPs around this file.

---

## Protocols

For anything you already know how to do, emit a protocol rather than driving
the instruments turn by turn. It is validated statically before anything
moves, it checkpoints after every step, it can be cancelled cooperatively
between steps, and it is a reviewable, diffable, version-controllable
artifact.

```python
proto = Protocol("bca_assay")
proto.step("dilute", "lh", "proc:serial_dilution", args={...})
proto.step("move",   "arm", "proc:move_labware",
           args={...}, depends_on=("dilute",), approval=token)
proto.step("read",   "reader", "proc:read_absorbance",
           args={"wavelength": 562.0}, depends_on=("move",), store_as="data")

problems = proto.validate(session)   # unknown devices, bad args, cycles,
                                     # missing approvals — before any motion
proto.run(session)                   # or run_protocol(mode="async") over any adapter
```

The division of labour this implies: **let the model explore interactively,
then have it emit a protocol; review the protocol; run the protocol
deterministically.** The model writes the controller. It is not the
controller.

---

---

## Knowledge with provenance

Agents plan from evidence, not from the open web. `search_literature`
queries **PubMed/MEDLINE only** — every record carries a PMID by
construction — and the curated protocol library holds versioned method
templates whose citations are *forced* to come from the trusted-source
registry (PubMed-indexed journals plus allowlisted pharma/vendor publishers:
Thermo Fisher, MilliporeSigma, NEB, QIAGEN, Promega, Bio-Rad, Roche,
Agilent, protocols.io, Opentrons, NIH/CDC/WHO/FDA). A provenance-free
template cannot even be constructed. `instantiate_protocol_template`
translates a template into *this* lab's workflow — parameters
limit-checked, device categories bound to real instruments, approvals
flagged, static validation attached — and returns a reviewable document;
nothing runs until `run_protocol`. Abstracts are handled as third-party
text, never as instructions.

---

## Agent oversight and RLHF

The safety engine judges each call; the **Supervisor** judges the pattern.
A burst of safety refusals — an agent arguing with the limits — suspends
that identity automatically (reads and the e-stop survive; only a named
human reinstates). HIGH/CRITICAL actuation passes a pre-execution
**reviewer**: rule-based by default, or an independent foundation model
(Claude / GPT via `policy.oversight.reviewer: anthropic|openai`) that can
veto the call — and fails **closed** if it is unreachable. Every human
judgement — approvals minted, jobs cancelled, e-stops, suspensions, and
explicit `submit_feedback` ratings — accumulates in a preference store
exportable as an RLHF/DPO-ready dataset (`FeedbackStore.to_dpo_pairs()`)
for tuning your agent models on your training infrastructure; no training
happens in-process, by design.

Individual **e-signature passwords** (PBKDF2, per person, re-entered at the
moment of signing) gate approval minting: the API key authenticates the
connection, the password authenticates the person — the two-component
control electronic-signature regulation expects.

---

## Consistent memory and intelligent recovery

The lab **remembers across restarts**. A durable memory (SQLite, beside the
audit log) holds the task board, incidents, device quarantines, agent
suspensions, and per-task protocol checkpoints. On every startup the server
prints — and any agent can query via `lab_tasks` — *where the lab stopped
and where it was going*: interrupted tasks resume first, from their
checkpoints (`run_protocol(task_id=..., resume=true)` skips completed
steps). Directives are human acts: only approver/admin principals add or
cancel tasks and resolve incidents; agents list, execute, and update
progress.

When something goes **physically wrong** mid-experiment, the system responds
the way a good tech would, not with a bare stack trace: the failing
instrument is **quarantined** (actuation refused, reads and other devices
unaffected — and the quarantine survives restarts), an **incident** is
opened, and the agent receives a **diagnosis** built from the driver's own
operating notes with concrete recommended actions — starting with *do not
retry; a physical fault does not clear by repetition*. A named human
resolving the incident is what returns the device to service. Failed task
runs keep their checkpoints, so after the repair the work continues from
where it stopped.

---

## Operations: dashboard, metrics, watchdog, provenance

Running `labaiagent serve --http` also serves an **operator console** at
`/` — a single self-contained HTML page (no build step, no CDN; it renders on
an air-gapped lab network): live device states, the durable task board, open
incidents and quarantines, jobs, the event stream, and the E-STOP button. The
page holds no privileged path: every byte it shows travels through the same
authenticated `/tools/*` endpoints agents use, and the API key never leaves a
JavaScript variable (no localStorage, no cookies, no URLs).

`/metrics` exposes Prometheus text-format gauges (devices by state, e-stop
latch, jobs, tasks, open incidents, quarantines, audit sequence head) for the
Grafana/Alertmanager stack a real deployment already runs. `--watchdog N`
starts a heartbeat monitor that probes each idle instrument's own self-test
every N seconds: three consecutive failures mark it ERROR (new actuation is
refused by the state gate) and publish `device.heartbeat_lost`; recovery is
automatic when the instrument answers again. The watchdog never probes a BUSY
device and never fires during an e-stop.

Every completed `run_protocol` writes a **signed run record** — protocol as
executed with per-step arguments/results/timings, instruments and driver
versions, software stack, actor, and the run's audit slice with hashes intact
— checksummed and HMAC-signed under the audit key, exportable and verifiable
via the `run_records` tool. That file is what a methods section cites and
what a batch record attaches; `docs/COMPLIANCE.md` maps it (and the rest of
the controls) clause-by-clause onto 21 CFR Part 11 and GAMP 5, and
`docs/VALIDATION_PLAN.md` is the fill-in IQ/OQ/PQ a pilot lab executes on
physical hardware.

Protocols spanning several instruments can opt into **parallel execution**
(`run_protocol(..., parallel=true)`): dependency-ready steps run concurrently
grouped by device — distinct instruments overlap, same-instrument steps stay
strictly ordered, and the safety engine's per-call gating is unchanged. On
real hardware, the Opentrons driver speaks the robot-server's **live
atomic command API** (verified against the published client wire format):
`load_pipette`, `load_labware`, `pick_up_tip`, `aspirate`, `dispense`,
`drop_tip`, `home` — so volume limits, interlocks, rate limits, approvals and
audit apply to *every single liquid movement*, not to an opaque two-hour
protocol blob. SCPI writes are verified: after each setpoint the
error queue is drained, and a silent rejection becomes a `PhysicalError` that
routes into the incident intelligence.

---

## Security model

Defense in depth, wire to bench: TLS → hashed API keys (constant-time,
brute-force lockout) → roles → per-actor autonomy ceilings and rate limits
enforced *inside* the safety engine (protocols and async jobs carry the
submitter's verified identity into every step) → six fail-closed safety
layers on the single invocation path → a hash-chained audit log that can be
**HMAC-keyed** (`LABAIAGENT_AUDIT_HMAC_KEY`) so even a whole-file rewrite
without the key is detectable. The full model, including residual risks we
do NOT defend against, is in `docs/THREAT_MODEL.md`; the adversarial review
record — every finding, fix, and named regression test — is `docs/REVIEW.md`;
the test evidence and its honest limits are `docs/VALIDATION.md`.

---

## What this does not do

Stated plainly, because a control layer that overstates its scope is
dangerous:

- **Not real-time control.** Anything needing a closed loop faster than
  ~100 ms belongs in firmware or a PLC, with LabAIAgent supervising.
- **Not motion planning.** Point it at a robot's own controller, or ROS 2.
- **Not a safety-certified system.** The interlocks are a software layer
  *above* your hardware E-stop and interlocked enclosures — never a
  substitute.
- **Not a validated GxP system.** It produces defensible records; validating
  them for a regulated workflow is still your IQ/OQ/PQ.
- **The simulators are not digital twins.** The physics is plausible, not
  predictive.

---

## Repository layout

```
labaiagent/
  core/
    types.py         units, limits, Param, Risk — stdlib only
    capability.py    @read/@write/@procedure; read/write may share a name
    device.py        the driver contract + per-instance config refinement
    safety.py        e-stop, interlocks, per-actor rate limits & ceilings
    audit.py         hash-chained tamper-evident log, verified on resume
    registry.py      driver registration, entry-point plugins, YAML loading
    conformance.py   16-check integration gate
  transports/        serial · TCP · HTTP · filewatch · COM · SiLA 2 · subprocess
  drivers/
    simulated/       6 instruments on one shared physical world
    hardware.py      Opentrons Flex · generic SCPI · watched-folder reader
  orchestration/
    session.py       LabSession — the single invocation path
    workflow.py      Protocol DAG, validation, checkpointing, cancellation
    jobs.py          async job engine for long-running operations
  gateway/
    registry.py      the 20 tools + dispatch — the one agent surface
    auth.py          principals, roles, API keys
    schemas.py       OpenAI / Anthropic / Gemini / OpenAPI exporters
    rest.py          REST + MCP-over-HTTP + SSE events server (stdlib)
    events.py        event bus (device state, jobs, approvals)
  integrations/      LangChain · LlamaIndex · CrewAI · AutoGen · smolagents ·
                     OpenAI & Anthropic executors · plain callables
  mcp/server.py      MCP adapter: stdio (SDK or dependency-free) + resources
  client.py          LabClient — Python SDK for a remote gateway
  templates/         scaffolding for new drivers
  cli.py             scaffold · verify · doctor · describe · serve · schemas ·
                     run · audit · drivers
```

---

## Status

Simulators and framework fully exercised: **232 tests** (including Hypothesis
property tests and a named regression test for every finding of four
adversarial review rounds — see `docs/REVIEW.md`), all nine drivers
strict-conformance-clean, the HTTP gateway tested end-to-end over real
sockets, and the Opentrons/SCPI wire contracts verified against fakes
speaking the published protocols. The hardware drivers **have not been run
against physical instruments** — each carries a "verify on first connect"
list, and `docs/VALIDATION_PLAN.md` is the IQ/OQ/PQ a pilot site executes
to close that gap.

Recommended rollout: `--readonly` → `--dry-run` → live with
`autonomy_ceiling: low` → raise to `medium` only once the audit log shows you
what the agent actually does.

Start with `labaiagent doctor --lab your_lab.yaml` after each instrument you
add. The forward plan, with acceptance criteria per item, is `docs/ROADMAP.md`.

---

## Release engineering

Apache-2.0 licensed (`LICENSE`). Versioned semantically (`CHANGELOG.md`). CI
runs ruff, mypy (strict on the public surface, `py.typed` shipped) and the
test suite on Python 3.10–3.13, Linux and Windows, then builds and smoke-tests
the wheel in a clean environment. `CONTRIBUTING.md` states the eight
non-negotiable invariants and requires two reviews — one adversarial, documented —
for any change on a safety path; `docs/RELEASE_PROCESS.md` is the G1–G7
release-gate checklist (triple-run flake gate, clean-venv install, security
regression, review-debt zero, field-impact notes for validated sites);
`SUPPORT.md` fixes version support windows and a deprecation policy under
which safety behavior never loosens outside a MAJOR. Deployment hardening
guidance lives in `SECURITY.md`.

---

## License, citation, and author

Apache-2.0 licensed (`LICENSE`). If you use LabAIAgent in your research,
please cite it — GitHub's "Cite this repository" button uses `CITATION.cff`.

Created and maintained by **Md Babu Mia, PhD** (BioMedsAI) — see `AUTHORS.md`.
