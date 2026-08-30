# Adversarial Review Record

This project was prepared for public release by running formal adversarial
review rounds against it and fixing — or explicitly documenting — every
finding. This file is the record: what was found, what was done, and what a
critic can still say. Publishing the criticism alongside the code is
deliberate; a control layer whose failure modes are hidden is more dangerous
than one whose failure modes are listed.

## Round 1 — safety-model and correctness review (pre-v1.0.0)

| # | Severity | Finding | Resolution | Regression test |
|---|----------|---------|------------|-----------------|
| 1.1 | **Critical** | Protocol steps executed under the *session* identity, not the submitting agent's: wrapping an over-ceiling actuation in a one-step protocol bypassed per-actor autonomy ceilings and rate limits, and misattributed the audit trail. | Verified actor threads through `Protocol.run`/`validate`, `JobManager.submit_protocol`, and the registry; static validation applies the same *effective* ceiling as runtime. | `test_protocol_cannot_shed_actor_ceiling`, `test_protocol_steps_audit_the_verified_actor`, `test_protocol_job_carries_actor` |
| 1.2 | High | HTTP request desync: `Transfer-Encoding: chunked` bodies were ignored, leaving octets in the socket buffer to be parsed as the next request — silent argument loss; request smuggling behind a proxy. | Chunked bodies and length-less POSTs refused with 411 + connection close. | `test_chunked_transfer_encoding_is_refused`, `test_post_without_content_length_is_refused` |
| 1.3 | High | Unbounded async-job accumulation: queued jobs were never evicted and the worker pool could be starved indefinitely by a looping agent. | Live-job cap (`max_active`, default 64) with a structured refusal. | `test_job_backpressure_cap` |
| 1.4 | High | Unbounded SSE subscribers, one blocked server thread each — a denial-of-service any authenticated observer could mount. | Subscriber cap (default 32); HTTP 503 past it. | `test_sse_subscriber_cap` |
| 1.5 | Medium | File-backed audit log kept an unbounded in-memory duplicate of every record — a guaranteed leak on the busiest control-path object. | In-memory list retained only in path-less mode. | `test_filebacked_audit_keeps_no_memory_copy` |
| 1.6 | Medium | `GatewayContext` per-session cache had a first-touch race: two threads could build two JobManagers, one silently shadowing the other's jobs. | Lock-guarded creation. | `test_context_is_cached_per_session` |
| 1.7 | Medium | Unexpected exceptions returned `str(exc)` to agents — internal paths and config fragments could leak. | Opaque correlation id to the caller; full detail only in the server log. | `test_internal_errors_are_opaque` |
| 1.8 | Medium | The software e-stop was filtered out of `--readonly` servers — the one mode where an observer-only agent might most need it. | `always_available` flag; e-stop callable in every mode by every role. | `test_emergency_stop_available_in_readonly_mode` |
| 1.9 | Low | `GET /health` required authentication, breaking liveness probes. | Unauthenticated bare pulse; details still need a key. | `test_health_is_unauthenticated_but_minimal` |
| 1.10 | Low | `get_audit_log` re-read and re-verified the whole log three times per call. | Single read shared across summary/verify/tail. | covered by suite |

Also fixed in round 1: audit chain verified on resume (a tampered log is
readable but refuses appends); e-stop reset re-runs each device's self-test;
rate-limit windows made per-actor; both ends of every `Range` probed by the
conformance suite; missing unit-conversion families; a CLI crash in
`verify file.py:Class`; `FileWatchTransport` timeouts no longer auto-retried.

## Round 2 — security-layer review (pre-v1.1.0)

| # | Severity | Finding | Resolution | Regression test |
|---|----------|---------|------------|-----------------|
| 2.1 | **High** | The "tamper-evident" audit claim was overstated: an unkeyed SHA-256 chain proves internal consistency, not authenticity — an adversary with write access can rewrite the whole file and recompute every link. | Optional **HMAC-SHA256 keyed chaining** (`hmac_key` / `LABAIAGENT_AUDIT_HMAC_KEY`); a whole-file rewrite without the key now fails verification. The unkeyed weakness is documented in the module docstring rather than hidden. | `test_keyed_audit_defeats_full_file_rewrite`, `test_keyed_log_requires_matching_key` |
| 2.2 | High | No transport security: the gateway spoke plaintext HTTP only; API keys transited in the clear on any non-loopback deployment. | Native TLS (`--tls-cert/--tls-key`, TLS ≥ 1.2); client CA pinning; loud startup warning for plaintext on non-loopback. | `test_tls_end_to_end`, `test_tls_requires_both_cert_and_key` |
| 2.3 | High | No brute-force resistance: unlimited API-key guessing at full speed. | Per-source-IP lockout (10 failures/60 s → 429 for 60 s, checked before any key comparison; `X-Forwarded-For` deliberately untrusted). | `test_auth_bruteforce_lockout` |
| 2.4 | Medium | Missing hardening headers; unbounded protocol step counts. | `nosniff`/`no-store`/`no-referrer` on every response; 500-step protocol cap with a structured refusal. | `test_security_headers_present`, `test_protocol_step_count_is_capped` |
| 2.5 | Medium | Coverage was unmeasured and the safety-critical validator had only example-based tests. | Coverage measured and published (see `docs/VALIDATION.md`); **property-based tests** (Hypothesis) added for the parameter validator, unit conversion, and audit-chain mutation detection. | `tests/test_property.py` |

