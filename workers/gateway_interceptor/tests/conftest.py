"""Test fixtures for the gateway_interceptor lambda.

Generates a fresh RSA keypair per test session, mints test JWTs against
it, and monkeypatches the handler's `_fetch_jwks` to serve the public
half — so tests don't need network or a running bridge.
"""
from __future__ import annotations

import base64
import hashlib
import time
from dataclasses import dataclass
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

from gateway_interceptor import handler


TEST_AUDIENCE = "agentcore-gateway"
TEST_ISSUER = "http://test-bridge"


def _int_to_base64url(n: int) -> str:
    byte_length = max((n.bit_length() + 7) // 8, 1)
    return base64.urlsafe_b64encode(n.to_bytes(byte_length, "big")).rstrip(b"=").decode("ascii")


@dataclass
class SigningKey:
    private_key: RSAPrivateKey
    kid: str
    private_pem: bytes
    jwk: dict[str, str]

    def mint(
        self,
        *,
        tenant_id: str = "slack-acme",
        audience: str = TEST_AUDIENCE,
        issuer: str = TEST_ISSUER,
        ttl: int = 300,
        kid: str | None = None,
    ) -> str:
        now = int(time.time())
        return jwt.encode(
            {
                "iss": issuer,
                "sub": tenant_id,
                "tenant_id": tenant_id,
                "aud": audience,
                "iat": now,
                "exp": now + ttl,
                "jti": "test",
            },
            self.private_pem,
            algorithm="RS256",
            headers={"kid": kid or self.kid},
        )

    def mint_expired(self, *, tenant_id: str = "slack-acme") -> str:
        now = int(time.time())
        return jwt.encode(
            {
                "iss": TEST_ISSUER,
                "sub": tenant_id,
                "tenant_id": tenant_id,
                "aud": TEST_AUDIENCE,
                "iat": now - 7200,
                "exp": now - 3600,
            },
            self.private_pem,
            algorithm="RS256",
            headers={"kid": self.kid},
        )


def _make_test_key() -> SigningKey:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    der = public_key.public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    digest = hashlib.sha256(der).digest()
    kid = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")[:16]
    nums = public_key.public_numbers()
    jwk = {
        "kty": "RSA",
        "use": "sig",
        "alg": "RS256",
        "kid": kid,
        "n": _int_to_base64url(nums.n),
        "e": _int_to_base64url(nums.e),
    }
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return SigningKey(private_key=private_key, kid=kid, private_pem=pem, jwk=jwk)


@pytest.fixture
def test_key() -> SigningKey:
    """Per-test fresh RSA key. Slow (~50ms RSA gen) but keeps tests
    independent so a kid-rotation test doesn't poison later tests."""
    return _make_test_key()


@pytest.fixture(autouse=True)
def _interceptor_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRIDGE_JWKS_URL", "http://test-bridge/jwks.json")
    monkeypatch.setenv("GATEWAY_JWT_ISSUER", TEST_ISSUER)
    monkeypatch.setenv("GATEWAY_JWT_AUDIENCE", TEST_AUDIENCE)
    monkeypatch.setenv("INTERCEPTOR_TARGET_DELIMITER", "___")
    handler._reset_jwks_cache_for_tests()
    yield
    handler._reset_jwks_cache_for_tests()


@pytest.fixture
def stub_jwks(monkeypatch: pytest.MonkeyPatch, test_key: SigningKey):
    """Replace the handler's JWKS fetcher with one that serves test_key."""
    fetch_count = {"n": 0}

    def fake_fetch() -> dict[str, jwt.PyJWK]:
        fetch_count["n"] += 1
        return {test_key.kid: jwt.PyJWK(test_key.jwk)}

    monkeypatch.setattr(handler, "_fetch_jwks", fake_fetch)
    return fetch_count


def make_request_event(
    *,
    method: str = "tools/list",
    tool_name: str | None = None,
    auth_header: str | None = None,
    include_headers: bool = True,
) -> dict[str, Any]:
    """Construct a synthetic interceptor input event."""
    body: dict[str, Any] = {"jsonrpc": "2.0", "id": 1, "method": method}
    if method == "tools/call" and tool_name is not None:
        body["params"] = {"name": tool_name, "arguments": {}}

    headers: dict[str, str] = {}
    if include_headers:
        headers["Accept"] = "application/json"
        if auth_header is not None:
            headers["Authorization"] = auth_header

    request: dict[str, Any] = {
        "path": "/mcp",
        "httpMethod": "POST",
        "body": body,
    }
    if include_headers:
        request["headers"] = headers

    return {
        "interceptorInputVersion": "1.0",
        "mcp": {
            "rawGatewayRequest": {"body": ""},
            "gatewayRequest": request,
        },
    }
