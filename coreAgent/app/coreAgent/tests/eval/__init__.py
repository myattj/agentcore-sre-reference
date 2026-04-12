"""Investigation eval harness.

Repeatable eval scenarios for the investigation wedge. Each scenario
defines a known-good outcome so we can measure whether the agent:
  1. Finds the right repo
  2. Finds the right file/function
  3. Identifies the right root cause
  4. Produces findings that match the expected answer (0-3 scale)

Run with: uv run pytest tests/eval/ -v --run-evals
(Requires AWS credentials for Bedrock and AGENT_LOCAL_STORES=1)
"""
