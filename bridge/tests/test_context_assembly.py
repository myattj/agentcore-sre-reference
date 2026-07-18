"""Tests for the agent-side context assembly pipeline.

These tests exercise pure logic (permalink parsing, skill matching) that
doesn't require agent runtime dependencies. The functions are imported
from the agent code path by injecting the coreAgent directory onto
sys.path — same approach used by infra/data/scripts/seed_tenants.py.

For full integration tests (actual Slack API calls, agent entrypoint),
use the smoke-test flow described in CLAUDE.md.
"""
from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from pydantic_core import SchemaValidator, ValidationError as CoreValidationError

# Inject coreAgent onto sys.path so we can import tenant.py and slack_api.py
# WITHOUT the agent's full venv. These modules only need pydantic (which the
# bridge venv already has).
_AGENT_CODE = str(Path(__file__).resolve().parents[2] / "coreAgent" / "app" / "coreAgent")
if _AGENT_CODE not in sys.path:
    sys.path.insert(0, _AGENT_CODE)

# Now we can import the agent modules that have no heavy deps.
from slack_api import parse_permalink  # type: ignore[import-not-found]  # noqa: E402
from tenant import (  # type: ignore[import-not-found]  # noqa: E402
    MAX_SKILL_MATCH_TEXT_LENGTH,
    SkillDef,
    compile_skill_trigger,
)


# ---------------------------------------------------------------------------
# parse_permalink
# ---------------------------------------------------------------------------


class TestParsePermalink:
    def test_standard_permalink(self) -> None:
        result = parse_permalink(
            "https://acme.slack.com/archives/C04ABCDE123/p1712345678123456"
        )
        assert result == ("C04ABCDE123", "1712345678.123456")

    def test_reconstructs_dot_correctly(self) -> None:
        """The last 6 digits become the fractional part of the thread_ts."""
        result = parse_permalink(
            "https://team.slack.com/archives/C111/p1700000000000000"
        )
        assert result is not None
        assert result[1] == "1700000000.000000"

    def test_different_workspace_names(self) -> None:
        for workspace in ["acme", "my-team", "company123"]:
            url = f"https://{workspace}.slack.com/archives/C999/p1234567890123456"
            result = parse_permalink(url)
            assert result is not None
            assert result[0] == "C999"

    def test_invalid_url_returns_none(self) -> None:
        assert parse_permalink("https://example.com") is None
        assert parse_permalink("not a url") is None
        assert parse_permalink("") is None

    def test_wrong_slack_path_returns_none(self) -> None:
        # Missing /archives/ segment
        assert parse_permalink("https://acme.slack.com/messages/C111/p123") is None

    def test_short_timestamp_returns_none(self) -> None:
        """Timestamps with <= 6 digits can't be split into integer.fraction."""
        assert parse_permalink("https://a.slack.com/archives/C111/p123456") is None


# ---------------------------------------------------------------------------
# Skill matching (reimplemented here to test the pure logic without
# importing context_assembler.py which has relative imports)
# ---------------------------------------------------------------------------

# This replicates the core matching logic from context_assembler._match_skill
# to verify the algorithm works correctly.

_compiled_triggers: dict[str, SchemaValidator] = {}


def _match_skill(
    user_message: str,
    skills: list[SkillDef],
    ctx: dict[str, Any],
) -> dict[str, Any] | None:
    """Test-local reimplementation of context_assembler._match_skill."""
    text = user_message.strip()
    if not text or not skills:
        return None
    match_text = text[:MAX_SKILL_MATCH_TEXT_LENGTH]

    for skill in skills:
        trigger = skill.trigger
        if trigger.startswith("/"):
            if match_text.lower().startswith(trigger.lower()):
                return _build_match(skill, ctx)
        else:
            matcher = _compiled_triggers.get(trigger)
            if matcher is None:
                try:
                    matcher = compile_skill_trigger(trigger)
                except ValueError:
                    continue
                _compiled_triggers[trigger] = matcher
            try:
                matcher.validate_python(match_text)
            except CoreValidationError:
                continue
            else:
                return _build_match(skill, ctx)
    return None


def _build_match(skill: SkillDef, ctx: dict[str, Any]) -> dict[str, Any]:
    placeholders = defaultdict(
        str,
        {
            "user_id": ctx.get("user_id", ""),
            "channel_id": ctx.get("channel_id", ""),
            "thread_id": ctx.get("thread_id", ""),
            "workspace_id": ctx.get("workspace_id", ""),
        },
    )
    try:
        resolved = skill.prompt_template.format_map(placeholders)
    except (KeyError, ValueError):
        resolved = skill.prompt_template
    return {
        "name": skill.name,
        "prompt_addition": resolved,
        "required_tools": list(skill.required_tools),
    }


