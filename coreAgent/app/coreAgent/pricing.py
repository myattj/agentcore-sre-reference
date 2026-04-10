"""Token-to-dollar pricing for Bedrock models.

Centralizes the per-million-token rates used by:
  - spend_tracker.py (cost-cap enforcement at invocation time)
  - infra/data/scripts/audit_query.py (ad-hoc cost CLI — duplicates
    the dict with a KEEP IN SYNC comment because it runs outside the
    agent's venv)

Prices are Bedrock on-demand rates for us-west-2 as of 2026-04.
Update when pricing changes or new models are added to the platform.
"""
from __future__ import annotations

# model_id pattern -> per-million-token rates (USD).
# Keys match the model_id stored in TenantConfig (the Bedrock cross-region
# inference ID, e.g. "global.anthropic.claude-sonnet-4-6").
MODEL_PRICING: dict[str, dict[str, float]] = {
    "global.anthropic.claude-sonnet-4-6": {
        "input_per_million": 3.00,
        "output_per_million": 15.00,
    },
    "anthropic.claude-sonnet-4-6": {
        "input_per_million": 3.00,
        "output_per_million": 15.00,
    },
}

# Conservative fallback: use the most expensive model's rates so we
# over-count rather than under-count when a model_id is unknown.
_FALLBACK_PRICING = {
    "input_per_million": 3.00,
    "output_per_million": 15.00,
}


def compute_cost_cents(model_id: str, input_tokens: int, output_tokens: int) -> int:
    """Return the estimated cost in integer cents (USD).

    Uses integer cents to avoid floating-point drift in running totals.
    Rounds up (ceiling) so we never under-count.
    """
    pricing = MODEL_PRICING.get(model_id, _FALLBACK_PRICING)
    cost_dollars = (
        input_tokens / 1_000_000 * pricing["input_per_million"]
        + output_tokens / 1_000_000 * pricing["output_per_million"]
    )
    # Round up to the nearest cent.
    import math
    return math.ceil(cost_dollars * 100)
