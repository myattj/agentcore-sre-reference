"""Tests for bridge/bridge/slack_interactions.py.

Covers:
  - ``extract_payload_json`` — form-urlencoded → JSON decode
  - ``parse_interactivity_payload`` — dict → InteractivityPayload
  - ``is_codebase_pick_action`` — action_id prefix match
  - ``build_codebase_pick_synthetic_message`` — payload → InboundMessage
  - ``post_response_url_update`` — one-time URL post (with mocked HTTP)
  - End-to-end via the POST /slack/interactions route using
    ``fastapi.testclient.TestClient`` and a monkeypatched tenant
    resolver + background task capture

Never hits Slack. Every HTTP boundary is stubbed.
"""
from __future__ import annotations

import json
import urllib.parse
from typing import Any

import pytest
from fastapi.testclient import TestClient

# conftest.py sets AGENT_RUNTIME_ARN to a dummy so `bridge.main` can
# instantiate its module-level AgentCoreClient at import time — no extra
# setup needed here.
from bridge.adapters.core import InboundMessage
from bridge.slack_interactions import (
    InteractivityPayload,
    build_codebase_pick_synthetic_message,
    extract_payload_json,
    is_codebase_pick_action,
    parse_interactivity_payload,
    post_response_url_update,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _sample_payload(
    *,
    action_id: str = "codebase_pick:0",
    action_value: str = "acme/platform",
    team_id: str = "T_ACME",
    channel_id: str = "C_ONCALL",
    thread_ts: str | None = "1712345670.100000",
    message_ts: str = "1712345678.123456",
) -> dict[str, Any]:
    """Build a realistic Slack block_actions payload for tests."""
    message: dict[str, Any] = {"ts": message_ts}
    if thread_ts is not None:
        message["thread_ts"] = thread_ts
    return {
        "type": "block_actions",
        "team": {"id": team_id, "domain": "acme"},
        "user": {"id": "U_JOSH", "name": "josh"},
        "api_app_id": "A_AGENTCORE",
        "token": "deprecated",
        "container": {
            "type": "message",
            "message_ts": message_ts,
            "channel_id": channel_id,
        },
        "trigger_id": "111.222.333",
        "channel": {"id": channel_id, "name": "platform-oncall"},
        "message": message,
        "response_url": "https://hooks.slack.com/actions/T_ACME/111/abc",
        "actions": [
            {
                "action_id": action_id,
                "block_id": "codebase_choice",
                "type": "button",
                "value": action_value,
                "action_ts": "1712345680.000000",
            }
        ],
    }


# ---------------------------------------------------------------------------
# extract_payload_json
# ---------------------------------------------------------------------------

class TestExtractPayloadJson:
    def test_happy_path(self) -> None:
        sample = _sample_payload()
        body = urllib.parse.urlencode({"payload": json.dumps(sample)}).encode()
        result = extract_payload_json(body)
        assert result is not None
        assert result["type"] == "block_actions"
        assert result["team"]["id"] == "T_ACME"

    def test_missing_payload_field(self) -> None:
        body = urllib.parse.urlencode({"other": "data"}).encode()
        assert extract_payload_json(body) is None

    def test_empty_body(self) -> None:
        assert extract_payload_json(b"") is None

    def test_malformed_json_in_payload(self) -> None:
        body = urllib.parse.urlencode({"payload": "{not json"}).encode()
        assert extract_payload_json(body) is None

    def test_non_dict_payload(self) -> None:
        """Payload must be a JSON object, not an array or scalar."""
        body = urllib.parse.urlencode({"payload": "[1, 2, 3]"}).encode()
        assert extract_payload_json(body) is None


# ---------------------------------------------------------------------------
# parse_interactivity_payload
# ---------------------------------------------------------------------------

class TestParseInteractivityPayload:
    def test_happy_path(self) -> None:
        parsed = parse_interactivity_payload(_sample_payload())
        assert parsed is not None
        assert parsed.team_id == "T_ACME"
        assert parsed.user_id == "U_JOSH"
        assert parsed.channel_id == "C_ONCALL"
        assert parsed.message_ts == "1712345678.123456"
        assert parsed.thread_ts == "1712345670.100000"
        assert parsed.action_id == "codebase_pick:0"
        assert parsed.action_value == "acme/platform"
        assert parsed.response_url.startswith("https://hooks.slack.com/")

    def test_missing_thread_ts_falls_back_to_message_ts(self) -> None:
        """Top-of-thread clicks have no thread_ts — message ts becomes the thread root."""
        payload = _sample_payload(thread_ts=None)
        parsed = parse_interactivity_payload(payload)
        assert parsed is not None
        assert parsed.thread_ts == parsed.message_ts

    def test_non_block_actions_type(self) -> None:
        payload = _sample_payload()
        payload["type"] = "view_submission"
        assert parse_interactivity_payload(payload) is None

    def test_missing_actions(self) -> None:
        payload = _sample_payload()
        payload["actions"] = []
        assert parse_interactivity_payload(payload) is None

    def test_missing_action_id(self) -> None:
        payload = _sample_payload()
        payload["actions"][0]["action_id"] = ""
        assert parse_interactivity_payload(payload) is None

    def test_missing_team_id(self) -> None:
        payload = _sample_payload()
        payload["team"] = {}
        assert parse_interactivity_payload(payload) is None

    def test_missing_channel_id(self) -> None:
        payload = _sample_payload()
        payload["channel"] = {}
        assert parse_interactivity_payload(payload) is None

    def test_non_dict_input(self) -> None:
        assert parse_interactivity_payload("not a dict") is None  # type: ignore[arg-type]
        assert parse_interactivity_payload(None) is None  # type: ignore[arg-type]

    def test_missing_response_url_is_allowed(self) -> None:
        """response_url is used for best-effort polish — the parser
        accepts payloads without it (gracefully degrades to 'no update')."""
        payload = _sample_payload()
        payload["response_url"] = ""
        parsed = parse_interactivity_payload(payload)
        assert parsed is not None
        assert parsed.response_url == ""


# ---------------------------------------------------------------------------
# is_codebase_pick_action
# ---------------------------------------------------------------------------

class TestIsCodebasePickAction:
    def test_index_zero(self) -> None:
        assert is_codebase_pick_action("codebase_pick:0")

    def test_index_positive(self) -> None:
        assert is_codebase_pick_action("codebase_pick:4")

    def test_bare_prefix(self) -> None:
        assert is_codebase_pick_action("codebase_pick")

    def test_different_action(self) -> None:
        assert not is_codebase_pick_action("escalate_pick:0")

    def test_empty_string(self) -> None:
        assert not is_codebase_pick_action("")


# ---------------------------------------------------------------------------
# build_codebase_pick_synthetic_message
# ---------------------------------------------------------------------------

class TestBuildSyntheticMessage:
    def test_includes_repo_in_text(self) -> None:
        parsed = parse_interactivity_payload(
            _sample_payload(action_value="acme/billing")
        )
        assert parsed is not None
        synthetic = build_codebase_pick_synthetic_message(parsed)
        assert "acme/billing" in synthetic.text

    def test_includes_going_forward_phrase(self) -> None:
        """The phrase must match the SHORTLIST prompt block's acknowledgment
        template — this is what keys the semantic extractor."""
        parsed = parse_interactivity_payload(_sample_payload())
        assert parsed is not None
        synthetic = build_codebase_pick_synthetic_message(parsed)
        assert "going forward" in synthetic.text

    def test_preserves_thread_context(self) -> None:
        parsed = parse_interactivity_payload(_sample_payload())
        assert parsed is not None
        synthetic = build_codebase_pick_synthetic_message(parsed)
        assert synthetic.thread_id == "1712345670.100000"
        assert synthetic.channel_id == "C_ONCALL"
        assert synthetic.workspace_id == "T_ACME"
        assert synthetic.user_id == "U_JOSH"

    def test_metadata_event_type(self) -> None:
        parsed = parse_interactivity_payload(_sample_payload())
        assert parsed is not None
        synthetic = build_codebase_pick_synthetic_message(parsed)
        assert synthetic.metadata["event_type"] == "codebase_pick"
        # No bot_id so bot-policy filtering doesn't trigger
        assert synthetic.metadata.get("bot_id") is None

    def test_empty_action_value_falls_back(self) -> None:
        payload = _sample_payload()
        payload["actions"][0]["value"] = ""
        parsed = parse_interactivity_payload(payload)
        assert parsed is not None
        synthetic = build_codebase_pick_synthetic_message(parsed)
        # Falls back to a generic phrase so the agent gets a coherent turn
        assert "selected codebase" in synthetic.text


# ---------------------------------------------------------------------------
# post_response_url_update — mock urllib
# ---------------------------------------------------------------------------

class TestPostResponseUrlUpdate:
    def test_empty_url_is_noop(self) -> None:
        """Shouldn't raise, shouldn't touch the network."""
        post_response_url_update("", "acme/platform")

    def test_swallows_http_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Any error from urllib must be caught and logged — this is
        polish, never load-bearing."""
        def raise_error(*args: Any, **kwargs: Any) -> Any:
            raise ConnectionError("network down")

        monkeypatch.setattr("urllib.request.urlopen", raise_error)
        post_response_url_update(
            "https://hooks.slack.com/actions/x", "acme/platform"
        )  # no exception

    def test_posts_replace_original_body(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify the body shape Slack expects."""
        captured: dict[str, Any] = {}

        class _Resp:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *args): return None

        def fake_urlopen(req: Any, timeout: int | None = None) -> Any:
            captured["url"] = req.full_url
            captured["body"] = req.data
            return _Resp()

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        post_response_url_update(
            "https://hooks.slack.com/actions/x", "acme/platform"
        )
        assert captured["url"] == "https://hooks.slack.com/actions/x"
        body = json.loads(captured["body"])
        assert body["replace_original"] is True
        assert "acme/platform" in body["text"]