## Round 3 — knowledge/oversight-layer review (pre-v1.2.0)

Development-time review of the knowledge, e-signature and oversight layers,
recorded here so the round numbering is contiguous and every accepted
finding stays traceable to its test.

| # | Severity | Finding | Resolution | Regression test |
|---|----------|---------|------------|-----------------|
| 3.1 | High | The pre-execution reviewer initially reviewed every irreversible MEDIUM action, vetoing routine liquid transfers — an oversight layer that blocks normal work gets disabled by its operators, which is worse than no oversight. | Review scope bound to the configured `review_risk` floor (default HIGH); MEDIUM actions pass on the safety engine alone. | `test_reviewed_call_proceeds_with_reason_and_token` |
| 3.2 | Medium | RLHF export produced ZERO preference pairs on realistic data: DPO pairing was keyed on (tool, device, capability), and approvals vs. rejections rarely collide on all three. | Pairing rekeyed on (device, capability) — the situation an operator actually judges. | `test_feedback_accumulates_and_exports_dpo_pairs` |
| 3.3 | Medium | The PubMed journal filter was believed applied but untested on the wire — the original test captured the wrong request URL, so a silently dropped filter would have passed CI. | Transport spy asserts the journal term appears in the actual `esearch` query string. | `test_journal_filter_reaches_the_query` |
| 3.4 | Low | A template parameter outside its limits surfaced to agents as an opaque `internal_error` instead of a correctable refusal naming the violated bound. | Wrapped as `LabAIAgentError` carrying the parameter, the value, and the permitted range. | `test_parameters_are_limit_checked`, `test_unknown_parameter_and_missing_device` |

## Round 4 — operations-layer review (pre-v1.4.1)

Independent adversarial review of the v1.4.0 additions (dashboard,
provenance, parallel executor, live Opentrons commands, SCPI hardening,
watchdog, metrics), with each finding reproduced against live code before
acceptance. Two findings were critical: both were ways real hardware could
keep moving, or move twice, after the control layer believed otherwise.

