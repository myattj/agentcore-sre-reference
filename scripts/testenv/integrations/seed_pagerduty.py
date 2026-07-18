#!/usr/bin/env python3
"""PagerDuty seeder.

Connects PagerDuty to the test tenant + seeds realistic content:

  - 3 escalation policies (sre-oncall, data-oncall, security-oncall)
  - 5 services (checkout-api, orders-api, user-service,
    ingest-pipeline, reporting-worker)
  - ~20 incidents: 12 historical/resolved, 6 currently-triggered, 2 acked

The unresolved incidents are intentional — when you manually drive
the agent against the test workspace, asking "any open pages?" will
surface them.

Secret shape at ``agentcore/testenv/pagerduty``:

    {"api_key": "<pagerduty REST API key>"}

Get the API key from Profile → User Settings → API Access Keys.

Because PagerDuty requires you to be logged in as a real user to
create incidents via the public API (it needs a ``From`` header with
your email), the secret may include an optional ``from_email`` key.
If omitted, the seeder tries to read it from the first user on the
account.

Usage:
    python -m scripts.testenv.integrations.seed_pagerduty --tenant slack-t0xxxxxxxxx
"""
from __future__ import annotations

import argparse
import sys
import time
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


_BASE_URL = "https://api.pagerduty.com"


# ----------------------------------------------------------------------------
# Seed content
# ----------------------------------------------------------------------------

_ESCALATION_POLICIES: list[dict[str, Any]] = [
    {
        "name": "Acme SRE oncall",
        "description": "SRE on-call rotation — infra, RDS, EKS, checkout-api, orders-api, user-service pages route here.",
        "num_loops": 2,
    },
    {
        "name": "Acme Data oncall",
        "description": "Data engineering on-call — Snowflake, dbt, ingest-pipeline, reporting-worker pages route here.",
        "num_loops": 2,
    },
    {
        "name": "Acme Security oncall",
        "description": "Security on-call — auth, SSO, IAM, cloudtrail alerts route here.",
        "num_loops": 3,
    },
]

_SERVICES: list[dict[str, Any]] = [
    {
        "name": "checkout-api",
        "description": "Checkout API service. Payment provider integration, retry-backoff critical path.",
        "escalation_policy_name": "Acme SRE oncall",
    },
    {
        "name": "orders-api",
        "description": "Orders API service. Order lifecycle, downstream from checkout-api.",
        "escalation_policy_name": "Acme SRE oncall",
    },
    {
        "name": "user-service",
        "description": "User service. Auth, profile, signup flows.",
        "escalation_policy_name": "Acme SRE oncall",
    },
    {
        "name": "ingest-pipeline",
        "description": "Event ingestion pipeline. Kafka → S3 → Snowflake COPY INTO.",
        "escalation_policy_name": "Acme Data oncall",
    },
    {
        "name": "reporting-worker",
        "description": "Reporting worker. Daily rollups, dbt model execution, metric exports.",
        "escalation_policy_name": "Acme Data oncall",
    },
]

