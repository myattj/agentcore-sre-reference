#!/usr/bin/env python3
"""Content-only Datadog seeder.

Populates a disposable Datadog account with Acme Data Co content:
~15 events and ~8 monitors that mirror the fictional incidents from the
Slack seed. It deliberately does not connect Datadog to a tenant. Datadog
requires two independent secrets, while a direct AgentCore Gateway target
supports one credential provider, so the bridge rejects that unsafe shape.

Secret shape at ``agentcore/testenv/datadog``:

    {
      "api_key": "<datadog api key>",
      "app_key": "<datadog application key>",
      "site":    "datadoghq.com"
    }

``site`` is one of: ``datadoghq.com`` (US1), ``datadoghq.eu`` (EU),
``us3.datadoghq.com`` / ``us5.datadoghq.com`` / ``ap1.datadoghq.com``
depending on where you signed up. Most free-tier accounts are US1.

Re-runs are idempotent: we tag every created resource with
``acme-testenv=true`` and skip the seeding phase if any tagged resource
already exists. Pass ``--force`` to re-seed anyway (will duplicate).

Usage:
    python -m scripts.testenv.integrations.seed_datadog \\
        --tenant slack-t0xxxxxxxxx --skip-connect

The explicit ``--skip-connect`` acknowledgement is required. Credentials are
used only by this local process to call Datadog's API; they are never sent to
the bridge or written to tenant configuration.
"""
from __future__ import annotations

import argparse
import sys
import time
from typing import Any

from ._common import (
    RateLimitedClient,
    configure_logging,
    err,
    grey,
    load_integration_secret,
    load_seeded_state,
    ok,
    save_seeded_state,
    step,
    warn,
)


# ----------------------------------------------------------------------------
# Seed content — events
# ----------------------------------------------------------------------------

_EVENTS: list[dict[str, Any]] = [
    # Feb checkout incident timeline
    {
        "title": "checkout-api deploy v2.18.3 (retry backoff fix)",
        "text": "Deploy completed in 6m12s. Part of the remediation for the Feb retry storm incident. Introduces exponential backoff with jitter on payment provider calls.",
        "alert_type": "info",
        "tags": ["service:checkout-api", "env:prod", "deploy", "incident:feb-retry-storm"],
    },
    {
        "title": "Retry storm detected on checkout-api",
        "text": "Payment provider recovered after ~90s outage and our retry policy sent 3x normal traffic at recovery. Circuit breaker rolled back. Fixed in v2.18.3.",
        "alert_type": "error",
        "tags": ["service:checkout-api", "env:prod", "incident:feb-retry-storm", "severity:sev2"],
    },
    {
        "title": "Circuit breaker threshold lowered on checkout-api",
        "text": "Per post-incident action item: lowered payment-provider circuit breaker timeout from 5s to 3s.",
        "alert_type": "info",
        "tags": ["service:checkout-api", "env:prod", "config-change"],
    },
    # Ingest pipeline ongoing contention
    {
        "title": "INGEST_WH split into stream + batch",
        "text": "acme-infra PR #92 merged. New INGEST_STREAM_WH and INGEST_BATCH_WH to isolate event stream from finance extract. Fixes ingest-pipeline lag contention tracked in ongoing incident.",
        "alert_type": "info",
        "tags": ["service:ingest-pipeline", "env:prod", "infrastructure"],
    },
    {
        "title": "Ingest pipeline lag elevated",
        "text": "p95 COPY INTO duration jumped from 8s to 45s. Intermittent bursts, correlated with finance extract runs. Snowflake support case #1234567.",
        "alert_type": "warning",
        "tags": ["service:ingest-pipeline", "env:prod", "incident:ingest-contention"],
    },
    # Orders dbt regression
    {
        "title": "fct_orders_daily row count regression detected",
        "text": "-40% vs 7-day average. Upstream finance ETL renamed order_total_cents to order_total_amount_cents without cross-team notice. Fix shipped in acme-data-api PR #434.",
        "alert_type": "error",
        "tags": ["service:reporting-worker", "env:prod", "dbt", "incident:orders-regression"],
    },
    {
        "title": "Backfill complete: fct_orders_daily (7 days)",
        "text": "Backfilled 7 days of fct_orders_daily after the silent-null-drop fix. Row counts now match expected range.",
        "alert_type": "success",
        "tags": ["service:reporting-worker", "env:prod", "dbt", "incident:orders-regression"],
    },
    # Routine ops
    {
        "title": "RDS prod password rotated",
        "text": "Quarterly rotation completed. Old version disabled, will delete after 24h hold per runbook.",
        "alert_type": "info",
        "tags": ["service:rds-prod", "env:prod", "security", "rotation"],
    },
    {
        "title": "EKS 1.29 upgrade: control plane complete",
        "text": "Control plane upgrade from 1.28 → 1.29 finished in 28m. Node groups rolling next window.",
        "alert_type": "info",
        "tags": ["env:prod", "eks", "upgrade"],
    },
    {
        "title": "Snowflake cost: April MTD -34% vs March",
        "text": "Auto-suspend tuning + workload splitting is working. Monthly forecast on track for $12k vs March's $18k.",
        "alert_type": "success",
        "tags": ["service:snowflake", "cost-optimization"],
    },
    {
        "title": "orders-api pod flapping: killed 7d9c4b5f6-xk8n2",
        "text": "One pod stuck in CrashLoopBackoff after latest deploy. Deployment auto-replacing. Error rate back under 0.1%.",
        "alert_type": "warning",
        "tags": ["service:orders-api", "env:prod"],
    },
    {
        "title": "ANALYTICS_WH resource monitor 80% notification",
        "text": "Monthly credit usage at 80% of quota. On track for month-end under quota. No action needed.",
        "alert_type": "info",
        "tags": ["service:snowflake", "cost"],
    },
    {
        "title": "user-service pod CrashLoopBackoff resolved",
        "text": "Stale configmap from a rollback. Forced rollout restart, all pods healthy.",
        "alert_type": "success",
        "tags": ["service:user-service", "env:prod"],
    },
    {
        "title": "Sentry regression: IntegrityError on POST /users",
        "text": "Front-end retry is re-POSTing on success. Not a backend bug. Coordinating with frontend team; not oncall-urgent.",
        "alert_type": "warning",
        "tags": ["service:user-service", "env:prod", "sentry-linked"],
    },
    {
        "title": "AWS RDS us-west-2 status: investigating",
        "text": "Upstream AWS status page showed degraded. No impact on our instances. Watching.",
        "alert_type": "info",
        "tags": ["env:prod", "upstream-aws"],
    },
]


