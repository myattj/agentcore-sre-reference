"""Security tests for the fail-closed Datadog connector surface."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

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

    def test_valid_site_is_explicitly_disabled_without_touching_secrets(
        self, session_token, _seed_tenant
    ):
        mock_provision = MagicMock()

        with patch(
            "bridge.gateway_provisioner.provision_integration",
            mock_provision,
        ):
            with TestClient(app) as client:
                resp = client.post(
                    "/api/tenants/slack-acme/integrations/datadog",
                    json={"site": "datadoghq.com"},
                    headers={"Authorization": f"Bearer {session_token}"},
                )

        assert resp.status_code == 200
        assert resp.json()["ok"] is False
        assert "trusted credential-broker" in resp.json()["error"]
        mock_provision.assert_not_called()

        from bridge.tenant_write import get_tenant_row

        row = get_tenant_row("slack-acme", "us-west-2")
        assert row["byo"]["gateway_auth"] is None
        assert row["byo"]["connected_integrations"] == []

    @pytest.mark.parametrize(
        "site",
        [
            "localhost",
            "127.0.0.1",
            "169.254.169.254",
            "datadoghq.com.evil.test",
            "datadoghq.com@127.0.0.1",
            'datadoghq.com\"}],\"paths\":{\"/pwn\":{}},\"x\":\"',
        ],
    )
    def test_unsafe_site_is_422_before_any_network_or_provisioning_call(
        self, session_token, site
    ):
        mock_provision = MagicMock()
        with patch(
            "bridge.gateway_provisioner.provision_integration",
            mock_provision,
        ):
            with TestClient(app) as client:
                resp = client.post(
                    "/api/tenants/slack-acme/integrations/datadog",
                    json={"site": site},
                    headers={"Authorization": f"Bearer {session_token}"},
                )

        assert resp.status_code == 422
        mock_provision.assert_not_called()

    def test_no_auth_returns_401(self):
        with TestClient(app) as client:
            resp = client.post(
                "/api/tenants/slack-acme/integrations/datadog",
                json={},
            )
        assert resp.status_code == 401

    def test_cross_tenant_returns_403(self):
        other_token = make_session_token("slack-other")
        with TestClient(app) as client:
            resp = client.post(
                "/api/tenants/slack-acme/integrations/datadog",
                json={},
                headers={"Authorization": f"Bearer {other_token}"},
            )
        assert resp.status_code == 403

    @pytest.mark.parametrize("secret_field", ["api_key", "app_key"])
    def test_raw_secret_fields_are_rejected_422(self, session_token, secret_field):
        with TestClient(app) as client:
            resp = client.post(
                "/api/tenants/slack-acme/integrations/datadog",
                json={secret_field: "raw-secret-sentinel"},
                headers={"Authorization": f"Bearer {session_token}"},
            )
        assert resp.status_code == 422
        assert "raw-secret-sentinel" not in resp.text
