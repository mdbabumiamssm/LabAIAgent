# Security Policy

LabAIAgent mediates between AI agents and physical laboratory instruments.
A security defect here can become a physical-world incident, so reports are
taken seriously and handled quickly.

## Reporting a vulnerability

Email the maintainers privately (see the repository's contact information)
rather than opening a public issue. Include a reproduction if you can.
You should receive an acknowledgement within 72 hours.

## Built-in security layers (v1.1.0)

Defense in depth, from the wire inward:

1. **TLS** — `--tls-cert/--tls-key` (TLS ≥ 1.2), or terminate at a reverse
   proxy. Plaintext on a non-loopback interface prints a loud warning.
2. **Authentication** — hashed API keys, constant-time comparison; the
   server refuses to bind non-loopback without a principals file.
3. **Brute-force lockout** — 10 failed keys per minute from one address →
   HTTP 429 for 60 s, decided before any key comparison.
4. **Authorization** — roles (observer/operator/approver/admin) enforced
   per tool; agents can never mint approvals; e-stop callable by everyone.
5. **Per-actor safety** — autonomy ceilings and rate-limit windows keyed to
   the verified identity, enforced inside the safety engine — including
   through protocols and async jobs.
6. **Hardened HTTP** — chunked bodies refused (411, anti-smuggling), 2 MiB
   body cap, 500-step protocol cap, SSE subscriber cap, live-job cap,
   `nosniff`/`no-store` headers, opaque internal errors.
7. **Keyed audit trail** — set `LABAIAGENT_AUDIT_HMAC_KEY` (or pass
   `hmac_key`) so chain links are HMAC-SHA256: a whole-file rewrite without
   the key is detectable, not just in-place edits. Keep the key off the log
   host; anchor the head hash externally on a schedule.
8. **Individual e-signatures** — per-person PBKDF2 passwords
   (`labaiagent hash-password`), re-entered at each approval minting: the
   key authenticates the connection, the password authenticates the person.
   Failed signings are audited.
9. **Agent oversight** — refusal-streak suspension per identity (human-only
   reinstatement) and pre-execution review of HIGH/CRITICAL actuation:
   rule-based, or an independent foundation model that fails CLOSED.
   Knowledge inputs are provenance-gated (PubMed-only literature; templates
   cite only allowlisted publishers) and treated as data, never
   instructions.

## Deployment hardening checklist

- **Never expose the HTTP gateway without authentication.** The server
  refuses to bind to non-loopback interfaces without a principals file;
  do not work around this. Enable TLS (above) for anything beyond a
  trusted bench network.
- **Set an audit HMAC key** (`LABAIAGENT_AUDIT_HMAC_KEY`) injected at
  service start — not stored in the lab config.
- Give **every agent its own API key** with the lowest role and autonomy
  ceiling that does the job; store keys hashed (`api_key_sha256`).
- Start every rollout `--readonly`, then `--dry-run`, then live with
  `autonomy_ceiling: low`.
- Keep the audit log on storage the agent cannot write to directly, and
  archive `runs/*.jsonl` off-machine; the chain head hash is your
  tamper-evidence anchor.
- The software e-stop is a convenience layer. It is **not** a substitute
  for the hardware E-stop, interlocked enclosures, or light curtains.

## Scope notes

- The stdio MCP server trusts the local OS user (the process owner). Do not
  wrap it in a network transport yourself; use `--http --auth` instead.
- `read:raw` / `write:raw` style passthrough capabilities on generic
  drivers are HIGH risk by declaration; leave `write:raw` behind
  `requires_confirmation` (the default) or raise it to `critical` in your
  lab config.
