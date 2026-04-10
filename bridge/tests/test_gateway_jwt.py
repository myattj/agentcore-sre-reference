"""Tests for the AgentCore Gateway JWT issuer + JWKS surface (week 4 chunk A).

Covers:
  - mint_token / verify_token roundtrip with the LOCAL_DEV ephemeral key
  - JWT claims shape (iss, aud, sub, tenant_id, iat, exp, jti)
  - JWKS document shape and that the published key matches the signing key
  - OIDC discovery document fields the Gateway needs
  - Stable-key path: BRIDGE_GATEWAY_JWT_PRIVATE_KEY_PEM is honored
  - Production safety: missing key WITHOUT LOCAL_DEV raises
  - Tenant ID validation
  - /jwks.json + /.well-known/openid-configuration FastAPI routes
  - Verification rejects tokens with the wrong audience or issuer
"""
from __future__ import annotations

import time

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from bridge import gateway_jwt
from bridge.gateway_jwt import (
    JWT_ALGORITHM,
    JWT_AUDIENCE,
    get_jwks,
    get_oidc_configuration,
    mint_token,
    verify_token,
)


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------

def _generate_pem() -> str:
    """Generate a fresh RSA private key in PEM form, for tests that want
    to inject their own stable key via env."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")


# ----------------------------------------------------------------------------
# mint / verify roundtrip
# ----------------------------------------------------------------------------

def test_mint_returns_three_part_jwt() -> None:
    token = mint_token("slack-acme")
    assert token.count(".") == 2  # header.payload.signature


def test_mint_verify_roundtrip_returns_claims() -> None:
    token = mint_token("slack-acme")
    claims = verify_token(token)
    assert claims["sub"] == "slack-acme"
    assert claims["tenant_id"] == "slack-acme"
    assert claims["aud"] == JWT_AUDIENCE
    assert claims["iss"] == "http://localhost:8000"  # default _issuer_url
    assert "iat" in claims
    assert "exp" in claims
    assert claims["exp"] > claims["iat"]
    assert "jti" in claims and len(claims["jti"]) == 32  # uuid4 hex


def test_mint_default_ttl_is_300_seconds() -> None:
    before = int(time.time())
    token = mint_token("slack-acme")
    after = int(time.time())
    claims = verify_token(token)
    # Should be ~5 minutes from now (allow a 2-second test fudge factor).
    assert before + 298 <= claims["exp"] <= after + 302


def test_mint_custom_ttl_respected() -> None:
    token = mint_token("slack-acme", ttl_seconds=60)
    claims = verify_token(token)
    assert claims["exp"] - claims["iat"] == 60


def test_mint_jti_is_unique_per_call() -> None:
    a = verify_token(mint_token("slack-acme"))
    b = verify_token(mint_token("slack-acme"))
    assert a["jti"] != b["jti"]


def test_mint_extra_claims_merged_but_cannot_override_security_fields() -> None:
    token = mint_token(
        "slack-acme",
        extra_claims={
            "custom": "hello",
            # These should be silently dropped by the merge logic.
            "iss": "https://attacker.example",
            "aud": "wrong",
            "tenant_id": "slack-other",
        },
    )
    claims = verify_token(token)
    assert claims["custom"] == "hello"
    assert claims["iss"] == "http://localhost:8000"
    assert claims["aud"] == JWT_AUDIENCE
    assert claims["tenant_id"] == "slack-acme"


def test_mint_rejects_empty_tenant_id() -> None:
    with pytest.raises(ValueError, match="tenant_id"):
        mint_token("")


# ----------------------------------------------------------------------------
# Verification negatives
# ----------------------------------------------------------------------------

def test_verify_rejects_wrong_audience() -> None:
    """A token signed by us but with a different audience must fail."""
    gateway_jwt._reset_key_cache()
    key = gateway_jwt._load_key()
    bad = jwt.encode(
        {
            "iss": "http://localhost:8000",
            "sub": "slack-acme",
            "tenant_id": "slack-acme",
            "aud": "not-the-gateway",
            "iat": int(time.time()),
            "exp": int(time.time()) + 60,
        },
        key.private_pem,
        algorithm=JWT_ALGORITHM,
        headers={"kid": key.kid},
    )
    with pytest.raises(jwt.InvalidAudienceError):
        verify_token(bad)


def test_verify_rejects_wrong_issuer() -> None:
    gateway_jwt._reset_key_cache()
    key = gateway_jwt._load_key()
    bad = jwt.encode(
        {
            "iss": "https://attacker.example",
            "sub": "slack-acme",
            "tenant_id": "slack-acme",
            "aud": JWT_AUDIENCE,
            "iat": int(time.time()),
            "exp": int(time.time()) + 60,
        },
        key.private_pem,
        algorithm=JWT_ALGORITHM,
        headers={"kid": key.kid},
    )
    with pytest.raises(jwt.InvalidIssuerError):
        verify_token(bad)


def test_verify_rejects_expired_token() -> None:
    gateway_jwt._reset_key_cache()
    key = gateway_jwt._load_key()
    bad = jwt.encode(
        {
            "iss": "http://localhost:8000",
            "sub": "slack-acme",
            "tenant_id": "slack-acme",
            "aud": JWT_AUDIENCE,
            "iat": int(time.time()) - 7200,
            "exp": int(time.time()) - 3600,  # 1h ago
        },
        key.private_pem,
        algorithm=JWT_ALGORITHM,
        headers={"kid": key.kid},
    )
    with pytest.raises(jwt.ExpiredSignatureError):
        verify_token(bad)


# ----------------------------------------------------------------------------
# JWKS
# ----------------------------------------------------------------------------

def test_jwks_shape() -> None:
    jwks = get_jwks()
    assert "keys" in jwks
    assert len(jwks["keys"]) == 1
    k = jwks["keys"][0]
    for required_field in ("kty", "use", "alg", "kid", "n", "e"):
        assert required_field in k, f"missing JWK field: {required_field}"
    assert k["kty"] == "RSA"
    assert k["alg"] == "RS256"
    assert k["use"] == "sig"


def test_jwks_kid_matches_minted_token_header() -> None:
    """The kid in JWKS must match the kid embedded in tokens we mint —
    otherwise the Gateway authorizer can't find the key to verify with."""
    token = mint_token("slack-acme")
    header = jwt.get_unverified_header(token)
    jwks = get_jwks()
    assert header["kid"] == jwks["keys"][0]["kid"]


