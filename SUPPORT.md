# Support policy

## Version support

| Series | Status | Receives |
|---|---|---|
| Latest MINOR (currently 1.4.x) | **Supported** | features, fixes, security patches |
| Previous MINOR | Maintenance | security patches and safety-relevant fixes only |
| Older | End of life | nothing — upgrade path documented in CHANGELOG |

A lab that cannot upgrade mid-study should pin (`labaiagent==X.Y.Z`) and
record the pin in its validation file; the CHANGELOG names, per release,
which OQ rows re-validation touches (docs/RELEASE_PROCESS.md G7).

## Deprecation policy

Public APIs (the 20 tools and their schemas, the `Device` driver contract,
`LabSession`, the REST routes, the audit and run-record schemas) are
deprecated in a MINOR with a runtime warning and removed no earlier than the
next MAJOR. Safety behavior is never "deprecated looser" — a control can
tighten in a MINOR (announced under "Tightened"), and only a MAJOR with a
migration note may relax one.

## Getting help

1. **Bugs and feature requests**: GitHub issues, with the template's
   environment block (version, Python, OS, transport, simulated/real).
   For a suspected *safety* defect (anything that could actuate wrongly),
   title it `[safety]` — these are triaged first.
2. **Security vulnerabilities**: never a public issue — follow
   `SECURITY.md` (private disclosure; acknowledgment within 72 hours).
3. **Integration questions** (new drivers, agent frameworks): GitHub
   discussions; the driver checklist in CONTRIBUTING.md answers most of
   them.

## Severity and response targets (self-hosted, best effort)

| Severity | Definition | Target |
|---|---|---|
| S1 | Actuation contrary to a safety rule; audit-integrity defect; auth bypass | Hotfix branch within 72 h of confirmation |
| S2 | Wrong behavior on a control path, workaround exists | Next PATCH |
| S3 | Non-control defect, docs, ergonomics | Next MINOR |

These are engineering commitments for the open-source project, not a
commercial SLA. A deployment that needs contractual response times, 24/7
coverage, or indemnification needs a commercial support agreement on top —
that is a business arrangement, not a code feature, and no such promise is
implied by this document.
