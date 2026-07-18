"""Security regression tests for the OAuth same-origin cookie handoff."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

from bridge import slack_oauth
from bridge.public_origin import load_oauth_public_config


def _set_urls(
    monkeypatch: pytest.MonkeyPatch,
    *,
    onboarding: str,
    bridge: str,
    callback: str,
    local_dev: bool = False,
) -> None:
    monkeypatch.setenv("ONBOARDING_BASE_URL", onboarding)
    monkeypatch.setenv("BRIDGE_PUBLIC_URL", bridge)
    monkeypatch.setenv("SLACK_REDIRECT_URI", callback)
    monkeypatch.setenv("SLACK_CLIENT_ID", "client-id")
    if local_dev:
        monkeypatch.setenv("LOCAL_DEV", "1")
    else:
        monkeypatch.delenv("LOCAL_DEV", raising=False)


def test_production_https_same_effective_origin_is_canonicalized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_urls(
        monkeypatch,
        onboarding="https://Example.COM:443/",
        bridge="https://example.com",
        callback="https://example.com:443/slack/oauth/callback",
    )

    config = load_oauth_public_config()

    assert config.onboarding_origin == "https://example.com"
    assert config.bridge_origin == "https://example.com"
    assert config.slack_redirect_uri == "https://example.com/slack/oauth/callback"


def test_local_loopback_http_is_allowed_only_on_one_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_urls(
        monkeypatch,
        onboarding="http://127.0.0.1:8000",
        bridge="http://127.0.0.1:8000/",
        callback="http://127.0.0.1:8000/slack/oauth/callback",
        local_dev=True,
    )

    assert load_oauth_public_config().onboarding_origin == "http://127.0.0.1:8000"


def test_documented_local_dev_ports_share_the_host_scoped_cookie(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_urls(
        monkeypatch,
        onboarding="http://localhost:3000",
        bridge="http://localhost:8000",
        callback="http://localhost:8000/slack/oauth/callback",
        local_dev=True,
    )

    config = load_oauth_public_config()

    assert config.onboarding_origin == "http://localhost:3000"
    assert config.bridge_origin == "http://localhost:8000"


def test_local_dev_rejects_different_loopback_host_spellings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_urls(
        monkeypatch,
        onboarding="http://localhost:3000",
        bridge="http://127.0.0.1:8000",
        callback="http://127.0.0.1:8000/slack/oauth/callback",
        local_dev=True,
    )

    with pytest.raises(RuntimeError, match="one loopback hostname"):
        load_oauth_public_config()


@pytest.mark.parametrize(
    ("onboarding", "bridge", "callback"),
    [
        (
            "http://example.com",
            "http://example.com",
            "http://example.com/slack/oauth/callback",
        ),
        (
            "https://ui.example.com",
            "https://bridge.example.com",
            "https://bridge.example.com/slack/oauth/callback",
        ),
        (
            "https://example.com:444",
            "https://example.com",
            "https://example.com/slack/oauth/callback",
        ),
        (
            "https://user:pass@example.com",
            "https://example.com",
            "https://example.com/slack/oauth/callback",
        ),
        (
            "https://example.com?next=https://evil.test",
            "https://example.com",
            "https://example.com/slack/oauth/callback",
        ),
        (
            "https://example.com#fragment",
            "https://example.com",
            "https://example.com/slack/oauth/callback",
        ),
        (
            "https://example.com/onboarding",
            "https://example.com",
            "https://example.com/slack/oauth/callback",
        ),
        (
            "https://example.com",
            "https://example.com",
            "https://example.com/other/callback",
        ),
        (
            "https://bad_host.example.com",
            "https://bad_host.example.com",
            "https://bad_host.example.com/slack/oauth/callback",
        ),
        (
            "https://bad host.example.com",
            "https://bad host.example.com",
            "https://bad host.example.com/slack/oauth/callback",
        ),
    ],
)
def test_unsafe_or_cross_origin_production_urls_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
    onboarding: str,
    bridge: str,
    callback: str,
) -> None:
    _set_urls(
        monkeypatch,
        onboarding=onboarding,
        bridge=bridge,
        callback=callback,
    )

    with pytest.raises(RuntimeError):
        load_oauth_public_config()


def test_local_dev_rejects_non_loopback_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_urls(
        monkeypatch,
        onboarding="http://dev.example.test:8000",
        bridge="http://dev.example.test:8000",
        callback="http://dev.example.test:8000/slack/oauth/callback",
        local_dev=True,
    )

    with pytest.raises(RuntimeError, match="loopback HTTP"):
        load_oauth_public_config()


def test_install_fails_before_state_or_slack_authorization_on_bad_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_urls(
        monkeypatch,
        onboarding="https://ui.example.test",
        bridge="https://bridge.example.test",
        callback="https://bridge.example.test/slack/oauth/callback",
    )
    state_calls: list[bool] = []
    monkeypatch.setattr(
        slack_oauth,
        "make_state_token",
        lambda: state_calls.append(True) or "should-not-be-created",
    )

    with pytest.raises(RuntimeError, match="same scheme, host, and effective port"):
        slack_oauth.build_install_redirect()

    assert state_calls == []


def test_install_uses_canonical_validated_redirect_uri(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_urls(
        monkeypatch,
        onboarding="https://example.test",
        bridge="https://EXAMPLE.test:443",
        callback="https://example.test:443/slack/oauth/callback",
    )

    response = slack_oauth.build_install_redirect()
    query = parse_qs(urlparse(response.headers["location"]).query)

    assert query["redirect_uri"] == ["https://example.test/slack/oauth/callback"]