def test_jwks_can_be_used_to_verify_a_minted_token() -> None:
    """Round-trip: mint with the private key, verify with the public key
    derived from the JWKS document. This is the exact flow the Gateway
    authorizer follows."""
    token = mint_token("slack-acme")
    jwks = get_jwks()
    # PyJWT can build a verifier directly from a JWK dict.
    public_key = jwt.PyJWK(jwks["keys"][0]).key
    claims = jwt.decode(
        token,
        public_key,
        algorithms=[JWT_ALGORITHM],
        audience=JWT_AUDIENCE,
        issuer="http://localhost:8000",
    )
    assert claims["tenant_id"] == "slack-acme"


# ----------------------------------------------------------------------------
# OIDC discovery
# ----------------------------------------------------------------------------

def test_oidc_configuration_required_fields() -> None:
    cfg = get_oidc_configuration()
    for required_field in (
        "issuer",
        "jwks_uri",
        "response_types_supported",
        "subject_types_supported",
        "id_token_signing_alg_values_supported",
    ):
        assert required_field in cfg, f"missing OIDC field: {required_field}"
    assert cfg["jwks_uri"].endswith("/jwks.json")
    assert cfg["jwks_uri"].startswith(cfg["issuer"])
    assert "RS256" in cfg["id_token_signing_alg_values_supported"]


def test_oidc_configuration_honors_bridge_public_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRIDGE_PUBLIC_URL", "https://bridge.example.com")
    gateway_jwt._reset_key_cache()
    cfg = get_oidc_configuration()
    assert cfg["issuer"] == "https://bridge.example.com"
    assert cfg["jwks_uri"] == "https://bridge.example.com/jwks.json"