class TestSkillMatching:
    CTX = {"user_id": "U123", "channel_id": "C456", "thread_id": "T789"}

    SKILLS = [
        SkillDef(
            trigger="/oncall-start",
            name="oncall-briefing",
            prompt_template="Briefing for {user_id} in {channel_id}",
            required_tools=["search_team_history"],
        ),
        SkillDef(
            trigger="/check-known",
            name="known-issues",
            prompt_template="Check known issues",
        ),
        SkillDef(
            trigger=r"(?i)escalate\s+to\s+",
            name="escalation-assist",
            prompt_template="Help escalate",
            required_tools=["escalate"],
        ),
    ]

    def test_slash_command_exact_prefix(self) -> None:
        match = _match_skill("/oncall-start please", self.SKILLS, self.CTX)
        assert match is not None
        assert match["name"] == "oncall-briefing"
        assert match["required_tools"] == ["search_team_history"]

    def test_slash_command_case_insensitive(self) -> None:
        match = _match_skill("/ONCALL-START", self.SKILLS, self.CTX)
        assert match is not None
        assert match["name"] == "oncall-briefing"

    def test_slash_command_no_match(self) -> None:
        match = _match_skill("/unknown-command", self.SKILLS, self.CTX)
        assert match is None

    def test_regex_trigger(self) -> None:
        match = _match_skill("please escalate to sre team", self.SKILLS, self.CTX)
        assert match is not None
        assert match["name"] == "escalation-assist"

    def test_regex_case_insensitive(self) -> None:
        match = _match_skill("ESCALATE TO security", self.SKILLS, self.CTX)
        assert match is not None
        assert match["name"] == "escalation-assist"

    def test_no_match_returns_none(self) -> None:
        match = _match_skill("just a normal question", self.SKILLS, self.CTX)
        assert match is None

    def test_empty_message_returns_none(self) -> None:
        match = _match_skill("", self.SKILLS, self.CTX)
        assert match is None

    def test_empty_skills_returns_none(self) -> None:
        match = _match_skill("hello", [], self.CTX)
        assert match is None

    def test_first_match_wins(self) -> None:
        """When multiple skills could match, the first one in the list wins."""
        skills = [
            SkillDef(trigger=r"hello", name="first", prompt_template="A"),
            SkillDef(trigger=r"hello", name="second", prompt_template="B"),
        ]
        match = _match_skill("hello world", skills, self.CTX)
        assert match is not None
        assert match["name"] == "first"

    def test_placeholder_resolution(self) -> None:
        match = _match_skill("/oncall-start", self.SKILLS, self.CTX)
        assert match is not None
        assert "U123" in match["prompt_addition"]
        assert "C456" in match["prompt_addition"]

    def test_bad_regex_rejected_at_config_boundary(self) -> None:
        with pytest.raises(ValidationError, match="safe regex subset"):
            SkillDef(trigger="[invalid", name="bad", prompt_template="x")

    def test_unknown_placeholder_left_empty(self) -> None:
        """Placeholders not in ctx resolve to empty string, not KeyError."""
        skill = SkillDef(
            trigger="/test",
            name="test",
            prompt_template="User {user_id} custom {unknown_field}",
        )
        match = _match_skill("/test", [skill], self.CTX)
        assert match is not None
        assert "U123" in match["prompt_addition"]
        assert "{unknown_field}" not in match["prompt_addition"]


# ---------------------------------------------------------------------------
# Tenant config loads new fields
# ---------------------------------------------------------------------------


class TestTenantConfigNewFields:
    """Verify the agent-side TenantConfig Pydantic model accepts the new fields."""

    def test_default_config_has_new_fields(self) -> None:
        from tenant import build_default_config  # type: ignore[import-not-found]

        config = build_default_config("test")
        # New tenants default to humans-only.
        assert config.bot_policy.allow_all_bots is False
        assert config.bot_policy.trusted_bot_ids == []
        assert config.bot_policy.open_channels == []
        assert config.context_assembly.resolve_permalinks is True
        assert config.context_assembly.thread_history_depth == 25
        assert config.skills == []
        assert config.escalation.routes == []

    def test_config_with_skills_parses(self) -> None:
        from tenant import TenantConfig  # type: ignore[import-not-found]

        data = {
            "tenant_id": "test",
            "skills": [
                {
                    "trigger": "/briefing",
                    "name": "briefing",
                    "prompt_template": "Do briefing",
                    "required_tools": ["echo"],
                }
            ],
        }
        config = TenantConfig.model_validate(data)
        assert len(config.skills) == 1
        assert config.skills[0].name == "briefing"
        assert config.skills[0].required_tools == ["echo"]

    def test_config_with_escalation_parses(self) -> None:
        from tenant import TenantConfig  # type: ignore[import-not-found]

        data = {
            "tenant_id": "test",
            "escalation": {
                "routes": [
                    {
                        "team_name": "sre",
                        "channel_id": "C_SRE",
                        "description": "SRE team",
                        "contacts": ["U_ALICE"],
                    }
                ]
            },
        }
        config = TenantConfig.model_validate(data)
        assert len(config.escalation.routes) == 1
        assert config.escalation.routes[0].team_name == "sre"
        assert config.escalation.routes[0].contacts == ["U_ALICE"]

    def test_demo_config_loads(self) -> None:
        """The demo.json file should parse with all new fields."""
        from tenant import TenantConfig  # type: ignore[import-not-found]

        demo_path = Path(__file__).resolve().parents[2] / "examples" / "tenants" / "demo.json"
        import json
        data = json.loads(demo_path.read_text())
        config = TenantConfig.model_validate(data)
        assert len(config.skills) == 2
        assert len(config.escalation.routes) == 3
        assert config.context_assembly.resolve_permalinks is True