# ----------------------------------------------------------------------------
# Seed content — monitors
# ----------------------------------------------------------------------------

_MONITORS: list[dict[str, Any]] = [
    {
        "name": "[acme-testenv] checkout-api p99 latency",
        "type": "metric alert",
        "query": "avg(last_5m):avg:trace.http.request.duration.by_service{service:checkout-api,env:prod}.rollup(avg, 60) > 0.8",
        "message": (
            "checkout-api p99 latency is above 800ms for 5 minutes. "
            "Runbook: acme-runbooks/infra/rds-slow-query.md (often correlated with RDS). "
            "Owner: @sre @morgan-chen"
        ),
        "tags": ["service:checkout-api", "env:prod", "acme-testenv"],
        "options": {
            "thresholds": {"critical": 0.8, "warning": 0.5},
            "notify_no_data": False,
            "evaluation_delay": 60,
        },
    },
    {
        "name": "[acme-testenv] orders-api 5xx rate",
        "type": "metric alert",
        "query": "sum(last_5m):sum:trace.http.request.errors{service:orders-api,env:prod}.as_count() / sum:trace.http.request.hits{service:orders-api,env:prod}.as_count() > 0.01",
        "message": (
            "orders-api error rate > 1% for 5 minutes. Check recent deploys in #eng-general, "
            "and the runbook at acme-runbooks/deploys/deploy-rollback.md if a rollback is needed."
        ),
        "tags": ["service:orders-api", "env:prod", "acme-testenv"],
        "options": {"thresholds": {"critical": 0.01}, "notify_no_data": False},
    },
    {
        "name": "[acme-testenv] user-service error rate",
        "type": "metric alert",
        "query": "sum(last_5m):sum:trace.http.request.errors{service:user-service,env:prod}.as_count() / sum:trace.http.request.hits{service:user-service,env:prod}.as_count() > 0.005",
        "message": (
            "user-service error rate > 0.5%. Correlate with Sentry issues tagged `user-service`. "
            "Owner: @sre"
        ),
        "tags": ["service:user-service", "env:prod", "acme-testenv"],
        "options": {"thresholds": {"critical": 0.005}, "notify_no_data": False},
    },
    {
        "name": "[acme-testenv] RDS prod connection count",
        "type": "metric alert",
        "query": "avg(last_5m):avg:aws.rds.database_connections{dbinstanceidentifier:acme-prod} > 400",
        "message": (
            "RDS connection count > 400 (max 500). Runbook: "
            "acme-runbooks/infra/rds-connection-exhaustion.md. "
            "Check pg_stat_activity for idle-in-transaction first."
        ),
        "tags": ["service:rds-prod", "env:prod", "acme-testenv"],
        "options": {"thresholds": {"critical": 400, "warning": 350}},
    },
    {
        "name": "[acme-testenv] ingest-pipeline lag",
        "type": "metric alert",
        "query": "avg(last_10m):avg:acme.ingest.copy_duration.p95{service:ingest-pipeline,env:prod} > 60",
        "message": (
            "Ingest pipeline COPY INTO p95 > 60s for 10 min. "
            "Known contention issue, runbook: acme-runbooks/data/ingest-pipeline-lag.md. "
            "Owner: @data-eng @priya-ramanathan"
        ),
        "tags": ["service:ingest-pipeline", "env:prod", "acme-testenv"],
        "options": {"thresholds": {"critical": 60, "warning": 30}},
    },
    {
        "name": "[acme-testenv] reporting-worker job runtime",
        "type": "metric alert",
        "query": "avg(last_30m):avg:acme.reporting.job_duration_minutes{service:reporting-worker,env:prod} > 20",
        "message": (
            "Reporting worker job exceeded 20min SLA. Often a Snowflake contention issue, not a worker bug. "
            "Check warehouse queue depth first."
        ),
        "tags": ["service:reporting-worker", "env:prod", "acme-testenv"],
        "options": {"thresholds": {"critical": 20}},
    },
    {
        "name": "[acme-testenv] checkout-api pod memory",
        "type": "metric alert",
        "query": "avg(last_10m):avg:kubernetes.memory.usage_pct{service:checkout-api,env:prod} > 0.85",
        "message": (
            "checkout-api memory > 85% on pods for 10 min. Expected during retry backfill runs; "
            "runbook: acme-runbooks/deploys/deploy-rollback.md for persistent pressure."
        ),
        "tags": ["service:checkout-api", "env:prod", "acme-testenv"],
        "options": {"thresholds": {"critical": 0.85, "warning": 0.8}},
    },
    {
        "name": "[acme-testenv] Snowflake warehouse credit budget",
        "type": "metric alert",
        "query": "avg(last_1h):avg:snowflake.warehouse.credits_used_monthly{warehouse:ANALYTICS_WH} > 200",
        "message": (
            "ANALYTICS_WH monthly credit usage > 200. Resource monitor will suspend at 250. "
            "Runbook: acme-runbooks/data/snowflake-cost-optimization.md. Owner: @priya-ramanathan"
        ),
        "tags": ["service:snowflake", "cost", "acme-testenv"],
        "options": {"thresholds": {"critical": 200, "warning": 150}},
    },
]