# Incidents: (service_name, title, body, urgency, status).
# status: "triggered" (unacked), "acknowledged" (in-progress), "resolved"
_INCIDENTS: list[dict[str, Any]] = [
    # ----- Feb checkout retry storm (historical, resolved) -----
    {
        "service": "checkout-api", "urgency": "high", "status": "resolved",
        "title": "checkout-api 5xx > 2% — retry storm after payment provider recovery",
        "body": "Payment provider had ~90s outage. Our retry policy sent 3x traffic on recovery and tipped them over again. Fixed in v2.18.3 (exponential backoff with jitter).",
    },
    {
        "service": "checkout-api", "urgency": "high", "status": "resolved",
        "title": "checkout-api p99 latency > 1.2s",
        "body": "Latency spike correlated with payment provider degradation (their gateway at 'degraded' on status page). Circuit breaker threshold adjusted 5s → 3s.",
    },
    # ----- Ingest pipeline ongoing (some resolved, one still triggered) -----
    {
        "service": "ingest-pipeline", "urgency": "low", "status": "triggered",
        "title": "ingest-pipeline lag p95 > 60s",
        "body": "COPY INTO contention from finance extract workload. PR #92 on acme-infra splits the warehouse; cutover scheduled.",
    },
    {
        "service": "ingest-pipeline", "urgency": "low", "status": "resolved",
        "title": "ingest-pipeline s3 write rate dropped to 0 for 3m",
        "body": "Upstream kafka broker restarted during rolling update. Self-recovered within 3 minutes.",
    },
    # ----- Orders dbt regression (resolved) -----
    {
        "service": "reporting-worker", "urgency": "high", "status": "resolved",
        "title": "fct_orders_daily row count -40% vs baseline",
        "body": "Finance ETL migration renamed upstream column. Silent null-drop in stg_orders dropped rows. Fixed in acme-data-api PR #434 + 7-day backfill.",
    },
    # ----- Routine alerts -----
    {
        "service": "orders-api", "urgency": "high", "status": "resolved",
        "title": "orders-api 5xx rate > 1.5% — flapping pod",
        "body": "orders-api-7d9c4b5f6-xk8n2 stuck in CrashLoopBackoff after deploy. Killed manually, deployment auto-replaced.",
    },
    {
        "service": "user-service", "urgency": "high", "status": "acknowledged",
        "title": "user-service 5xx rate > 1% for 2m",
        "body": "Investigating. Possible correlation with recent configmap rollback.",
    },
    {
        "service": "checkout-api", "urgency": "high", "status": "triggered",
        "title": "checkout-api p95 latency > 500ms for 10m",
        "body": "Latency elevated, baseline 120ms, current 612ms. Not yet escalated.",
    },
    {
        "service": "reporting-worker", "urgency": "low", "status": "triggered",
        "title": "reporting-worker queue length > 500",
        "body": "842 jobs pending. Backing up since 09:00. Correlated with Snowflake warehouse contention.",
    },
    {
        "service": "ingest-pipeline", "urgency": "low", "status": "triggered",
        "title": "dbt cloud: int_users_daily SLA missed",
        "body": "Expected 04:00 UTC, ran 05:14 UTC. Investigating whether it's the same COPY INTO contention.",
    },
    {
        "service": "checkout-api", "urgency": "high", "status": "resolved",
        "title": "user-service pod CrashLoopBackoff (1 of 8)",
        "body": "Stale configmap mounted after rollback. Forced rollout restart, pod healthy.",
    },
    {
        "service": "orders-api", "urgency": "low", "status": "resolved",
        "title": "RDS prod connection count > 400 (max 500)",
        "body": "pool_size=25 override in checkout-api config. Fixed in PR #423.",
    },
    {
        "service": "ingest-pipeline", "urgency": "low", "status": "resolved",
        "title": "Snowflake warehouse ANALYTICS_WH suspended",
        "body": "Auto-suspend with auto_resume=false. Resolved by ALTER WAREHOUSE SET AUTO_RESUME = TRUE.",
    },
    {
        "service": "reporting-worker", "urgency": "low", "status": "resolved",
        "title": "daily_metrics_rollup runtime 38min (threshold 20min)",
        "body": "Snowflake warehouse contention, not a worker issue. Queue backed up since 09:00.",
    },
    {
        "service": "user-service", "urgency": "high", "status": "triggered",
        "title": "KeyError: 'payment_method' in transform_orders()",
        "body": "14 events in 5m. Upstream orders data missing payment_method for a handful of rows starting today.",
    },
    {
        "service": "checkout-api", "urgency": "low", "status": "triggered",
        "title": "checkout-api memory > 85% on 3 of 20 pods",
        "body": "Expected during retry backfill run. Known behavior; will self-resolve.",
    },
    {
        "service": "orders-api", "urgency": "low", "status": "resolved",
        "title": "ALB target group checkout-api-tg: 2 of 20 unhealthy",
        "body": "Same two pods from earlier crashloopbackoff. Rejoined within 90s.",
    },
    {
        "service": "ingest-pipeline", "urgency": "high", "status": "acknowledged",
        "title": "dbt cloud: fct_orders_daily run FAILED",
        "body": "SQL compilation error: Table UPSTREAM_ORDERS does not exist. Fix in acme-data-api PR #431.",
    },
    {
        "service": "checkout-api", "urgency": "high", "status": "resolved",
        "title": "EKS node ip-10-0-14-182 cpu > 90% for 10m",
        "body": "reporting-worker pod stuck in retry loop. Drained and rescheduled.",
    },
    {
        "service": "user-service", "urgency": "low", "status": "resolved",
        "title": "Sentry regression: IntegrityError on POST /users",
        "body": "Front-end retry re-POSTing on success. Frontend fix in flight. Not a backend bug.",
    },
]


# ----------------------------------------------------------------------------
# Seeder
# ----------------------------------------------------------------------------

