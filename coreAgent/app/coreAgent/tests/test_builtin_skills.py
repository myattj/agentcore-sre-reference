"""Tests for builtin_skills.py: merge logic and trigger matching."""
from __future__ import annotations

import re

from builtin_skills import BUILTIN_SKILLS, merge_skills
from tenant import SkillDef


# ---------------------------------------------------------------------------
# merge_skills()
# ---------------------------------------------------------------------------

class TestMergeSkills:
    """Unit tests for the merge_skills() function."""

    def test_builtins_only(self):
        """No tenant skills -> returns all builtins unchanged."""
        result = merge_skills([])
        assert result == BUILTIN_SKILLS

    def test_tenant_override_by_name(self):
        """A tenant skill with the same name replaces the built-in."""
        custom = SkillDef(
            trigger=r"(?i)custom trigger",
            name="incident-response",
            prompt_template="Custom incident prompt",
            required_tools=["custom_tool"],
        )
        result = merge_skills([custom])
        names = [s.name for s in result]
        # The custom one should appear first (tenant skills come first)
        assert names[0] == "incident-response"
        assert result[0].prompt_template == "Custom incident prompt"
        # The built-in incident-response should be gone
        assert names.count("incident-response") == 1

    def test_ordering_preserved(self):
        """Tenant skills come first, then un-overridden builtins in order."""
        custom = SkillDef(
            trigger=r"(?i)my skill",
            name="my-custom-skill",
            prompt_template="Custom prompt",
        )
        result = merge_skills([custom])
        assert result[0].name == "my-custom-skill"
        # All builtins follow
        builtin_names = [s.name for s in BUILTIN_SKILLS]
        remaining = [s.name for s in result[1:]]
        assert remaining == builtin_names

    def test_empty_tenant_skills_empty_builtins(self):
        """Both empty -> returns empty list."""
        assert merge_skills([], builtins=[]) == []

    def test_multiple_overrides(self):
        """Overriding multiple built-ins removes all of them."""
        overrides = [
            SkillDef(
                trigger="(?i)pm",
                name="postmortem-drafter",
                prompt_template="Custom PM",
            ),
            SkillDef(
                trigger="(?i)deploy",
                name="deploy-watchdog",
                prompt_template="Custom deploy",
            ),
        ]
        result = merge_skills(overrides)
        names = [s.name for s in result]
        assert names.count("postmortem-drafter") == 1
        assert names.count("deploy-watchdog") == 1
        # Overrides come first
        assert names[0] == "postmortem-drafter"
        assert names[1] == "deploy-watchdog"

    def test_custom_builtins_param(self):
        """Passing a custom builtins list replaces the default."""
        custom_builtins = [
            SkillDef(trigger="(?i)a", name="alpha", prompt_template="A"),
            SkillDef(trigger="(?i)b", name="beta", prompt_template="B"),
        ]
        result = merge_skills([], builtins=custom_builtins)
        assert [s.name for s in result] == ["alpha", "beta"]


# ---------------------------------------------------------------------------
# Skill trigger regex matching
# ---------------------------------------------------------------------------

