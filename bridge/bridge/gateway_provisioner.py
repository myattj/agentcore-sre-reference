"""Per-tenant Gateway target + credential provisioner (week 4 chunk D).

When a customer connects a new integration (e.g. Datadog) through the
onboarding UI, the bridge calls into this module to:
  1. Create an API key credential provider for the integration's API key
  2. Create a Gateway target pointing at the integration's OpenAPI spec,
     referencing the credential provider
  3. Enable BYO on the tenant row and set the shared Gateway URL as the
     tenant's `gateway_endpoint`

The shared Gateway itself is created by `infra/data/scripts/provision_gateway.py`
(chunk C) and its coordinates are stored in SSM Parameter Store. This module
reads those coordinates lazily.

## Naming conventions (critical for interceptor enforcement)

Credential providers: `tenant-{tenant_id}-{integration}-apikey`
Targets:             `tenant-{tenant_id}-{integration}`

The interceptor Lambda (chunk B) parses the tool name from a `tools/call`
MCP request, splits on `INTERCEPTOR_TARGET_DELIMITER` (default `___`), and
verifies the left half starts with `tenant-{tenant_id}-`. This only works
if targets are named exactly as specified above.

## Idempotency

Both `ensure_credential_provider` and `ensure_gateway_target` are idempotent:
they check for existing resources by name before creating new ones. Re-running
against an already-provisioned integration is a no-op.

## Required env vars / SSM parameters

  AWS_REGION                 — region (default: us-west-2)
  /agentcore/gateway/id      — SSM: gateway identifier (from chunk C)
  /agentcore/gateway/url     — SSM: gateway URL written to tenant config

When LOCAL_DEV=1, the SSM reads are skipped and dummy values are used so
tests don't need real AWS.
"""
from __future__ import annotations

import logging
import os
from functools import lru_cache
from typing import Any

log = logging.getLogger(__name__)


# ----------------------------------------------------------------------------
# SSM-backed gateway coordinates (shared Gateway, provisioned in chunk C)
# ----------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _gateway_coordinates() -> dict[str, str]:
    """Read the shared gateway's ID + URL from SSM Parameter Store.

    In LOCAL_DEV, returns dummy values (no SSM call).
    """
    if os.getenv("LOCAL_DEV") == "1":
        log.info("gateway_provisioner: LOCAL_DEV mode — using dummy coordinates")
        return {
            "gateway_id": "local-gateway-id",
            "gateway_url": "http://localhost:9999/gateway",
        }

    import boto3

    region = os.getenv("AWS_REGION", "us-west-2")
    ssm = boto3.client("ssm", region_name=region)

    names = ["/agentcore/gateway/id", "/agentcore/gateway/url"]
    resp = ssm.get_parameters(Names=names, WithDecryption=False)
    params = {p["Name"]: p["Value"] for p in resp.get("Parameters", [])}

    gw_id = params.get("/agentcore/gateway/id")
    gw_url = params.get("/agentcore/gateway/url")
    if not gw_id or not gw_url:
        raise RuntimeError(
            "SSM parameters /agentcore/gateway/{id,url} not found. "
            "Run infra/data/scripts/provision_gateway.py first (chunk C)."
        )
    return {"gateway_id": gw_id, "gateway_url": gw_url}


def reset_provisioner_for_tests() -> None:
    """Test helper: clear cached gateway coordinates."""
    _gateway_coordinates.cache_clear()


# ----------------------------------------------------------------------------
# Naming conventions
# ----------------------------------------------------------------------------

def _credential_provider_name(tenant_id: str, integration: str) -> str:
    return f"tenant-{tenant_id}-{integration}-apikey"


def _target_name(tenant_id: str, integration: str) -> str:
    return f"tenant-{tenant_id}-{integration}"


# ----------------------------------------------------------------------------
# Credential provider (API key)
# ----------------------------------------------------------------------------

def _find_credential_provider(
    client: Any, name: str
) -> dict[str, Any] | None:
    """Return an existing API key credential provider by name, or None."""
    paginator = client.get_paginator("list_api_key_credential_providers")
    for page in paginator.paginate():
        for item in page.get("credentialProviders", []):
            if item.get("name") == name:
                return item
    return None


def ensure_credential_provider(
    tenant_id: str,
    integration: str,
    api_key: str,
    *,
    region: str | None = None,
) -> str:
    """Idempotent: create or find the API key credential provider.

    Returns the `credentialProviderArn`.
    """
    import boto3

    region = region or os.getenv("AWS_REGION", "us-west-2")
    client = boto3.client("bedrock-agentcore-control", region_name=region)
    name = _credential_provider_name(tenant_id, integration)

    existing = _find_credential_provider(client, name)
    if existing:
        arn = existing.get("credentialProviderArn") or ""
        log.info("ensure_credential_provider: reusing %s → %s", name, arn)
        return arn

    log.info("ensure_credential_provider: creating %s", name)
    resp = client.create_api_key_credential_provider(
        name=name,
        apiKey=api_key,
    )
    arn = resp["credentialProviderArn"]
    log.info("ensure_credential_provider: created %s → %s", name, arn)
    return arn


# ----------------------------------------------------------------------------
# Gateway target
# ----------------------------------------------------------------------------

