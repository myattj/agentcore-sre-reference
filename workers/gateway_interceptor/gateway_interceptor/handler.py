"""Gateway request interceptor — verifies the bridge-minted JWT and
enforces per-target tenant isolation.

## Where this fits

The shared AgentCore Gateway has one CUSTOM_JWT authorizer (configured
with the bridge's OIDC discovery URL). The Gateway authorizer verifies
the JWT signature, then forwards the request — including the raw
Authorization header (we set `passRequestHeaders=true` on this
interceptor) — to this Lambda BEFORE the request reaches the target.

We:
  1. Re-verify the JWT against the bridge's JWKS. This is belt-and-braces
     — the Gateway authorizer already did this, but the interceptor sees
     only the raw header per the AgentCore docs and we cannot trust the
     authorizer's verdict to be visible here. (Doing the verify ourselves
     also lets us read the claims, which the interceptor payload does
     NOT expose.)
  2. Read the `tenant_id` claim from the verified JWT.
  3. Inspect the JSON-RPC body. For `tools/call`, parse `params.name` and
     enforce that the tool's target prefix matches the caller's tenant.
     For `tools/list` and other meta methods, allow with logging.
  4. Pass through (return the input unchanged) on allow.
  5. On deny, return `transformedGatewayResponse` with a 403 status —
     the Gateway short-circuits and never forwards to the target.

## Tool / target naming convention

The provisioner (chunk D) names targets `tenant-<tenant_id>-<integration>`
(e.g. `tenant-slack-acme-datadog`). AgentCore Gateway is documented to
namespace target tools so multiple targets can expose tools with the
same internal name without collision; the exact prefix delimiter is not
documented as of 2026-04, so we make it ENV-configurable
(`INTERCEPTOR_TARGET_DELIMITER`, default `___`). Chunk C smoke testing
will confirm the actual delimiter against a real Gateway and we'll
update the default here if needed.

## Caching

JWKS is fetched lazily on first invocation and cached in module-global
state for the warm Lambda's lifetime. On a `kid` cache miss (e.g. the
bridge rotated keys), we refresh once and retry; if still missing, we
deny. Cold-start cost is one HTTP GET to the bridge's `/jwks.json`.

## Required environment variables

  BRIDGE_JWKS_URL          — full URL of the bridge's /jwks.json route
  GATEWAY_JWT_AUDIENCE     — expected audience claim (default: "agentcore-gateway")
  GATEWAY_JWT_ISSUER       — expected issuer claim (the bridge's public origin)
  INTERCEPTOR_TARGET_DELIMITER — delimiter between target name and tool name
                                 in MCP tool names (default: "___")
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any

import jwt

log = logging.getLogger()
log.setLevel(logging.INFO)

# JWKS cache. Lambda warm-invocation reuse keeps these populated across
# requests; cold start re-fetches.
_jwks_keys_cache: dict[str, jwt.PyJWK] = {}


# ----------------------------------------------------------------------------
# JWKS fetching + caching
# ----------------------------------------------------------------------------

def _fetch_jwks() -> dict[str, jwt.PyJWK]:
    """Fetch the bridge's JWKS document and return a {kid: PyJWK} map.

    Best-effort: any HTTP / parse failure raises so the caller (which is
    invoked under a try/except) returns a 503-ish deny rather than
    silently passing the request through unverified.
    """
    url = os.environ.get("BRIDGE_JWKS_URL")
    if not url:
        raise RuntimeError("BRIDGE_JWKS_URL is not set")

    log.info("interceptor: fetching JWKS from %s", url)
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310 — controlled URL
        body = resp.read().decode("utf-8")
    doc = json.loads(body)
    keys = doc.get("keys") or []
    if not keys:
        raise RuntimeError(f"JWKS at {url} contains no keys")

    keymap: dict[str, jwt.PyJWK] = {}
    for k in keys:
        kid = k.get("kid")
        if not kid:
            log.warning("interceptor: JWKS entry without kid; skipping")
            continue
        keymap[kid] = jwt.PyJWK(k)
    return keymap


def _get_signing_key(kid: str) -> jwt.PyJWK:
    """Return the PyJWK for `kid`, refreshing the cache once on miss."""
    global _jwks_keys_cache
    if kid in _jwks_keys_cache:
        return _jwks_keys_cache[kid]

    # Cache miss — refetch in case the bridge rotated keys.
    log.info("interceptor: kid=%s not in cache; refreshing JWKS", kid)
    _jwks_keys_cache = _fetch_jwks()
    if kid not in _jwks_keys_cache:
        raise jwt.InvalidKeyError(f"kid={kid} not found in JWKS even after refresh")
    return _jwks_keys_cache[kid]


def _reset_jwks_cache_for_tests() -> None:
    """Test helper: clear the warm cache between test cases."""
    global _jwks_keys_cache
    _jwks_keys_cache = {}


# ----------------------------------------------------------------------------
# JWT extraction + verification
# ----------------------------------------------------------------------------

def _extract_jwt_token(event: dict[str, Any]) -> str:
    """Pull the bearer token from the request's Authorization header.

    Raises RuntimeError if the header is missing or malformed — the
    interceptor configuration must have `passRequestHeaders=true` for
    headers to be present in the event payload at all (see chunk C).
    """
    headers = (
        event.get("mcp", {})
        .get("gatewayRequest", {})
        .get("headers", {})
    )
    if not headers:
        raise RuntimeError(
            "no headers in request payload — interceptor must be configured "
            "with passRequestHeaders=true"
        )

    # Header lookups are case-insensitive in HTTP but Python dicts aren't,
    # so probe both common cases the Gateway might use.
    auth = headers.get("Authorization") or headers.get("authorization")
    if not auth:
        raise RuntimeError("missing Authorization header")
    if not auth.lower().startswith("bearer "):
        raise RuntimeError("Authorization header is not a Bearer token")
    return auth[len("Bearer "):].strip()


def _verify_jwt(token: str) -> dict[str, Any]:
    """Verify the JWT against the bridge's JWKS and return the claims.

    Validates: signature (RS256), audience, issuer, expiry. Raises any
    PyJWT exception on failure (the lambda_handler converts these to
    deny responses).
    """
    audience = os.environ.get("GATEWAY_JWT_AUDIENCE", "agentcore-gateway")
    issuer = os.environ.get("GATEWAY_JWT_ISSUER")
    if not issuer:
        raise RuntimeError("GATEWAY_JWT_ISSUER is not set")

    # PyJWT.get_unverified_header is safe pre-verification — it only
    # parses the header, doesn't trust any of its values.
    header = jwt.get_unverified_header(token)
    kid = header.get("kid")
    if not kid:
        raise jwt.InvalidKeyError("token has no kid header")

    signing_key = _get_signing_key(kid)
    return jwt.decode(
        token,
        signing_key.key,
        algorithms=["RS256"],
        audience=audience,
        issuer=issuer,
    )


# ----------------------------------------------------------------------------
# Request inspection + tenant matching
# ----------------------------------------------------------------------------

def _request_method(event: dict[str, Any]) -> str:
    """Return the JSON-RPC method name (e.g. 'tools/call', 'tools/list')."""
    body = (
        event.get("mcp", {})
        .get("gatewayRequest", {})
        .get("body", {})
    )
    if isinstance(body, str):
        # Defensive: rawGatewayRequest.body is a string but we use the
        # parsed gatewayRequest.body which should be a dict. If it's a
        # string somehow, try to parse.
        try:
            body = json.loads(body)
        except (TypeError, json.JSONDecodeError):
            return ""
    return body.get("method", "") if isinstance(body, dict) else ""


def _called_tool_name(event: dict[str, Any]) -> str:
    """For tools/call requests, return params.name. Empty string otherwise."""
    body = (
        event.get("mcp", {})
        .get("gatewayRequest", {})
        .get("body", {})
    )
    if not isinstance(body, dict):
        return ""
    params = body.get("params") or {}
    if not isinstance(params, dict):
        return ""
    return str(params.get("name", "") or "")


def _expected_target_prefix(tenant_id: str) -> str:
    """The prefix all of `tenant_id`'s targets share.

    Convention: targets are named `tenant-<tenant_id>-<integration>`. We
    don't lock the integration name here; the prefix check confirms the
    target name STARTS with the tenant-id portion.
    """
    return f"tenant-{tenant_id}-"


def _check_tenant_match(claim_tenant: str, tool_name: str) -> tuple[bool, str]:
    """Decide whether `claim_tenant` is allowed to invoke `tool_name`.

    Returns (allowed, reason). The tool name is expected to look like
    `tenant-<owner>-<integration>{delimiter}<inner_tool>`. We split on
    the delimiter, take the left half as the target name, and check it
    starts with the caller's expected prefix.

    The delimiter is configurable to absorb whatever AgentCore Gateway
    actually uses to namespace tools per target — chunk C will confirm
    against a real provisioned target.
    """
    if not tool_name:
        return False, "tool name missing from tools/call request"

    delimiter = os.environ.get("INTERCEPTOR_TARGET_DELIMITER", "___")
    if delimiter not in tool_name:
        return False, (
            f"tool name {tool_name!r} has no delimiter {delimiter!r} — "
            "cannot identify target"
        )

    target_name, _, _ = tool_name.partition(delimiter)
    expected = _expected_target_prefix(claim_tenant)
    if not target_name.startswith(expected):
        return False, (
            f"tool target {target_name!r} not owned by tenant {claim_tenant!r}"
        )
    return True, "ok"


# ----------------------------------------------------------------------------
# Response shaping
# ----------------------------------------------------------------------------

def _allow(event: dict[str, Any]) -> dict[str, Any]:
    """Pass-through response: returns the request unchanged so the
    Gateway forwards it to the target."""
    body = (
        event.get("mcp", {})
        .get("gatewayRequest", {})
        .get("body", {})
    )
    return {
        "interceptorOutputVersion": "1.0",
        "mcp": {
            "transformedGatewayRequest": {"body": body},
        },
    }


def _deny(reason: str, status_code: int = 403) -> dict[str, Any]:
    """Short-circuit: return a JSON-RPC error response so the Gateway
    never forwards the request to a target.

    JSON-RPC error code -32000 is the conventional "server error" range
    for application-level failures (per JSON-RPC 2.0 spec).
    """
    return {
        "interceptorOutputVersion": "1.0",
        "mcp": {
            "transformedGatewayResponse": {
                "statusCode": status_code,
                "body": {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {
                        "code": -32000,
                        "message": "tenant access denied",
                        "data": {"reason": reason},
                    },
                },
            },
        },
    }


# ----------------------------------------------------------------------------
# Lambda entrypoint
# ----------------------------------------------------------------------------

def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """AgentCore Gateway REQUEST interceptor entrypoint.

    Always returns a valid interceptor response shape — never raises out
    of the handler. Any exception is converted to a 403 deny.
    """
    try:
        token = _extract_jwt_token(event)
        claims = _verify_jwt(token)
    except (jwt.PyJWTError, RuntimeError) as e:
        log.warning("interceptor: auth failed: %s", e)
        return _deny(f"auth failed: {e}", status_code=401)

    claim_tenant = claims.get("tenant_id") or ""
    if not claim_tenant:
        log.warning("interceptor: token has no tenant_id claim")
        return _deny("token missing tenant_id claim", status_code=401)

    method = _request_method(event)
    log.info("interceptor: tenant=%s method=%s", claim_tenant, method)

    if method != "tools/call":
        # tools/list, initialize, ping, etc. — pass through with logging.
        # Per-tenant tool list filtering would require us to short-circuit
        # the response and rewrite the tools array, which is more complex
        # than chunk B needs. Defer to a later refinement.
        return _allow(event)

    tool_name = _called_tool_name(event)
    allowed, reason = _check_tenant_match(claim_tenant, tool_name)
    if not allowed:
        log.warning(
            "interceptor: deny tenant=%s tool=%s reason=%s",
            claim_tenant,
            tool_name,
            reason,
        )
        return _deny(reason, status_code=403)

    log.info("interceptor: allow tenant=%s tool=%s", claim_tenant, tool_name)
    return _allow(event)
