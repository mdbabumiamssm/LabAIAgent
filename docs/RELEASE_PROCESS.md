# Release process

Every release of software that actuates lab hardware is a small act of
liability. This checklist is the deliberate friction between "the tests
pass" and "a lab can install this". A release manager executes it top to
bottom and records the evidence; any red stops the release.

## Versioning policy (SemVer, strictly)

- **MAJOR**: any breaking change to the tool surface, the driver contract
  (`Device` subclass API), the wire formats (REST routes, MCP shapes,
  audit-record or run-record schema), or a safety-relevant default.
- **MINOR**: new capabilities that change no existing behavior (new tools —
  after the `tool-surface` design gate — new drivers, new adapters).
- **PATCH**: fixes and documentation. A patch release must not change any
  schema, default, or public signature.
- Safety-relevant behavior may become *stricter* in a MINOR (documented in
  the CHANGELOG under "Tightened"); it may never become looser outside a
  MAJOR with a migration note.

## Gate checklist

### G1 — Code freeze sanity
- [ ] `git status` clean; `main` is the release commit
- [ ] CHANGELOG section for this version is complete, dated, and honest
      (including "Tightened" and "Known gaps")
- [ ] Version bumped in exactly three places and equal:
      `pyproject.toml`, `labaiagent/__init__.py.__version__`, `CITATION.cff`

### G2 — Automated verification (all on a clean checkout)
- [ ] `pytest` — 100% pass, zero skips introduced this cycle
- [ ] Suite run **three consecutive times** — zero flakes (a flaky test on a
      safety path is a bug, not an annoyance)
- [ ] `ruff check .` and `mypy labaiagent` clean
- [ ] Every `examples/*.py` runs to completion
- [ ] Property-based tests at full example count (no `--hypothesis-seed`
      pinning left in)

### G3 — Packaging
- [ ] `python -m build` produces sdist + wheel
- [ ] Wheel installs into a **fresh venv** with only PyYAML; import works;
      `labaiagent --version` correct; tool count correct
- [ ] Optional extras each install and import on a fresh venv (spot-check
      `[mcp]`, `[langchain]`)

### G4 — Security regression
- [ ] Server refuses non-loopback bind without auth
- [ ] Plaintext-on-network warning prints; TLS pair loads
- [ ] Brute-force lockout returns 429 before key comparison
- [ ] Audit chain verifies keyed and unkeyed; a mutated record fails
- [ ] A run record verifies; a mutated record fails; a wrong key fails
- [ ] No placeholder secrets in any shipped config (`grep -ri "changeme\|placeholder" config/`)

### G5 — Review debt
- [ ] Every REVIEW.md finding for this cycle has status Fixed + regression
      test, or an explicitly argued acceptance
- [ ] THREAT_MODEL.md reflects any new attack surface added this cycle
- [ ] docs/VALIDATION.md test counts and category table match reality

### G6 — Publication
- [ ] Tag `vX.Y.Z`, signed if the org signs tags
- [ ] `twine upload` (or the org's registry)
- [ ] GitHub release notes = the CHANGELOG section, verbatim
- [ ] Post-publish smoke: `pip install labaiagent==X.Y.Z` from the registry
      into a fresh venv, run example 01

### G7 — Field impact (for deployments under validation)
- [ ] CHANGELOG names the OQ rows a validated site must re-execute
      (see docs/VALIDATION_PLAN.md "periodic review")
- [ ] If any audit/run-record schema field changed: migration note +
      verifier tolerance documented

## Hotfix path

A confirmed safety or security defect in a released version short-circuits
G1–G3 timing but never G2's content: fix on a branch from the release tag,
full gates, PATCH release, and a REVIEW.md entry. Users are notified through
the SECURITY.md channel when the defect is exploitable.

## Records

Keep the executed checklist (this file, filled in) with the tag — it is the
release's quality record, and the thing an auditor or acquirer asks for
first.
