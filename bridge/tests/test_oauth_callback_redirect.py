"""Tests for `handle_oauth_callback` redirect behavior.

Week 3 changed the callback from returning placeholder HTML to a 302
redirect into the onboarding UI. These tests lock in the redirect shape
and the error-path slugs so the Next.js side can rely on them.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from bridge import slack_oauth
from bridge.slack_oauth import (
    handle_oauth_callback,
    make_state_token,
    verify_session_token,
)


class _FakeOAuthResponse:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)


class _FakeSlackClient:
    def __init__(self, response: dict[str, Any]) -> None:
        self._response = response

    async def oauth_v2_access(self, **_kwargs: Any) -> _FakeOAuthResponse:
        return _FakeOAuthResponse(self._response)


def _set_slack_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_CLIENT_ID", "id-x")
    monkeypatch.setenv("SLACK_CLIENT_SECRET", "secret-x")
    monkeypatch.setenv("SLACK_REDIRECT_URI", "https://example.test/slack/oauth/callback")
    monkeypatch.setenv("ONBOARDING_BASE_URL", "http://localhost:3000")


def _stub_provisioning(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    """Replace the boto3-using helpers with in-memory stubs. Returns a
    record dict that tests can assert against."""
    calls: dict[str, list[Any]] = {
        "upsert_default": [],
        "store_bot_token": [],
        "upsert_workspace": [],
        "invalidate_cache": [],
    }

    def fake_upsert_default(tenant_id: str, region: str) -> None:
        calls["upsert_default"].append((tenant_id, region))

    def fake_store_bot_token(tenant_id: str, bot_token: str, region: str) -> None:
        calls["store_bot_token"].append((tenant_id, bot_token, region))

    def fake_upsert_workspace(workspace_id: str, tenant_id: str, region: str) -> None:
        calls["upsert_workspace"].append((workspace_id, tenant_id, region))

    def fake_invalidate(tenant_id: str) -> None:
        calls["invalidate_cache"].append(tenant_id)

    monkeypatch.setattr(slack_oauth, "upsert_default_tenant_row", fake_upsert_default)
    monkeypatch.setattr(slack_oauth, "_store_bot_token", fake_store_bot_token)
    monkeypatch.setattr(slack_oauth, "upsert_workspace_mapping", fake_upsert_workspace)
    monkeypatch.setattr(slack_oauth, "invalidate_token_cache", fake_invalidate)
    return calls


def _patch_slack_sdk(
    monkeypatch: pytest.MonkeyPatch, response: dict[str, Any]
) -> None:
    # `handle_oauth_callback` imports AsyncWebClient lazily inside the
    # function. Patch the module that actually gets imported at call time.
    import slack_sdk.web.async_client as async_client_mod

    def _fake_ctor(*_args: Any, **_kwargs: Any) -> _FakeSlackClient:
        return _FakeSlackClient(response)

    monkeypatch.setattr(async_client_mod, "AsyncWebClient", _fake_ctor)


async def test_callback_success_redirects_with_verifiable_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_slack_env(monkeypatch)
    calls = _stub_provisioning(monkeypatch)
    _patch_slack_sdk(
        monkeypatch,
        {
            "ok": True,
            "team": {"id": "T12345", "name": "Acme"},
            "access_token": "xoxb-fake",
        },
    )

    state = make_state_token()
    response = await handle_oauth_callback(code="code-x", state=state)

    assert response.status_code == 302
    location = response.headers["location"]
    parsed = urlparse(location)
    assert parsed.scheme == "http"
    assert parsed.netloc == "localhost:3000"
    assert parsed.path == "/onboarding/slack-t12345/welcome"

    qs = parse_qs(parsed.query)
    token = qs["t"][0]
    assert verify_session_token(token) == "slack-t12345"

    # Provisioning was called with the right args
    assert calls["upsert_default"] == [("slack-t12345", "us-west-2")]
    assert calls["store_bot_token"] == [("slack-t12345", "xoxb-fake", "us-west-2")]
    assert calls["upsert_workspace"] == [("T12345", "slack-t12345", "us-west-2")]
    assert calls["invalidate_cache"] == ["slack-t12345"]


async def test_callback_invalid_state_redirects_to_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_slack_env(monkeypatch)
    _stub_provisioning(monkeypatch)
    # No need to patch slack_sdk — we should fail before the exchange.

    response = await handle_oauth_callback(code="code-x", state="not-a-valid-state")

    assert response.status_code == 302
    parsed = urlparse(response.headers["location"])
    assert parsed.path == "/onboarding/error"
    assert parse_qs(parsed.query)["reason"] == ["invalid_state"]


async def test_callback_exchange_failure_redirects_to_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_slack_env(monkeypatch)
    _stub_provisioning(monkeypatch)
    _patch_slack_sdk(
        monkeypatch,
        {"ok": False, "error": "invalid_code"},
    )

    state = make_state_token()
    response = await handle_oauth_callback(code="code-x", state=state)

    assert response.status_code == 302
    parsed = urlparse(response.headers["location"])
    assert parsed.path == "/onboarding/error"
    assert parse_qs(parsed.query)["reason"] == ["exchange_failed"]


async def test_callback_missing_config_redirects_to_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If Slack env vars are missing, the callback cannot proceed even
    with a valid state."""
    monkeypatch.setenv("ONBOARDING_BASE_URL", "http://localhost:3000")
    monkeypatch.delenv("SLACK_CLIENT_ID", raising=False)
    monkeypatch.delenv("SLACK_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("SLACK_REDIRECT_URI", raising=False)

    state = make_state_token()
    response = await handle_oauth_callback(code="code-x", state=state)

    assert response.status_code == 302
    parsed = urlparse(response.headers["location"])
    assert parsed.path == "/onboarding/error"
    assert parse_qs(parsed.query)["reason"] == ["not_configured"]