# ---------------------------------------------------------------------------
# End-to-end: POST /slack/interactions via TestClient
# ---------------------------------------------------------------------------

@pytest.fixture
def client() -> TestClient:
    from bridge.main import app
    return TestClient(app)


@pytest.fixture
def capture_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> list[dict[str, Any]]:
    """Replace dispatch_async with a capture-only stub so we can assert
    what the route ends up dispatching to the agent.

    We capture POSITIONAL args only, which matches how main.py calls
    ``background.add_task(dispatch_async, slack, inbound, client, tenant_id)``.
    """
    captured: list[dict[str, Any]] = []

    async def fake_dispatch(
        adapter: Any,
        inbound: InboundMessage,
        client_arg: Any,
        tenant_id: str,
    ) -> None:
        captured.append(
            {
                "tenant_id": tenant_id,
                "inbound": inbound,
                "adapter_name": getattr(adapter, "name", None),
            }
        )

    monkeypatch.setattr("bridge.main.dispatch_async", fake_dispatch)
    return captured


@pytest.fixture
def stub_tenant_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Map team_id T_ACME → tenant_id acme; unknown teams raise KeyError."""
    def fake_resolve(workspace_id: str) -> str:
        if workspace_id == "T_ACME":
            return "acme"
        raise KeyError(workspace_id)

    monkeypatch.setattr("bridge.main.resolve_tenant_id", fake_resolve)


@pytest.fixture
def stub_response_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Swallow response_url calls so the test doesn't hit Slack."""
    monkeypatch.setattr(
        "bridge.main.post_response_url_update",
        lambda url, repo: None,
    )


