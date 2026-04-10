"""JWT issuer + JWKS for AgentCore Gateway CUSTOM_JWT authorization.

The bridge mints a short-lived RS256 JWT per agent invocation and threads
it into the agent payload's `ctx.gateway_jwt`. The agent forwards it as a
`Authorization: Bearer <jwt>` header on every MCP call to the shared
Gateway. The Gateway's CUSTOM_JWT authorizer fetches our JWKS via the
OIDC discovery URL (served by `bridge/main.py`) and verifies the
signature; the per-tenant interceptor Lambda then reads the `tenant_id`
claim from the verified token to enforce per-target tenant scoping.

## Key management

In production, the RSA private key lives in
`BRIDGE_GATEWAY_JWT_PRIVATE_KEY_PEM` (PEM-encoded). In LOCAL_DEV the
module generates a fresh keypair at first use and logs a warning — this
means JWKS rotates on every bridge restart, which is fine for dev because
the Gateway re-fetches JWKS on key-not-found.

## Why RS256 and not HS256

JWKS only supports asymmetric keys (the Gateway needs to verify with the
public half). HS256 would require sharing the symmetric secret with AWS,
which defeats the point. RS256 is the safest broadly-supported option.

## Why a separate secret from BRIDGE_OAUTH_STATE_SECRET

OAuth state tokens and onboarding session tokens are HMAC over a shared
symmetric secret (see slack_oauth.py). Gateway JWTs need a private key.
Different shape, different lifecycle (state secret rotates per env;
JWT key may rotate independently as part of crypto hygiene). Keep them
separate so a rotation of one doesn't invalidate the other.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import time
import uuid
from functools import lru_cache
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, RSAPublicKey

log = logging.getLogger(__name__)

# JWT claim conventions for AgentCore Gateway. The audience is fixed —
# the Gateway's CUSTOM_JWT authorizer is configured with this in
# `allowedAudience`, so any token with a different `aud` is rejected.
JWT_AUDIENCE = "agentcore-gateway"
JWT_ALGORITHM = "RS256"

# Default lifetime for a minted token. Five minutes is enough to cover
# the longest single agent invocation we'd realistically run; tools that
# go background via add_async_task get separate tokens on each Gateway
# call (we don't keep one alive for hours).
DEFAULT_JWT_TTL_SECONDS = 300


def _int_to_base64url(n: int) -> str:
    """Convert a non-negative int to unpadded base64url for JWK n/e fields."""
    byte_length = max((n.bit_length() + 7) // 8, 1)
    raw = n.to_bytes(byte_length, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _compute_kid(public_key: RSAPublicKey) -> str:
    """Stable key ID derived from the SubjectPublicKeyInfo DER fingerprint.

    Using a fingerprint (rather than a UUID) means the same key always
    gets the same kid across processes — important if the bridge runs as
    multiple replicas reading the same secret.
    """
    der = public_key.public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    digest = hashlib.sha256(der).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")[:16]


class _JwtKey:
    """Loaded RSA keypair plus its derived kid + JWK representation."""

    def __init__(self, private_key: RSAPrivateKey) -> None:
        self.private_key = private_key
        self.public_key = private_key.public_key()
        self.kid = _compute_kid(self.public_key)
        nums = self.public_key.public_numbers()
        self.jwk: dict[str, str] = {
            "kty": "RSA",
            "use": "sig",
            "alg": JWT_ALGORITHM,
            "kid": self.kid,
            "n": _int_to_base64url(nums.n),
            "e": _int_to_base64url(nums.e),
        }
        # PEM form, used by PyJWT.encode. Cached so we don't re-serialize
        # on every mint() call.
        self.private_pem = self.private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )


@lru_cache(maxsize=1)
def _load_key() -> _JwtKey:
    """Load the RSA key from env, or generate one for LOCAL_DEV.

    Cached for the lifetime of the process — call `_reset_key_cache()`
    in tests to reload after monkeypatching env vars.
    """
    pem = os.getenv("BRIDGE_GATEWAY_JWT_PRIVATE_KEY_PEM")
    if pem:
        private_key = serialization.load_pem_private_key(
            pem.encode("utf-8") if isinstance(pem, str) else pem,
            password=None,
        )
        if not isinstance(private_key, RSAPrivateKey):
            raise RuntimeError(
                "BRIDGE_GATEWAY_JWT_PRIVATE_KEY_PEM is not an RSA private key"
            )
        return _JwtKey(private_key)

    # No key configured — generate an ephemeral one. Only acceptable in
    # LOCAL_DEV. The warning is loud because shipping this to production
    # silently would mean every restart invalidates outstanding tokens.
    if os.getenv("LOCAL_DEV") != "1":
        raise RuntimeError(
            "BRIDGE_GATEWAY_JWT_PRIVATE_KEY_PEM is required in production. "
            "Generate with: openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048"
        )
    log.warning(
        "BRIDGE_GATEWAY_JWT_PRIVATE_KEY_PEM not set; generating ephemeral RSA key "
        "(LOCAL_DEV only — production must provide a stable key)"
    )
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return _JwtKey(private_key)


def _reset_key_cache() -> None:
    """Test helper: drop the cached key so the next call re-reads env."""
    _load_key.cache_clear()


def _issuer_url() -> str:
    """Public URL the Gateway will use to fetch our OIDC discovery doc.

    In LOCAL_DEV defaults to http://localhost:8000. In production this
    must be set to the bridge's public origin (e.g. an ngrok URL during
    testing, or the load balancer URL once the bridge runs on Fargate).
    """
    return os.getenv("BRIDGE_PUBLIC_URL", "http://localhost:8000").rstrip("/")


def mint_token(
    tenant_id: str,
    *,
    ttl_seconds: int = DEFAULT_JWT_TTL_SECONDS,
    extra_claims: dict[str, Any] | None = None,
) -> str:
    """Mint a fresh per-invocation JWT for `tenant_id`.

    Claims:
      iss        — bridge public URL (matches OIDC discovery `issuer`)
      sub        — tenant_id (also exposed as `tenant_id` claim for clarity)
      tenant_id  — primary routing claim, read by interceptor Lambda
      aud        — JWT_AUDIENCE constant (Gateway's allowedAudience)
      iat        — now (unix seconds)
      exp        — now + ttl_seconds
      jti        — random per-call uuid (for log correlation)

    Tenant IDs must be non-empty. The mint call is cheap (RS256 sign on
    a 2048-bit key is sub-millisecond) so we don't cache tokens — every
    agent invocation gets its own.
    """
    if not tenant_id:
        raise ValueError("mint_token: tenant_id is required")

    key = _load_key()
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": _issuer_url(),
        "sub": tenant_id,
        "tenant_id": tenant_id,
        "aud": JWT_AUDIENCE,
        "iat": now,
        "exp": now + ttl_seconds,
        "jti": uuid.uuid4().hex,
    }
    if extra_claims:
        # Don't let extras clobber security-relevant claims.
        for reserved in ("iss", "sub", "tenant_id", "aud", "iat", "exp"):
            extra_claims.pop(reserved, None)
        claims.update(extra_claims)

    return jwt.encode(
        claims,
        key.private_pem,
        algorithm=JWT_ALGORITHM,
        headers={"kid": key.kid},
    )


def verify_token(token: str) -> dict[str, Any]:
    """Verify a JWT minted by this module and return the decoded claims.

    Used by tests and by any local consumer that wants to round-trip a
    token. The Gateway's authorizer does its own verification via the
    JWKS endpoint — this function exists for symmetric testing.
    """
    key = _load_key()
    return jwt.decode(
        token,
        key.public_key,
        algorithms=[JWT_ALGORITHM],
        audience=JWT_AUDIENCE,
        issuer=_issuer_url(),
    )


def get_jwks() -> dict[str, Any]:
    """Return the JWKS document served at /jwks.json."""
    key = _load_key()
    return {"keys": [key.jwk]}


def get_oidc_configuration() -> dict[str, Any]:
    """Return the OIDC discovery document served at /.well-known/openid-configuration.

    Minimum fields the Gateway's CUSTOM_JWT authorizer needs:
      issuer
      jwks_uri
      response_types_supported
      subject_types_supported
      id_token_signing_alg_values_supported

    The authorization_endpoint and token_endpoint fields are placeholder
    URLs — we're not actually an OAuth IdP, we just expose the discovery
    + JWKS surface for verification. Some OIDC clients require these
    fields to be present even when unused.
    """
    issuer = _issuer_url()
    return {
        "issuer": issuer,
        "jwks_uri": f"{issuer}/jwks.json",
        "authorization_endpoint": f"{issuer}/oidc/authorize",
        "token_endpoint": f"{issuer}/oidc/token",
        "response_types_supported": ["id_token"],
        "subject_types_supported": ["public"],
        "id_token_signing_alg_values_supported": [JWT_ALGORITHM],
    }