def _pd_headers(api_key: str, from_email: str | None = None) -> dict[str, str]:
    h = {
        "Authorization": f"Token token={api_key}",
        "Accept": "application/vnd.pagerduty+json;version=2",
        "Content-Type": "application/json",
    }
    if from_email:
        h["From"] = from_email
    return h


def _get_first_user_email(client: RateLimitedClient) -> str | None:
    r = client.get("/users", params={"limit": 1})
    if r.status_code != 200:
        return None
    users = (r.json() or {}).get("users") or []
    if not users:
        return None
    return users[0].get("email")


def _list_escalation_policies(client: RateLimitedClient) -> list[dict[str, Any]]:
    r = client.get("/escalation_policies", params={"limit": 100})
    if r.status_code != 200:
        return []
    return (r.json() or {}).get("escalation_policies") or []


def _create_escalation_policy(
    client: RateLimitedClient,
    *,
    name: str,
    description: str,
    num_loops: int,
    first_user_id: str | None,
) -> str | None:
    # An escalation policy needs at least one rule with a target. Use
    # the first user on the account as the target for all rules. In a
    # real setup you'd map this to per-team on-call schedules.
    if not first_user_id:
        return None
    body = {
        "escalation_policy": {
            "type": "escalation_policy",
            "name": name,
            "description": description,
            "num_loops": num_loops,
            "escalation_rules": [
                {
                    "escalation_delay_in_minutes": 10,
                    "targets": [{"id": first_user_id, "type": "user_reference"}],
                }
            ],
        }
    }
    r = client.post("/escalation_policies", json=body)
    if r.status_code not in (200, 201):
        return None
    return (r.json() or {}).get("escalation_policy", {}).get("id")


def _list_services(client: RateLimitedClient) -> list[dict[str, Any]]:
    r = client.get("/services", params={"limit": 100})
    if r.status_code != 200:
        return []
    return (r.json() or {}).get("services") or []


def _create_service(
    client: RateLimitedClient,
    *,
    name: str,
    description: str,
    escalation_policy_id: str,
) -> str | None:
    body = {
        "service": {
            "type": "service",
            "name": name,
            "description": description,
            "escalation_policy": {
                "id": escalation_policy_id,
                "type": "escalation_policy_reference",
            },
            "alert_creation": "create_alerts_and_incidents",
        }
    }
    r = client.post("/services", json=body)
    if r.status_code not in (200, 201):
        return None
    return (r.json() or {}).get("service", {}).get("id")


def _get_first_user_id(client: RateLimitedClient) -> str | None:
    r = client.get("/users", params={"limit": 1})
    if r.status_code != 200:
        return None
    users = (r.json() or {}).get("users") or []
    return users[0].get("id") if users else None


def _create_incident(
    client: RateLimitedClient,
    *,
    service_id: str,
    title: str,
    body: str,
    urgency: str,
    from_email: str,
) -> str | None:
    payload = {
        "incident": {
            "type": "incident",
            "title": title,
            "service": {"id": service_id, "type": "service_reference"},
            "urgency": urgency,
            "body": {"type": "incident_body", "details": body},
        }
    }
    r = client.post(
        "/incidents",
        json=payload,
        headers={"From": from_email},
    )
    if r.status_code not in (200, 201):
        return None
    return (r.json() or {}).get("incident", {}).get("id")


def _transition_incident(
    client: RateLimitedClient,
    *,
    incident_id: str,
    status: str,  # "acknowledged" or "resolved"
    from_email: str,
) -> bool:
    body = {"incident": {"type": "incident_reference", "status": status}}
    r = client.put(
        f"/incidents/{incident_id}",
        json=body,
        headers={"From": from_email},
    )
    return r.status_code in (200, 201)


