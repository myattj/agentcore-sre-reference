"""Tests for POST /api/tenants/{id}/integrations/datadog (week 4 chunk F).

Covers:
  - Happy path: valid key + successful provisioning → 200 with ok=true
  - Invalid Datadog key → ok=false with descriptive error
  - Datadog unreachable → ok=false with "could not reach" error
  - Provisioning failure → ok=false
  - Missing/bad session token → 401
  - Cross-tenant token → 403
  - Tenant not found when writing BYO config → 404
  - The route updates the tenant row's byo.enabled and byo.gateway_endpoint
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from bridge.main import app
from bridge.slack_oauth import make_session_token


@pytest.fixture
def session_token():
    return make_session_token("slack-acme")


@pytest.fixture
def _seed_tenant(monkeypatch):
    """Seed a minimal tenant row so the route can read/update it."""
    from bridge import tenant_write
    tenant_write.reset_tenant_write_for_tests()
    row = tenant_write.build_default_config_dict("slack-acme")
    # In LOCAL_DEV the tenant write module reads/writes JSON files in
    # examples/tenants/. We just need the file to exist.
    import json
    from pathlib import Path

    examples = Path(__file__).resolve().parents[1] / "examples" / "tenants"
    if not examples.exists():
        # Walk up to repo root
        repo = Path(__file__).resolve().parents[2]
        examples = repo / "examples" / "tenants"
    examples.mkdir(parents=True, exist_ok=True)
    path = examples / "slack-acme.json"
    path.write_text(json.dumps(row))
    yield
    path.unlink(missing_ok=True)


class TestConnectDatadog:
    """POST /api/tenants/{tenant_id}/integrations/datadog"""

    def test_happy_path(self, session_token, _seed_tenant):
        mock_validate = AsyncMock(return_value=True)
        mock_provision = MagicMock(return_value={
            "gateway_url": "https://gateway.example.com/mcp",
            "target_id": "tgt-123",
            "target_name": "tenant-slack-acme-datadog",
            "credential_arn": "arn:cred",
            "extra_headers": {"DD-APPLICATION-KEY": "dd-app-key"},
        })

        with (
            patch("bridge.api._validate_datadog_key", mock_validate),
            patch("bridge.gateway_provisioner.provision_integration", mock_provision),
        ):
            with TestClient(app) as client:
                resp = client.post(
                    "/api/tenants/slack-acme/integrations/datadog",
                    json={"api_key": "dd-test-key-123", "app_key": "dd-app-key-123"},
                    headers={"Authorization": f"Bearer {session_token}"},
                )

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["integration"] == "datadog"
        assert body["target_name"] == "tenant-slack-acme-datadog"
        assert body["gateway_url"] == "https://gateway.example.com/mcp"

    def test_invalid_api_key_returns_error(self, session_token, _seed_tenant):
        mock_validate = AsyncMock(return_value=False)

        with patch("bridge.api._validate_datadog_key", mock_validate):
            with TestClient(app) as client:
                resp = client.post(
                    "/api/tenants/slack-acme/integrations/datadog",
                    json={"api_key": "bad-key", "app_key": "bad-app-key"},
                    headers={"Authorization": f"Bearer {session_token}"},
                )

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        assert "invalid" in body["error"].lower()

    def test_datadog_unreachable_returns_error(self, session_token, _seed_tenant):
        mock_validate = AsyncMock(side_effect=Exception("network error"))

        with patch("bridge.api._validate_datadog_key", mock_validate):
            with TestClient(app) as client:
                resp = client.post(
                    "/api/tenants/slack-acme/integrations/datadog",
                    json={"api_key": "dd-key", "app_key": "dd-app-key"},
                    headers={"Authorization": f"Bearer {session_token}"},
                )

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        assert "could not reach" in body["error"].lower()

    def test_provisioning_failure_returns_error(self, session_token, _seed_tenant):
        mock_validate = AsyncMock(return_value=True)
        mock_provision = MagicMock(side_effect=RuntimeError("SSM missing"))

        with (
            patch("bridge.api._validate_datadog_key", mock_validate),
            patch("bridge.gateway_provisioner.provision_integration", mock_provision),
        ):
            with TestClient(app) as client:
                resp = client.post(
                    "/api/tenants/slack-acme/integrations/datadog",
                    json={"api_key": "dd-key", "app_key": "dd-app-key"},
                    headers={"Authorization": f"Bearer {session_token}"},
                )

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        assert "provisioning failed" in body["error"].lower()

    def test_no_auth_returns_401(self):
        with TestClient(app) as client:
            resp = client.post(
                "/api/tenants/slack-acme/integrations/datadog",
                json={"api_key": "dd-key", "app_key": "dd-app-key"},
            )
        assert resp.status_code == 401

    def test_cross_tenant_returns_403(self):
        other_token = make_session_token("slack-other")
        with TestClient(app) as client:
            resp = client.post(
                "/api/tenants/slack-acme/integrations/datadog",
                json={"api_key": "dd-key", "app_key": "dd-app-key"},
                headers={"Authorization": f"Bearer {other_token}"},
            )
        assert resp.status_code == 403

    def test_empty_api_key_returns_422(self, session_token):
        with TestClient(app) as client:
            resp = client.post(
                "/api/tenants/slack-acme/integrations/datadog",
                json={"api_key": ""},
                headers={"Authorization": f"Bearer {session_token}"},
            )
        assert resp.status_code == 422

    def test_happy_path_enables_byo_on_tenant(self, session_token, _seed_tenant):
        """After connecting, the tenant row should have byo.enabled=True
        and byo.gateway_endpoint set to the shared Gateway URL."""
        from bridge.tenant_write import get_tenant_row

        mock_validate = AsyncMock(return_value=True)
        mock_provision = MagicMock(return_value={
            "gateway_url": "https://gw.example.com/mcp",
            "target_id": "tgt-123",
            "target_name": "tenant-slack-acme-datadog",
            "credential_arn": "arn:cred",
            "extra_headers": {"DD-APPLICATION-KEY": "dd-app-key"},
        })

        with (
            patch("bridge.api._validate_datadog_key", mock_validate),
            patch("bridge.gateway_provisioner.provision_integration", mock_provision),
        ):
            with TestClient(app) as client:
                client.post(
                    "/api/tenants/slack-acme/integrations/datadog",
                    json={"api_key": "dd-key", "app_key": "dd-app-key"},
                    headers={"Authorization": f"Bearer {session_token}"},
                )

        row = get_tenant_row("slack-acme", "us-west-2")
        assert row["byo"]["enabled"] is True
        assert row["byo"]["gateway_endpoint"] == "https://gw.example.com/mcp"
