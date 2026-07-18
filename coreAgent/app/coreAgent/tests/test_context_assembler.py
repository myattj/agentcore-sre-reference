"""Tests for context_assembler.py: skill matching, integration injection,
and the assemble_context pipeline."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from context_assembler import (
    _compiled_triggers,
    _build_integration_block,
    _match_skill,
    _resolve_permalinks,
    assemble_context,
)
from tenant import (
    MAX_SKILL_MATCH_TEXT_LENGTH,
    MAX_SKILL_TRIGGER_LENGTH,
    ByoConfig,
    ContextAssemblyConfig,
    SkillDef,
)


# ---------------------------------------------------------------------------
# _match_skill()
# ---------------------------------------------------------------------------

class TestMatchSkill:
    """Unit tests for the internal _match_skill function."""

    def _skill(self, **kwargs) -> SkillDef:
        defaults = {
            "trigger": r"(?i)test trigger",
            "name": "test-skill",
            "prompt_template": "Test prompt for {user_id} in {channel_id}",
            "required_tools": ["tool_a"],
            "channels": [],
        }
        defaults.update(kwargs)
        return SkillDef(**defaults)

    def test_regex_match(self, sample_ctx):
        skill = self._skill()
        result = _match_skill("this has a test trigger word", [skill], sample_ctx)
        assert result is not None
        assert result.name == "test-skill"
        assert "tool_a" in result.required_tools

    def test_no_match(self, sample_ctx):
        skill = self._skill()
        result = _match_skill("nothing here", [skill], sample_ctx)
        assert result is None

    def test_slash_command_match(self, sample_ctx):
        skill = self._skill(trigger="/deploy", name="slash-deploy")
        result = _match_skill("/deploy prod", [skill], sample_ctx)
        assert result is not None
        assert result.name == "slash-deploy"

    def test_slash_command_no_match(self, sample_ctx):
        skill = self._skill(trigger="/deploy", name="slash-deploy")
        result = _match_skill("let's deploy to prod", [skill], sample_ctx)
        assert result is None

    def test_first_match_wins(self, sample_ctx):
        skills = [
            self._skill(trigger=r"(?i)fire", name="first"),
            self._skill(trigger=r"(?i)fire", name="second"),
        ]
        result = _match_skill("alert fired", skills, sample_ctx)
        assert result.name == "first"

    def test_channel_whitelist_blocks(self, sample_ctx):
        skill = self._skill(channels=["C_OTHER"])
        result = _match_skill("test trigger", [skill], sample_ctx)
        assert result is None

    def test_channel_whitelist_allows(self, sample_ctx):
        skill = self._skill(channels=["C_TEST"])
        result = _match_skill("test trigger", [skill], sample_ctx)
        assert result is not None

    def test_empty_message(self, sample_ctx):
        skill = self._skill()
        assert _match_skill("", [skill], sample_ctx) is None
        assert _match_skill("  ", [skill], sample_ctx) is None

    def test_empty_skills(self, sample_ctx):
        assert _match_skill("test trigger", [], sample_ctx) is None

    def test_placeholder_resolution(self, sample_ctx):
        skill = self._skill(
            prompt_template="User {user_id} in {channel_id}, thread {thread_id}"
        )
        result = _match_skill("test trigger", [skill], sample_ctx)
        assert "U_TEST" in result.prompt_addition
        assert "C_TEST" in result.prompt_addition
        assert "1712345678.123456" in result.prompt_addition

    def test_catastrophic_nested_repeat_is_rejected(self):
        with pytest.raises(ValidationError, match="must not repeat a group"):
            self._skill(trigger=r"(a+)+$")

    def test_oversized_trigger_is_rejected(self):
        with pytest.raises(ValidationError, match="at most 512 characters"):
            self._skill(trigger="a" * (MAX_SKILL_TRIGGER_LENGTH + 1))

    def test_match_text_is_bounded(self, sample_ctx, monkeypatch):
        observed: list[str] = []

        class RecordingMatcher:
            def validate_python(self, value: str) -> str:
                observed.append(value)
                return value

        skill = self._skill(trigger="bounded-input-trigger")
        monkeypatch.setitem(
            _compiled_triggers,
            skill.trigger,
            RecordingMatcher(),
        )

        result = _match_skill(
            "bounded-input-trigger" + ("x" * 100_000),
            [skill],
            sample_ctx,
        )

        assert result is not None
        assert len(observed) == 1
        assert len(observed[0]) == MAX_SKILL_MATCH_TEXT_LENGTH


# ---------------------------------------------------------------------------
# _build_integration_block()
# ---------------------------------------------------------------------------

class TestBuildIntegrationBlock:
    """Tests for the integration injection prompt builder."""

    def test_none_byo(self):
        assert _build_integration_block(None) == ""

    def test_byo_disabled(self):
        byo = ByoConfig(enabled=False, connected_integrations=["datadog"])
        assert _build_integration_block(byo) == ""

    def test_no_integrations(self):
        byo = ByoConfig(enabled=True, connected_integrations=[])
        assert _build_integration_block(byo) == ""

    def test_datadog_connected(self):
        byo = ByoConfig(enabled=True, connected_integrations=["datadog"])
        block = _build_integration_block(byo)
        assert "Datadog" in block
        assert "`query_metrics`" in block
        assert "`get_recent_alerts`" in block
        assert "`search_logs`" in block
        assert "Connected Monitoring Integrations" in block

    def test_pagerduty_connected(self):
        byo = ByoConfig(enabled=True, connected_integrations=["pagerduty"])
        block = _build_integration_block(byo)
        assert "PagerDuty" in block
        assert "`list_incidents`" in block

    def test_multiple_integrations(self):
        byo = ByoConfig(
            enabled=True,
            connected_integrations=["datadog", "pagerduty"],
        )
        block = _build_integration_block(byo)
        assert "Datadog" in block
        assert "PagerDuty" in block

    def test_unknown_integration_ignored(self):
        byo = ByoConfig(
            enabled=True,
            connected_integrations=["unknown_service"],
        )
        # Unknown integrations are silently skipped
        assert _build_integration_block(byo) == ""

    def test_mixed_known_unknown(self):
        byo = ByoConfig(
            enabled=True,
            connected_integrations=["datadog", "confluence"],
        )
        block = _build_integration_block(byo)
        # Datadog is known; Confluence is not in the monitoring map
        assert "Datadog" in block
        assert "Confluence" not in block


# ---------------------------------------------------------------------------
# _resolve_permalinks() authorization
# ---------------------------------------------------------------------------


class TestResolvePermalinksAuthorization:
    def _config(self) -> ContextAssemblyConfig:
        return ContextAssemblyConfig(max_permalinks=3)

    def test_current_channel_allowed_without_requester_user(
        self, monkeypatch
    ) -> None:
        import context_assembler

        fetched: list[tuple[str, str]] = []
        monkeypatch.setattr(
            context_assembler.slack_api,
            "get_bot_token",
            lambda _tenant: "xoxb-test",
        )
        monkeypatch.setattr(
            context_assembler.slack_api,
            "parse_permalink",
            lambda _url: ("C_CURRENT", "1.2"),
        )
        monkeypatch.setattr(
            context_assembler.slack_api,
            "is_user_member_of_channel",
            lambda *_args: pytest.fail("current channel needs no membership lookup"),
        )

        def fetch(_token: str, channel: str, thread: str) -> str:
            fetched.append((channel, thread))
            return "Thread (1 messages):\n\n**U1**: current"

        monkeypatch.setattr(
            context_assembler.slack_api,
            "fetch_thread_replies",
            fetch,
        )

        result = _resolve_permalinks(
            {
                "channel_id": "C_CURRENT",
                "permalinks": ["https://workspace.slack.com/archives/C_CURRENT/p1"],
            },
            self._config(),
            "tenant-a",
        )

        assert fetched == [("C_CURRENT", "1.2")]
        assert "Referenced Thread" in result

    @pytest.mark.parametrize("membership", [False, None])
    def test_cross_channel_denied_without_positive_membership(
        self, monkeypatch, membership: bool | None
    ) -> None:
        import context_assembler

        monkeypatch.setattr(
            context_assembler.slack_api,
            "get_bot_token",
            lambda _tenant: "xoxb-test",
        )
        monkeypatch.setattr(
            context_assembler.slack_api,
            "parse_permalink",
            lambda _url: ("C_TARGET", "1.2"),
        )
        monkeypatch.setattr(
            context_assembler.slack_api,
            "is_user_member_of_channel",
            lambda *_args: membership,
        )
        monkeypatch.setattr(
            context_assembler.slack_api,
            "fetch_thread_replies",
            lambda *_args: pytest.fail("unauthorized permalink must not be fetched"),
        )

        result = _resolve_permalinks(
            {
                "channel_id": "C_CURRENT",
                "user_id": "U_REQUESTER",
                "permalinks": ["https://workspace.slack.com/archives/C_TARGET/p1"],
            },
            self._config(),
            "tenant-a",
        )

        assert result == ""

    def test_cross_channel_denied_when_requester_is_missing(
        self, monkeypatch
    ) -> None:
        import context_assembler

        monkeypatch.setattr(
            context_assembler.slack_api,
            "get_bot_token",
            lambda _tenant: "xoxb-test",
        )
        monkeypatch.setattr(
            context_assembler.slack_api,
            "parse_permalink",
            lambda _url: ("C_TARGET", "1.2"),
        )
        monkeypatch.setattr(
            context_assembler.slack_api,
            "is_user_member_of_channel",
            lambda *_args: pytest.fail("missing requester must skip membership lookup"),
        )
        monkeypatch.setattr(
            context_assembler.slack_api,
            "fetch_thread_replies",
            lambda *_args: pytest.fail("unauthorized permalink must not be fetched"),
        )

        result = _resolve_permalinks(
            {
                "channel_id": "C_CURRENT",
                "permalinks": ["https://workspace.slack.com/archives/C_TARGET/p1"],
            },
            self._config(),
            "tenant-a",
        )

        assert result == ""

    def test_cross_channel_membership_error_fails_closed(self, monkeypatch) -> None:
        import context_assembler

        monkeypatch.setattr(
            context_assembler.slack_api,
            "get_bot_token",
            lambda _tenant: "xoxb-test",
        )
        monkeypatch.setattr(
            context_assembler.slack_api,
            "parse_permalink",
            lambda _url: ("C_TARGET", "1.2"),
        )

        def fail_membership(*_args: str) -> bool:
            raise OSError("Slack unavailable")

        monkeypatch.setattr(
            context_assembler.slack_api,
            "is_user_member_of_channel",
            fail_membership,
        )
        monkeypatch.setattr(
            context_assembler.slack_api,
            "fetch_thread_replies",
            lambda *_args: pytest.fail("unauthorized permalink must not be fetched"),
        )

        result = _resolve_permalinks(
            {
                "channel_id": "C_CURRENT",
                "user_id": "U_REQUESTER",
                "permalinks": ["https://workspace.slack.com/archives/C_TARGET/p1"],
            },
            self._config(),
            "tenant-a",
        )

        assert result == ""

    def test_cross_channel_allowed_after_positive_membership(
        self, monkeypatch
    ) -> None:
        import context_assembler

        checks: list[tuple[str, str, str]] = []
        fetched: list[tuple[str, str]] = []
        monkeypatch.setattr(
            context_assembler.slack_api,
            "get_bot_token",
            lambda _tenant: "xoxb-test",
        )
        monkeypatch.setattr(
            context_assembler.slack_api,
            "parse_permalink",
            lambda _url: ("C_TARGET", "1.2"),
        )

        def member(token: str, channel: str, user: str) -> bool:
            checks.append((token, channel, user))
            return True

        def fetch(_token: str, channel: str, thread: str) -> str:
            fetched.append((channel, thread))
            return "Thread (1 messages):\n\n**U1**: authorized"

        monkeypatch.setattr(
            context_assembler.slack_api,
            "is_user_member_of_channel",
            member,
        )
        monkeypatch.setattr(
            context_assembler.slack_api,
            "fetch_thread_replies",
            fetch,
        )

        result = _resolve_permalinks(
            {
                "channel_id": "C_CURRENT",
                "user_id": "U_REQUESTER",
                "permalinks": ["https://workspace.slack.com/archives/C_TARGET/p1"],
            },
            self._config(),
            "tenant-a",
        )

        assert checks == [("xoxb-test", "C_TARGET", "U_REQUESTER")]
        assert fetched == [("C_TARGET", "1.2")]
        assert "authorized" in result


# ---------------------------------------------------------------------------
# assemble_context() pipeline
# ---------------------------------------------------------------------------

class TestAssembleContext:
    """Integration tests for the full assembly pipeline."""

    def _default_config(self) -> ContextAssemblyConfig:
        return ContextAssemblyConfig(
            resolve_permalinks=False,
            inject_thread_history=False,
        )

    def test_no_skill_match(self, sample_ctx):
        """When no skill matches, returns the original message and prompt."""
        result = assemble_context(
            user_message="hello world",
            ctx=sample_ctx,
            assembly_config=self._default_config(),
            skills=[],
            effective_prompt="base prompt",
            tenant_id="test-tenant",
        )
        assert result.enriched_message == "hello world"
        assert result.system_prompt == "base prompt"
        assert result.extra_tools == []
        assert result.matched_skill is None

    def test_skill_match_modifies_prompt(self, sample_ctx):
        skill = SkillDef(
            trigger=r"(?i)incident",
            name="test-incident",
            prompt_template="Handle this incident for {user_id}.",
            required_tools=["escalate"],
        )
        result = assemble_context(
            user_message="there's an incident in payments",
            ctx=sample_ctx,
            assembly_config=self._default_config(),
            skills=[skill],
            effective_prompt="base prompt",
            tenant_id="test-tenant",
        )
        assert result.matched_skill == "test-incident"
        assert "Handle this incident for U_TEST" in result.system_prompt
        assert "escalate" in result.extra_tools

    def test_integration_injection_with_datadog(self, sample_ctx):
        byo = ByoConfig(enabled=True, connected_integrations=["datadog"])
        result = assemble_context(
            user_message="alert fired",
            ctx=sample_ctx,
            assembly_config=self._default_config(),
            skills=[],
            effective_prompt="base prompt",
            tenant_id="test-tenant",
            byo=byo,
        )
        assert "Connected Monitoring Integrations" in result.system_prompt
        assert "Datadog" in result.system_prompt
        assert "`query_metrics`" in result.system_prompt

    def test_integration_injection_skipped_when_byo_disabled(self, sample_ctx):
        byo = ByoConfig(enabled=False, connected_integrations=["datadog"])
        result = assemble_context(
            user_message="alert fired",
            ctx=sample_ctx,
            assembly_config=self._default_config(),
            skills=[],
            effective_prompt="base prompt",
            tenant_id="test-tenant",
            byo=byo,
        )
        assert "Connected Monitoring Integrations" not in result.system_prompt

    def test_integration_injection_without_byo(self, sample_ctx):
        """Backward compat: byo=None doesn't crash."""
        result = assemble_context(
            user_message="hello",
            ctx=sample_ctx,
            assembly_config=self._default_config(),
            skills=[],
            effective_prompt="base prompt",
            tenant_id="test-tenant",
            byo=None,
        )
        assert "Connected Monitoring Integrations" not in result.system_prompt

    def test_skill_match_plus_integration(self, sample_ctx):
        """Both skill match and integration injection can coexist."""
        skill = SkillDef(
            trigger=r"(?i)alert fired",
            name="incident-response",
            prompt_template="Triage this alert.",
            required_tools=["escalate"],
        )
        byo = ByoConfig(enabled=True, connected_integrations=["datadog"])
        result = assemble_context(
            user_message="alert fired for high CPU",
            ctx=sample_ctx,
            assembly_config=self._default_config(),
            skills=[skill],
            effective_prompt="base prompt",
            tenant_id="test-tenant",
            byo=byo,
        )
        assert result.matched_skill == "incident-response"
        assert "Triage this alert" in result.system_prompt
        assert "Datadog" in result.system_prompt
        assert "escalate" in result.extra_tools
