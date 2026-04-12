"""Seed content packs for the AgentCore Reference test-env rig.

Each pack exposes a ``build() -> list[SeedMessage]`` function. The
seeder imports them in the order defined by
``seed_slack_history._load_packs()``.

Authoring rules:

  - Every SeedMessage.key must be globally unique across ALL packs.
    Use a pack-specific prefix (``ch_``, ``qa_``, ``rb_``, ``al_``,
    ``in_``) to keep collisions obvious.
  - Within a pack, parents MUST come before their threaded replies —
    the seeder resolves ``parent_key`` against already-posted state
    and skips any reply whose parent hasn't been posted yet.
  - Keep content realistic: use the fictional Acme Data Co service
    names (checkout-api, orders-api, user-service, ingest-pipeline,
    reporting-worker), stack (Snowflake, dbt, Airflow, EKS,
    Terraform, Datadog, Sentry), and team (Morgan, Priya, Alex,
    Jamie, Sam, Taylor, Riley, Jordan). Realism drives what the
    agent can actually surface when the user searches history.
"""