class TestSkillTriggers:
    """Verify each built-in skill triggers on expected inputs and doesn't
    false-positive on unrelated messages."""

    def _matches(self, skill_name: str, text: str) -> bool:
        """Check if a built-in skill's trigger matches the given text."""
        skill = next(s for s in BUILTIN_SKILLS if s.name == skill_name)
        return bool(re.search(skill.trigger, text, re.IGNORECASE))

    # -- postmortem-drafter --
    def test_postmortem_triggers(self):
        assert self._matches("postmortem-drafter", "can you draft a postmortem?")
        assert self._matches("postmortem-drafter", "write up the retro for yesterday")
        assert self._matches("postmortem-drafter", "incident retrospective needed")
        assert self._matches("postmortem-drafter", "blameless review time")

    def test_postmortem_no_false_positive(self):
        assert not self._matches("postmortem-drafter", "the alert fired at 2pm")
        assert not self._matches("postmortem-drafter", "deploy to staging")

    # -- status-update-drafter --
    def test_status_update_triggers(self):
        assert self._matches("status-update-drafter", "draft a status update")
        assert self._matches("status-update-drafter", "what do we tell customers?")
        assert self._matches("status-update-drafter", "customer facing notice needed")

    def test_status_update_no_false_positive(self):
        assert not self._matches("status-update-drafter", "the outage lasted 3 hours")
        assert not self._matches("status-update-drafter", "who is oncall?")

    # -- oncall-handoff --
    def test_handoff_triggers(self):
        assert self._matches("oncall-handoff", "I'm handing off to the next oncall")
        assert self._matches("oncall-handoff", "end of my shift, here's the summary")
        assert self._matches("oncall-handoff", "passing the pager now")
        assert self._matches("oncall-handoff", "taking over oncall")

    def test_handoff_no_false_positive(self):
        assert not self._matches("oncall-handoff", "p1 in payments service")
        assert not self._matches("oncall-handoff", "how do I restart redis?")

    # -- runbook-lookup --
    def test_runbook_triggers(self):
        assert self._matches("runbook-lookup", "is there a runbook for this?")
        assert self._matches("runbook-lookup", "how do we fix the redis cache?")
        assert self._matches("runbook-lookup", "remediation steps for OOM")
        assert self._matches("runbook-lookup", "steps to recover the database")

    def test_runbook_no_false_positive(self):
        assert not self._matches("runbook-lookup", "alert fired for high CPU")
        assert not self._matches("runbook-lookup", "draft a postmortem")

    # -- deploy-watchdog --
    def test_deploy_triggers(self):
        assert self._matches("deploy-watchdog", "just deployed payments-v2.3")
        assert self._matches("deploy-watchdog", "shipping to prod now")
        assert self._matches("deploy-watchdog", "rollout complete for api-gateway")

    def test_deploy_no_false_positive(self):
        assert not self._matches("deploy-watchdog", "sev-1 in production")
        assert not self._matches("deploy-watchdog", "write a postmortem")

    # -- incident-response (catch-all, last) --
    def test_incident_triggers(self):
        assert self._matches("incident-response", "alert fired for high CPU usage")
        assert self._matches("incident-response", "we have an outage")
        assert self._matches("incident-response", "incident reported for payments")
        assert self._matches("incident-response", "API is going down")
        assert self._matches("incident-response", "pages going off right now")
        assert self._matches("incident-response", "this is a p1")
        assert self._matches("incident-response", "sev-2 in progress")

    def test_incident_no_false_positive(self):
        assert not self._matches("incident-response", "hey can you search for docs on redis?")
        assert not self._matches("incident-response", "draft a postmortem")
        assert not self._matches("incident-response", "how do I fix the cache?")

    # -- Ordering: first match wins --
    def test_first_match_wins_postmortem_over_incident(self):
        """'outage' appears in both postmortem-drafter and incident-response
        triggers, but postmortem-drafter should come first."""
        text = "draft a retro for the outage"
        # postmortem should match
        assert self._matches("postmortem-drafter", text)
        # incident would also match on 'outage'
        assert self._matches("incident-response", text)
        # But postmortem comes first in BUILTIN_SKILLS
        postmortem_idx = next(
            i for i, s in enumerate(BUILTIN_SKILLS)
            if s.name == "postmortem-drafter"
        )
        incident_idx = next(
            i for i, s in enumerate(BUILTIN_SKILLS)
            if s.name == "incident-response"
        )
        assert postmortem_idx < incident_idx

    def test_incident_response_is_last(self):
        """incident-response must be the last skill (broadest catch-all)."""
        assert BUILTIN_SKILLS[-1].name == "incident-response"

    # -- incident-response has code tools --
    def test_incident_response_includes_code_tools(self):
        """The investigation wedge requires code tools in incident-response."""
        skill = next(s for s in BUILTIN_SKILLS if s.name == "incident-response")
        code_tools = {"code_search", "code_read_file", "code_list_commits", "code_find_symbol"}
        assert code_tools.issubset(set(skill.required_tools))
