# Contributing to LabAIAgent

LabAIAgent controls physical laboratory instruments. A wrong line of code
here does not throw an exception — it moves a robot arm, empties a sample,
or silently corrupts an audit trail someone will one day show a regulator.
The process below is calibrated to that reality. It is stricter than a
typical open-source project on purpose.

## Ground rules (non-negotiable invariants)

Any change violating one of these is rejected regardless of its other
merits. They are the load-bearing walls:

1. **One invocation path.** Everything that touches an instrument goes
   through `LabSession.call`. No second door, ever — not for tests, not for
   "just this driver", not behind a flag.
2. **Config only tightens.** Lab configuration may narrow driver-declared
   limits, raise risk classes, and add confirmation requirements. It may
   never widen, lower, or remove them.
3. **Fail closed.** A missing interlock, a raising reviewer, an unreadable
   principals file, a broken audit chain — every ambiguous state resolves to
   "refuse", never to "proceed".
4. **The tool surface is fixed.** Twenty tools. Adding one requires a design
   discussion first (issue tagged `tool-surface`); "one tool per capability"
   proposals are rejected on sight — that architecture is what this project
   exists to replace.
5. **Reads are risk-NONE and side-effect free.** If it actuates, it is a
   `@write` or a `@procedure`, whatever it is named.
6. **Errors after physical actuation are never `TransientError`.** The retry
   layer re-executes transient failures; a re-executed aspirate is a double
   aspirate (see REVIEW.md finding 4.2). Once hardware may have moved, raise
   `PhysicalError` (state unknown / fault) or `TransportError` (do not
   retry), never anything the session will retry.
7. **Humans hold the irreversible verbs.** Approval minting, e-stop reset,
   quarantine release, suspension reinstatement, task cancellation: these
   require a named human principal. Code that gives an agent any of them is
   a security defect.
8. **stdlib-only core.** New runtime dependencies for the core package are
   rejected; optional integrations live behind extras.

## Change workflow

1. **Issue first** for anything beyond a typo: state the problem, the
   proposed approach, and which invariants it touches.
2. **Branch** from `main`; one logical change per PR. Commits are
   imperative-mood, present tense, and explain *why*.
3. **Write the test first** when fixing a bug — the regression test that
   fails on `main` and passes on your branch, named after the failure
   (see `tests/test_v14.py::TestRound4Regressions` for the pattern).
4. **Gates** — a PR is reviewable only when ALL of these pass locally:
   `pytest` (100%, no skips added), `ruff check .`, `mypy labaiagent`,
   every example in `examples/` runs, `python -m build` succeeds.
5. **Review**: at least one maintainer review; changes touching
   `core/safety.py`, `core/audit.py`, `gateway/auth.py`,
   `oversight/`, or any driver's risk/limit declarations need TWO reviews,
   one of which must adversarially attempt to defeat the change (document
   the attempt in the PR).
6. **Docs move with code**: CHANGELOG entry in the same PR; VALIDATION.md
   category counts updated; THREAT_MODEL.md if the attack surface changed;
   COMPLIANCE.md if a control changed.

## Driver contributions (the most common PR)

Start from `labaiagent scaffold <vendor>.<model>`. The checklist reviewers
apply:

- [ ] `vendor`, `model`, `category`, `driver_version`, honest `notes`
      (quirks and gotchas — the things the vendor manual does not say)
- [ ] Every capability declares units and limits; every limit reflects the
      INSTRUMENT's physical envelope, not the use case (the lab config
      narrows per-site)
- [ ] Risk classes justified in the PR description; anything irreversible
      is `reversible=False`; anything raw/unvalidated is HIGH+confirmation
- [ ] `_halt()` implemented for anything that moves
- [ ] `_self_test()` is cheap, safe to run while idle, and meaningful
- [ ] Transport injectable; the test file exercises the real wire shapes
      against a fake (see `FakeOTServer` in `tests/test_v14.py`)
- [ ] Failure mapping: instrument faults → `PhysicalError` (with the
      instrument's own diagnostic text); connectivity → `TransportError`;
      retry-safe blips → `TransientError` — and NOTHING transient after the
      point of no return
- [ ] "VERIFY ON FIRST CONNECT" list in the docstring
- [ ] `labaiagent verify` passes at `--level strict`

## Security findings

Do not open a public issue for an exploitable defect. Follow `SECURITY.md`.
A security fix PR gets a regression test and a REVIEW.md entry like any
other finding — silence is not a control.

## Code style

Enforced by ruff and mypy (run them; do not argue with them in PRs).
Beyond the tools: docstrings explain *why* (design intent, hazards,
trade-offs), comments are for the non-obvious, error messages tell the
caller what to DO next, and public APIs carry type hints. Line length 100.

## Licensing

Apache-2.0. By contributing you agree your contribution is licensed under
the project license. Keep third-party code out unless its license is
Apache-2.0-compatible and its provenance is stated in the PR.
