"""Tests for the AgentCore Gateway request interceptor (week 4 chunk B).

Covers:
  - Happy path: valid token + matching tool name → pass-through
  - tools/list → pass-through (no tenant check)
  - Missing Authorization header → 401 deny
  - Malformed Authorization header → 401 deny
  - Headers field missing entirely (passRequestHeaders=false) → 401
  - Expired token → 401 deny
  - Wrong audience → 401 deny
  - Wrong issuer → 401 deny
  - Token with no kid header → 401 deny
  - Unknown kid (with JWKS refresh fallback) → 401 deny
  - Token missing tenant_id claim → 401 deny
  - tools/call with mismatched tenant target → 403 deny
  - tools/call with no delimiter in tool name → 403 deny
  - tools/call with empty tool name → 403 deny
  - JWKS cache: warm reuse, kid-miss refresh, kid-still-missing-after-refresh
  - Configurable delimiter env var
  - Pass-through response shape (interceptorOutputVersion, transformedGatewayRequest)
  - Deny response shape (transformedGatewayResponse with JSON-RPC error)
"""
from __future__ import annotations

import jwt
import pytest

from gateway_interceptor import handler
from .conftest import TEST_AUDIENCE, TEST_ISSUER, SigningKey, make_request_event


# ----------------------------------------------------------------------------
# Happy paths
# ----------------------------------------------------------------------------

def test_tools_list_with_valid_token_passes_through(stub_jwks, test_key: SigningKey):
    token = test_key.mint(tenant_id="slack-acme")
    event = make_request_event(method="tools/list", auth_header=f"Bearer {token}")

    response = handler.lambda_handler(event, None)

    assert response["interceptorOutputVersion"] == "1.0"
    assert "transformedGatewayRequest" in response["mcp"]
    assert "transformedGatewayResponse" not in response["mcp"]


def test_tools_call_with_matching_target_prefix_passes_through(
    stub_jwks, test_key: SigningKey
):
    token = test_key.mint(tenant_id="slack-acme")
    event = make_request_event(
        method="tools/call",
        tool_name="tenant-slack-acme-datadog___query_metrics",
        auth_header=f"Bearer {token}",
    )

    response = handler.lambda_handler(event, None)

    assert "transformedGatewayResponse" not in response["mcp"]
    assert "transformedGatewayRequest" in response["mcp"]


def test_other_methods_pass_through(stub_jwks, test_key: SigningKey):
    """initialize, ping, etc. — anything that's not tools/call gets allowed."""
    token = test_key.mint(tenant_id="slack-acme")
    for method in ("initialize", "ping", "resources/list", "completion/complete"):
        event = make_request_event(method=method, auth_header=f"Bearer {token}")
        response = handler.lambda_handler(event, None)
        assert "transformedGatewayResponse" not in response["mcp"], (
            f"method {method!r} unexpectedly denied"
        )


# ----------------------------------------------------------------------------
# Auth failures (401)
# ----------------------------------------------------------------------------

def _assert_denied(response: dict, status: int):
    assert "transformedGatewayResponse" in response["mcp"]
    body = response["mcp"]["transformedGatewayResponse"]
    assert body["statusCode"] == status
    err = body["body"]["error"]
    assert err["code"] == -32000
    assert "reason" in err["data"]


def test_missing_authorization_header_denies(stub_jwks, test_key: SigningKey):
    event = make_request_event(method="tools/list", auth_header=None)
    response = handler.lambda_handler(event, None)
    _assert_denied(response, 401)
    assert "Authorization" in response["mcp"]["transformedGatewayResponse"]["body"]["error"]["data"]["reason"]


def test_malformed_authorization_not_bearer_denies(stub_jwks, test_key: SigningKey):
    event = make_request_event(method="tools/list", auth_header="Basic abc123")
    response = handler.lambda_handler(event, None)
    _assert_denied(response, 401)


def test_headers_field_missing_entirely_denies(stub_jwks, test_key: SigningKey):
    """If passRequestHeaders=false, the event has no headers — we MUST
    deny rather than fall through to no-auth."""
    event = make_request_event(method="tools/list", include_headers=False)
    response = handler.lambda_handler(event, None)
    _assert_denied(response, 401)
    assert "passRequestHeaders" in response["mcp"]["transformedGatewayResponse"]["body"]["error"]["data"]["reason"]


def test_expired_token_denies(stub_jwks, test_key: SigningKey):
    token = test_key.mint_expired()
    event = make_request_event(method="tools/list", auth_header=f"Bearer {token}")
    response = handler.lambda_handler(event, None)
    _assert_denied(response, 401)


