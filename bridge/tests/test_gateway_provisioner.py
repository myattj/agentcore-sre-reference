"""Tests for the per-tenant Gateway target provisioner (week 4 chunk D).

Tests the provisioner in LOCAL_DEV mode (dummy SSM coordinates) and with
mocked boto3 to verify:
  - Naming conventions (critical for interceptor enforcement)
  - Idempotency: re-calling ensure_* returns existing resource
  - CreateApiKeyCredentialProvider request shape
  - CreateGatewayTarget request shape
  - provision_integration end-to-end orchestration
  - Error handling when SSM parameters are missing (non-LOCAL_DEV)
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from bridge import gateway_provisioner
from bridge.gateway_provisioner import (
    _credential_provider_name,
    _target_name,
    ensure_credential_provider,
    ensure_gateway_target,
    provision_integration,
)


# ----------------------------------------------------------------------------
# Naming conventions
# ----------------------------------------------------------------------------

def test_credential_provider_name_follows_convention():
    assert _credential_provider_name("slack-acme", "datadog") == "tenant-slack-acme-datadog-apikey"


def test_target_name_follows_convention():
    assert _target_name("slack-acme", "datadog") == "tenant-slack-acme-datadog"


def test_target_name_includes_trailing_tenant_id_for_interceptor_prefix_check():
    """The interceptor checks `target_name.startswith(f'tenant-{claim_tenant}-')`.
    Verify the naming convention has the right number of dashes."""
    name = _target_name("slack-acme", "datadog")
    # Must match the interceptor's _expected_target_prefix
    assert name.startswith("tenant-slack-acme-")
    # Must NOT match a different tenant with a prefix overlap
    assert not name.startswith("tenant-slack-acmecorp-")


# ----------------------------------------------------------------------------
# ensure_credential_provider
# ----------------------------------------------------------------------------

def test_ensure_credential_provider_creates_when_not_found():
    mock_client = MagicMock()
    mock_paginator = MagicMock()
    mock_paginator.paginate.return_value = [{"credentialProviders": []}]
    mock_client.get_paginator.return_value = mock_paginator
    mock_client.create_api_key_credential_provider.return_value = {
        "credentialProviderArn": "arn:aws:bedrock-agentcore:us-west-2:123:credential-provider/abc",
        "name": "tenant-slack-acme-datadog-apikey",
        "apiKeySecretArn": {"secretArn": "arn:aws:secretsmanager:..."},
    }

    with patch("boto3.client", return_value=mock_client):
        arn = ensure_credential_provider("slack-acme", "datadog", "dd-key-123")

    assert arn == "arn:aws:bedrock-agentcore:us-west-2:123:credential-provider/abc"
    mock_client.create_api_key_credential_provider.assert_called_once_with(
        name="tenant-slack-acme-datadog-apikey",
        apiKey="dd-key-123",
    )


def test_ensure_credential_provider_reuses_existing():
    mock_client = MagicMock()
    mock_paginator = MagicMock()
    mock_paginator.paginate.return_value = [{
        "credentialProviders": [{
            "name": "tenant-slack-acme-datadog-apikey",
            "credentialProviderArn": "arn:existing",
        }],
    }]
    mock_client.get_paginator.return_value = mock_paginator

    with patch("boto3.client", return_value=mock_client):
        arn = ensure_credential_provider("slack-acme", "datadog", "dd-key-123")

    assert arn == "arn:existing"
    mock_client.create_api_key_credential_provider.assert_not_called()


# ----------------------------------------------------------------------------
# ensure_gateway_target
# ----------------------------------------------------------------------------

def test_ensure_gateway_target_creates_when_not_found():
    mock_client = MagicMock()
    mock_paginator = MagicMock()
    mock_paginator.paginate.return_value = [{"items": []}]
    mock_client.get_paginator.return_value = mock_paginator
    mock_client.create_gateway_target.return_value = {
        "targetId": "tgt-abc123",
        "name": "tenant-slack-acme-datadog",
        "gatewayArn": "arn:gw",
        "status": "CREATING",
    }

    with patch("boto3.client", return_value=mock_client):
        result = ensure_gateway_target(
            "slack-acme",
            "datadog",
            openapi_spec='{"openapi":"3.0.0"}',
            credential_provider_arn="arn:cred",
            credential_header_name="DD-API-KEY",
        )

    assert result["target_id"] == "tgt-abc123"
    assert result["target_name"] == "tenant-slack-acme-datadog"

    call_kwargs = mock_client.create_gateway_target.call_args.kwargs
    assert call_kwargs["gatewayIdentifier"] == "local-gateway-id"  # LOCAL_DEV dummy
    assert call_kwargs["name"] == "tenant-slack-acme-datadog"
    assert call_kwargs["targetConfiguration"]["mcp"]["openApiSchema"]["inlinePayload"] == '{"openapi":"3.0.0"}'

    cred_config = call_kwargs["credentialProviderConfigurations"][0]
    assert cred_config["credentialProviderType"] == "API_KEY"
    cred = cred_config["credentialProvider"]["apiKeyCredentialProvider"]
    assert cred["providerArn"] == "arn:cred"
    assert cred["credentialParameterName"] == "DD-API-KEY"
    assert cred["credentialLocation"] == "HEADER"
    assert "credentialPrefix" not in cred  # empty prefix rejected by boto3


def test_ensure_gateway_target_reuses_existing():
    mock_client = MagicMock()
    mock_paginator = MagicMock()
    mock_paginator.paginate.return_value = [{
        "items": [{
            "name": "tenant-slack-acme-datadog",
            "targetId": "tgt-existing",
        }],
    }]
    mock_client.get_paginator.return_value = mock_paginator

    with patch("boto3.client", return_value=mock_client):
        result = ensure_gateway_target(
            "slack-acme",
            "datadog",
            openapi_spec="{}",
            credential_provider_arn="arn:cred",
        )

    assert result["target_id"] == "tgt-existing"
    mock_client.create_gateway_target.assert_not_called()


# ----------------------------------------------------------------------------
# provision_integration (end-to-end orchestration)
# ----------------------------------------------------------------------------

def test_provision_integration_orchestrates_credential_then_target():
    """Verify provision_integration calls ensure_credential_provider first,
    passes the result to ensure_gateway_target, and returns the shared
    Gateway URL."""
    mock_client = MagicMock()

    # credential provider listing returns empty (will create)
    cred_paginator = MagicMock()
    cred_paginator.paginate.return_value = [{"credentialProviders": []}]

    # target listing returns empty (will create)
    target_paginator = MagicMock()
    target_paginator.paginate.return_value = [{"items": []}]

    def get_paginator(name):
        if name == "list_api_key_credential_providers":
            return cred_paginator
        return target_paginator

    mock_client.get_paginator.side_effect = get_paginator
    mock_client.create_api_key_credential_provider.return_value = {
        "credentialProviderArn": "arn:cred-new",
        "name": "tenant-slack-acme-datadog-apikey",
        "apiKeySecretArn": {"secretArn": "arn:secret"},
    }
    mock_client.create_gateway_target.return_value = {
        "targetId": "tgt-new",
        "name": "tenant-slack-acme-datadog",
        "gatewayArn": "arn:gw",
        "status": "CREATING",
    }

    with patch("boto3.client", return_value=mock_client):
        result = provision_integration(
            "slack-acme",
            "datadog",
            api_key="dd-key-123",
            openapi_spec='{"openapi":"3.0.0"}',
            credential_header_name="DD-API-KEY",
        )

    assert result["gateway_url"] == "http://localhost:9999/gateway"  # LOCAL_DEV dummy
    assert result["target_id"] == "tgt-new"
    assert result["target_name"] == "tenant-slack-acme-datadog"
    assert result["credential_arn"] == "arn:cred-new"

    # credential provider created with the raw API key
    mock_client.create_api_key_credential_provider.assert_called_once()
    # target created with the credential provider ARN
    target_call = mock_client.create_gateway_target.call_args.kwargs
    cred = target_call["credentialProviderConfigurations"][0]["credentialProvider"]["apiKeyCredentialProvider"]
    assert cred["providerArn"] == "arn:cred-new"


# ----------------------------------------------------------------------------
# SSM coordinate fetch
# ----------------------------------------------------------------------------

def test_local_dev_returns_dummy_coordinates():
    """In LOCAL_DEV=1 (set by conftest), SSM is not called."""
    gateway_provisioner.reset_provisioner_for_tests()
    coords = gateway_provisioner._gateway_coordinates()
    assert coords["gateway_id"] == "local-gateway-id"
    assert "localhost" in coords["gateway_url"]


def test_non_local_dev_raises_if_ssm_params_missing(monkeypatch):
    """Without LOCAL_DEV, missing SSM params must raise — not silently
    fall back to dummy values."""
    monkeypatch.delenv("LOCAL_DEV", raising=False)
    gateway_provisioner.reset_provisioner_for_tests()

    mock_ssm = MagicMock()
    mock_ssm.get_parameters.return_value = {"Parameters": []}

    with patch("boto3.client", return_value=mock_ssm):
        with pytest.raises(RuntimeError, match="SSM parameters"):
            gateway_provisioner._gateway_coordinates()
