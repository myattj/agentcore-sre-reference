"""Investigation eval tests.

These are end-to-end evals that invoke a real Strands Agent with Bedrock
(option C from the plan: bypasses AgentCore Runtime, tests real reasoning).
Requires AWS credentials and --run-evals flag.

Run: uv run pytest tests/eval/ -v --run-evals

The harness:
  1. Builds a Strands Agent with the investigation skill's system prompt
  2. Injects mock tool functions that return canned responses
  3. Sends the scenario's user message
  4. Scores the response against the expected outcome
  5. Reports per-scenario and aggregate scores
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from tests.eval.scenarios import ALL_SCENARIOS, EvalScenario
from tests.eval.scoring import EvalScore, score_response


# ---------------------------------------------------------------------------
# Scoring-only tests (no LLM, always run)
# ---------------------------------------------------------------------------

class TestScoringRubric:
    """Verify the scoring function itself works correctly."""

    def test_perfect_score(self):
        """A response with all expected keywords scores max."""
        from tests.eval.scenarios import SCENARIO_CPU_SPIKE_DEPLOY

        response = (
            "The CPU spike correlates with commit abc123f deployed at 14:13. "
            "The batch reconciliation change introduced an N+1 query in "
            "BatchProcessor.process_all() that hits the DB for every transaction. "
            "CPU went from 23% to 95% immediately after deploy."
        )
        tool_calls = [
            "read_thread_context", "query_metrics", "code_list_commits",
            "code_read_file",
        ]
        score = score_response(SCENARIO_CPU_SPIKE_DEPLOY, response, tool_calls)
        assert score.found_repo == 1
        assert score.found_root_cause == 1
        assert score.quality >= 2
        assert score.total >= 4

    def test_zero_score_hallucination(self):
        """A completely wrong response scores low."""
        from tests.eval.scenarios import SCENARIO_CPU_SPIKE_DEPLOY

        response = (
            "I've checked and everything looks normal. The memory usage is "
            "within acceptable limits and there are no recent incidents. "
            "The network latency is stable."
        )
        score = score_response(SCENARIO_CPU_SPIKE_DEPLOY, response, [])
        assert score.found_repo == 0
        assert score.quality <= 1
        assert score.total <= 3

    def test_sparse_context_asks_questions(self):
        """Sparse context scenario scores well when agent asks questions."""
        from tests.eval.scenarios import SCENARIO_SPARSE_CONTEXT

        response = (
            "I see a P1 was declared, but I need more details to help:\n"
            "- Which service or system is affected?\n"
            "- What symptoms are you seeing?\n"
            "- When did this start happening?"
        )
        score = score_response(SCENARIO_SPARSE_CONTEXT, response, ["read_thread_context"])
        assert score.found_repo == 1  # Correctly didn't try to find a repo
        assert score.quality >= 2

    def test_false_positive_identified(self):
        """False positive scenario scores well when agent identifies pattern."""
        from tests.eval.scenarios import SCENARIO_FALSE_POSITIVE

        response = (
            "This appears to be expected behavior — the nightly ETL batch "
            "processing runs 2-4am UTC and regularly spikes memory to ~85%. "
            "Past team discussions confirm this is a known pattern. "
            "Recommendation: tune the alert threshold for data-pipeline "
            "to avoid these false positive alerts during the ETL window."
        )
        tool_calls = ["read_thread_context", "query_metrics", "search_team_history"]
        score = score_response(SCENARIO_FALSE_POSITIVE, response, tool_calls)
        assert score.found_root_cause == 1
        assert score.quality >= 2


# ---------------------------------------------------------------------------
# Live LLM evals (requires --run-evals + AWS creds)
# ---------------------------------------------------------------------------

def _build_mock_tool(name: str, response: Any):
    """Build a mock tool function that returns a canned response."""
    from strands import tool

    response_str = json.dumps(response) if isinstance(response, dict) else str(response)

    @tool(name=name)
    def mock_tool(**kwargs) -> str:
        """Mock tool for eval testing."""
        return response_str

    return mock_tool


def _run_scenario(scenario: EvalScenario) -> EvalScore:
    """Run a single eval scenario against a live Strands Agent.

    Uses Bedrock directly (option C): no AgentCore Runtime, just the
    Strands SDK talking to Claude via Bedrock's invoke model API.
    """
    from strands import Agent
    from strands.models.bedrock import BedrockModel

    from builtin_skills import BUILTIN_SKILLS
    from context_assembler import _build_integration_block
    from tenant import ByoConfig

    # Build the system prompt as the real pipeline would:
    # base prompt + skill prompt + integration hint
    incident_skill = next(
        s for s in BUILTIN_SKILLS if s.name == "incident-response"
    )
    system_prompt = (
        "You are an SRE assistant investigating a production incident.\n\n"
        f"## Active Skill: {incident_skill.name}\n\n"
        f"{incident_skill.prompt_template}"
    )
    # Replace placeholders
    system_prompt = system_prompt.replace("{user_id}", "U_SRE1")
    system_prompt = system_prompt.replace("{channel_id}", "C_INCIDENTS")

    # Add integration hint if monitoring tools are available
    has_monitoring = any(
        t in scenario.available_tools
        for t in ("query_metrics", "get_recent_alerts", "search_logs")
    )
    if has_monitoring:
        byo = ByoConfig(enabled=True, connected_integrations=["datadog"])
        block = _build_integration_block(byo)
        if block:
            system_prompt += "\n\n" + block

    # Build mock tools from scenario's mock_tool_responses
    tools = []
    tool_calls_log: list[str] = []

    for tool_name in scenario.available_tools:
        mock_response = scenario.mock_tool_responses.get(tool_name, {})

        # read_thread_context returns the scenario's thread context
        if tool_name == "read_thread_context":
            mock_response = scenario.thread_context

        tools.append(_build_mock_tool(tool_name, mock_response))

    model = BedrockModel(
        model_id="us.anthropic.claude-sonnet-4-6",
        region_name="us-west-2",
    )

    agent = Agent(
        model=model,
        system_prompt=system_prompt,
        tools=tools,
    )

    # Run the agent
    result = agent(scenario.user_message)
    response_text = str(result)

    # Extract tool calls from the result
    # (Strands logs tool usage in the result's messages)
    if hasattr(result, "messages"):
        for msg in result.messages:
            if isinstance(msg, dict) and msg.get("role") == "assistant":
                for block in msg.get("content", []):
                    if isinstance(block, dict) and "toolUse" in block:
                        tool_calls_log.append(block["toolUse"]["name"])

    return score_response(scenario, response_text, tool_calls_log)


@pytest.mark.eval
@pytest.mark.parametrize(
    "scenario",
    ALL_SCENARIOS,
    ids=[s.id for s in ALL_SCENARIOS],
)
def test_investigation_scenario(scenario: EvalScenario):
    """Run a single investigation eval scenario and assert minimum quality."""
    score = _run_scenario(scenario)
    print(f"\n{score.summary()}")
    print(f"Response preview: {score.response_text[:300]}...")

    # Minimum bar: total score >= 3 out of 6. This is deliberately low
    # to avoid flaky tests while still catching gross regressions.
    # Raise the bar as the investigation skill improves.
    assert score.total >= 3, (
        f"Investigation quality below minimum bar: {score.summary()}"
    )
