# Validation Evidence

State of the evidence behind LabAIAgent v1.4.1, stated the way a methods
section should be: what was tested, how, with what result, and what has NOT
been tested. Reviewers should be able to falsify every claim here by
running the suite.

## Reproducing every number in this document

```bash
pip install -e ".[dev]"
python -m pytest tests/ -q --cov=labaiagent --cov-report=term
ruff check labaiagent/ tests/ examples/
mypy labaiagent/
python -m build && pip install dist/*.whl   # clean-env install
```

All simulators are seeded (`World(seed=...)` in every example and fixture);
example outputs are deterministic given the seed.

## Test suite: 232 tests, all passing

The full suite is additionally run three consecutive times at every
release gate; a flaky test on a safety path is treated as a defect
(docs/RELEASE_PROCESS.md, G2).

| Category | Tests | What is demonstrated |
|----------|-------|----------------------|
| Types & units | 12 | unit-aware limits, refusal of unknown units, cross-family conversion refusal |
| Capability contract | 8 | decorator → validation/manifest/reference derivation, per-instance isolation |
| Driver contract | 10 | contract violations rejected at construction, config-only-tightens invariant |
| Safety engine | 19 | **refusal behaviour of every layer**: e-stop latch, state gate, limits, interlocks (incl. fail-closed on missing/raising interlocks), per-actor rate limits, approval scope/expiry/single-use |
| Audit | 9 | chain verification, tamper & deletion detection, refusal recording, keyed-mode rewrite detection |
| Protocol engine | 10 | static validation (unknown device/capability, bad args, cycles, missing approvals), abort/continue semantics, checkpoint resume, cancellation |
| Gateway registry & auth | 12 | role boundaries, per-actor ceilings, approval minting restricted to humans, opaque internal errors |
| Async jobs | 7 | job lifecycle, failure capture, cancellation between steps, back-pressure cap, audit of transitions |
| REST/MCP over HTTP (live sockets) | 12 | end-to-end auth, TLS, 411/429/403/401 behaviour, MCP-over-HTTP, OpenAPI serving |
| Schema exporters & framework shims | 10 | OpenAI/Anthropic/Gemini/OpenAPI shapes, executor round-trips, deep-copy isolation |
| stdio MCP & scaffolding | 4 | full JSON-RPC round trip incl. resources; every scaffold variant compiles |
| Review-regression tests | 17 | one named test per accepted adversarial finding (see REVIEW.md) |
| **Property-based (Hypothesis)** | 7 | see below |
| Knowledge layer | 12 | trusted-source suffix matching (look-alike domains refused), PubMed-only construction, citation enforcement, template parameter limits, category binding, template→protocol execution against the simulated lab |
| E-signatures & oversight | 15 | password required/verified/audited per signing; refusal-streak suspension with human-only reinstatement; rule + foundation-model reviewer verdicts incl. fail-closed on garbage; feedback capture and DPO pairing |
| Memory, continuity & recovery | 15 | kv/tasks/incidents survive restart (fresh handles over the same files), interrupted-first scheduling, checkpoint path traversal stripped, suspension/quarantine persistence, physical-fault quarantine + diagnosis + human-gated release, crash-resume skipping completed steps |
| Simulated physics | 9 | pipetting CV model, Beer–Lambert reads, qPCR efficiency/Ct, interlock physics |
| Hardware wire contracts (v1.4) | 7 | Opentrons live-command enqueue/poll shapes against a fake robot-server speaking the published API (intent=setup, wellLocation, flowRate), robot failure → PhysicalError with errorType, live-session refusal beside an active run, safety gating BEFORE transport, SCPI error-queue drain and rejected-setpoint detection |
| Parallel execution (v1.4) | 4 | cross-device overlap proven by interval timestamps, same-device strict ordering, context/$ref correctness under concurrency, wave-granular abort |
| Provenance (v1.4) | 5 | record written on success and on failure, checksum+signature verification, tamper and wrong-key detection, audit-slice scoping to the run, tool surface |
| Round-4 review regressions (v1.4.1) | 12 | one named test per accepted round-4 finding: parallel abort/cancel inside waves, no double actuation via retry after enqueue, provenance normalisation, watchdog lock discipline and quarantine-respecting recovery, early-cancel settlement, crash-resume context restoration, live-run invalidation, SCPI stale-error attribution, dashboard attribute escaping, job-pool shutdown |
| Watchdog & ops surface (v1.4) | 6 | 3-strike trip → ERROR + audit, auto-recovery, BUSY/e-stop probe exclusion, thread lifecycle, dashboard CSP and headers, Prometheus metrics format |

