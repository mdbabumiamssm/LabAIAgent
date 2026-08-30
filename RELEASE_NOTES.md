# LabAIAgent 1.4.1 — release package

The universal AI-agent gateway for laboratory instruments: vendor-neutral
control, six fail-closed safety layers, and a tamper-evident audit trail.
License: Apache-2.0.

## What is in this package

| Path | Contents |
|---|---|
| `labaiagent/` | The library (stdlib + PyYAML core; 20-tool agent gateway, safety engine, drivers, memory, provenance, oversight) |
| `dist/` | Installable artifacts built from this exact tree: `labaiagent-1.4.1-py3-none-any.whl` and the sdist |
| `tests/` | 232 automated tests (unit, property-based, live-socket HTTP, review regressions) |
| `examples/` | 7 runnable end-to-end demos (simulated lab; no hardware needed) |
| `docs/` | VALIDATION.md (test evidence), VALIDATION_PLAN.md (IQ/OQ/PQ), COMPLIANCE.md (21 CFR Part 11 / GAMP 5 map), THREAT_MODEL.md, RELEASE_PROCESS.md, technical slide deck |
| `config/` | Example lab + principals files. **Placeholders only** — every `change_me` key and password must be replaced before any networked deployment |
| `REVIEW.md` | Four published adversarial review rounds: 31 findings, each fixed with a named regression test |
| `CHECKSUMS.sha256` | SHA-256 of every file in this package (see VERIFY instructions below) |

## Verify this package (5 minutes, no trust required)

```bash
# 1. Integrity: every file matches the manifest
sha256sum -c CHECKSUMS.sha256          # expect: all OK

# 2. The wheel installs clean and exposes the 20-tool gateway
python -m venv .venv && . .venv/bin/activate
pip install dist/labaiagent-1.4.1-py3-none-any.whl PyYAML
python -c "import labaiagent; from labaiagent.gateway.registry import TOOL_SPECS; \
           print(labaiagent.__version__, len(TOOL_SPECS), 'tools')"   # 1.4.1 20 tools

# 3. The full test suite passes from this tree
pip install pytest hypothesis
python -m pytest tests/ -q             # expect: 232 passed

# 4. See it work end to end (simulated lab, safe anywhere)
python examples/01_bca_assay.py
python examples/06_memory_and_recovery.py
python examples/07_operations_and_provenance.py
```

## Quick start after verification

```bash
labaiagent serve --lab config/example_lab.yaml --readonly   # then open Claude/MCP on it
labaiagent serve --lab config/example_lab.yaml --http       # dashboard at http://127.0.0.1:8859/
```

Recommended rollout: `--readonly` → `--dry-run` → live with a low autonomy
ceiling. Before ANY networked deployment: replace every placeholder in
`config/principals.yaml`, set `LABAIAGENT_AUDIT_HMAC_KEY`, and enable TLS.

## Honest scope statement

The framework, safety engine, gateway, and recovery layer are exhaustively
tested in software (see `docs/VALIDATION.md`, including its Limitations
section). The hardware drivers are written and wire-verified against
published protocols but **have not driven physical instruments from this
codebase**; `docs/VALIDATION_PLAN.md` is the IQ/OQ/PQ a site executes to
close that gap. A software e-stop is a layer above hardware safety systems,
never a substitute.

Changelog for this release: see `CHANGELOG.md` ([1.4.1] and [1.4.0]).
