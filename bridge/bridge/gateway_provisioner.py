"""Per-tenant Gateway target and credential provisioner.

When a customer connects a new integration (e.g. Datadog) through the
onboarding UI, the bridge calls into this module to:
  1. Create an API key credential provider for the integration's API key
  2. Create a Gateway target pointing at the integration's OpenAPI spec,
     referencing the credential provider
  3. Enable BYO on the tenant row and set the shared Gateway URL as the
     tenant's `gateway_endpoint`

The shared Gateway itself is created by `infra/data/scripts/provision_gateway.py`,
and its coordinates are stored in SSM Parameter Store. This module reads those
coordinates lazily.

## Naming conventions (critical for interceptor enforcement)

Credential providers: `tenant-v1-{base32(tenant_id)}-{integration}-apikey`
Targets:             `tenant-v1-{base32(tenant_id)}-{integration}`

The Gateway interceptor Lambda parses the tool name from a `tools/call`
MCP request, splits on `INTERCEPTOR_TARGET_DELIMITER` (default `___`), and
decodes the exact tenant owner from the versioned target name. Base32 keeps
the owner segment lossless while avoiding the hyphen used between fields, so
tenant IDs such as `foo` and `foo-bar` cannot overlap.

Legacy unversioned targets (`tenant-{tenant_id}-{integration}`) are not
accepted by the interceptor. Existing deployments must reprovision their
integrations before enabling the strict interceptor; see bridge/README.md.

## Idempotency

Both `ensure_credential_provider` and `ensure_gateway_target` are idempotent:
they create missing resources and update existing resources to the requested
credential and target configuration. Reconnecting therefore rotates tokens
and applies OpenAPI or endpoint changes instead of reporting stale success.

## Required env vars / SSM parameters

  AWS_REGION                 — region (default: us-west-2)
  /agentcore/gateway/id      — SSM: gateway identifier
  /agentcore/gateway/url     — SSM: gateway URL written to tenant config

When LOCAL_DEV=1, the SSM reads are skipped and dummy values are used so
tests don't need real AWS.
"""

from __future__ import annotations

import base64
import logging
import os
import re
from functools import lru_cache
from typing import Any

log = logging.getLogger(__name__)


# ----------------------------------------------------------------------------
# SSM-backed coordinates for the shared Gateway
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
            "Run infra/data/scripts/provision_gateway.py first."
        )
    return {"gateway_id": gw_id, "gateway_url": gw_url}


def reset_provisioner_for_tests() -> None:
    """Test helper: clear cached gateway coordinates."""
    _gateway_coordinates.cache_clear()


# ----------------------------------------------------------------------------
# Naming conventions
# ----------------------------------------------------------------------------

_TENANT_ID_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_INTEGRATION_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_MAX_TENANT_ID_LENGTH = 40
_MAX_INTEGRATION_LENGTH = 24
_TARGET_PREFIX = "tenant-v1-"


def _validate_resource_component(
    value: str,
    *,
    field: str,
    pattern: re.Pattern[str],
    max_length: int,
) -> str:
    """Validate a human-readable identifier before it enters an AWS name."""
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    if not value or len(value) > max_length or pattern.fullmatch(value) is None:
        raise ValueError(
            f"{field} must be a lowercase ASCII slug of at most "
            f"{max_length} characters; got {value!r}"
        )
    return value


def _encode_tenant_owner(tenant_id: str) -> str:
    """Return the canonical, lossless owner segment used in target names.

    The 40-character tenant limit expands to at most 64 Base32 characters.
    Together with the 24-character integration limit, the resulting target
    stays within AgentCore's 100-character target-name constraint.
    """
    tenant_id = _validate_resource_component(
        tenant_id,
        field="tenant_id",
        pattern=_TENANT_ID_RE,
        max_length=_MAX_TENANT_ID_LENGTH,
    )
    return (
        base64.b32encode(tenant_id.encode("ascii")).decode("ascii").rstrip("=").lower()
    )


def _validated_integration(integration: str) -> str:
    return _validate_resource_component(
        integration,
        field="integration",
        pattern=_INTEGRATION_RE,
        max_length=_MAX_INTEGRATION_LENGTH,
    )