# ----------------------------------------------------------------------------
# Seeder
# ----------------------------------------------------------------------------

_SEEDED_TAG = "acme-testenv"


def _datadog_base_url(site: str) -> str:
    # Datadog's API base changes per site. "api." prefix is standard.
    return f"https://api.{site}"


def _check_already_seeded(client: RateLimitedClient) -> int:
    """Return the number of already-seeded events we can find (best-effort).

    Datadog's Events API accepts tag filters but has limited search; we
    query the last 30 days for events matching our tag and count them.
    Non-zero means the seeder ran before.
    """
    # Events API v1 — query last 30 days for events with our tag.
    end = int(time.time())
    start = end - 30 * 86400
    r = client.get(
        "/api/v1/events",
        params={"start": start, "end": end, "tags": f"acme-testenv={_SEEDED_TAG}"},
    )
    if r.status_code != 200:
        # If the check itself fails, proceed optimistically rather than
        # abort — seeding is idempotent enough that a duplicate event
        # pair isn't the end of the world.
        return 0
    data = r.json() or {}
    return len(data.get("events", []) or [])


def _post_event(client: RateLimitedClient, event: dict[str, Any]) -> str | None:
    """POST one event. Returns the created event id, or None on failure."""
    body = dict(event)
    # Ensure the seeded tag is present on every event so re-run checks work.
    tags = list(body.get("tags") or [])
    if _SEEDED_TAG not in tags:
        tags.append(_SEEDED_TAG)
    body["tags"] = tags
    body.setdefault("source_type_name", "acme-testenv")
    r = client.post("/api/v1/events", json=body)
    if r.status_code not in (200, 202):
        return None
    data = r.json() or {}
    return str(data.get("event", {}).get("id") or data.get("id") or "")


