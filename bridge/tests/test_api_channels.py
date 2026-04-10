"""Tests for `GET /api/tenants/{tenant_id}/channels`."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from slack_sdk.errors import SlackApiError

from bridge.api_models import ChannelInfo
from bridge.main import app
from bridge.slack_oauth import make_session_token


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_channels_requires_auth(client: TestClient) -> None:
    r = client.get("/api/tenants/slack-alpha/channels")
    assert r.status_code == 401


def test_channels_success(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_list(tenant_id: str) -> list[ChannelInfo]:
        assert tenant_id == "slack-alpha"
        return [
            ChannelInfo(id="C1", name="general", is_private=False),
            ChannelInfo(id="C2", name="random", is_private=False),
        ]

    monkeypatch.setattr("bridge.api.list_channels_for_tenant", fake_list)

    token = make_session_token("slack-alpha")
    r = client.get("/api/tenants/slack-alpha/channels", headers=_auth(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["channels"] == [
        {"id": "C1", "name": "general", "is_private": False},
        {"id": "C2", "name": "random", "is_private": False},
    ]
    assert body["needs_reinstall"] is False


def test_channels_no_token_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_list(tenant_id: str) -> list[ChannelInfo]:
        raise KeyError(tenant_id)

    monkeypatch.setattr("bridge.api.list_channels_for_tenant", fake_list)

    token = make_session_token("slack-alpha")
    r = client.get("/api/tenants/slack-alpha/channels", headers=_auth(token))
    assert r.status_code == 404


def test_channels_slack_api_error_502(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_list(tenant_id: str) -> list[ChannelInfo]:
        raise SlackApiError(
            message="ratelimited",
            response={"ok": False, "error": "ratelimited"},
        )

    monkeypatch.setattr("bridge.api.list_channels_for_tenant", fake_list)

    token = make_session_token("slack-alpha")
    r = client.get("/api/tenants/slack-alpha/channels", headers=_auth(token))
    assert r.status_code == 502


def test_channels_missing_scope_returns_needs_reinstall(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bot token is valid but missing channels:read / groups:read.
    Should NOT 502 — should return 200 with `needs_reinstall=true` so
    the onboarding UI can show a re-install hint."""

    async def fake_list(tenant_id: str) -> list[ChannelInfo]:
        raise SlackApiError(
            message="missing_scope",
            response={"ok": False, "error": "missing_scope"},
        )

    monkeypatch.setattr("bridge.api.list_channels_for_tenant", fake_list)

    token = make_session_token("slack-alpha")
    r = client.get("/api/tenants/slack-alpha/channels", headers=_auth(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {"channels": [], "needs_reinstall": True}


def test_channels_empty_list_200(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty list is a valid response (bot not invited anywhere yet)."""

    async def fake_list(tenant_id: str) -> list[ChannelInfo]:
        return []

    monkeypatch.setattr("bridge.api.list_channels_for_tenant", fake_list)

    token = make_session_token("slack-alpha")
    r = client.get("/api/tenants/slack-alpha/channels", headers=_auth(token))
    assert r.status_code == 200
    assert r.json() == {"channels": [], "needs_reinstall": False}
