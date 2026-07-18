"""Focused tests for Slack Web API profile lookup helpers."""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path
from types import ModuleType

import pytest


def _load_slack_api() -> ModuleType:
    """Load the real module because the shared conftest stubs ``slack_api``."""
    module_path = Path(__file__).resolve().parent.parent / "slack_api.py"
    spec = importlib.util.spec_from_file_location("slack_api_under_test", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


slack_api = _load_slack_api()


def test_get_user_info_returns_user_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    user = {
        "id": "U123",
        "real_name": "Ada Lovelace",
        "profile": {"display_name": "ada", "title": "Engineer"},
    }
    calls: list[tuple[str, str, dict[str, str]]] = []

    def fake_slack_get(
        token: str,
        method: str,
        params: dict[str, str],
    ) -> dict[str, object]:
        calls.append((token, method, params))
        return {"ok": True, "user": user}

    monkeypatch.setattr(slack_api, "_slack_get", fake_slack_get)

    assert slack_api.get_user_info("xoxb-test", "U123") == user
    assert calls == [("xoxb-test", "users.info", {"user": "U123"})]


def test_get_user_info_returns_none_when_slack_rejects_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        slack_api,
        "_slack_get",
        lambda *_args, **_kwargs: {
            "ok": False,
            "error": "user_not_found",
            "user": {"id": "U123"},
        },
    )

    assert slack_api.get_user_info("xoxb-test", "U123") is None


def test_get_user_info_returns_none_for_malformed_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        slack_api,
        "_slack_get",
        lambda *_args, **_kwargs: {"ok": True, "user": "not-an-object"},
    )

    assert slack_api.get_user_info("xoxb-test", "U123") is None


def test_get_user_info_returns_none_when_request_raises(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def raise_request_error(*_args: object, **_kwargs: object) -> None:
        raise OSError("network unavailable")

    monkeypatch.setattr(slack_api, "_slack_get", raise_request_error)

    with caplog.at_level(logging.WARNING, logger=slack_api.__name__):
        assert slack_api.get_user_info("xoxb-test", "U123") is None

    assert "get_user_info failed for U123" in caplog.text