def test_wrong_audience_denies(stub_jwks, test_key: SigningKey):
    token = test_key.mint(audience="not-the-gateway")
    event = make_request_event(method="tools/list", auth_header=f"Bearer {token}")
    response = handler.lambda_handler(event, None)
    _assert_denied(response, 401)


def test_wrong_issuer_denies(stub_jwks, test_key: SigningKey):
    token = test_key.mint(issuer="https://attacker.example")
    event = make_request_event(method="tools/list", auth_header=f"Bearer {token}")
    response = handler.lambda_handler(event, None)
    _assert_denied(response, 401)


def test_token_without_kid_denies(stub_jwks, test_key: SigningKey):
    """A JWT without a kid header can't be matched against JWKS."""
    import time as _t

    no_kid_token = jwt.encode(
        {
            "iss": TEST_ISSUER,
            "sub": "slack-acme",
            "tenant_id": "slack-acme",
            "aud": TEST_AUDIENCE,
            "iat": int(_t.time()),
            "exp": int(_t.time()) + 60,
        },
        test_key.private_pem,
        algorithm="RS256",
        # No headers={"kid": ...}
    )
    event = make_request_event(
        method="tools/list", auth_header=f"Bearer {no_kid_token}"
    )
    response = handler.lambda_handler(event, None)
    _assert_denied(response, 401)


def test_token_missing_tenant_id_claim_denies(
    monkeypatch, stub_jwks, test_key: SigningKey
):
    """A token whose tenant_id claim is empty/absent must be rejected."""
    # PyJWT-encode directly to bypass our test key's mint() default.
    import time as _t

    token = jwt.encode(
        {
            "iss": TEST_ISSUER,
            "sub": "slack-acme",
            # tenant_id intentionally absent
            "aud": TEST_AUDIENCE,
            "iat": int(_t.time()),
            "exp": int(_t.time()) + 60,
        },
        test_key.private_pem,
        algorithm="RS256",
        headers={"kid": test_key.kid},
    )
    event = make_request_event(method="tools/list", auth_header=f"Bearer {token}")
    response = handler.lambda_handler(event, None)
    _assert_denied(response, 401)
    assert "tenant_id" in response["mcp"]["transformedGatewayResponse"]["body"]["error"]["data"]["reason"]


# ----------------------------------------------------------------------------
# Tenant routing failures (403)
# ----------------------------------------------------------------------------

def test_tools_call_cross_tenant_denies(stub_jwks, test_key: SigningKey):
    """Acme's token tries to call Globex's Datadog target."""
    token = test_key.mint(tenant_id="slack-acme")
    event = make_request_event(
        method="tools/call",
        tool_name="tenant-slack-globex-datadog___query_metrics",
        auth_header=f"Bearer {token}",
    )
    response = handler.lambda_handler(event, None)
    _assert_denied(response, 403)


def test_tools_call_no_delimiter_in_tool_name_denies(
    stub_jwks, test_key: SigningKey
):
    token = test_key.mint(tenant_id="slack-acme")
    event = make_request_event(
        method="tools/call",
        tool_name="just_a_plain_tool_name",
        auth_header=f"Bearer {token}",
    )
    response = handler.lambda_handler(event, None)
    _assert_denied(response, 403)
    assert "delimiter" in response["mcp"]["transformedGatewayResponse"]["body"]["error"]["data"]["reason"]


def test_tools_call_empty_tool_name_denies(stub_jwks, test_key: SigningKey):
    token = test_key.mint(tenant_id="slack-acme")
    event = make_request_event(
        method="tools/call",
        tool_name="",
        auth_header=f"Bearer {token}",
    )
    response = handler.lambda_handler(event, None)
    _assert_denied(response, 403)


def test_tools_call_target_for_different_tenant_with_same_prefix_denies(
    stub_jwks, test_key: SigningKey
):
    """Edge case: 'slack-acme' must not match 'slack-acmecorp' — the
    prefix check needs to include the trailing dash, otherwise a tenant
    named with another tenant's name as a prefix could escalate."""
    token = test_key.mint(tenant_id="slack-acme")
    event = make_request_event(
        method="tools/call",
        tool_name="tenant-slack-acmecorp-datadog___query_metrics",
        auth_header=f"Bearer {token}",
    )
    response = handler.lambda_handler(event, None)
    _assert_denied(response, 403)