def _post_interactions(
    client: TestClient, payload: dict[str, Any]
) -> Any:
    form_body = urllib.parse.urlencode({"payload": json.dumps(payload)})
    return client.post(
        "/slack/interactions",
        content=form_body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )


class TestInteractionsRoute:
    def test_happy_path_dispatches_synthetic_message(
        self,
        client: TestClient,
        capture_dispatch: list[dict[str, Any]],
        stub_tenant_resolver: None,
        stub_response_url: None,
    ) -> None:
        response = _post_interactions(
            client, _sample_payload(action_value="acme/billing")
        )
        assert response.status_code == 200
        assert response.json() == {"ok": True}

        assert len(capture_dispatch) == 1
        call = capture_dispatch[0]
        assert call["tenant_id"] == "acme"
        assert call["adapter_name"] == "slack"
        inbound = call["inbound"]
        assert inbound.workspace_id == "T_ACME"
        assert inbound.channel_id == "C_ONCALL"
        assert inbound.thread_id == "1712345670.100000"
        assert "acme/billing" in inbound.text
        assert "going forward" in inbound.text
        assert inbound.metadata["event_type"] == "codebase_pick"

    def test_unknown_tenant_drops_without_dispatch(
        self,
        client: TestClient,
        capture_dispatch: list[dict[str, Any]],
        stub_tenant_resolver: None,
        stub_response_url: None,
    ) -> None:
        payload = _sample_payload()
        payload["team"] = {"id": "T_UNKNOWN"}
        response = _post_interactions(client, payload)
        assert response.status_code == 200
        assert response.json() == {"ok": True}
        assert capture_dispatch == []

    def test_non_codebase_action_does_not_dispatch(
        self,
        client: TestClient,
        capture_dispatch: list[dict[str, Any]],
        stub_tenant_resolver: None,
        stub_response_url: None,
    ) -> None:
        payload = _sample_payload(action_id="escalate_approve:0")
        response = _post_interactions(client, payload)
        assert response.status_code == 200
        assert capture_dispatch == []

    def test_malformed_body_returns_200_and_no_dispatch(
        self,
        client: TestClient,
        capture_dispatch: list[dict[str, Any]],
    ) -> None:
        response = client.post(
            "/slack/interactions",
            content="not a form body",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert response.status_code == 200
        assert capture_dispatch == []

    def test_unsupported_payload_type_returns_200_and_no_dispatch(
        self,
        client: TestClient,
        capture_dispatch: list[dict[str, Any]],
        stub_tenant_resolver: None,
    ) -> None:
        payload = _sample_payload()
        payload["type"] = "view_submission"
        response = _post_interactions(client, payload)
        assert response.status_code == 200
        assert capture_dispatch == []
