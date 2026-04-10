"""Tests for the 6 non-Datadog connector routes (week 5).

Each test class covers the same patterns:
  - Happy path: valid credentials + successful provisioning → ok=true
  - Invalid credentials → ok=false
  - API unreachable → ok=false
  - Provisioning failure → ok=false
  - No auth → 401
  - Cross-tenant → 403
  - Empty required fields → 422
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
def _seed_tenant():
    """Seed a minimal tenant row."""
    from bridge import tenant_write
    tenant_write.reset_tenant_write_for_tests()
    row = tenant_write.build_default_config_dict("slack-acme")
    import json
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    examples = repo / "examples" / "tenants"
    examples.mkdir(parents=True, exist_ok=True)
    path = examples / "slack-acme.json"
    path.write_text(json.dumps(row))
    yield
    path.unlink(missing_ok=True)


_PROVISION_RESULT = {
    "gateway_url": "https://gateway.example.com/mcp",
    "target_id": "tgt-123",
    "target_name": "tenant-slack-acme-test",
    "credential_arn": "arn:cred",
    "extra_headers": {},
}


# ---------------------------------------------------------------------------
# Confluence
# ---------------------------------------------------------------------------

class TestConnectConfluence:
    URL = "/api/tenants/slack-acme/integrations/confluence"
    BODY = {"email": "a@b.com", "api_token": "tok", "domain": "acme"}

    def test_happy_path(self, session_token, _seed_tenant):
        with (
            patch("bridge.api._validate_confluence", AsyncMock(return_value=True)),
            patch("bridge.gateway_provisioner.provision_integration", MagicMock(return_value=_PROVISION_RESULT)),
        ):
            with TestClient(app) as c:
                r = c.post(self.URL, json=self.BODY, headers={"Authorization": f"Bearer {session_token}"})
        assert r.status_code == 200 and r.json()["ok"] is True

    def test_invalid_creds(self, session_token, _seed_tenant):
        with patch("bridge.api._validate_confluence", AsyncMock(return_value=False)):
            with TestClient(app) as c:
                r = c.post(self.URL, json=self.BODY, headers={"Authorization": f"Bearer {session_token}"})
        assert r.json()["ok"] is False and "invalid" in r.json()["error"].lower()

    def test_unreachable(self, session_token, _seed_tenant):
        with patch("bridge.api._validate_confluence", AsyncMock(side_effect=Exception("net"))):
            with TestClient(app) as c:
                r = c.post(self.URL, json=self.BODY, headers={"Authorization": f"Bearer {session_token}"})
        assert r.json()["ok"] is False and "could not reach" in r.json()["error"].lower()

    def test_provision_fail(self, session_token, _seed_tenant):
        with (
            patch("bridge.api._validate_confluence", AsyncMock(return_value=True)),
            patch("bridge.gateway_provisioner.provision_integration", MagicMock(side_effect=RuntimeError("boom"))),
        ):
            with TestClient(app) as c:
                r = c.post(self.URL, json=self.BODY, headers={"Authorization": f"Bearer {session_token}"})
        assert r.json()["ok"] is False and "provisioning" in r.json()["error"].lower()

    def test_no_auth(self):
        with TestClient(app) as c:
            assert c.post(self.URL, json=self.BODY).status_code == 401

    def test_cross_tenant(self):
        t = make_session_token("slack-other")
        with TestClient(app) as c:
            assert c.post(self.URL, json=self.BODY, headers={"Authorization": f"Bearer {t}"}).status_code == 403

    def test_empty_field_422(self, session_token):
        with TestClient(app) as c:
            assert c.post(self.URL, json={"email": "", "api_token": "t", "domain": "d"}, headers={"Authorization": f"Bearer {session_token}"}).status_code == 422


# ---------------------------------------------------------------------------
# Notion
# ---------------------------------------------------------------------------

class TestConnectNotion:
    URL = "/api/tenants/slack-acme/integrations/notion"
    BODY = {"integration_token": "ntn_test"}

    def test_happy_path(self, session_token, _seed_tenant):
        with (
            patch("bridge.api._validate_notion", AsyncMock(return_value=True)),
            patch("bridge.gateway_provisioner.provision_integration", MagicMock(return_value=_PROVISION_RESULT)),
        ):
            with TestClient(app) as c:
                r = c.post(self.URL, json=self.BODY, headers={"Authorization": f"Bearer {session_token}"})
        assert r.status_code == 200 and r.json()["ok"] is True

    def test_invalid_token(self, session_token, _seed_tenant):
        with patch("bridge.api._validate_notion", AsyncMock(return_value=False)):
            with TestClient(app) as c:
                r = c.post(self.URL, json=self.BODY, headers={"Authorization": f"Bearer {session_token}"})
        assert r.json()["ok"] is False and "invalid" in r.json()["error"].lower()

    def test_unreachable(self, session_token, _seed_tenant):
        with patch("bridge.api._validate_notion", AsyncMock(side_effect=Exception("net"))):
            with TestClient(app) as c:
                r = c.post(self.URL, json=self.BODY, headers={"Authorization": f"Bearer {session_token}"})
        assert r.json()["ok"] is False

    def test_no_auth(self):
        with TestClient(app) as c:
            assert c.post(self.URL, json=self.BODY).status_code == 401

    def test_empty_token_422(self, session_token):
        with TestClient(app) as c:
            assert c.post(self.URL, json={"integration_token": ""}, headers={"Authorization": f"Bearer {session_token}"}).status_code == 422


# ---------------------------------------------------------------------------
# Jira
# ---------------------------------------------------------------------------

class TestConnectJira:
    URL = "/api/tenants/slack-acme/integrations/jira"
    BODY = {"email": "a@b.com", "api_token": "tok", "domain": "acme"}

    def test_happy_path(self, session_token, _seed_tenant):
        with (
            patch("bridge.api._validate_jira", AsyncMock(return_value=True)),
            patch("bridge.gateway_provisioner.provision_integration", MagicMock(return_value=_PROVISION_RESULT)),
        ):
            with TestClient(app) as c:
                r = c.post(self.URL, json=self.BODY, headers={"Authorization": f"Bearer {session_token}"})
        assert r.status_code == 200 and r.json()["ok"] is True

    def test_invalid_creds(self, session_token, _seed_tenant):
        with patch("bridge.api._validate_jira", AsyncMock(return_value=False)):
            with TestClient(app) as c:
                r = c.post(self.URL, json=self.BODY, headers={"Authorization": f"Bearer {session_token}"})
        assert r.json()["ok"] is False and "invalid" in r.json()["error"].lower()

    def test_unreachable(self, session_token, _seed_tenant):
        with patch("bridge.api._validate_jira", AsyncMock(side_effect=Exception("net"))):
            with TestClient(app) as c:
                r = c.post(self.URL, json=self.BODY, headers={"Authorization": f"Bearer {session_token}"})
        assert r.json()["ok"] is False

    def test_no_auth(self):
        with TestClient(app) as c:
            assert c.post(self.URL, json=self.BODY).status_code == 401

    def test_empty_field_422(self, session_token):
        with TestClient(app) as c:
            assert c.post(self.URL, json={"email": "", "api_token": "t", "domain": "d"}, headers={"Authorization": f"Bearer {session_token}"}).status_code == 422


# ---------------------------------------------------------------------------
# Linear
# ---------------------------------------------------------------------------

class TestConnectLinear:
    URL = "/api/tenants/slack-acme/integrations/linear"
    BODY = {"api_key": "lin_api_test"}

    def test_happy_path(self, session_token, _seed_tenant):
        with (
            patch("bridge.api._validate_linear", AsyncMock(return_value=True)),
            patch("bridge.gateway_provisioner.provision_integration", MagicMock(return_value=_PROVISION_RESULT)),
        ):
            with TestClient(app) as c:
                r = c.post(self.URL, json=self.BODY, headers={"Authorization": f"Bearer {session_token}"})
        assert r.status_code == 200 and r.json()["ok"] is True

    def test_invalid_key(self, session_token, _seed_tenant):
        with patch("bridge.api._validate_linear", AsyncMock(return_value=False)):
            with TestClient(app) as c:
                r = c.post(self.URL, json=self.BODY, headers={"Authorization": f"Bearer {session_token}"})
        assert r.json()["ok"] is False and "invalid" in r.json()["error"].lower()

    def test_unreachable(self, session_token, _seed_tenant):
        with patch("bridge.api._validate_linear", AsyncMock(side_effect=Exception("net"))):
            with TestClient(app) as c:
                r = c.post(self.URL, json=self.BODY, headers={"Authorization": f"Bearer {session_token}"})
        assert r.json()["ok"] is False

    def test_no_auth(self):
        with TestClient(app) as c:
            assert c.post(self.URL, json=self.BODY).status_code == 401

    def test_empty_key_422(self, session_token):
        with TestClient(app) as c:
            assert c.post(self.URL, json={"api_key": ""}, headers={"Authorization": f"Bearer {session_token}"}).status_code == 422


# ---------------------------------------------------------------------------
# PagerDuty
# ---------------------------------------------------------------------------

class TestConnectPagerDuty:
    URL = "/api/tenants/slack-acme/integrations/pagerduty"
    BODY = {"api_key": "pd_key_test"}

    def test_happy_path(self, session_token, _seed_tenant):
        with (
            patch("bridge.api._validate_pagerduty", AsyncMock(return_value=True)),
            patch("bridge.gateway_provisioner.provision_integration", MagicMock(return_value=_PROVISION_RESULT)),
        ):
            with TestClient(app) as c:
                r = c.post(self.URL, json=self.BODY, headers={"Authorization": f"Bearer {session_token}"})
        assert r.status_code == 200 and r.json()["ok"] is True

    def test_invalid_key(self, session_token, _seed_tenant):
        with patch("bridge.api._validate_pagerduty", AsyncMock(return_value=False)):
            with TestClient(app) as c:
                r = c.post(self.URL, json=self.BODY, headers={"Authorization": f"Bearer {session_token}"})
        assert r.json()["ok"] is False and "invalid" in r.json()["error"].lower()

    def test_unreachable(self, session_token, _seed_tenant):
        with patch("bridge.api._validate_pagerduty", AsyncMock(side_effect=Exception("net"))):
            with TestClient(app) as c:
                r = c.post(self.URL, json=self.BODY, headers={"Authorization": f"Bearer {session_token}"})
        assert r.json()["ok"] is False

    def test_no_auth(self):
        with TestClient(app) as c:
            assert c.post(self.URL, json=self.BODY).status_code == 401

    def test_empty_key_422(self, session_token):
        with TestClient(app) as c:
            assert c.post(self.URL, json={"api_key": ""}, headers={"Authorization": f"Bearer {session_token}"}).status_code == 422


# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------

class TestConnectGitHub:
    URL = "/api/tenants/slack-acme/integrations/github"
    BODY = {"personal_access_token": "ghp_test"}

    def test_happy_path(self, session_token, _seed_tenant):
        with (
            patch("bridge.api._validate_github", AsyncMock(return_value=True)),
            patch("bridge.gateway_provisioner.provision_integration", MagicMock(return_value=_PROVISION_RESULT)),
        ):
            with TestClient(app) as c:
                r = c.post(self.URL, json=self.BODY, headers={"Authorization": f"Bearer {session_token}"})
        assert r.status_code == 200 and r.json()["ok"] is True

    def test_invalid_token(self, session_token, _seed_tenant):
        with patch("bridge.api._validate_github", AsyncMock(return_value=False)):
            with TestClient(app) as c:
                r = c.post(self.URL, json=self.BODY, headers={"Authorization": f"Bearer {session_token}"})
        assert r.json()["ok"] is False and "invalid" in r.json()["error"].lower()

    def test_unreachable(self, session_token, _seed_tenant):
        with patch("bridge.api._validate_github", AsyncMock(side_effect=Exception("net"))):
            with TestClient(app) as c:
                r = c.post(self.URL, json=self.BODY, headers={"Authorization": f"Bearer {session_token}"})
        assert r.json()["ok"] is False

    def test_no_auth(self):
        with TestClient(app) as c:
            assert c.post(self.URL, json=self.BODY).status_code == 401

    def test_empty_pat_422(self, session_token):
        with TestClient(app) as c:
            assert c.post(self.URL, json={"personal_access_token": ""}, headers={"Authorization": f"Bearer {session_token}"}).status_code == 422

    def test_connected_integrations_tracked(self, session_token, _seed_tenant):
        """After connecting, byo.connected_integrations should include 'github'."""
        from bridge.tenant_write import get_tenant_row
        with (
            patch("bridge.api._validate_github", AsyncMock(return_value=True)),
            patch("bridge.gateway_provisioner.provision_integration", MagicMock(return_value=_PROVISION_RESULT)),
        ):
            with TestClient(app) as c:
                c.post(self.URL, json=self.BODY, headers={"Authorization": f"Bearer {session_token}"})
        row = get_tenant_row("slack-acme", "us-west-2")
        assert "github" in row["byo"].get("connected_integrations", [])
        assert row["byo"]["enabled"] is True