def _find_gateway_target(
    client: Any, gateway_id: str, name: str
) -> dict[str, Any] | None:
    """Return an existing target by name, or None."""
    paginator = client.get_paginator("list_gateway_targets")
    for page in paginator.paginate(gatewayIdentifier=gateway_id):
        for item in page.get("items", []):
            if item.get("name") == name:
                return item
    return None


def ensure_gateway_target(
    tenant_id: str,
    integration: str,
    *,
    openapi_spec: str,
    credential_provider_arn: str,
    credential_header_name: str = "DD-API-KEY",
    forwarded_headers: list[str] | None = None,
    region: str | None = None,
) -> dict[str, str]:
    """Idempotent: create or find the Gateway target for this tenant + integration.

    Args:
        tenant_id: the customer's tenant_id (e.g. "slack-acme")
        integration: short name (e.g. "datadog")
        openapi_spec: the OpenAPI JSON/YAML spec as a string, inlined
        credential_provider_arn: from ensure_credential_provider()
        credential_header_name: HTTP header for the API key
        forwarded_headers: additional headers the Gateway should forward
            from the agent's MCP request to the target (e.g.
            ["DD-APPLICATION-KEY"]). Used for secondary credentials that
            can't go through the credential provider (max 1 per target).
        region: AWS region override

    Returns a dict with `target_id` and `target_name`.
    """
    import boto3

    region = region or os.getenv("AWS_REGION", "us-west-2")
    client = boto3.client("bedrock-agentcore-control", region_name=region)
    coords = _gateway_coordinates()
    gateway_id = coords["gateway_id"]
    name = _target_name(tenant_id, integration)

    existing = _find_gateway_target(client, gateway_id, name)
    if existing:
        target_id = existing.get("targetId") or existing.get("id") or ""
        log.info("ensure_gateway_target: reusing %s → %s", name, target_id)
        return {"target_id": target_id, "target_name": name}

    log.info("ensure_gateway_target: creating %s on gateway %s", name, gateway_id)
    kwargs: dict[str, Any] = {
        "gatewayIdentifier": gateway_id,
        "name": name,
        "description": f"Tenant {tenant_id} - {integration} integration",
        "targetConfiguration": {
            "mcp": {
                "openApiSchema": {
                    "inlinePayload": openapi_spec,
                },
            },
        },
        "credentialProviderConfigurations": [
            {
                "credentialProviderType": "API_KEY",
                "credentialProvider": {
                    "apiKeyCredentialProvider": {
                        "providerArn": credential_provider_arn,
                        "credentialParameterName": credential_header_name,
                        "credentialLocation": "HEADER",
                    },
                },
            },
        ],
    }
    # Forward additional headers from the agent to the target. Used for
    # secondary credentials like Datadog's Application Key — the agent
    # sends it as a request header, and the Gateway passes it through.
    if forwarded_headers:
        kwargs["metadataConfiguration"] = {
            "allowedRequestHeaders": forwarded_headers,
        }

    resp = client.create_gateway_target(**kwargs)
    target_id = resp["targetId"]
    log.info("ensure_gateway_target: created %s → %s", name, target_id)
    return {"target_id": target_id, "target_name": name}


# ----------------------------------------------------------------------------
# High-level: provision a full integration for a tenant
# ----------------------------------------------------------------------------

def provision_integration(
    tenant_id: str,
    integration: str,
    *,
    api_key: str,
    app_key: str | None = None,
    openapi_spec: str,
    credential_header_name: str = "DD-API-KEY",
    app_key_header_name: str = "DD-APPLICATION-KEY",
    region: str | None = None,
) -> dict[str, str]:
    """Provision a full integration: credential provider + Gateway target.

    The primary API key goes through the credential provider (stored in
    Secrets Manager by AgentCore). Secondary credentials like Datadog's
    Application Key are forwarded as request headers from the agent —
    the Gateway target is configured with `allowedRequestHeaders` to
    pass them through. The agent sends the app key header because the
    bridge writes it into the tenant's `byo.gateway_auth.headers` dict,
    which `_build_byo_auth` merges into every MCP request.

    Returns a dict with:
      gateway_url        — shared Gateway URL (the tenant's byo.gateway_endpoint)
      target_id          — the provisioned target ID
      target_name        — the target name (tenant-<id>-<integration>)
      credential_arn     — the API key credential provider ARN
      extra_headers      — dict of headers the agent should send (app key etc.)

    The caller (bridge route in chunk F) writes `gateway_url` into the
    tenant's `byo.gateway_endpoint` and stores `extra_headers` in
    `byo.gateway_auth.headers` so the agent sends them on every call.
    """
    region = region or os.getenv("AWS_REGION", "us-west-2")

    api_key_arn = ensure_credential_provider(
        tenant_id, integration, api_key, region=region
    )

    forwarded: list[str] = []
    extra_headers: dict[str, str] = {}
    if app_key:
        forwarded.append(app_key_header_name)
        extra_headers[app_key_header_name] = app_key

    target = ensure_gateway_target(
        tenant_id,
        integration,
        openapi_spec=openapi_spec,
        credential_provider_arn=api_key_arn,
        credential_header_name=credential_header_name,
        forwarded_headers=forwarded or None,
        region=region,
    )
    coords = _gateway_coordinates()
    return {
        "gateway_url": coords["gateway_url"],
        "target_id": target["target_id"],
        "target_name": target["target_name"],
        "credential_arn": api_key_arn,
        "extra_headers": extra_headers,
    }
