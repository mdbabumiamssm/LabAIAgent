# Compliance mapping — 21 CFR Part 11 and GAMP 5

**Status of this document.** This is an honest engineering-to-regulation map,
not a compliance certificate. LabAIAgent supplies *technical controls*; Part 11
compliance is a property of a **validated deployment** (software + SOPs +
training + validation evidence), which only the deploying organisation can
achieve. This document tells that organisation exactly which control satisfies
which clause, and which obligations remain procedural on their side.

## 21 CFR Part 11 — Electronic Records; Electronic Signatures

### Subpart B — Electronic Records (§11.10 controls for closed systems)

| Clause | Requirement (abridged) | LabAIAgent technical control | Deployment obligation |
|---|---|---|---|
| §11.10(a) | Validation of systems to ensure accuracy, reliability, consistent intended performance | 218-test automated suite incl. property-based tests; `docs/VALIDATION.md` (software) and `docs/VALIDATION_PLAN.md` (IQ/OQ/PQ template for on-instrument validation) | Execute the IQ/OQ/PQ on each installation; retain evidence |
| §11.10(b) | Accurate and complete copies of records, human-readable and electronic | Audit trail is human-readable JSONL; run records are self-contained JSON exportable via the `run_records` tool | Define retention/archival SOP |
| §11.10(c) | Protection of records throughout retention period | Append-only hash-chained audit log (HMAC-SHA256 keyed); run records checksummed + HMAC-signed; tamper detected by `verify()` on both | Back up the files; protect the HMAC key (KMS/env injection); anchor head hashes externally |
| §11.10(d) | Limiting system access to authorized individuals | API-key authentication (hashed at rest, constant-time compare), per-IP brute-force lockout, refusal to serve unauthenticated off loopback | Key issuance/rotation SOP; personnel list |
| §11.10(e) | Secure, computer-generated, time-stamped audit trails; record changes shall not obscure previous entries | Every invoke/result/refusal/error/approval/e-stop is a chained, timestamped record; the chain refuses to extend a broken history; nothing is ever rewritten | Time synchronisation (NTP) on the host |
| §11.10(f) | Operational system checks to enforce permitted sequencing | Fail-closed safety engine: state gate, parameter limits, interlocks, protocol static validation before actuation | Author lab-specific interlocks in config |
| §11.10(g) | Authority checks (only authorized individuals can use the system, sign, alter records) | Role hierarchy (observer < operator < approver < admin) enforced per tool; approval minting restricted to approver+; human-only incident resolution and reinstatement | Map roles to real personnel |
| §11.10(h) | Device checks to determine validity of data-input sources | Driver self-tests at connect; heartbeat watchdog (`--watchdog`); SCPI write verification via error-queue drain; conformance suite (`labaiagent verify`) | Run `verify` at installation; enable the watchdog |
| §11.10(i) | Persons have education/training to perform tasks | — (procedural) | Training SOP |
| §11.10(j) | Written accountability policies for e-signatures | — (procedural) | Signature-accountability SOP |
| §11.10(k) | Controls over systems documentation | Versioned docs in-repo; CHANGELOG with per-release deltas | Controlled-copy procedure |

### Subpart C — Electronic Signatures (§11.50, §11.70, §11.100, §11.200, §11.300)

| Clause | Requirement (abridged) | LabAIAgent technical control | Deployment obligation |
|---|---|---|---|
| §11.50 | Signed records contain printed name, date/time, meaning of signature | Approval issuance is audited with operator id, timestamp, device, capability, reason, TTL and uses | Display-name mapping in principals file |
| §11.70 | Signatures linked to their records, not excisable | Approval events are links in the hash chain; removing one breaks every later record | — |
| §11.100(a) | Signatures unique to one individual, not reused/reassigned | Per-principal identity; per-principal PBKDF2 password | Identity-proofing SOP (§11.100(b) is procedural) |
| §11.200(a)(1) | Two distinct components (e.g., ID + password) | API key authenticates the connection; the e-signature **password is re-entered at each signing** and never cached | — |
| §11.300 | Controls of passwords/IDs: uniqueness, periodic checks, loss management, transaction safeguards | PBKDF2-HMAC-SHA256 (200k iterations, per-user salt); failed signings audited (`esign_failed`); brute-force lockout at the transport | Password rotation + revocation SOP |

**Known gaps (stated, not hidden):** §11.10(i)/(j)/(k) and §11.100(b) are
procedural by nature. LabAIAgent does not yet render a printed-name signature
manifest page (planned; the data is all present in the audit trail).

## GAMP 5 alignment

- **Category.** Deployed as instrument-control software, LabAIAgent is GAMP
  software category 4 (configured product) — configuration is the lab YAML and
  principals file; category 5 applies only if you write custom drivers.
- **Specifications.** URS ↔ `README.md` (capability claims), FS ↔
  `docs/VALIDATION.md` claim table, DS ↔ module docstrings + `REVIEW.md`.
- **Risk-based approach.** The framework itself encodes risk classes
  (NONE/LOW/MEDIUM/HIGH/CRITICAL per capability) and applies proportionate
  controls (autonomy ceilings, approvals, reviewer oversight) — the same logic
  GAMP 5 asks the validator to apply to the system.
- **Verification.** Software: the automated suite (218 tests) run at install
  (`pip install labaiagent[dev] && pytest`). Hardware: `docs/VALIDATION_PLAN.md`.
- **Data integrity (ALCOA+).** Attributable (actor per record), Legible
  (JSONL/JSON), Contemporaneous (timestamps at write, fsync'd), Original
  (append-only chain), Accurate (verified writes, validated params) + Complete
  / Consistent / Enduring / Available (run records, retention SOP).

## What an auditor should ask for

1. The executed IQ/OQ/PQ for this installation (from `VALIDATION_PLAN.md`).
2. `labaiagent audit <log> verify` output — chain intact, keyed.
3. A sampled run record and its `run_records verify` result.
4. The principals file review: roles vs. personnel, password entries present.
5. The SOP set: keys, passwords, retention, training, signature accountability.
