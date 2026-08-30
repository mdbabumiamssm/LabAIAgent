# Changelog

All notable changes to LabAIAgent are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and the project follows
[Semantic Versioning](https://semver.org/).

## [1.4.1] — 2026-08-29

Round-4 hardening release: an independent adversarial review of the v1.4.0
operations layer (12 accepted findings, 2 critical), every fix carrying a
named regression test, plus the corporate development-process package.

### Fixed (safety-relevant — full detail in REVIEW.md, round 4)

- **Parallel abort/cancel (critical, 4.1)**: parallel wave lanes now check a
  shared stop flag and `should_cancel` before EVERY step; a FAILED+ABORT
  step or a cancellation stops all lanes at the next step boundary instead
  of letting other instruments keep actuating through the wave.
- **Double actuation via retry (critical, 4.2)**: once an Opentrons live
  command is enqueued, nothing transient can escape to the session retry
  layer (which would re-enqueue the physical action). Poll blips are
  absorbed; a poll timeout is now `PhysicalError` ("enqueued and may still
  execute — do not re-send"); the protocol-run poll timeout is a
  non-retryable `TransportError`.
- **Provenance false tamper alarms (4.3)**: record bodies are canonical-JSON
  normalised before sealing, so records containing datetime/Path/tuple step
  results verify correctly instead of reading as tampered.
- **Watchdog probe race (4.4)**: probes now hold the session's per-device
  lock with state and e-stop re-checked inside it; a device whose lock is
  busy is skipped that round — probe traffic can no longer interleave with
  an in-flight actuation.
- **Watchdog over-recovery (4.5)**: recovery restores only errors carrying
  the watchdog's own trip prefix and never a supervisor-quarantined device —
  heartbeat can no longer bypass human-released quarantine.
- **Early-cancelled jobs (4.6)**: a protocol job cancelled while queued now
  settles properly (audit record, `on_done`, run record, task status) instead
  of leaving its task stuck `in_progress` forever.
- **Crash-resume with `$refs` (4.7)**: checkpoints persist context values and
  step results; resumed protocols that pass data between steps no longer
  abort on the first `$ref`.
- **Stale Opentrons live run (4.8)**: `_halt` stops the live command run too
  and clears the cache; a failed enqueue clears it — no more posting into a
  dead run after an e-stop.
- **SCPI stale-error blame (4.9)**: the error queue is drained before each
  verified write, so pre-existing instrument errors are no longer attributed
  to the current setpoint (false quarantine of a healthy instrument).
- **Dashboard attribute escaping (4.10)**: `esc()` escapes quotes; state CSS
  classes are whitelisted tokens — closes an attribute-breakout XSS vector
  for malicious state strings.
- Metrics render large counters exactly (4.12); `GatewayServer.stop()` gains
  opt-in `shutdown_jobs=True` (4.11).

### Added

- **Corporate SDLC package**: `CONTRIBUTING.md` (the eight non-negotiable
  invariants, two-reviewer rule with a documented adversarial attempt for
  safety-path changes, driver-PR checklist), `docs/RELEASE_PROCESS.md`
  (versioning policy and the G1–G7 release-gate checklist, including
  triple-run flake gate and field-impact notes for validated sites),
  `SUPPORT.md` (version support windows, deprecation policy — safety never
  loosens outside a MAJOR — severity/response targets).
- REVIEW.md round-4 record: 12 findings, resolutions, regression tests.

### Tests

- 14 new regression tests (`TestRound4Regressions` + SCPI semantics).
  Total: 232, verified over three consecutive full-suite runs.

## [1.4.0] — 2026-08-29

Industrial-operations release: the screen, the evidence, and real hardware.

### Added

- **Operator dashboard** served by the gateway at `GET /`: one self-contained
  HTML console (no build step, no CDN, renders air-gapped) — live device
  states, durable task board, incidents and quarantines, jobs, event stream
  (fetch-streamed SSE with proper auth headers), and the E-STOP button. The
  shell carries no data and no privileged path: everything flows through the
  same authenticated `/tools/*` endpoints agents use; the API key stays in a
  page variable, never in storage or URLs. Strict CSP on the page.
- **Signed run-record provenance** (`labaiagent.provenance`): every completed
  `run_protocol` (sync, async, and failed runs) writes a self-contained JSON
  record — protocol as executed, per-step results and timings, device and
  driver versions, software stack, actor, and the run's audit slice with
  hashes intact — checksummed (SHA-256) and HMAC-signed under the audit key.
  New tool **`run_records`** (tool 20: list/get/verify). Stored in
  `run_records/` beside the audit log.
- **Parallel DAG execution**: `run_protocol(..., parallel=true)` (and
  `Protocol.run(parallel=True)`) runs each wave of dependency-ready steps
  concurrently, grouped by device — distinct instruments overlap,
  same-instrument steps stay strictly ordered, per-call safety unchanged.
  Context and checkpoints are lock-guarded; the audit log was already
  thread-safe.
- **Opentrons live atomic commands**: the Flex/OT-2 driver now drives the
  robot-server command API directly (`POST /runs/{id}/commands`, verified
  against the published client wire format): `load_pipette`, `load_labware`,
  `pick_up_tip`, `aspirate`, `dispense`, `drop_tip`, `home` — each one a
  separately safety-gated, audited action with real volume/flow limits. A
  failed robot command surfaces as `PhysicalError` carrying the robot's own
  errorType/detail and routes into incident intelligence. The old
  protocol-upload path remains for validated repeated assays.
- **SCPI verified writes**: after every setpoint the error queue is drained
  (`SYST:ERR?` until clear, bounded); a silently rejected setpoint becomes a
  `PhysicalError` instead of a wrong experiment. New `read:error_queue`.
  Disable per-instrument with `config.verify_writes: false`.
- **Heartbeat watchdog** (`labaiagent serve --http --watchdog N`): probes
  each idle instrument's own self-test every N seconds; 3 consecutive
  failures mark it ERROR (state gate refuses new actuation), publish
  `device.heartbeat_lost`, and audit it; recovery is automatic and audited.
  Never probes BUSY devices; never runs during an e-stop.
- **Prometheus metrics** at `GET /metrics` (text format 0.0.4): devices by
  state, e-stop latch, jobs, tasks, open incidents, quarantines, run-record
  count, audit sequence head, per-device invocation counters.
- **Compliance package**: `docs/COMPLIANCE.md` — clause-by-clause 21 CFR
  Part 11 and GAMP 5 mapping with stated gaps; `docs/VALIDATION_PLAN.md` —
  executable IQ/OQ/PQ template for on-instrument validation at pilot sites.
- `AuditLog.seq` (thread-safe sequence head) for provenance slicing.

### Fixed

- The Opentrons driver's notes wrongly claimed live per-well pipetting is
  impossible over HTTP; it is (the setup-command mechanism), and the driver
  now implements it.

### Tests

- 22 new tests (`tests/test_v14.py`): Opentrons wire-shape and failure
  mapping, safety gating before transport, SCPI error-queue behaviour,
  parallel overlap/serialisation/abort semantics, run-record integrity and
  tamper detection (wrong-key included), watchdog trip/recover/skip rules,
  dashboard CSP, metrics format, 20-tool surface. Total: 218.

## [1.3.0] — 2026-08-29

Continuity and recovery release: the lab remembers, and it reacts.

### Added

- **Durable lab memory** (`labaiagent.memory.LabMemory`): SQLite (WAL,
  stdlib-only) beside the audit log. Tasks, incidents, device quarantines,
  agent suspensions, and key-value state all survive restarts; the startup
  report answers "where did I stop, where was I going" and is printed at
  boot and queryable by agents.
- **Task board** (`lab_tasks`, tool 19): humans (approver/admin) record and
  cancel directives — optionally carrying a protocol document — and resolve
  incidents; agents list (interrupted-first `next_task`), execute, and
  update progress. Task add/cancel and incident resolution are audited.
- **Crash-resumable protocol execution**: `run_protocol(task_id=...,
  resume=true)` binds a run to a task, checkpoints every step to a
  server-derived path (never caller-supplied), auto-updates task status on
  settlement (sync and async), and resumes after a crash skipping completed
  steps. Failed runs keep their checkpoint for post-repair resumption.
- **Incident intelligence**: a PhysicalError — sync or inside an async job —
  quarantines the device (persistently; reads and other instruments stay
  live), opens a persistent incident, and attaches a rule-based diagnosis to
  the failure payload: the driver's own operating notes, recent audit
  context, and recommended actions beginning with "do NOT retry". Protocols
  and direct actuation on a quarantined device are vetoed; only a named
  human resolving the incident (which lifts the quarantine when no others
  remain open) returns it to service.
- Supervisor suspensions and quarantines persist via memory: a restart never
  amnesties a suspended agent or frees a jammed instrument.
- Example 06 (crash → remember → resume → fault → quarantine → human fix →
  back to work, all on one verified audit chain); 15 new tests (196 total).

The fixed tool surface grew once, 18 → 19, and remains fixed.

## [1.2.0] — 2026-08-29

Knowledge, identity, and oversight release.

### Added

- **Knowledge layer** (`labaiagent.knowledge`): PubMed-only literature
  browser over NCBI E-utilities (rate-limited, offline-testable, every hit
  PMID-carrying); a trusted-source registry (PubMed-indexed journals +
  allowlisted pharma/vendor publishers, suffix-safe domain matching); a
  curated protocol-template library whose citations are FORCED through the
  registry (Smith 1985 PMID:3843705 BCA; MIQE PMID:19246619 qPCR), with
  limit-checked parameters and category→device binding that translates a
  published method into this lab's validated, reviewable workflow.
  New tools: `search_literature`, `list_protocol_templates`,
  `instantiate_protocol_template` (all read-only/observer).
- **Individual e-signature passwords**: per-principal PBKDF2 (200k
  iterations, salted, constant-time); when a person has a password on file,
  minting an approval requires it re-entered per signature; failures are
  audited. `labaiagent hash-password` generates entries.
- **Agent oversight** (`labaiagent.oversight`): Supervisor with
  refusal-streak anomaly detection and automatic per-identity suspension
  (reads + e-stop survive; only a named human reinstates — no tool exists
  for it); pre-execution reviewers for HIGH/CRITICAL actuation — rule-based
  default, or a **foundation-model reviewer** (Anthropic/OpenAI SDKs) that
  fails CLOSED on any error; oversight verdicts and suspensions audited.
  On by default for networked servers; configurable via
  `policy.oversight` in the lab YAML.
- **RLHF feedback substrate**: every human judgement (approvals granted,
  cancellations, e-stops, suspensions, `submit_feedback` ratings — the new
  approver-gated tool) accumulates in a JSONL preference store with
  `export_jsonl()` and `to_dpo_pairs()` for preference-tuning agent models
  on external training infrastructure. Deliberately no in-process training.
- Example 05 (`knowledge_and_oversight`) demonstrating all of the above
  offline; 27 new tests (181 total).

The fixed tool surface grew once, 14 → 18, and remains fixed.

## [1.1.0] — 2026-08-29

Security-layer release, after a second adversarial review round
(`REVIEW.md` has the complete record; `docs/THREAT_MODEL.md` the model).

### Added — defense in depth

- **Keyed audit chaining (HMAC-SHA256)**: `AuditLog(hmac_key=...)` or the
  `LABAIAGENT_AUDIT_HMAC_KEY` env var. Detects whole-file rewrites, which an
  unkeyed hash chain cannot (that weakness is now documented, not implied
  away). Wrong-key and forged-rewrite cases are regression-tested.
- **Native TLS** on the gateway (`serve --http --tls-cert --tls-key`,
  TLS ≥ 1.2), HTTPS support with CA pinning in `LabClient`, and a loud
  warning when serving plaintext on a non-loopback interface.
- **Brute-force lockout**: 10 failed API keys per minute from one address →
  HTTP 429 for 60 s, decided before any key comparison; `X-Forwarded-For`
  deliberately untrusted.
- **Hardened HTTP**: `X-Content-Type-Options: nosniff`,
  `Cache-Control: no-store`, `Referrer-Policy: no-referrer` on every
  response; 500-step protocol cap; correct `Retry-After` on 429.
- **Property-based tests** (Hypothesis): limit soundness for arbitrary
  floats, exact unit-conversion round-trips with cross-family refusal, and
  audit detection of any single-field mutation. Coverage measured and
  published in `docs/VALIDATION.md` (68 % overall; safety-critical core
  86–100 %).
- `REVIEW.md` (adversarial review record), `docs/THREAT_MODEL.md`,
  `docs/VALIDATION.md`, `CITATION.cff`.

## [1.0.0] — 2026-08-29

First public release, after a full adversarial pre-release review.

### Security / safety-model fixes (from the review)

- **Protocols can no longer shed the submitter's identity.** Every protocol
  step now executes as the verified principal that submitted it, so
  per-actor autonomy ceilings, per-actor rate limits, and audit attribution
  apply identically to direct calls, protocol runs, and async protocol jobs.
  Static validation applies the same *effective* ceiling as runtime.
- **HTTP request-desync closed.** The gateway refuses `Transfer-Encoding`
  bodies and POSTs without `Content-Length` (411) and closes the connection,
  instead of silently mis-framing kept-alive requests.
- **Back-pressure everywhere.** Async job submission is capped
  (`max_active`, default 64) with a structured refusal; SSE event streams
  are capped (`max_subscribers`, default 32) with a 503; the file-backed
  audit log no longer keeps an unbounded in-memory duplicate of every record.
- **Opaque internal errors.** Unexpected exceptions return a correlation id
  only; details stay in the server log.
- **Software e-stop is now reachable in `--readonly` mode** (stopping is
  always safe); it remains callable by every role including observers.
- **`GET /health` answers without authentication** (bare liveness pulse
  only; lab details still require a key).
- `GatewayContext` session caching is lock-guarded (no more duplicate
  JobManagers under concurrent first use).

### Added (since 0.1.0 / CONDUIT)

- **Universal Agent Gateway**: one fixed 14-tool registry rendered for MCP
  (stdio + HTTP + resources), REST + OpenAPI 3.1, OpenAI / Anthropic /
  Gemini tool schemas, and framework adapters for LangChain, LlamaIndex,
  CrewAI, AutoGen, and smolagents; `LabClient` Python SDK; SSE event stream.
- **Async job engine** with cooperative cancellation between protocol steps;
  synchronous calls estimated over 300 s are refused with guidance.
- **Verified identities**: API keys → principals with roles
  (observer / operator / approver / admin), per-actor ceilings and rate
  limits enforced inside the safety engine; agents cannot mint approvals.
- MCP resources (`lab://manifest`, `lab://reference`, `lab://audit/tail`).
- CLI: `serve --http --auth`, `schemas --format openai|anthropic|gemini|openapi`.

### Fixed

- Audit chain verified on resume; a tampered log stays readable but refuses
  new appends.
- E-stop reset re-runs each halted device's self-test; failures land in
  ERROR, not IDLE.
- Rate-limit windows are per actor.
- Unit conversions for mass/molar concentration, flow and pressure families
  (cross-family conversions still refuse — that needs a molar mass).
- Conformance suite probes both ends of every `Range` and bad `OneOf`
  values; typed safety errors report their subclass name.
- `FileWatchTransport` timeouts are no longer auto-retried as transient.
- `labaiagent verify file.py:Class` no longer crashes on a shadowed import.
- `snapshot` is recorded in the audit trail.

### Tooling

- ruff + mypy clean; `py.typed` shipped; CI workflow (lint, type-check,
  tests on Python 3.10–3.13); 136 tests.

## [0.1.0] — 2026-08-25

Internal prototype (as CONDUIT): driver contract, six-layer safety engine,
hash-chained audit, protocol DAG, seven transports, simulators, MCP stdio
server, conformance suite, CLI.