def _credential_provider_name(tenant_id: str, integration: str) -> str:
    owner = _encode_tenant_owner(tenant_id)
    integration = _validated_integration(integration)
    return f"{_TARGET_PREFIX}{owner}-{integration}-apikey"


def _target_name(tenant_id: str, integration: str) -> str:
    owner = _encode_tenant_owner(tenant_id)
    integration = _validated_integration(integration)
    return f"{_TARGET_PREFIX}{owner}-{integration}"


# ----------------------------------------------------------------------------
# Credential provider (API key)
# ----------------------------------------------------------------------------


def _find_credential_provider(client: Any, name: str) -> dict[str, Any] | None:
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
    """Create the API key provider or rotate its key when it already exists.

    Returns the `credentialProviderArn`.
    """
    name = _credential_provider_name(tenant_id, integration)

    import boto3

    region = region or os.getenv("AWS_REGION", "us-west-2")
    client = boto3.client("bedrock-agentcore-control", region_name=region)

    existing = _find_credential_provider(client, name)
    if existing:
        log.info("ensure_credential_provider: rotating key for %s", name)
        resp = client.update_api_key_credential_provider(
            name=name,
            apiKey=api_key,
        )
        arn = resp.get("credentialProviderArn")
        if not arn:
            raise RuntimeError(
                f"AgentCore returned no credentialProviderArn while updating {name}"
            )
        log.info("ensure_credential_provider: updated %s → %s", name, arn)
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
    region: str | None = None,
) -> dict[str, str]:
    """Create the Gateway target or reconcile its complete desired state.

    Args:
        tenant_id: the customer's tenant_id (e.g. "slack-acme")
        integration: short name (e.g. "datadog")
        openapi_spec: the OpenAPI JSON/YAML spec as a string, inlined
        credential_provider_arn: from ensure_credential_provider()
        credential_header_name: HTTP header for the API key
        region: AWS region override

    Returns a dict with `target_id` and `target_name`.
    """
    name = _target_name(tenant_id, integration)

    import boto3

    region = region or os.getenv("AWS_REGION", "us-west-2")
    client = boto3.client("bedrock-agentcore-control", region_name=region)
    coords = _gateway_coordinates()
    gateway_id = coords["gateway_id"]

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

    existing = _find_gateway_target(client, gateway_id, name)
    if existing:
        target_id = existing.get("targetId") or existing.get("id") or ""
        if not target_id:
            raise RuntimeError(f"Existing AgentCore target {name} has no target ID")
        log.info("ensure_gateway_target: reconciling %s → %s", name, target_id)
        client.update_gateway_target(targetId=target_id, **kwargs)
        return {"target_id": target_id, "target_name": name}

    log.info("ensure_gateway_target: creating %s on gateway %s", name, gateway_id)
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
    openapi_spec: str,
    credential_header_name: str = "DD-API-KEY",
    region: str | None = None,
) -> dict[str, str]:
    """Provision a full integration: credential provider + Gateway target.

    The API key goes through the AgentCore credential provider and is never
    written to tenant configuration. AgentCore currently permits exactly one
    credential-provider configuration per target, so connectors that require
    multiple secrets must use a trusted broker target instead of forwarding a
    second raw credential through tenant-controlled request headers.

    Returns a dict with:
      gateway_url        — shared Gateway URL (the tenant's byo.gateway_endpoint)
      target_id          — the provisioned target ID
      target_name        — the versioned target name
      credential_arn     — the API key credential provider ARN
    The integration route writes only `gateway_url` into the tenant's
    `byo.gateway_endpoint`.
    """
    region = region or os.getenv("AWS_REGION", "us-west-2")

    api_key_arn = ensure_credential_provider(
        tenant_id, integration, api_key, region=region
    )

    target = ensure_gateway_target(
        tenant_id,
        integration,
        openapi_spec=openapi_spec,
        credential_provider_arn=api_key_arn,
        credential_header_name=credential_header_name,
        region=region,
    )
    coords = _gateway_coordinates()
    return {
        "gateway_url": coords["gateway_url"],
        "target_id": target["target_id"],
        "target_name": target["target_name"],
        "credential_arn": api_key_arn,
    }
