"""Tests for fail-closed Slack channel membership lookup."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_CORE_AGENT_DIR = (
    Path(__file__).resolve().parents[2] / "coreAgent" / "app" / "coreAgent"
)
if str(_CORE_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(_CORE_AGENT_DIR))

import slack_api  # type: ignore[import-not-found]  # noqa: E402


def test_membership_lookup_paginates_until_exact_user_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    def fake_get(
        _token: str,
        method: str,
        params: dict[str, object],
    ) -> dict[str, object]:
        assert method == "conversations.members"
        calls.append(params)
        if len(calls) == 1:
            return {
                "ok": True,
                "members": ["U_OTHER"],
                "response_metadata": {"next_cursor": "page-2"},
            }
        return {
            "ok": True,
            "members": ["U_REQUESTER"],
            "response_metadata": {"next_cursor": ""},
        }

    monkeypatch.setattr(slack_api, "_slack_get", fake_get)

    assert (
        slack_api.is_user_member_of_channel("xoxb-test", "C_TARGET", "U_REQUESTER")
        is True
    )
    assert calls[0]["cursor"] is None
    assert calls[1]["cursor"] == "page-2"


def test_membership_lookup_returns_false_only_after_complete_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        slack_api,
        "_slack_get",
        lambda *_args, **_kwargs: {
            "ok": True,
            "members": ["U_OTHER"],
            "response_metadata": {"next_cursor": ""},
        },
    )

    assert (
        slack_api.is_user_member_of_channel("xoxb-test", "C_TARGET", "U_REQUESTER")
        is False
    )


@pytest.mark.parametrize(
    "response",
    [
        {"ok": False, "error": "missing_scope"},
        {"ok": True, "members": "not-a-list"},
        {"ok": True, "members": [], "response_metadata": "bad-shape"},
    ],
)
def test_membership_lookup_fails_closed_on_indeterminate_response(
    monkeypatch: pytest.MonkeyPatch,
    response: dict[str, object],
) -> None:
    monkeypatch.setattr(
        slack_api,
        "_slack_get",
        lambda *_args, **_kwargs: response,
    )

    assert (
        slack_api.is_user_member_of_channel("xoxb-test", "C_TARGET", "U_REQUESTER")
        is None
    )


def test_membership_lookup_fails_closed_on_api_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise OSError("network down")

    monkeypatch.setattr(slack_api, "_slack_get", fail)

    assert (
        slack_api.is_user_member_of_channel("xoxb-test", "C_TARGET", "U_REQUESTER")
        is None
    )
