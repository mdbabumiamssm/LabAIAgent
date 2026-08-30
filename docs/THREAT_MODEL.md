# Threat Model

LabAIAgent mediates between AI agents and physical laboratory instruments.
This document states what we protect, from whom, with which mechanisms, and
— just as importantly — what we explicitly do not defend against. Residual
risks are listed, not hidden.

## Assets

A1. **Physical integrity**: instruments, samples, reagents, and the people
    around them.
A2. **The audit trail**: the evidence of who did what, including refusals.
A3. **Experimental data** returned by reads and procedures.
A4. **Credentials**: API keys, approval tokens, the audit HMAC key.
A5. **Availability** of the control plane itself.

## Trust boundaries

```
 [AI agent / remote client]--(HTTP/TLS, API key)--→ [Gateway]
 [local agent (Claude Desktop etc.)]--(stdio pipe)--→ [MCP server]
                                  both → dispatch → LabSession.call
                                  → SafetyEngine → Driver → Transport → Instrument
```

- Everything left of the gateway is **untrusted** (including well-meaning
  agents: an LLM's tool call is treated as potentially wrong or hostile).
- The stdio pipe trusts the **local OS user** who launched the process —
  the same trust the shell itself grants. Do not re-expose stdio over a
  network; use `--http --auth`.
- The gateway process, the lab config, and the driver code are trusted:
  whoever can edit them owns the lab. Protect them with OS permissions and
  code review.

## Adversaries and mitigations

### T1. Malicious or malfunctioning agent with a valid key
The primary adversary. An LLM can emit destructive, repetitive, or
nonsensical calls with perfect syntax.

- Fail-closed six-layer safety engine on the single invocation path
  (e-stop latch → state gate → declared parameter limits → live interlocks
  → per-actor rate limits → human approval tokens above the ceiling).
- Per-actor autonomy ceilings; identity can only *narrow* permissions.
- Roles: an observer key cannot actuate; no agent role can mint approvals.
- Protocols and async jobs carry the submitter's identity into every step
  (an agent cannot launder its identity through a protocol — REVIEW 1.1).
- The oversight Supervisor suspends an identity after a refusal streak
  (arguing with the limits) and routes HIGH/CRITICAL actuation through an
  independent reviewer — rule-based or a second foundation model — that
  fails closed. Approval minting can additionally demand the individual's
  e-signature password.
- Live-job cap and per-actor sliding-window rate limits absorb runaway
  loops; anything long-running is cancellable between steps; the e-stop is
  callable by every role in every mode and latches until a named human
  clears it, after which each device must pass its self-test.

### T2. Network attacker (no key)
- Non-loopback binding refuses to start without an authenticator.
- TLS ≥ 1.2 (`--tls-cert/--tls-key`) or a TLS-terminating reverse proxy;
  plaintext non-loopback startup prints an explicit warning.
- API keys stored hashed (SHA-256) and compared constant-time; per-IP
  brute-force lockout answers 429 before any key comparison.
- Chunked-encoding requests are refused (411) to remove the request-desync
  / smuggling class; bodies capped at 2 MiB; protocols at 500 steps;
  responses carry `nosniff` / `no-store`.
- Unauthenticated surface is exactly one route: `GET /health`, returning
  `{"ok": true}` and nothing else.

### T3. Adversary with write access to the audit file
- Unkeyed mode detects *modification* (hash chain) — but a full-file
  rewrite can recompute an unkeyed chain. **Keyed mode (HMAC-SHA256)**
  defeats the rewrite unless the attacker also holds the key; keep the key
  off the log host (env injection/KMS) and anchor the head hash externally
  on a schedule.
- A log that fails verification stays readable for forensics but refuses
  new appends — a broken chain cannot be quietly extended.

### T4. Denial of service
- SSE subscriber cap (503 past it), live-job cap, per-actor rate limits,
  auth-failure lockout, body/step caps. Residual: a distributed flood still
  requires upstream network controls; the gateway is a lab service, not a
  CDN edge.

### T5. Malicious lab configuration / driver (insider)
- Config can only **tighten** driver-declared limits — a config that widens
  a range, lowers a risk class, or disables a confirmation is rejected at
  load.
- The conformance suite refuses drivers whose declared limits are not
  actually enforced (both ends of every range probed), whose reads actuate,
  or whose movers lack `_halt`.
- Residual: driver code itself is trusted Python. Review drivers like the
  control software they are; pin dependencies; use the entry-point plugin
  mechanism only for packages you would install anyway.

### T6. Prompt injection against the agent
Out of scope *for this layer* by design — and this is precisely why the
layer exists: whatever text convinces the model, the tool surface it
reaches is bounded by role, ceiling, limits, interlocks, and approvals.
The blast radius of a fully compromised operator-role agent is the set of
actions at or below its ceiling — visible, rate-limited, and audited.

### T7. Poisoned knowledge (literature/protocol injection)
An adversary cannot point the knowledge layer at arbitrary content: the
literature browser can query exactly one database (PubMed; every record
PMID-carrying), protocol templates refuse construction unless every citation
resolves to the trusted-source registry (suffix-safe domain matching defeats
look-alike hosts), and the registry has NO runtime extension API — adding a
source is a code change under review. Abstracts/titles are labelled and
handled as third-party text. Residual: PubMed content itself is trusted at
the level of peer review, no further.


## Explicit non-goals

No IEC 61508 / ISO 13849 functional-safety claim; no substitute for
hardware E-stops or interlocked enclosures; no real-time guarantees; no
multi-node high availability; no defense against an adversary with root on
the gateway host (they own the process and the lab).
