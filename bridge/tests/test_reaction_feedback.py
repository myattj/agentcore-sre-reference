"""Fail-closed identity tests for reaction-derived agent feedback."""
from __future__ import annotations

from typing import Any

import pytest

from bridge.reaction_feedback import dispatch_reaction_feedback


def _event(**overrides: Any) -> dict[str, Any]:
    event: dict[str, Any] = {
        "reaction": "+1",
        "user": "U_REACTOR",
        "team_id": "T_WORKSPACE",
        "api_app_id": "A_AGENT",
        "item": {"type": "message", "channel": "C_ONE", "ts": "2.0"},
    }
    event.update(overrides)
    return event


class _Adapter:
    def __init__(self, message: dict[str, Any]) -> None:
        self.message = message
        self.calls: list[tuple[str, str, str]] = []

    async def fetch_message(
        self,
        workspace_id: str,
        channel_id: str,
        message_ts: str,
    ) -> dict[str, Any] | None:
        self.calls.append((workspace_id, channel_id, message_ts))
        return self.message


class _Client:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def invoke(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


@pytest.mark.asyncio
async def test_feedback_requires_exact_event_and_message_app_identity() -> None:
    adapter = _Adapter(
        {
            "app_id": "A_AGENT",
            "bot_id": "B_AGENT",
            "text": "Healthy now.",
            "thread_ts": "2.0",
        }
    )
    client = _Client()

    await dispatch_reaction_feedback(
        adapter, _event(), client, "tenant-a", "A_AGENT"
    )

    assert len(client.calls) == 1
    assert client.calls[0]["tenant_id"] == "tenant-a"
    assert client.calls[0]["ctx"]["feedback"]["bot_answer"] == "Healthy now."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("configured_app", "event_app"),
    [("", "A_AGENT"), ("A_AGENT", ""), ("A_AGENT", "A_FOREIGN")],
)
async def test_feedback_fails_before_fetch_without_verified_event_identity(
    configured_app: str,
    event_app: str,
) -> None:
    adapter = _Adapter({"app_id": "A_AGENT", "bot_id": "B_AGENT"})
    client = _Client()

    await dispatch_reaction_feedback(
        adapter,
        _event(api_app_id=event_app),
        client,
        "tenant-a",
        configured_app,
    )

    assert adapter.calls == []
    assert client.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("message", [{"app_id": "A_FOREIGN"}, {"bot_id": "B_AGENT"}])
async def test_feedback_rejects_foreign_or_unattributed_bot_messages(
    message: dict[str, Any],
) -> None:
    adapter = _Adapter(message)
    client = _Client()

    await dispatch_reaction_feedback(
        adapter, _event(), client, "tenant-a", "A_AGENT"
    )

    assert len(adapter.calls) == 1
    assert client.calls == []