def test_oidc_configuration_strips_trailing_slash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRIDGE_PUBLIC_URL", "https://bridge.example.com/")
    gateway_jwt._reset_key_cache()
    cfg = get_oidc_configuration()
    assert cfg["issuer"] == "https://bridge.example.com"


# ----------------------------------------------------------------------------
# Key source: env-provided PEM vs ephemeral generation
# ----------------------------------------------------------------------------

def test_stable_key_from_env_is_loaded(monkeypatch: pytest.MonkeyPatch) -> None:
    pem = _generate_pem()
    monkeypatch.setenv("BRIDGE_GATEWAY_JWT_PRIVATE_KEY_PEM", pem)
    gateway_jwt._reset_key_cache()

    key1 = gateway_jwt._load_key()
    gateway_jwt._reset_key_cache()
    key2 = gateway_jwt._load_key()
    # Same PEM in env → same kid across reloads.
    assert key1.kid == key2.kid


def test_ephemeral_keys_are_unique_across_reloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BRIDGE_GATEWAY_JWT_PRIVATE_KEY_PEM", raising=False)
    gateway_jwt._reset_key_cache()
    key1 = gateway_jwt._load_key()
    gateway_jwt._reset_key_cache()
    key2 = gateway_jwt._load_key()
    assert key1.kid != key2.kid


def test_production_without_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without LOCAL_DEV=1, a missing key must raise — never silently
    generate ephemeral keys in production."""
    monkeypatch.delenv("BRIDGE_GATEWAY_JWT_PRIVATE_KEY_PEM", raising=False)
    monkeypatch.delenv("LOCAL_DEV", raising=False)
    gateway_jwt._reset_key_cache()
    with pytest.raises(RuntimeError, match="BRIDGE_GATEWAY_JWT_PRIVATE_KEY_PEM"):
        gateway_jwt._load_key()


def test_production_with_non_rsa_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """If someone hands us an EC or Ed25519 PEM, fail loudly rather than
    silently producing a non-RS256 token Gateway would reject anyway."""
    from cryptography.hazmat.primitives.asymmetric import ec

    ec_key = ec.generate_private_key(ec.SECP256R1())
    pem = ec_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    monkeypatch.setenv("BRIDGE_GATEWAY_JWT_PRIVATE_KEY_PEM", pem)
    gateway_jwt._reset_key_cache()
    with pytest.raises(RuntimeError, match="not an RSA"):
        gateway_jwt._load_key()


# ----------------------------------------------------------------------------
# FastAPI routes
# ----------------------------------------------------------------------------

def test_jwks_route_serves_published_jwks() -> None:
    from bridge.main import app

    with TestClient(app) as client:
        resp = client.get("/jwks.json")
    assert resp.status_code == 200
    body = resp.json()
    assert "keys" in body
    assert len(body["keys"]) == 1
    assert body["keys"][0]["kty"] == "RSA"


def test_oidc_discovery_route_serves_configuration() -> None:
    from bridge.main import app

    with TestClient(app) as client:
        resp = client.get("/.well-known/openid-configuration")
    assert resp.status_code == 200
    body = resp.json()
    assert "issuer" in body
    assert "jwks_uri" in body
    assert body["jwks_uri"].endswith("/jwks.json")


def test_jwks_and_discovery_routes_are_consistent() -> None:
    """End-to-end: a token minted by the bridge can be verified using
    only the public surfaces (discovery → jwks → verify). This is what
    the Gateway authorizer does."""
    from bridge.main import app

    token = mint_token("slack-acme")
    with TestClient(app) as client:
        oidc = client.get("/.well-known/openid-configuration").json()
        jwks = client.get(oidc["jwks_uri"].replace("http://testserver", "")).json()

    public_key = jwt.PyJWK(jwks["keys"][0]).key
    claims = jwt.decode(
        token,
        public_key,
        algorithms=[JWT_ALGORITHM],
        audience=JWT_AUDIENCE,
        issuer=oidc["issuer"],
    )
    assert claims["tenant_id"] == "slack-acme"
