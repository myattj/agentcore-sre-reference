#!/usr/bin/env python3
"""Sentry seeder.

Sentry is different from the other integrations: you can't directly
create an "issue" — issues are derived from error events ingested via
the Sentry SDK or the store/envelope endpoint. This seeder uses the
envelope endpoint (the modern ingestion path) to POST ~10 structured
error events that Sentry then groups into issues.

Secret shape at ``agentcore/testenv/sentry``:

    {
      "auth_token":   "<sentry internal integration token>",
      "dsn":          "<project DSN, e.g. https://abc@o12345.ingest.sentry.io/67890>",
      "organization": "<your org slug>",
      "project":      "<your project slug>"
    }

The DSN comes from your project's SDK setup page. The auth_token is an
internal integration token with ``event:write`` + ``event:read`` scope
from Settings → Custom Integrations.

Note: ``auth_token`` is currently unused by the ingestion path (DSN
is sufficient for /envelope/), but it's kept in the shape so the
bridge route (which may need it for other endpoints) has it available.

Usage:
    python -m scripts.testenv.integrations.seed_sentry --tenant slack-t0xxxxxxxxx
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from ._common import (
    RateLimitedClient,
    bridge_connect_integration,
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
# Event content — each becomes a Sentry issue (grouped by fingerprint)
# ----------------------------------------------------------------------------

_EVENTS: list[dict[str, Any]] = [
    {
        "service": "checkout-api",
        "message": "RetryBudgetExhausted: checkout-api to payment-provider",
        "exception_type": "RetryBudgetExhausted",
        "exception_value": "Retry budget exhausted after 5 attempts. Circuit breaker tripped.",
        "level": "error",
        "tags": {"service": "checkout-api", "env": "prod", "incident": "feb-retry-storm"},
        "fingerprint": ["checkout-api", "retry-budget-exhausted"],
    },
    {
        "service": "checkout-api",
        "message": "PaymentProviderTimeout: /v1/charges",
        "exception_type": "PaymentProviderTimeout",
        "exception_value": "Timeout after 3000ms calling payment provider /v1/charges.",
        "level": "error",
        "tags": {"service": "checkout-api", "env": "prod"},
        "fingerprint": ["checkout-api", "payment-timeout"],
    },
    {
        "service": "user-service",
        "message": "IntegrityError: duplicate key value violates unique constraint 'users_email_key'",
        "exception_type": "IntegrityError",
        "exception_value": "duplicate key value violates unique constraint \"users_email_key\"",
        "level": "error",
        "tags": {"service": "user-service", "env": "prod", "endpoint": "POST /users"},
        "fingerprint": ["user-service", "users-email-unique-violation"],
    },
    {
        "service": "reporting-worker",
        "message": "KeyError: 'payment_method'",
        "exception_type": "KeyError",
        "exception_value": "'payment_method' not found in transform_orders input row",
        "level": "error",
        "tags": {"service": "reporting-worker", "env": "prod", "incident": "orders-regression"},
        "fingerprint": ["reporting-worker", "transform-orders-keyerror"],
    },
    {
        "service": "ingest-pipeline",
        "message": "SnowflakeOperationalError: 604 — Statement canceled",
        "exception_type": "OperationalError",
        "exception_value": "Snowflake statement canceled — pipe queue contention",
        "level": "error",
        "tags": {"service": "ingest-pipeline", "env": "prod", "incident": "ingest-contention"},
        "fingerprint": ["ingest-pipeline", "snowflake-cancel-604"],
    },
    {
        "service": "orders-api",
        "message": "ValidationError: order.total must be positive",
        "exception_type": "ValidationError",
        "exception_value": "order.total = -12.50 (expected >= 0)",
        "level": "error",
        "tags": {"service": "orders-api", "env": "prod"},
        "fingerprint": ["orders-api", "validation-total-negative"],
    },
    {
        "service": "checkout-api",
        "message": "Connection pool exhausted",
        "exception_type": "PoolTimeout",
        "exception_value": "No connection available after 10s wait. pool_size=25, in_use=25",
        "level": "error",
        "tags": {"service": "checkout-api", "env": "prod"},
        "fingerprint": ["checkout-api", "rds-pool-exhausted"],
    },
    {
        "service": "user-service",
        "message": "JWT decode failure: ExpiredSignatureError",
        "exception_type": "ExpiredSignatureError",
        "exception_value": "JWT has expired",
        "level": "warning",
        "tags": {"service": "user-service", "env": "prod", "endpoint": "GET /profile"},
        "fingerprint": ["user-service", "jwt-expired"],
    },
    {
        "service": "reporting-worker",
        "message": "dbt run failed: fct_orders_daily",
        "exception_type": "DbtRuntimeError",
        "exception_value": "Database Error: SQL compilation error: Table UPSTREAM_ORDERS does not exist",
        "level": "error",
        "tags": {"service": "reporting-worker", "env": "prod", "dbt_model": "fct_orders_daily"},
        "fingerprint": ["reporting-worker", "dbt-fct-orders-daily"],
    },
    {
        "service": "ingest-pipeline",
        "message": "KafkaError: Broker transport failure",
        "exception_type": "KafkaError",
        "exception_value": "Broker transport failure — partition rebalance in progress",
        "level": "warning",
        "tags": {"service": "ingest-pipeline", "env": "prod"},
        "fingerprint": ["ingest-pipeline", "kafka-transport-failure"],
    },
]


def _parse_dsn(dsn: str) -> dict[str, str]:
    """Parse a Sentry DSN into its components.

    DSN shape: ``https://<public_key>@<host>/<project_id>``
    Returns: ``{"public_key", "host", "project_id", "scheme"}``
    """
    # Simple manual parse — urllib doesn't handle the embedded key cleanly
    if "://" not in dsn:
        raise ValueError(f"invalid DSN (missing scheme): {dsn}")
    scheme, rest = dsn.split("://", 1)
    if "@" not in rest:
        raise ValueError(f"invalid DSN (missing public key): {dsn}")
    public_key, host_path = rest.split("@", 1)
    if "/" not in host_path:
        raise ValueError(f"invalid DSN (missing project id): {dsn}")
    host, project_id = host_path.rsplit("/", 1)
    return {
        "scheme": scheme,
        "public_key": public_key,
        "host": host,
        "project_id": project_id,
    }


def _build_event_payload(
    template: dict[str, Any],
) -> dict[str, Any]:
    """Build a Sentry event payload in the JSON event format.

    Schema reference: https://develop.sentry.dev/sdk/event-payloads/
    """
    return {
        "event_id": uuid.uuid4().hex,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "platform": "python",
        "level": template["level"],
        "logger": template["service"],
        "server_name": f"{template['service']}-prod",
        "release": "v2.18.3",
        "environment": "prod",
        "message": template["message"],
        "tags": template.get("tags") or {},
        "fingerprint": template.get("fingerprint") or ["{{ default }}"],
        "exception": {
            "values": [
                {
                    "type": template["exception_type"],
                    "value": template["exception_value"],
                    "module": template["service"],
                }
            ]
        },
    }


def _envelope_url(dsn_parts: dict[str, str]) -> str:
    return (
        f"{dsn_parts['scheme']}://{dsn_parts['host']}"
        f"/api/{dsn_parts['project_id']}/envelope/"
    )


def _auth_header(dsn_parts: dict[str, str]) -> str:
    return (
        "Sentry sentry_version=7,"
        f"sentry_key={dsn_parts['public_key']},"
        "sentry_client=agentcore-testenv/1.0"
    )


def _post_event(
    client: RateLimitedClient,
    *,
    envelope_url: str,
    auth_header: str,
    event: dict[str, Any],
) -> bool:
    """POST one envelope containing a single event item.

    Envelope format: a JSON header line, a JSON item header line, and
    a JSON payload line, newline-separated, all in one body.
    """
    header = json.dumps({"event_id": event["event_id"]})
    item_header = json.dumps({"type": "event", "content_type": "application/json"})
    payload = json.dumps(event)
    body = f"{header}\n{item_header}\n{payload}\n"
    r = client.post(
        envelope_url,
        content=body,
        headers={
            "Content-Type": "application/x-sentry-envelope",
            "X-Sentry-Auth": auth_header,
        },
    )
    return r.status_code in (200, 202)


def run_seed(
    tenant_id: str,
    *,
    region: str | None = None,
    bridge_url: str | None = None,
    skip_connect: bool = False,
    skip_seed: bool = False,
    force: bool = False,
) -> int:
    step("Loading Sentry credentials from Secrets Manager")
    try:
        creds = load_integration_secret(
            "sentry",
            region=region,
            required_keys=["auth_token", "dsn", "organization", "project"],
        )
    except RuntimeError as e:
        err(str(e))
        return 1
    ok(f"creds loaded (org: {creds['organization']}, project: {creds['project']})")

    if skip_connect:
        warn("--skip-connect: skipping bridge integration connect")
    else:
        # NB: Sentry is NOT currently in the bridge's integration connect
        # routes (api.py has datadog/confluence/notion/jira/linear/
        # pagerduty/github but no sentry). Skip the connect step with a
        # warning rather than error out — the content seeder still does
        # the useful thing.
        warn("no bridge connect route for Sentry yet (api.py has 6 integrations, Sentry isn't one). "
             "Skipping the Gateway connect step. If you add a connect_sentry route in api.py later, "
             "enable it here.")

    if skip_seed:
        warn("--skip-seed: skipping content seed")
        return 0

    step("Seeding Sentry events via envelope endpoint")
    try:
        dsn_parts = _parse_dsn(creds["dsn"])
    except ValueError as e:
        err(str(e))
        return 1

    envelope_url = _envelope_url(dsn_parts)
    auth_header = _auth_header(dsn_parts)
    grey(f"  envelope URL: {envelope_url}")

    state = load_seeded_state("sentry")
    if state.get("events") and not force:
        warn(f"found existing state ({len(state['events'])} seeded events) — pass --force to re-seed")
        return 0

    client = RateLimitedClient(min_interval_s=0.3)

    posted: list[str] = []
    try:
        for i, template in enumerate(_EVENTS, 1):
            event = _build_event_payload(template)
            ok_post = _post_event(
                client,
                envelope_url=envelope_url,
                auth_header=auth_header,
                event=event,
            )
            if ok_post:
                posted.append(event["event_id"])
                grey(f"  event {i}/{len(_EVENTS)}: {template['exception_type']} ({template['service']})")
            else:
                warn(f"  event {i} failed: {template['exception_type']}")
    finally:
        client.close()

    ok(f"{len(posted)}/{len(_EVENTS)} events posted")

    state["events"] = posted
    state["last_run"] = int(time.time())
    save_seeded_state("sentry", state)

    step("Sentry seed complete")
    grey(
        f"  open https://sentry.io/organizations/{creds['organization']}"
        f"/issues/?project=&statsPeriod=24h to verify"
    )
    grey("  events may take ~30s to appear as grouped issues")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed Sentry for the AgentCore Reference test env.")
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--region", default=None)
    parser.add_argument("--bridge-url", default=None)
    parser.add_argument("--skip-connect", action="store_true")
    parser.add_argument("--skip-seed", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    configure_logging(args.verbose)
    return run_seed(
        args.tenant,
        region=args.region,
        bridge_url=args.bridge_url,
        skip_connect=args.skip_connect,
        skip_seed=args.skip_seed,
        force=args.force,
    )


if __name__ == "__main__":
    sys.exit(main())
