#!/usr/bin/env python3
"""On-demand alert injector for manual testing.

Posts a single realistic PagerDuty / Datadog / Sentry alert to one of
the test env alert channels, impersonating the relevant bot via
``chat:write.customize``. Returns the Slack permalink so the user can
watch the agent react in real time.

Usage:

  python -m scripts.testenv.inject_alert --tenant slack-t123 \\
      --type pagerduty --severity P2 --service checkout-api

The --service arg maps to a channel via a simple rule set:
  checkout-api / orders-api / user-service / eks → #alerts-sre
  ingest-pipeline / dbt / snowflake / reporting   → #alerts-data
  auth / iam / sso / cloudtrail                    → #alerts-security

Pass --channel explicitly to override.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from ._channels import discover_and_join
from ._common import (
    RateLimitedPoster,
    configure_logging,
    load_seeder_bot_token,
    make_slack_client,
    persona,
)
from ._state import SeederState


ALERT_TYPES = {
    "pagerduty": "pagerduty",
    "datadog": "datadog",
    "sentry": "sentry",
    "statuspage": "statuspage",
}

SERVICE_TO_CHANNEL: dict[str, str] = {
    "checkout-api": "alerts-sre",
    "orders-api": "alerts-sre",
    "user-service": "alerts-sre",
    "eks": "alerts-sre",
    "rds": "alerts-sre",
    "ingest-pipeline": "alerts-data",
    "reporting-worker": "alerts-data",
    "dbt": "alerts-data",
    "snowflake": "alerts-data",
    "auth": "alerts-security",
    "iam": "alerts-security",
    "sso": "alerts-security",
    "cloudtrail": "alerts-security",
}


def _build_text(
    *,
    alert_type: str,
    severity: str,
    service: str,
    summary: str,
    symptom: str,
) -> str:
    """Render an alert body that looks like the real alert type."""
    now = datetime.now(timezone.utc).strftime("%H:%M UTC")
    if alert_type == "pagerduty":
        return (
            f":fire: [{severity}] {service} — {summary}. "
            f"Triggered: {now}. {symptom}"
        )
    if alert_type == "datadog":
        return (
            f":rotating_light: [{severity}] {service} — {summary} "
            f"(triggered {now}). {symptom} "
            f"service={service} env=prod"
        )
    if alert_type == "sentry":
        return (
            f":bug: [New issue] {service} — {summary} "
            f"(first seen {now}). {symptom}"
        )
    if alert_type == "statuspage":
        return (
            f":traffic_light: [Upstream] {service} — {summary} "
            f"(posted {now}). {symptom}"
        )
    return f"[{severity}] {service} — {summary}. {symptom}"


def _default_symptom(service: str) -> str:
    return {
        "checkout-api": "5xx rate > 2% for 3m. p99 latency spiking. **unacked**",
        "orders-api": "5xx rate > 1.5% for 5m. request queue building up.",
        "user-service": "CrashLoopBackoff on 2 of 8 pods. configmap may be stale.",
        "ingest-pipeline": "lag p95 > 60s for 8m. snowflake COPY INTO queue > 50.",
        "reporting-worker": "job runtime 3x baseline. queue length climbing.",
        "dbt": "model `fct_orders_daily` run FAILED. upstream column error.",
        "snowflake": "warehouse `ANALYTICS_WH` suspended. human action required.",
        "auth": "15 failed SSO logins from one user in 5m.",
        "iam": "new IAM user created outside terraform. cloudtrail alert.",
    }.get(service, "metric threshold exceeded. **unacked**")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Inject one alert into the test env Slack workspace.",
    )
    parser.add_argument("--tenant", required=True)
    parser.add_argument(
        "--type",
        choices=sorted(ALERT_TYPES),
        default="pagerduty",
        help="Which fake alert bot to impersonate.",
    )
    parser.add_argument(
        "--severity",
        default="P3",
        help="Severity label (P1/P2/P3). Used literally in the text.",
    )
    parser.add_argument(
        "--service",
        default="checkout-api",
        help="Service name — determines the channel via SERVICE_TO_CHANNEL.",
    )
    parser.add_argument(
        "--summary",
        default=None,
        help="Short summary (fallback: derived from --service).",
    )
    parser.add_argument(
        "--symptom",
        default=None,
        help="Symptom line (fallback: derived from --service).",
    )
    parser.add_argument(
        "--channel",
        default=None,
        help="Override channel name (default: derived from --service).",
    )
    parser.add_argument("--region", default=None)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    configure_logging(args.verbose)

    # Resolve the channel
    channel_name = args.channel or SERVICE_TO_CHANNEL.get(args.service, "alerts-sre")

    # Build the body
    summary = args.summary or f"{args.service} health degraded"
    symptom = args.symptom or _default_symptom(args.service)
    text = _build_text(
        alert_type=args.type,
        severity=args.severity,
        service=args.service,
        summary=summary,
        symptom=symptom,
    )

    # Load auth + Slack client
    try:
        bot_token = load_seeder_bot_token()
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    client = make_slack_client(bot_token)
    state = SeederState(args.tenant)

    channel_map, missing = discover_and_join(client, state)
    if channel_name not in channel_map:
        print(
            f"error: channel #{channel_name} not found in workspace "
            f"(missing: {missing}). run bootstrap.py first.",
            file=sys.stderr,
        )
        return 1

    p = persona(args.type)
    poster = RateLimitedPoster(client)
    response = poster.post(
        channel=channel_map[channel_name],
        text=text,
        username=p.username,
        icon_emoji=p.icon_emoji,
    )
    ts = response.get("ts", "")
    channel_id = channel_map[channel_name]

    # Pull the permalink so the user can click straight to the message.
    try:
        perma = client.chat_getPermalink(channel=channel_id, message_ts=ts)
        permalink = perma.get("permalink") or ""
    except Exception:  # noqa: BLE001
        permalink = ""

    print(f"\n✓ posted {args.type} alert to #{channel_name}")
    print(f"  {text}\n")
    if permalink:
        print(f"  → {permalink}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
