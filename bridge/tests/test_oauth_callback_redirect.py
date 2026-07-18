"""Tests for `handle_oauth_callback` redirect behavior.

The callback returns a 302 redirect into the onboarding UI. These tests
lock in the redirect shape
and the error-path slugs so the Next.js side can rely on them.
"""
from __future__ import annotations

from http.cookies import SimpleCookie
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from bridge import slack_oauth
from bridge.main import app
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
    monkeypatch.setenv("ONBOARDING_BASE_URL", "https://example.test")
    monkeypatch.setenv("BRIDGE_PUBLIC_URL", "https://example.test")


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


def _state_nonce(state: str) -> str:
    return state.split(".", 1)[0]


async def test_callback_success_sets_cookie_and_redirects_without_bearer_url(
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
    response = await handle_oauth_callback(
        code="code-x",
        state=state,
        browser_nonce=_state_nonce(state),
    )

    assert response.status_code == 302
    location = response.headers["location"]
    parsed = urlparse(location)
    assert parsed.scheme == "https"
    assert parsed.netloc == "example.test"
    assert parsed.path == "/onboarding/slack-t12345/integrations"
    assert parsed.query == ""

    cookies = SimpleCookie()
    for header in response.headers.getlist("set-cookie"):
        cookies.load(header)
    session_cookie = cookies["tenant_session"]
    token = session_cookie.value
    assert verify_session_token(token) == "slack-t12345"
    assert session_cookie["httponly"] is True
    assert session_cookie["secure"] is True
    assert session_cookie["samesite"] == "lax"
    assert session_cookie["max-age"] == "3600"
    assert response.headers["cache-control"] == "no-store"

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

    response = await handle_oauth_callback(
        code="code-x",
        state="not-a-valid-state",
        browser_nonce="wrong-browser",
    )

    assert response.status_code == 302
    parsed = urlparse(response.headers["location"])
    assert parsed.path == "/onboarding/error"
    assert parse_qs(parsed.query)["reason"] == ["invalid_state"]


async def test_callback_exchange_failure_redirects_to_error(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _set_slack_env(monkeypatch)
    _stub_provisioning(monkeypatch)
    _patch_slack_sdk(
        monkeypatch,
        {
            "ok": False,
            "error": "invalid_code",
            "access_token": "slack-token-must-never-reach-logs",
        },
    )

    state = make_state_token()
    response = await handle_oauth_callback(
        code="code-x",
        state=state,
        browser_nonce=_state_nonce(state),
    )

    assert response.status_code == 302
    parsed = urlparse(response.headers["location"])
    assert parsed.path == "/onboarding/error"
    assert parse_qs(parsed.query)["reason"] == ["exchange_failed"]
    assert "invalid_code" in caplog.text
    assert "slack-token-must-never-reach-logs" not in caplog.text


async def test_callback_missing_config_redirects_to_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If Slack env vars are missing, the callback cannot proceed even
    with a valid state."""
    monkeypatch.setenv("ONBOARDING_BASE_URL", "http://localhost:8000")
    monkeypatch.setenv("BRIDGE_PUBLIC_URL", "http://localhost:8000")
    monkeypatch.setenv(
        "SLACK_REDIRECT_URI", "http://localhost:8000/slack/oauth/callback"
    )
    monkeypatch.delenv("SLACK_CLIENT_ID", raising=False)
    monkeypatch.delenv("SLACK_CLIENT_SECRET", raising=False)

    state = make_state_token()
    response = await handle_oauth_callback(
        code="code-x",
        state=state,
        browser_nonce=_state_nonce(state),
    )

    assert response.status_code == 302
    parsed = urlparse(response.headers["location"])
    assert parsed.path == "/onboarding/error"
    assert parse_qs(parsed.query)["reason"] == ["not_configured"]


def test_oauth_state_is_bound_to_the_browser_that_started_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_slack_env(monkeypatch)
    _stub_provisioning(monkeypatch)
    _patch_slack_sdk(
        monkeypatch,
        {
            "ok": True,
            "team": {"id": "T12345", "name": "Acme"},
            "access_token": "xoxb-fake",
        },
    )

    browser_a = TestClient(app, base_url="https://example.test")
    browser_b = TestClient(app, base_url="https://example.test")
    install = browser_a.get("/slack/install", follow_redirects=False)
    state = parse_qs(urlparse(install.headers["location"]).query)["state"][0]
    state_cookie = SimpleCookie()
    state_cookie.load(install.headers["set-cookie"])
    assert state_cookie["slack_oauth_state"]["httponly"] is True
    assert state_cookie["slack_oauth_state"]["secure"] is True
    assert state_cookie["slack_oauth_state"]["samesite"] == "lax"
    assert state_cookie["slack_oauth_state"]["path"] == "/slack/oauth/callback"

    swapped = browser_b.get(
        "/slack/oauth/callback",
        params={"code": "code-x", "state": state},
        follow_redirects=False,
    )
    assert parse_qs(urlparse(swapped.headers["location"]).query)["reason"] == [
        "invalid_state"
    ]
    assert "tenant_session" not in browser_b.cookies

    legitimate = browser_a.get(
        "/slack/oauth/callback",
        params={"code": "code-x", "state": state},
        follow_redirects=False,
    )
    assert urlparse(legitimate.headers["location"]).path == (
        "/onboarding/slack-t12345/integrations"
    )
    assert "tenant_session" in browser_a.cookies
    assert "slack_oauth_state" not in browser_a.cookies
