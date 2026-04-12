"""Tests for context_assembler.py: skill matching, integration injection,
and the assemble_context pipeline."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from context_assembler import (
    AssembledContext,
    SkillMatch,
    _build_integration_block,
    _match_skill,
    assemble_context,
)
from tenant import ByoConfig, ContextAssemblyConfig, SkillDef


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