def _post_monitor(client: RateLimitedClient, monitor: dict[str, Any]) -> str | None:
    body = dict(monitor)
    tags = list(body.get("tags") or [])
    if _SEEDED_TAG not in tags:
        tags.append(_SEEDED_TAG)
    body["tags"] = tags
    r = client.post("/api/v1/monitor", json=body)
    if r.status_code not in (200, 201):
        return None
    data = r.json() or {}
    return str(data.get("id") or "")


def run_seed(
    tenant_id: str,
    *,
    region: str | None = None,
    skip_connect: bool = False,
    force: bool = False,
) -> int:
    """Seed synthetic content after an explicit no-connect acknowledgement."""
    del tenant_id  # Kept for a consistent testenv seeder CLI.
    if not skip_connect:
        err(
            "Datadog tenant connection is intentionally disabled. Re-run with "
            "--skip-connect to seed synthetic content directly."
        )
        return 2

    # ---- 1. Load credentials ----
    step("Loading Datadog credentials from Secrets Manager")
    try:
        creds = load_integration_secret(
            "datadog",
            region=region,
            required_keys=["api_key", "app_key", "site"],
        )
    except RuntimeError as e:
        err(str(e))
        return 1
    site = creds["site"]
    ok(f"creds loaded (site: {site})")

    # ---- 2. Seed content directly ----
    warn("--skip-connect acknowledged: no tenant or Gateway changes will be made")

    step("Seeding Datadog events + monitors")
    client = RateLimitedClient(
        base_url=_datadog_base_url(site),
        headers={
            "DD-API-KEY": creds["api_key"],
            "DD-APPLICATION-KEY": creds["app_key"],
            "Content-Type": "application/json",
        },
        min_interval_s=0.3,
    )

    state = load_seeded_state("datadog")
    already = _check_already_seeded(client)
    if already and not force:
        warn(f"found {already} existing events tagged acme-testenv — skipping seed (pass --force to re-run)")
        client.close()
        return 0

    posted_events: list[str] = []
    posted_monitors: list[str] = []

    try:
        # Events
        for i, event in enumerate(_EVENTS, 1):
            event_id = _post_event(client, event)
            if event_id:
                posted_events.append(event_id)
                grey(f"  event {i}/{len(_EVENTS)}: {event['title'][:60]}")
            else:
                warn(f"  event {i} failed: {event['title'][:60]}")
        ok(f"{len(posted_events)}/{len(_EVENTS)} events posted")

        # Monitors
        for i, monitor in enumerate(_MONITORS, 1):
            monitor_id = _post_monitor(client, monitor)
            if monitor_id:
                posted_monitors.append(monitor_id)
                grey(f"  monitor {i}/{len(_MONITORS)}: {monitor['name']}")
            else:
                warn(f"  monitor {i} failed: {monitor['name']}")
        ok(f"{len(posted_monitors)}/{len(_MONITORS)} monitors created")

    finally:
        client.close()

    state["events"] = posted_events
    state["monitors"] = posted_monitors
    state["last_run"] = int(time.time())
    save_seeded_state("datadog", state)

    step("Datadog seed complete")
    grey(f"  dashboard: https://app.{site}/event/explorer?query=tags%3A{_SEEDED_TAG}")
    grey(f"  monitors:  https://app.{site}/monitors/manage?q=tag%3A%22{_SEEDED_TAG}%22")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed Datadog for the Agent test env.")
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--region", default=None)
    parser.add_argument(
        "--skip-connect",
        action="store_true",
        required=True,
        help="Required acknowledgement: seed content without connecting a tenant",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-seed content even if already-seeded events are found",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    configure_logging(args.verbose)
    return run_seed(
        args.tenant,
        region=args.region,
        skip_connect=args.skip_connect,
        force=args.force,
    )


if __name__ == "__main__":
    sys.exit(main())