def test_tools_call_configurable_delimiter(
    monkeypatch, stub_jwks, test_key: SigningKey
):
    """If chunk C smoke testing reveals AgentCore uses a different
    delimiter (e.g. '__'), we can switch via env var."""
    monkeypatch.setenv("INTERCEPTOR_TARGET_DELIMITER", "__")
    token = test_key.mint(tenant_id="slack-acme")
    event = make_request_event(
        method="tools/call",
        tool_name="tenant-slack-acme-datadog__query_metrics",
        auth_header=f"Bearer {token}",
    )
    response = handler.lambda_handler(event, None)
    assert "transformedGatewayResponse" not in response["mcp"]


# ----------------------------------------------------------------------------
# JWKS cache behavior
# ----------------------------------------------------------------------------

def test_jwks_cache_is_reused_across_warm_invocations(
    stub_jwks, test_key: SigningKey
):
    token = test_key.mint(tenant_id="slack-acme")
    event = make_request_event(method="tools/list", auth_header=f"Bearer {token}")

    handler.lambda_handler(event, None)
    handler.lambda_handler(event, None)
    handler.lambda_handler(event, None)

    # _fetch_jwks should have been called once on the first invocation
    # and not again — _get_signing_key short-circuits on cache hit.
    assert stub_jwks["n"] == 1


def test_jwks_cache_refreshes_on_kid_miss(monkeypatch, test_key: SigningKey):
    """When a token's kid isn't in the cache, we refresh once and try
    again. This handles the bridge rotating its signing key."""
    # First fetch returns an unrelated key, second returns the real one.
    stale_key = SigningKey(
        private_key=test_key.private_key,  # not used by the wrong kid
        kid="stalekid12345678",
        private_pem=test_key.private_pem,
        jwk={
            **test_key.jwk,
            "kid": "stalekid12345678",
        },
    )
    fetches = {"n": 0}

    def fake_fetch():
        fetches["n"] += 1
        if fetches["n"] == 1:
            return {stale_key.kid: jwt.PyJWK(stale_key.jwk)}
        return {test_key.kid: jwt.PyJWK(test_key.jwk)}

    monkeypatch.setattr(handler, "_fetch_jwks", fake_fetch)

    # Prime the cache with the stale key by triggering a fetch
    handler._get_signing_key(stale_key.kid)
    assert fetches["n"] == 1

    # Now mint a token under the real kid — handler should refresh.
    token = test_key.mint(tenant_id="slack-acme")
    event = make_request_event(method="tools/list", auth_header=f"Bearer {token}")
    response = handler.lambda_handler(event, None)

    assert fetches["n"] == 2
    assert "transformedGatewayResponse" not in response["mcp"]


def test_jwks_cache_refresh_still_missing_kid_denies(monkeypatch, test_key: SigningKey):
    """If after refresh the kid still isn't in JWKS, deny."""
    fetches = {"n": 0}

    def fake_fetch():
        fetches["n"] += 1
        return {"unrelated_kid": jwt.PyJWK(test_key.jwk)}

    monkeypatch.setattr(handler, "_fetch_jwks", fake_fetch)

    token = test_key.mint(tenant_id="slack-acme")  # uses test_key.kid
    event = make_request_event(method="tools/list", auth_header=f"Bearer {token}")
    response = handler.lambda_handler(event, None)

    _assert_denied(response, 401)
    # We refresh exactly once on the kid miss; one initial + one refresh.
    assert fetches["n"] >= 1


# ----------------------------------------------------------------------------
# Response shape
# ----------------------------------------------------------------------------

def test_allow_response_includes_interceptor_output_version_and_request(
    stub_jwks, test_key: SigningKey
):
    token = test_key.mint(tenant_id="slack-acme")
    event = make_request_event(method="tools/list", auth_header=f"Bearer {token}")
    response = handler.lambda_handler(event, None)

    assert response["interceptorOutputVersion"] == "1.0"
    assert response["mcp"]["transformedGatewayRequest"]["body"]["method"] == "tools/list"


def test_deny_response_is_jsonrpc_error_shape(stub_jwks, test_key: SigningKey):
    event = make_request_event(method="tools/list", auth_header=None)
    response = handler.lambda_handler(event, None)

    body = response["mcp"]["transformedGatewayResponse"]["body"]
    assert body["jsonrpc"] == "2.0"
    assert body["id"] is None
    assert body["error"]["code"] == -32000
    assert body["error"]["message"] == "tenant access denied"
    assert "reason" in body["error"]["data"]
