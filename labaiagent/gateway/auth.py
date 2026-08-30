"""Principals, roles, and API-key authentication.

The audit trail is only as trustworthy as the ``actor`` field, and an actor
that any client can claim is decoration. A ``Principal`` is a *verified*
identity: agents and humans present an API key, the gateway resolves it, and
the principal id -- not a self-reported string -- is what reaches the audit
chain, the rate limiter, and the per-actor autonomy ceiling.

principals.yaml:

    principals:
      - id: agent:claude
        kind: agent
        role: operator            # observer | operator | approver | admin
        api_key: "lak_live_..."   # or api_key_sha256: "<hex>"
        autonomy_ceiling: medium  # optional, further limits this identity
      - id: user:babu
        kind: human
        role: admin
        api_key_sha256: "9f2c..."
    allow_anonymous: false        # true -> unauthenticated callers become
    anonymous_role: observer      #         this read-only principal

Keys may be stored hashed (``api_key_sha256``) so the config file itself is
not a credential store. Plaintext ``api_key`` is accepted for lab-bench
convenience and hashed at load time.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from ..core.errors import ConfigurationError
from ..core.types import Risk


class Role(str, Enum):
    OBSERVER = "observer"    # read-only tools
    OPERATOR = "operator"    # actuation up to the effective ceiling
    APPROVER = "approver"    # + may mint approval tokens
    ADMIN = "admin"          # + e-stop reset, policy changes

    @property
    def rank(self) -> int:
        return {"observer": 0, "operator": 1, "approver": 2, "admin": 3}[self.value]

    def allows(self, required: Role) -> bool:
        return self.rank >= required.rank


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# -- individual passwords (electronic signatures) ---------------------------
#
# API keys authenticate the CONNECTION; passwords authenticate the PERSON at
# signature moments (minting an approval token). This is the two-component
# control 21 CFR Part 11 expects of an electronic signature: something the
# session holds (the key) plus something only the individual knows (the
# password), re-entered at the moment of signing.

_PBKDF2_ITERATIONS = 200_000


def hash_password(password: str, *, iterations: int = _PBKDF2_ITERATIONS,
                  _salt: bytes | None = None) -> str:
    """PBKDF2-HMAC-SHA256, salted. Format:
    ``pbkdf2_sha256$<iterations>$<salt hex>$<hash hex>``."""
    if not password:
        raise ValueError("Empty passwords are not accepted.")
    salt = _salt if _salt is not None else os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt,
                             iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${dk.hex()}"


def verify_password_hash(stored: str, password: str) -> bool:
    """Constant-time verification of a stored PBKDF2 entry."""
    try:
        scheme, iters, salt_hex, hash_hex = stored.split("$")
        if scheme != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(dk.hex(), hash_hex)
    except (ValueError, AttributeError):
        return False


@dataclass
class Principal:
    id: str
    kind: str = "agent"                    # agent | human | service
    role: Role = Role.OBSERVER
    api_key_sha256: str = ""
    #: PBKDF2 password entry. When set, signature-level actions (minting an
    #: approval token) require the password to be re-entered -- the identity
    #: component of an electronic signature.
    password_pbkdf2: str = ""
    autonomy_ceiling: Risk | None = None   # None -> session ceiling applies
    rate_limits: dict[str, dict[str, float]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "role": self.role.value,
                "autonomy_ceiling": (self.autonomy_ceiling.value
                                     if self.autonomy_ceiling else None)}


#: The safe default when no auth config is provided at all: a single
#: anonymous operator, matching the trust model of a local stdio pipe where
#: the OS user already owns the process.
LOCAL_PRINCIPAL = Principal(id="local", kind="service", role=Role.OPERATOR)

ANONYMOUS_OBSERVER = Principal(id="anonymous", kind="service", role=Role.OBSERVER)


class Authenticator:
    """Resolves API keys to principals. Constant-time comparison throughout."""

    def __init__(self, principals: Iterable[Principal] = (), *,
                 allow_anonymous: bool = False,
                 anonymous_role: Role = Role.OBSERVER) -> None:
        self._by_hash: dict[str, Principal] = {}
        self._by_id: dict[str, Principal] = {}
        for p in principals:
            if p.id in self._by_id:
                raise ConfigurationError(f"Duplicate principal id {p.id!r}")
            self._by_id[p.id] = p
            if p.api_key_sha256:
                self._by_hash[p.api_key_sha256] = p
        self.allow_anonymous = allow_anonymous
        self.anonymous = Principal(id="anonymous", kind="service",
                                   role=anonymous_role)

    # -- construction --------------------------------------------------------

    @classmethod
    def from_config(cls, source: str | Path | dict[str, Any]) -> Authenticator:
        if not isinstance(source, dict):
            import yaml
            p = Path(source)
            if not p.exists():
                raise ConfigurationError(f"Principals file not found: {p}")
            source = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        principals: list[Principal] = []
        for spec in source.get("principals", []):
            if "id" not in spec:
                raise ConfigurationError(f"Principal stanza missing 'id': {spec}")
            key_hash = spec.get("api_key_sha256", "")
            if not key_hash and spec.get("api_key"):
                key_hash = _sha256(str(spec["api_key"]))
            pw_entry = spec.get("password_pbkdf2", "")
            if not pw_entry and spec.get("password"):
                # Plaintext accepted for bench convenience; hashed at load so
                # it never lives in memory beyond this point.
                pw_entry = hash_password(str(spec["password"]))
            ceiling = spec.get("autonomy_ceiling")
            principals.append(Principal(
                id=str(spec["id"]),
                kind=spec.get("kind", "agent"),
                role=Role(spec.get("role", "observer")),
                api_key_sha256=key_hash,
                password_pbkdf2=pw_entry,
                autonomy_ceiling=Risk(ceiling) if ceiling else None,
                rate_limits=spec.get("rate_limits", {}) or {},
            ))
        return cls(principals,
                   allow_anonymous=bool(source.get("allow_anonymous", False)),
                   anonymous_role=Role(source.get("anonymous_role", "observer")))

    # -- resolution -----------------------------------------------------------

    def authenticate(self, api_key: str | None) -> Principal | None:
        """Return the principal for a presented key, the anonymous principal
        if none was presented and anonymous access is on, else None."""
        if api_key:
            presented = _sha256(api_key)
            for stored, principal in self._by_hash.items():
                if hmac.compare_digest(stored, presented):
                    return principal
            return None
        return self.anonymous if self.allow_anonymous else None

    def get(self, principal_id: str) -> Principal | None:
        return self._by_id.get(principal_id)

    def verify_password(self, principal: Principal, password: str) -> bool:
        """Electronic-signature check for one individual. False when the
        principal has no password on file (they cannot e-sign) or when the
        password does not match."""
        if not principal.password_pbkdf2:
            return False
        return verify_password_hash(principal.password_pbkdf2, password)

    def principals(self) -> list[Principal]:
        return list(self._by_id.values())

    # -- session wiring --------------------------------------------------------

    def apply_to_session(self, session: Any) -> None:
        """Install per-principal ceilings and rate limits into the safety
        engine, so identity limits are enforced at the single policy point
        rather than in each adapter."""
        for p in self._by_id.values():
            if p.autonomy_ceiling is not None:
                session.safety.actor_ceilings[p.id] = p.autonomy_ceiling
            for key, spec in p.rate_limits.items():
                # Per-principal limits reuse the per-actor windows of the
                # shared limiter; the tightest configured window wins.
                session.safety.set_rate_limit(
                    key, int(spec["max_calls"]), float(spec["window_s"]))


__all__ = ["Role", "Principal", "Authenticator", "LOCAL_PRINCIPAL",
           "ANONYMOUS_OBSERVER", "hash_password", "verify_password_hash"]