### Property-based results (300 randomized examples per property)

- **P1 — limit soundness**: for arbitrary finite floats, a declared `Range`
  never admits an out-of-range value; NaN/±Inf never pass; `OneOf` admits
  only listed values; `Pattern` is full-match (an embedded valid token in
  junk is rejected).
- **P2 — unit algebra**: conversions round-trip exactly (rel. 1e-12) within
  a dimension family and are refused across families (mass-concentration ↔
  molar requires a molar mass and is never guessed).
- **P3 — audit integrity**: any single-field mutation of any record in a
  chain is detected by `verify()`.

## Static analysis

ruff: clean (config in `pyproject.toml`). mypy: clean over all 58 modules,
`py.typed` shipped. Wheel builds reproducibly and passes a clean-venv
install + CLI smoke test (CI enforces all of this on Python 3.10–3.13,
Linux and Windows).

## Coverage: 70 % overall — and why that number is honest

| Region | Coverage | Note |
|--------|----------|------|
| `core/` (safety, capability, device, audit, types, errors) | 86–100 % | the safety-critical path; `capability.py` 99 %, `safety.py` 92 %, `errors.py` 100 % |
| `orchestration/` (session, workflow, jobs) | 83–89 % | |
| `gateway/` (registry, auth, rest, schemas) | 79–100 % | live-socket tested |
| Simulated drivers & world | 83–84 % | |
| `drivers/hardware.py` | partial | wire contracts (Opentrons live commands, SCPI verified writes) exercised against fakes speaking the published protocols; physical paths require instruments — see Limitations |
| `transports/concrete.py` | 30 % | serial/COM/SiLA paths require hardware; filewatch is tested |
| framework-specific integration shims | 0 % | require the third-party frameworks; the shared shim core is tested |
| `knowledge/` (sources, pubmed, library) | 90–100 % | network transport injected; parsing/gating fully exercised |
| `oversight/` (supervisor, feedback) | 88–95 % | foundation-model reviewer exercised via injected completions |

Chasing a higher total by mocking hardware would manufacture confidence
rather than evidence; the untested regions are exactly the ones that
require physical instruments or third-party frameworks, and they are
labelled as such in the code.

## Limitations (what this evidence does NOT show)

1. **No physical-instrument validation.** The three hardware drivers
   (Opentrons Flex, generic SCPI, watched-folder reader) are written
   against published interfaces — the Opentrons live-command wire shapes
   are additionally verified test-by-test against the published client's
   contract — and pass static conformance, but have never driven real
   hardware from this codebase. `docs/VALIDATION_PLAN.md` is the
   executable IQ/OQ/PQ that closes this gap per installation; until a
   pilot lab executes it, no real-lab operational claim is made.
2. **Simulators are plausible, not predictive.** The pipetting-error, BCA
   and qPCR models reproduce the *shape* of real behaviour for protocol
   debugging; they are not calibrated digital twins.
3. **Single-node scope.** One gateway process per lab; availability and
   durability of jobs/approvals across restarts are out of scope (fail-
   closed by design).
4. **Parallelism is opt-in and wave-granular.** `parallel=true` overlaps
   distinct instruments only (same-device order is strict, proven by
   test); audit records from a parallel wave interleave in wall-clock
   order rather than protocol order. Default execution remains serial and
   totally ordered.
5. **Timing side channels** on the safety engine have not been studied;
   the engine's decisions are not secret, so this is theoretical.
6. **The foundation-model reviewer is only as good as the model.** Its
   fail-closed error handling is tested; the *quality* of live LLM verdicts
   is not benchmarked here and should be evaluated per deployment before
   relying on it over the rule-based reviewer.
7. **RLHF is a dataset, not a loop.** The feedback store produces
   preference data; no claim is made about the effect of tuning on agent
   behaviour until such a tuning study is run.