| # | Severity | Finding | Resolution | Regression test |
|---|----------|---------|------------|-----------------|
| 4.1 | **Critical** | Parallel wave lanes ignored ABORT and cancellation between their own steps: in a flat protocol, a FAILED+ABORT step (or `cancel_job`) left every other lane still dispatching physical actions — the documented "a protocol stops between steps" contract was broken exactly when parallel mode was on. | Shared `stop_wave` event checked before EVERY lane step; abort and `should_cancel` now stop all lanes at the next step boundary, remaining steps are SKIPPED (in-flight steps still finish — never interrupt a physical action). | `test_r4_1_parallel_abort_stops_other_lanes`, `test_r4_1_parallel_cancellation_honoured_within_wave` |
| 4.2 | **Critical** | A transient error while *polling* an already-enqueued Opentrons command escaped as `TransientError`, which `LabSession.call` retries — re-enqueueing the same aspirate: one tool call, two physical liquid movements, audited as one success plus a "retry". | Point-of-no-return discipline in `_command`: after the enqueue POST succeeds, poll blips are absorbed internally and a poll timeout raises `PhysicalError` ("enqueued and may still execute — do not re-send"). The protocol-run poll loop likewise absorbs blips and raises non-retryable `TransportError` on timeout ("play" already happened). | `test_r4_2_poll_blip_does_not_double_actuate`, `test_r4_2_poll_timeout_is_physical_never_transient` |
| 4.3 | High | Run-record verification false-positived as "tampered" for any record whose step results contained non-JSON-native values (datetime, Path, tuples): the canonical form hashed at save differed from what a JSON round-trip of the file re-canonicalises to — legitimate evidence permanently unverifiable. | Bodies are canonical-JSON-normalised **before** sealing, so the bytes hashed are exactly the bytes any later `verify()` reconstructs. | `test_r4_3_records_with_non_json_values_still_verify` |
| 4.4 | High | Watchdog check-then-probe TOCTOU: a device could turn BUSY between the state check and the self-test probe, interleaving probe traffic with an in-flight actuation (the exact hazard the module forbids); the e-stop check raced `estop.trip`. | Each probe now holds the session's per-device lock (the same lock actuation holds) with the state and e-stop re-checked inside it; a busy lock skips the probe this round. | `test_r4_4_probe_skipped_while_device_lock_held` |
| 4.5 | High | Watchdog recovery resurrected devices it had no right to: a device in ERROR from a physical fault (with an open incident and unreleased quarantine) that later answered a self-test was silently restored to IDLE — bypassing human-released quarantine at the state-gate level. | Recovery restores only what the watchdog broke: the current error must carry the watchdog's own trip prefix, and a supervisor-quarantined device is never auto-restored. | `test_r4_5_recovery_never_clears_error_it_did_not_set`, `test_r4_5_recovery_never_lifts_a_quarantine` |
| 4.6 | High | A protocol job cancelled while still queued returned before its `finally`: no `job_finished` audit record, `on_done` never fired — a task-linked run left its task stuck `in_progress` forever with no run record. | The early-cancel path now settles like every terminal path: audited, emitted, `on_done` invoked. | `test_r4_6_cancel_before_start_still_settles` |
| 4.7 | High | Crash-resume dropped the shared context: checkpoints stored context *keys* only, so any resumed protocol whose later steps referenced `$stored` values aborted immediately — the runs that most need resuming are the ones passing data forward. | Checkpoints persist context values and per-step results; resume restores statuses, results, and the context. | `test_r4_7_resume_restores_context_for_dollar_refs` |
| 4.8 | Medium | The cached Opentrons live-run id was never invalidated: after an e-stop or robot-side run expiry, subsequent commands posted into a dead run (confusing 4xx, device wedged until reconnect); `_halt` stopped only the protocol run, not the live command run. | `_halt` stops both runs and clears the cache; a failed enqueue clears it too, so the next command opens a fresh run. | `test_r4_8_halt_invalidates_live_run` |
| 4.9 | Medium | SCPI verified writes blamed pre-existing (stale) queue errors on the current setpoint — a healthy write could raise `PhysicalError` and quarantine a healthy instrument. | The queue is drained (and discarded) *before* the write and checked again after, so any raised error is attributable to this setpoint. | `test_r4_9_stale_queue_errors_not_blamed_on_this_write` |
| 4.10 | Medium | Dashboard `esc()` did not escape quotes; `state()` interpolated a server-supplied string into an HTML *attribute* — a malicious state-like string could break out of the attribute (and `unsafe-inline` CSP permits inline handlers). | `esc()` escapes both quote characters; the state CSS class is now a whitelisted token (`/^[a-z_]+$/` else "unknown"), never raw input. | `test_r4_10_dashboard_escapes_attribute_context` |
| 4.11 | Low | `GatewayServer.stop()` never shut the job thread pool down (resource leak in embedding/tests). | `stop(shutdown_jobs=True)` opt-in (default False is deliberate: the context is session-scoped and may outlive the server — documented). | `test_r4_12_server_stop_can_shut_down_job_pool` |
| 4.12 | Low | `/metrics` rendered large counters with `%g`, losing precision past ~1e6 (audit heads and invocation counters get there in a long campaign). | Integer values rendered exactly. | covered by `test_metrics_prometheus_format` |

Also reviewed and confirmed clean in round 4: metrics label escaping and
scrape cost; `_run_protocol` provenance wiring (record written exactly once
per run, on every settle path); record-id sanitisation and atomic writes;
watchdog thread lifecycle; auth/throttle ordering on the new routes (the
dashboard shell is intentionally unauthenticated and carries no data).

## What a critic can still say — and our answer

1. **"The hardware drivers have never touched hardware."** True, and stated
   in every relevant docstring with a "verify on first connect" list. The
   framework, simulators, safety engine, and gateway are exhaustively
   tested; the three hardware drivers are written against published APIs and
   pass static conformance. Physical validation is the next milestone and a
   precondition for any peer-reviewed claim about real-lab operation.
2. **"Protocol steps run serially even when the DAG allows parallelism."**
   Serial remains the default (totally ordered, replayable audit). Since
   v1.4.0, `parallel=true` is the explicit opt-in: waves are grouped by
   device (same-instrument order stays strict), abort/cancel are honoured
   per step inside every lane (round-4 finding 4.1), and audit records from
   a wave interleave in wall-clock order — stated in `docs/VALIDATION.md`.
3. **"Approvals and job state die with the process."** Correct — both are
   in-process by design; a restart fails closed (pending approvals void,
   jobs must be resubmitted, protocol checkpoints allow resume). Durable
   queues are a deliberate non-goal at this layer.
4. **"A software e-stop is not a safety system."** Agreed, loudly, in the
   README, SECURITY.md, and the threat model: this is a software layer above
   hardware E-stops, interlocked enclosures, and light curtains, never a
   substitute — no IEC 61508 / ISO 13849 claim is made.
5. **"An agent with a valid operator key can still do operator-level
   damage."** Yes — authorization bounds capability, it does not supply
   judgment. That is exactly why ceilings, rate limits, interlocks,
   approvals, and the audit trail exist, and why the recommended rollout is
   readonly → dry-run → low ceiling → medium.

Review methodology: independent adversarial code review with reproduction
of each suspected defect before acceptance; findings without a reproduction
or a concrete failure scenario were discarded. Every accepted finding
carries a regression test named after the failure.
