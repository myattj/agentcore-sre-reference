"""Tests for the per-tenant Gateway target provisioner.

Tests the provisioner in LOCAL_DEV mode (dummy SSM coordinates) and with
mocked boto3 to verify:
  - Naming conventions (critical for interceptor enforcement)
  - Reconciliation: re-calling ensure_* rotates credentials and updates targets
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
    assert (
        _credential_provider_name("slack-acme", "datadog")
        == "tenant-v1-onwgcy3lfvqwg3lf-datadog-apikey"
    )


def test_target_name_follows_convention():
    assert _target_name("slack-acme", "datadog") == "tenant-v1-onwgcy3lfvqwg3lf-datadog"


def test_overlapping_tenant_ids_have_distinct_owner_segments():
    assert _target_name("foo", "datadog") == "tenant-v1-mzxw6-datadog"
    assert _target_name("foo-bar", "datadog") == "tenant-v1-mzxw6llcmfza-datadog"


@pytest.mark.parametrize(
    "tenant_id",
    ["", "Foo", "foo--bar", "foo_bar", "a" * 41],
)
def test_target_name_rejects_invalid_tenant_ids(tenant_id: str):
    with pytest.raises(ValueError, match="tenant_id must be"):
        _target_name(tenant_id, "datadog")


@pytest.mark.parametrize(
    "integration",
    ["", "DataDog", "data--dog", "data_dog", "a" * 25],
)
def test_target_name_rejects_invalid_integrations(integration: str):
    with pytest.raises(ValueError, match="integration must be"):
        _target_name("slack-acme", integration)


def test_maximum_identifiers_fit_agentcore_target_name_limit():
    tenant_id = "a" * 40
    integration = "b" * 24
    assert len(_target_name(tenant_id, integration)) <= 100


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
        "name": "tenant-v1-onwgcy3lfvqwg3lf-datadog-apikey",
        "apiKeySecretArn": {"secretArn": "arn:aws:secretsmanager:..."},
    }

    with patch("boto3.client", return_value=mock_client):
        arn = ensure_credential_provider("slack-acme", "datadog", "dd-key-123")

    assert arn == "arn:aws:bedrock-agentcore:us-west-2:123:credential-provider/abc"
    mock_client.create_api_key_credential_provider.assert_called_once_with(
        name="tenant-v1-onwgcy3lfvqwg3lf-datadog-apikey",
        apiKey="dd-key-123",
    )


def test_invalid_names_are_rejected_before_aws_client_creation():
    with patch("boto3.client") as client:
        with pytest.raises(ValueError, match="tenant_id must be"):
            ensure_credential_provider("foo--bar", "datadog", "dd-key-123")
    client.assert_not_called()


def test_ensure_credential_provider_rotates_existing_api_key():
    mock_client = MagicMock()
    mock_paginator = MagicMock()
    mock_paginator.paginate.return_value = [
        {
            "credentialProviders": [
                {
                    "name": "tenant-v1-onwgcy3lfvqwg3lf-datadog-apikey",
                    "credentialProviderArn": "arn:existing",
                }
            ],
        }
    ]
    mock_client.get_paginator.return_value = mock_paginator
    mock_client.update_api_key_credential_provider.return_value = {
        "credentialProviderArn": "arn:existing",
    }

    with patch("boto3.client", return_value=mock_client):
        arn = ensure_credential_provider(
            "slack-acme", "datadog", "rotated-dd-key-456"
        )

    assert arn == "arn:existing"
    mock_client.update_api_key_credential_provider.assert_called_once_with(
        name="tenant-v1-onwgcy3lfvqwg3lf-datadog-apikey",
        apiKey="rotated-dd-key-456",
    )
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
        "name": "tenant-v1-onwgcy3lfvqwg3lf-datadog",
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
    assert result["target_name"] == "tenant-v1-onwgcy3lfvqwg3lf-datadog"

    call_kwargs = mock_client.create_gateway_target.call_args.kwargs
    assert call_kwargs["gatewayIdentifier"] == "local-gateway-id"  # LOCAL_DEV dummy
    assert call_kwargs["name"] == "tenant-v1-onwgcy3lfvqwg3lf-datadog"
    assert (
        call_kwargs["targetConfiguration"]["mcp"]["openApiSchema"]["inlinePayload"]
        == '{"openapi":"3.0.0"}'
    )
    assert "metadataConfiguration" not in call_kwargs

    cred_config = call_kwargs["credentialProviderConfigurations"][0]
    assert cred_config["credentialProviderType"] == "API_KEY"
    cred = cred_config["credentialProvider"]["apiKeyCredentialProvider"]
    assert cred["providerArn"] == "arn:cred"
    assert cred["credentialParameterName"] == "DD-API-KEY"
    assert cred["credentialLocation"] == "HEADER"
    assert "credentialPrefix" not in cred  # empty prefix rejected by boto3


def test_ensure_gateway_target_reconciles_domain_spec_and_credentials():
    mock_client = MagicMock()
    mock_paginator = MagicMock()
    mock_paginator.paginate.return_value = [
        {
            "items": [
                {
                    "name": "tenant-v1-onwgcy3lfvqwg3lf-datadog",
                    "targetId": "tgt-existing",
                }
            ],
        }
    ]
    mock_client.get_paginator.return_value = mock_paginator
    new_spec = (
        '{"openapi":"3.0.0","servers":'
        '[{"url":"https://new-domain.atlassian.net/rest/api/3"}]}'
    )

    with patch("boto3.client", return_value=mock_client):
        result = ensure_gateway_target(
            "slack-acme",
            "datadog",
            openapi_spec=new_spec,
            credential_provider_arn="arn:rotated-cred",
            credential_header_name="Authorization",
        )

    assert result["target_id"] == "tgt-existing"
    update_kwargs = mock_client.update_gateway_target.call_args.kwargs
    assert update_kwargs["gatewayIdentifier"] == "local-gateway-id"
    assert update_kwargs["targetId"] == "tgt-existing"
    assert update_kwargs["name"] == "tenant-v1-onwgcy3lfvqwg3lf-datadog"
    assert (
        update_kwargs["targetConfiguration"]["mcp"]["openApiSchema"][
            "inlinePayload"
        ]
        == new_spec
    )
    credential = update_kwargs["credentialProviderConfigurations"][0][
        "credentialProvider"
    ]["apiKeyCredentialProvider"]
    assert credential["providerArn"] == "arn:rotated-cred"
    assert credential["credentialParameterName"] == "Authorization"
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
        "name": "tenant-v1-onwgcy3lfvqwg3lf-datadog-apikey",
        "apiKeySecretArn": {"secretArn": "arn:secret"},
    }
    mock_client.create_gateway_target.return_value = {
        "targetId": "tgt-new",
        "name": "tenant-v1-onwgcy3lfvqwg3lf-datadog",
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
    assert result["target_name"] == "tenant-v1-onwgcy3lfvqwg3lf-datadog"
    assert result["credential_arn"] == "arn:cred-new"

    # credential provider created with the raw API key
    mock_client.create_api_key_credential_provider.assert_called_once()
    # target created with the credential provider ARN
    target_call = mock_client.create_gateway_target.call_args.kwargs
    cred = target_call["credentialProviderConfigurations"][0]["credentialProvider"][
        "apiKeyCredentialProvider"
    ]
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
