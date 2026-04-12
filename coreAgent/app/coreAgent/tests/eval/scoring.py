"""Scoring rubric for investigation eval scenarios.

Each scenario is scored on four dimensions:
  1. Found the right repo (0/1)
  2. Found the right file/function (0/1)
  3. Identified the right root cause (0/1)
  4. Overall findings quality (0-3 scale)

The quality score is keyword-based: we check how many of the expected
keywords appear in the agent's response. This is a rough heuristic —
it catches gross failures (agent hallucinated a completely wrong answer)
without being brittle to phrasing differences.

Total score per scenario: 0-6 points.
"""
from __future__ import annotations

from dataclasses import dataclass

from tests.eval.scenarios import EvalScenario


@dataclass
class EvalScore:
    """Score for a single eval scenario run."""
    scenario_id: str
    found_repo: int  # 0 or 1
    found_file: int  # 0 or 1
    found_root_cause: int  # 0 or 1
    quality: int  # 0-3
    tool_calls: list[str]  # names of tools the agent called
    response_text: str  # full agent response for review

    @property
    def total(self) -> int:
        return self.found_repo + self.found_file + self.found_root_cause + self.quality

    @property
    def max_score(self) -> int:
        return 6

    def summary(self) -> str:
        return (
            f"[{self.scenario_id}] {self.total}/{self.max_score} "
            f"(repo={self.found_repo} file={self.found_file} "
            f"cause={self.found_root_cause} quality={self.quality}/3)"
        )


def score_response(
    scenario: EvalScenario,
    response_text: str,
    tool_calls: list[str],
) -> EvalScore:
    """Score an agent's response against a scenario's expected outcome.

    This is a best-effort heuristic scorer, not a deterministic check.
    For evals where the agent should ask questions (sparse context,
    ambiguous repo), we score the question quality instead of the
    investigation quality.
    """
    expected = scenario.expected
    text_lower = response_text.lower()

    # 1. Found the right repo
    found_repo = 0
    if expected.get("found_repo") is True:
        # If expected found_repo is True, check if any code tool was called
        code_tools_used = any(
            t.startswith("code_") for t in tool_calls
        )
        if code_tools_used:
            found_repo = 1
    elif expected.get("found_repo") is False:
        # Expected NOT to find repo (should ask for clarification)
        if expected.get("should_ask_clarification"):
            if any(kw in text_lower for kw in ["which", "clarif", "specify"]):
                found_repo = 1
        elif expected.get("should_ask_questions"):
            if "?" in response_text:
                found_repo = 1
        else:
            # No code tools used is correct
            code_tools_used = any(t.startswith("code_") for t in tool_calls)
            if not code_tools_used:
                found_repo = 1

    # 2. Found the right file
    found_file = 0
    expected_file = expected.get("found_file")
    if expected_file is None:
        # No specific file expected — score 1 if agent didn't
        # hallucinate a specific file as root cause
        found_file = 1
    elif expected_file and expected_file.lower() in text_lower:
        found_file = 1

    # 3. Found the right root cause
    found_root_cause = 0
    expected_cause = expected.get("found_root_cause")
    if expected_cause is None:
        # No root cause expected (e.g. sparse context scenario)
        found_root_cause = 1
    elif expected_cause:
        # Check if key terms from the expected cause appear
        cause_words = expected_cause.lower().split()
        matches = sum(1 for w in cause_words if w in text_lower)
        if matches >= len(cause_words) * 0.5:
            found_root_cause = 1

    # 4. Quality score (0-3)
    quality_keywords = expected.get("quality_keywords", [])
    if quality_keywords:
        matched = sum(
            1 for kw in quality_keywords
            if kw.lower() in text_lower
        )
        ratio = matched / len(quality_keywords)
        if ratio >= 0.7:
            quality = 3
        elif ratio >= 0.4:
            quality = 2
        elif ratio >= 0.2:
            quality = 1
        else:
            quality = 0
    else:
        quality = 0

    return EvalScore(
        scenario_id=scenario.id,
        found_repo=found_repo,
        found_file=found_file,
        found_root_cause=found_root_cause,
        quality=quality,
        tool_calls=tool_calls,
        response_text=response_text,
    )
