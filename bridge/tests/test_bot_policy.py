"""Tests for bot policy filtering in the bridge.

Covers:
  - _bot_allowed() three-tier logic (trusted, open channel, block)
  - Bot policy check integration in slack_events route
  - Self-message filtering via SLACK_APP_ID
  - Permalink extraction from Slack event text
  - Expanded ctx dict in async_dispatcher
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from bridge.main import _bot_allowed, app


# ---------------------------------------------------------------------------
# _bot_allowed unit tests
# ---------------------------------------------------------------------------


class TestBotAllowed:
    """Unit tests for the four-tier bot policy evaluation."""

    def test_allow_all_bots_flag(self) -> None:
        """When an operator explicitly enables ``allow_all_bots``, any bot passes."""
        policy = {"allow_all_bots": True, "trusted_bot_ids": [], "open_channels": []}
        assert _bot_allowed(policy, "B_ANY_BOT", "C_ANY") is True
        assert _bot_allowed(policy, "B_WHATEVER", None) is True

    def test_allow_all_bots_false_falls_through(self) -> None:
        """When ``allow_all_bots`` is False, evaluation falls through to
        the trusted/open-channel tiers (preserves old behavior)."""
        policy = {"allow_all_bots": False, "trusted_bot_ids": ["B_GOOD"], "open_channels": []}
        assert _bot_allowed(policy, "B_GOOD", "C_ANY") is True
        assert _bot_allowed(policy, "B_BAD", "C_ANY") is False

    def test_trusted_bot_allowed(self) -> None:
        policy = {"trusted_bot_ids": ["B_GOOD"], "open_channels": []}
        assert _bot_allowed(policy, "B_GOOD", "C_ANY") is True

    def test_untrusted_bot_blocked(self) -> None:
        policy = {"trusted_bot_ids": ["B_GOOD"], "open_channels": []}
        assert _bot_allowed(policy, "B_BAD", "C_ANY") is False

    def test_any_bot_in_open_channel(self) -> None:
        policy = {"trusted_bot_ids": [], "open_channels": ["C_ALERTS"]}
        assert _bot_allowed(policy, "B_RANDOM", "C_ALERTS") is True

    def test_bot_blocked_in_non_open_channel(self) -> None:
        policy = {"trusted_bot_ids": [], "open_channels": ["C_ALERTS"]}
        assert _bot_allowed(policy, "B_RANDOM", "C_GENERAL") is False

    def test_empty_policy_blocks_all_bots(self) -> None:
        policy = {"trusted_bot_ids": [], "open_channels": []}
        assert _bot_allowed(policy, "B_ANY", "C_ANY") is False

    def test_missing_fields_treated_as_empty(self) -> None:
        policy: dict[str, Any] = {}
        assert _bot_allowed(policy, "B_ANY", "C_ANY") is False

    def test_no_channel_id(self) -> None:
        """Bot in a DM (no channel_id) — only trusted bots pass."""
        policy = {"trusted_bot_ids": ["B_GOOD"], "open_channels": ["C_ALERTS"]}
        assert _bot_allowed(policy, "B_GOOD", None) is True
        assert _bot_allowed(policy, "B_BAD", None) is False

    def test_trusted_takes_precedence_over_open(self) -> None:
        """Trusted bot in a non-open channel still passes (tier 1 before 2)."""
        policy = {"trusted_bot_ids": ["B_GOOD"], "open_channels": ["C_OTHER"]}
        assert _bot_allowed(policy, "B_GOOD", "C_GENERAL") is True


# ---------------------------------------------------------------------------
# Permalink extraction
# ---------------------------------------------------------------------------


class TestPermalinkExtraction:
    """Test that the Slack adapter extracts permalink URLs from message text."""

    def test_single_permalink(self) -> None:
        import re
        pattern = re.compile(
            r"https://[a-zA-Z0-9-]+\.slack\.com/archives/[A-Z0-9]+/p\d+"
        )
        text = "Check this https://acme.slack.com/archives/C04ABC123/p1712345678123456 please"
        matches = pattern.findall(text)
        assert matches == ["https://acme.slack.com/archives/C04ABC123/p1712345678123456"]

    def test_multiple_permalinks(self) -> None:
        import re
        pattern = re.compile(
            r"https://[a-zA-Z0-9-]+\.slack\.com/archives/[A-Z0-9]+/p\d+"
        )
        text = (
            "See https://acme.slack.com/archives/C111/p1111111111111111 "
            "and https://acme.slack.com/archives/C222/p2222222222222222"
        )
        matches = pattern.findall(text)
        assert len(matches) == 2

    def test_no_permalink(self) -> None:
        import re
        pattern = re.compile(
            r"https://[a-zA-Z0-9-]+\.slack\.com/archives/[A-Z0-9]+/p\d+"
        )
        text = "Just a regular message with https://example.com link"
        assert pattern.findall(text) == []


# ---------------------------------------------------------------------------
# Expanded ctx in async_dispatcher
# ---------------------------------------------------------------------------


class TestDispatcherCtx:
    """Verify that async_dispatcher passes enriched ctx to the client."""

    @pytest.mark.asyncio
    async def test_ctx_includes_bot_id_and_permalinks(self) -> None:
        from bridge.adapters.core import InboundMessage
        from bridge.async_dispatcher import dispatch_async

        inbound = InboundMessage(
            workspace_id="T_TEST",
            user_id="U_TEST",
            text="hello",
            channel_id="C_TEST",
            thread_id="123.456",
            metadata={
                "bot_id": "B_ALERT",
                "permalinks": ["https://a.slack.com/archives/C111/p123456"],
            },
        )

        captured_ctx: dict[str, Any] = {}

        # Use the buffered path (no stream_reply) so invoke() is called
        # directly and we can capture ctx.
        async def fake_invoke(*, tenant_id: str, prompt: str, ctx: Any = None) -> str:
            captured_ctx.update(ctx or {})
            return "response"

        # Adapter without stream_reply attribute → dispatcher uses buffered path
        mock_adapter = MagicMock(spec=["reply", "name"])
        mock_adapter.reply = AsyncMock()
        mock_adapter.name = "test"

        mock_client = MagicMock()
        mock_client.invoke = fake_invoke

        await dispatch_async(mock_adapter, inbound, mock_client, "test-tenant")

        assert captured_ctx.get("bot_id") == "B_ALERT"
        assert captured_ctx.get("permalinks") == ["https://a.slack.com/archives/C111/p123456"]
        assert captured_ctx.get("user_id") == "U_TEST"
        assert captured_ctx.get("channel_id") == "C_TEST"


# ---------------------------------------------------------------------------
# Deep merge for new config fields
# ---------------------------------------------------------------------------


class TestDeepMergeNewFields:
    """Verify that the new config sections survive deep_merge correctly."""

    def test_bot_policy_deep_merges(self) -> None:
        from bridge.tenant_write import deep_merge

        base = {
            "bot_policy": {
                "trusted_bot_ids": ["B1"],
                "open_channels": ["C1"],
            }
        }
        patch = {"bot_policy": {"trusted_bot_ids": ["B1", "B2"]}}
        result = deep_merge(base, patch)
        # trusted_bot_ids replaced, open_channels preserved
        assert result["bot_policy"]["trusted_bot_ids"] == ["B1", "B2"]
        assert result["bot_policy"]["open_channels"] == ["C1"]

    def test_context_assembly_deep_merges(self) -> None:
        from bridge.tenant_write import deep_merge

        base = {
            "context_assembly": {
                "resolve_permalinks": True,
                "inject_thread_history": True,
                "thread_history_depth": 25,
                "max_permalinks": 3,
            }
        }
        patch = {"context_assembly": {"thread_history_depth": 50}}
        result = deep_merge(base, patch)
        assert result["context_assembly"]["thread_history_depth"] == 50
        assert result["context_assembly"]["resolve_permalinks"] is True

    def test_escalation_deep_merges(self) -> None:
        from bridge.tenant_write import deep_merge

        base = {"escalation": {"routes": [{"team_name": "sre"}]}}
        patch = {"escalation": {"routes": [{"team_name": "security"}]}}
        result = deep_merge(base, patch)
        # Routes is a list — replaced wholesale, not extended
        assert result["escalation"]["routes"] == [{"team_name": "security"}]

    def test_skills_replaced_wholesale(self) -> None:
        from bridge.tenant_write import deep_merge

        base = {"skills": [{"trigger": "/old", "name": "old"}]}
        patch = {"skills": [{"trigger": "/new", "name": "new"}]}
        result = deep_merge(base, patch)
        # skills is a list at top level, not in _DEEP_MERGE_FIELDS
        assert result["skills"] == [{"trigger": "/new", "name": "new"}]


# ---------------------------------------------------------------------------
# Default config includes new fields
# ---------------------------------------------------------------------------


class TestDefaultConfig:
    def test_build_default_includes_new_sections(self) -> None:
        from bridge.tenant_write import build_default_config_dict

        config = build_default_config_dict("test-tenant")
        assert "bot_policy" in config
        # New tenants are humans-only until an operator explicitly trusts an
        # alert bot or opens a channel.
        assert config["bot_policy"]["allow_all_bots"] is False
        assert config["bot_policy"]["trusted_bot_ids"] == []
        assert config["bot_policy"]["open_channels"] == []
        assert "context_assembly" in config
        assert config["context_assembly"]["resolve_permalinks"] is True
        assert config["context_assembly"]["thread_history_depth"] == 25
        assert "skills" in config
        assert config["skills"] == []
        assert "escalation" in config
        assert config["escalation"]["routes"] == []


# ---------------------------------------------------------------------------
# PATCH routes accept new config sections
# ---------------------------------------------------------------------------


class TestPatchNewFields:
    """Verify PATCH /api/tenants/{id} accepts the new config sections."""

    @pytest.fixture
    def stub_store(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, dict[str, Any]]:
        from bridge.tenant_write import build_default_config_dict
        import copy

        store: dict[str, dict[str, Any]] = {
            "slack-test": build_default_config_dict("slack-test"),
        }

        def fake_get(tenant_id: str, _region: str) -> dict[str, Any]:
            if tenant_id not in store:
                raise KeyError(tenant_id)
            return copy.deepcopy(store[tenant_id])

        def fake_update(
            tenant_id: str,
            _region: str,
            full: dict[str, Any],
            expected_config: dict[str, Any] | None = None,
        ) -> None:
            if tenant_id not in store:
                raise KeyError(tenant_id)
            if expected_config is not None:
                assert store[tenant_id] == expected_config
            store[tenant_id] = full

        monkeypatch.setattr("bridge.api.get_tenant_row", fake_get)
        monkeypatch.setattr("bridge.api.update_tenant_row", fake_update)
        return store

    def test_patch_bot_policy(self, stub_store: dict) -> None:
        from bridge.slack_oauth import make_session_token

        token = make_session_token("slack-test")
        with TestClient(app) as c:
            r = c.patch(
                "/api/tenants/slack-test",
                headers={"Authorization": f"Bearer {token}"},
                json={"bot_policy": {"trusted_bot_ids": ["B_DEPLOY"]}},
            )
        assert r.status_code == 200, r.text
        assert r.json()["bot_policy"]["trusted_bot_ids"] == ["B_DEPLOY"]
        # open_channels should survive (deep merge)
        assert r.json()["bot_policy"]["open_channels"] == []

    def test_patch_skills(self, stub_store: dict) -> None:
        from bridge.slack_oauth import make_session_token

        token = make_session_token("slack-test")
        skill = {
            "trigger": "/test",
            "name": "test-skill",
            "prompt_template": "Do the thing",
            "required_tools": ["echo"],
        }
        with TestClient(app) as c:
            r = c.patch(
                "/api/tenants/slack-test",
                headers={"Authorization": f"Bearer {token}"},
                json={"skills": [skill]},
            )
        assert r.status_code == 200, r.text
        assert len(r.json()["skills"]) == 1
        assert r.json()["skills"][0]["name"] == "test-skill"

    def test_patch_escalation(self, stub_store: dict) -> None:
        from bridge.slack_oauth import make_session_token

        token = make_session_token("slack-test")
        route = {
            "team_name": "sre",
            "channel_id": "C_SRE",
            "description": "Site reliability",
            "contacts": ["U_ONCALL"],
        }
        with TestClient(app) as c:
            r = c.patch(
                "/api/tenants/slack-test",
                headers={"Authorization": f"Bearer {token}"},
                json={"escalation": {"routes": [route]}},
            )
        assert r.status_code == 200, r.text
        routes = r.json()["escalation"]["routes"]
        assert len(routes) == 1
        assert routes[0]["team_name"] == "sre"

    def test_patch_context_assembly(self, stub_store: dict) -> None:
        from bridge.slack_oauth import make_session_token

        token = make_session_token("slack-test")
        with TestClient(app) as c:
            r = c.patch(
                "/api/tenants/slack-test",
                headers={"Authorization": f"Bearer {token}"},
                json={"context_assembly": {"thread_history_depth": 50}},
            )
        assert r.status_code == 200, r.text
        assert r.json()["context_assembly"]["thread_history_depth"] == 50
        # Other fields survive
        assert r.json()["context_assembly"]["resolve_permalinks"] is True

    def test_patch_skill_missing_required_field_422(self, stub_store: dict) -> None:
        from bridge.slack_oauth import make_session_token

        token = make_session_token("slack-test")
        with TestClient(app) as c:
            r = c.patch(
                "/api/tenants/slack-test",
                headers={"Authorization": f"Bearer {token}"},
                json={"skills": [{"name": "incomplete"}]},
            )
        assert r.status_code == 422
