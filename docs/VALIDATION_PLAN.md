# On-instrument validation plan (IQ / OQ / PQ template)

This is the executable gap between "the software is tested" and "this
installation is validated." Every LabAIAgent capability claim is covered by
the automated suite against simulated and fake-transport instruments; **this
plan is what a pilot lab executes against physical hardware** to produce the
evidence that closes §11.10(a) for their installation — and, run at two or
more sites, the dataset a methods paper cites.

Fill one copy per installation. Every step's evidence column should reference
an audit-log sequence range or a run-record id — the system generates its own
evidence; do not screenshot what you can cite.

- Installation: ______________  Site: ______________  Date: ______________
- Executed by: ______________  Reviewed/approved by (QA): ______________
- LabAIAgent version: ______  Lab config hash: ______  Principals file hash: ______

## IQ — Installation Qualification

| # | Step | Acceptance criterion | Evidence |
|---|---|---|---|
| IQ-1 | `pip install labaiagent==<pinned>` in the target environment | Exit 0; `labaiagent --version` prints the pinned version | terminal capture |
| IQ-2 | `pip install labaiagent[dev]` then `pytest` on the workstation | 100% pass (218 tests) | pytest junit xml |
| IQ-3 | Lab YAML review: every device stanza, every config-narrowed limit | Config only *narrows* driver limits (startup would refuse otherwise); limits match the site SOPs | signed config diff |
| IQ-4 | Principals file: roles, hashed keys, e-sign passwords | No placeholder keys/passwords remain; roles map to the personnel list | QA sign-off |
| IQ-5 | `LABAIAGENT_AUDIT_HMAC_KEY` injected at service start | `labaiagent audit <log> verify` reports a keyed, intact chain | audit verify output |
| IQ-6 | TLS material in place (or documented loopback-only topology) | Server refuses non-loopback bind without auth; TLS ≥ 1.2 on network binds | config + probe |
| IQ-7 | `labaiagent verify <driver>` conformance for each driver used | No FAIL findings at level=strict | conformance reports |

## OQ — Operational Qualification (per instrument)

Run each against the physical instrument, via the gateway (not the vendor GUI),
as an `operator`-role principal.

| # | Step | Acceptance criterion | Evidence |
|---|---|---|---|
| OQ-1 | `connect` + driver self-test | Device reaches IDLE; self-test true | audit seq |
| OQ-2 | Every `read:` capability once | Values plausible against the instrument's own display; units match | audit seq range |
| OQ-3 | One in-range `write:` per setpoint | Instrument display shows the setpoint; SCPI drivers show a drained, clean error queue | audit seq |
| OQ-4 | One **out-of-range** write per limited param | Refused BEFORE transport (a `refused` audit record, no instrument change) | audit seq |
| OQ-5 | A HIGH-risk capability without approval | Refused; then approved via `request_approval` with e-sign password → executes | audit seq pair |
| OQ-6 | Wrong e-sign password | Refused; `esign_failed` recorded | audit seq |
| OQ-7 | `emergency_stop` mid-procedure | Motion halts ≤ 2 s; state ESTOPPED; reset requires named operator; post-reset self-test runs | stopwatch + audit |
| OQ-8 | Physical fault injection (e.g., aspirate from a deliberately empty well) | PhysicalError → incident opened, device quarantined, diagnosis returned; actuation refused until human `resolve_incident` | incident id + audit |
| OQ-9 | Kill the server process mid-protocol (with a task attached); restart | Startup report names the interrupted task; `run_protocol(task_id, resume=true)` skips completed steps | startup log + record |
| OQ-10 | Unplug the instrument with `--watchdog` enabled | `heartbeat_lost` within 3 intervals; device ERROR; recovery on reconnect | audit seq pair |
| OQ-11 | Opentrons live-command cycle (if applicable): load_pipette → load_labware → pick_up_tip → aspirate → dispense → drop_tip → home | Each atomic command audited individually; liquid visibly moved; volumes within pipette spec | run record id |

## PQ — Performance Qualification (per assay)

Execute the lab's real assay end-to-end via `run_protocol` (a template
instantiation where one exists), three independent runs, two operators.

| # | Step | Acceptance criterion | Evidence |
|---|---|---|---|
| PQ-1 | Full assay, run 1–3 | Assay-specific accuracy/precision criteria (define here: ______) met on all runs | run record ids |
| PQ-2 | Gravimetric or dye-based volume check on liquid-handling steps | CV and bias within instrument spec (e.g., ±5% at 50 µL — set per pipette) | balance/plate data |
| PQ-3 | Parallel mode (if used): same assay with `parallel=true` | Results statistically indistinguishable from sequential runs; audit shows no same-device interleaving | run record ids |
| PQ-4 | Run-record integrity | `run_records verify` passes for every PQ record; tamper test on a COPY fails verification | verify outputs |
| PQ-5 | Cross-check | Reconstruct one run purely from its run record; independent scientist confirms it describes what happened | signed statement |

## Acceptance and periodic review

The installation is validated when every IQ/OQ/PQ row has evidence and QA has
signed. Revalidate OQ rows touched by: a LabAIAgent upgrade (CHANGELOG names
the affected areas), a driver change, a config limit change, or instrument
service. Schedule an annual PQ-1 repeat as a drift check.