def run_seed(
    tenant_id: str,
    *,
    region: str | None = None,
    bridge_url: str | None = None,
    skip_connect: bool = False,
    skip_seed: bool = False,
    force: bool = False,
) -> int:
    # ---- 1. Credentials ----
    step("Loading PagerDuty credentials from Secrets Manager")
    try:
        creds = load_integration_secret(
            "pagerduty", region=region, required_keys=["api_key"]
        )
    except RuntimeError as e:
        err(str(e))
        return 1
    ok("creds loaded")

    # ---- 2. Bridge connect ----
    if skip_connect:
        warn("--skip-connect: skipping bridge integration connect")
    else:
        step(f"Connecting PagerDuty to tenant {tenant_id} via bridge")
        try:
            resp = bridge_connect_integration(
                tenant_id,
                "pagerduty",
                body={"api_key": creds["api_key"]},
                bridge_url=bridge_url,
                region=region,
            )
        except RuntimeError as e:
            err(str(e))
            return 1
        ok(f"gateway target ready: {resp.get('target_name')}")

    if skip_seed:
        warn("--skip-seed: skipping content seed")
        return 0

    # ---- 3. Seed ----
    step("Seeding PagerDuty escalation policies, services, and incidents")
    client = RateLimitedClient(
        base_url=_BASE_URL,
        headers=_pd_headers(creds["api_key"]),
        min_interval_s=0.6,  # PD public API rate limit is ~960 req/min
    )

    try:
        # Resolve a "from" email (PagerDuty requires it for incident ops)
        from_email = creds.get("from_email") or _get_first_user_email(client)
        if not from_email:
            err("could not resolve a From email — add `from_email` to the secret or ensure the account has at least one user")
            return 1
        grey(f"  from email: {from_email}")

        # Get first user id for escalation policy rules
        first_user_id = _get_first_user_id(client)
        if not first_user_id:
            err("could not resolve a user id for escalation rules")
            return 1

        state = load_seeded_state("pagerduty")

        # --- Escalation policies (idempotent by name) ---
        existing_policies = {p.get("name"): p for p in _list_escalation_policies(client)}
        policy_ids: dict[str, str] = {}
        for ep in _ESCALATION_POLICIES:
            existing = existing_policies.get(ep["name"])
            if existing and not force:
                policy_ids[ep["name"]] = existing.get("id", "")
                grey(f"  escalation policy (reused): {ep['name']}")
                continue
            pid = _create_escalation_policy(
                client,
                name=ep["name"],
                description=ep["description"],
                num_loops=ep["num_loops"],
                first_user_id=first_user_id,
            )
            if pid:
                policy_ids[ep["name"]] = pid
                grey(f"  escalation policy: {ep['name']}")
            else:
                warn(f"  escalation policy failed: {ep['name']}")

        # --- Services (idempotent by name) ---
        existing_services = {s.get("name"): s for s in _list_services(client)}
        service_ids: dict[str, str] = {}
        for svc in _SERVICES:
            existing = existing_services.get(svc["name"])
            if existing and not force:
                service_ids[svc["name"]] = existing.get("id", "")
                grey(f"  service (reused): {svc['name']}")
                continue
            ep_name = svc["escalation_policy_name"]
            ep_id = policy_ids.get(ep_name)
            if not ep_id:
                warn(f"  service {svc['name']} needs missing policy {ep_name}, skipping")
                continue
            sid = _create_service(
                client,
                name=svc["name"],
                description=svc["description"],
                escalation_policy_id=ep_id,
            )
            if sid:
                service_ids[svc["name"]] = sid
                grey(f"  service: {svc['name']}")

        ok(f"{len(policy_ids)} policies, {len(service_ids)} services ready")

        # --- Incidents ---
        if state.get("incidents") and not force:
            warn(f"found existing state ({len(state['incidents'])} seeded incidents) — pass --force to re-seed")
        else:
            posted: list[str] = []
            for i, incident in enumerate(_INCIDENTS, 1):
                svc_id = service_ids.get(incident["service"])
                if not svc_id:
                    continue
                iid = _create_incident(
                    client,
                    service_id=svc_id,
                    title=incident["title"],
                    body=incident["body"],
                    urgency=incident["urgency"],
                    from_email=from_email,
                )
                if not iid:
                    warn(f"  incident {i} failed: {incident['title'][:60]}")
                    continue
                posted.append(iid)
                if incident["status"] in ("acknowledged", "resolved"):
                    _transition_incident(
                        client,
                        incident_id=iid,
                        status=incident["status"],
                        from_email=from_email,
                    )
                grey(f"  incident {i}/{len(_INCIDENTS)} [{incident['status']}]: {incident['title'][:60]}")
            ok(f"{len(posted)}/{len(_INCIDENTS)} incidents created")
            state["incidents"] = posted
            state["last_run"] = int(time.time())
            save_seeded_state("pagerduty", state)

    finally:
        client.close()

    step("PagerDuty seed complete")
    grey("  open https://<your-subdomain>.pagerduty.com/incidents to verify")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed PagerDuty for the Agent test env.")
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
