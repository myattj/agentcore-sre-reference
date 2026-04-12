"""Alert stream — PagerDuty / Datadog / Sentry / Statuspage alerts.

~35 standalone alerts across ``#alerts-sre``, ``#alerts-data``, and
``#alerts-security``. About half have threaded human acks and
follow-ups (e.g. "looking"), mirroring what a real alert channel
looks like mid-week.

The alerts are seeded WITHOUT blocks for simplicity — plain text is
fine for the agent's ``search_team_history`` to index, and plain text
renders just as legibly in Slack as blocks do for short alerts.
Custom usernames from ``chat:write.customize`` make them visually
distinct from human messages in the channel sidebar.

Severity distribution: 60% SEV-3, 30% SEV-2, 10% SEV-1. Most alerts
self-resolve or are triaged and closed; a couple are unresolved, for
the user to drive triage against when testing.
"""
from __future__ import annotations

from .._common import SeedMessage


def build() -> list[SeedMessage]:
    m: list[SeedMessage] = []

    # ------- #alerts-sre ---------------------------------------------------

    m.append(SeedMessage("al_001", "alerts-sre", "datadog",
        ":rotating_light: [P3] checkout-api p99 latency > 800ms for 5m (us-west-2) — current: 1.24s, 10x baseline. service=checkout-api env=prod"))
    m.append(SeedMessage("al_001a", "alerts-sre", "morgan", 
        "looking. just deployed v2.18.3, checking if it's related", parent_key="al_001"))
    m.append(SeedMessage("al_001b", "alerts-sre", "morgan", 
        "ok, not the deploy — it's a spike in /checkout/confirm endpoint, payment provider returning slow. draining traffic via the circuit breaker", parent_key="al_001"))
    m.append(SeedMessage("al_001c", "alerts-sre", "datadog", 
        ":white_check_mark: [RECOVERED] checkout-api p99 latency back under 200ms", parent_key="al_001"))

    m.append(SeedMessage("al_002", "alerts-sre", "pagerduty",
        ":fire: [P2] high error rate on orders-api — 5xx rate > 2% for 3m. triggered: 14:22 PT. ack?"))
    m.append(SeedMessage("al_002a", "alerts-sre", "taylor", 
        "acking, investigating", parent_key="al_002"))
    m.append(SeedMessage("al_002b", "alerts-sre", "taylor", 
        "looks like a bad pod after the latest deploy — `orders-api-7d9c4b5f6-xk8n2` is flapping. killed it, deployment is auto-replacing. watching", parent_key="al_002"))
    m.append(SeedMessage("al_002c", "alerts-sre", "pagerduty", 
        ":white_check_mark: [RESOLVED] orders-api error rate < 0.1%, closed", parent_key="al_002"))

    m.append(SeedMessage("al_003", "alerts-sre", "datadog",
        ":rotating_light: [P3] EKS node `ip-10-0-14-182.us-west-2.compute.internal` cpu > 90% for 10m"))
    m.append(SeedMessage("al_003a", "alerts-sre", "riley", 
        "it's the reporting-worker pods scheduled on that node, one is stuck in a retry loop. I'll drain + reschedule", parent_key="al_003"))

    m.append(SeedMessage("al_004", "alerts-sre", "pagerduty",
        ":fire: [P2] checkout-api 5xx spike — 3.2% for 2m (normal: 0.05%). triggered: 09:47 PT"))
    m.append(SeedMessage("al_004a", "alerts-sre", "morgan", 
        "acking", parent_key="al_004"))
    m.append(SeedMessage("al_004b", "alerts-sre", "morgan", 
        "downstream payment provider returned 503s for about 90s then recovered. our retry policy absorbed it but the pre-retry 5xx bumped the alert. expected after the retry-backoff fix but this is still noisy. will tune the alert threshold", parent_key="al_004"))

    m.append(SeedMessage("al_005", "alerts-sre", "datadog",
        ":warning: [P3] RDS prod — connection count > 400 (max: 500) for 5m"))
    m.append(SeedMessage("al_005a", "alerts-sre", "morgan", 
        "this is the pool_size=25 issue sam mentioned in #ask-platform. tracked as PR #423 on acme-data-api, deploying as soon as CI passes", parent_key="al_005"))

    m.append(SeedMessage("al_006", "alerts-sre", "pagerduty",
        ":fire: [P3] user-service pod CrashLoopBackoff in prod. count: 1 of 8. triggered: 11:31 PT"))
    m.append(SeedMessage("al_006a", "alerts-sre", "taylor", 
        "looking", parent_key="al_006"))
    m.append(SeedMessage("al_006b", "alerts-sre", "taylor", 
        "config map was mounted stale from a recent rollback — forced rollout restart, back to healthy", parent_key="al_006"))

    m.append(SeedMessage("al_007", "alerts-sre", "sentry",
        ":bug: [New issue] `IntegrityError: duplicate key value violates unique constraint \"users_email_key\"` in user-service POST /users (2 events/min)"))
    m.append(SeedMessage("al_007a", "alerts-sre", "riley", 
        "looks like our front-end retry is re-POSTing on success, not a backend bug. coordinating with frontend on the fix, not oncall-urgent", parent_key="al_007"))

    m.append(SeedMessage("al_008", "alerts-sre", "statuspage",
        ":traffic_light: [AWS] RDS service in us-west-2 — status: investigating. No impact on our instances yet."))
    m.append(SeedMessage("al_008a", "alerts-sre", "morgan", 
        "watching. no impact to us, prod rds is running clean", parent_key="al_008"))

    m.append(SeedMessage("al_009", "alerts-sre", "datadog",
        ":rotating_light: [P3] checkout-api memory > 85% (threshold 80%) on 3 of 20 pods for 8m"))
    m.append(SeedMessage("al_009a", "alerts-sre", "morgan", 
        "expected during the retry backfill run. this is a known thing, runbook is `checkout-api-memory-pressure.md`. will self-resolve within 20 min", parent_key="al_009"))

    m.append(SeedMessage("al_010", "alerts-sre", "pagerduty",
        ":fire: [P3] ALB target group `checkout-api-tg` — 2 of 20 targets unhealthy"))
    m.append(SeedMessage("al_010a", "alerts-sre", "taylor", 
        "same two pods from the crashloopbackoff earlier, still coming back. they'll rejoin in ~90s", parent_key="al_010"))

    # Unresolved — for the user to triage manually
    m.append(SeedMessage("al_011", "alerts-sre", "datadog",
        ":rotating_light: [P3] checkout-api p95 latency > 500ms for 10m (us-west-2). current: 612ms, baseline: 120ms. service=checkout-api env=prod region=us-west-2"))
    m.append(SeedMessage("al_011a", "alerts-sre", "datadog", 
        ":warning: escalating to P2 — latency still elevated after 10m", parent_key="al_011"))

    m.append(SeedMessage("al_012", "alerts-sre", "pagerduty",
        ":fire: [P2] user-service 5xx rate > 1% for 2m. triggered: 15:08 PT. **unacked**"))

    # ------- #alerts-data --------------------------------------------------

    m.append(SeedMessage("al_020", "alerts-data", "datadog",
        ":warning: [P3] ingest-pipeline — snowflake COPY INTO queue depth > 50 for 10m (was: avg 8)"))
    m.append(SeedMessage("al_020a", "alerts-data", "priya", 
        "known issue, see thread in this channel from thursday. investigating with snowflake support; hard problem to repro", parent_key="al_020"))

    m.append(SeedMessage("al_021", "alerts-data", "pagerduty",
        ":fire: [P2] dbt cloud — `fct_orders_daily` run FAILED (exit 2). error: 'Database Error: SQL compilation error: Table UPSTREAM_ORDERS does not exist'"))
    m.append(SeedMessage("al_021a", "alerts-data", "priya", 
        "the upstream table was renamed yesterday as part of the finance ETL migration and I missed updating the ref. PR incoming", parent_key="al_021"))
    m.append(SeedMessage("al_021b", "alerts-data", "priya", 
        ":point_up: merged — `acme-data-api` PR #431. re-running the model now", parent_key="al_021"))

    m.append(SeedMessage("al_022", "alerts-data", "datadog",
        ":rotating_light: [P3] reporting-worker job `daily_metrics_rollup` — runtime 38 min (threshold: 20 min). last 3 runs all exceeded."))
    m.append(SeedMessage("al_022a", "alerts-data", "priya", 
        "snowflake warehouse is hot today, this is not a worker issue. the autosuspend logic is working but the warehouse queue has been backed up since 09:00", parent_key="al_022"))

    m.append(SeedMessage("al_023", "alerts-data", "sentry",
        ":bug: [New issue] `KeyError: 'payment_method'` in reporting-worker `transform_orders()` (14 events in 5m)"))
    m.append(SeedMessage("al_023a", "alerts-data", "jordan", 
        "upstream orders data is missing payment_method for a handful of rows starting today. looking at whether it's a data quality issue or a schema change", parent_key="al_023"))

    m.append(SeedMessage("al_024", "alerts-data", "pagerduty",
        ":fire: [P3] ingest-pipeline — s3 write rate dropped to 0 for 3m (normal: 200 events/s)"))
    m.append(SeedMessage("al_024a", "alerts-data", "priya", 
        "the upstream kafka broker restarted during a rolling update, back to normal already", parent_key="al_024"))

    m.append(SeedMessage("al_025", "alerts-data", "datadog",
        ":warning: [P3] dbt model `stg_users` snowflake credits consumed > 50 (threshold: 30) in last run"))
    m.append(SeedMessage("al_025a", "alerts-data", "priya", 
        "the incremental strategy is scanning too much — I have a fix in draft to narrow the `unique_key` check", parent_key="al_025"))

    m.append(SeedMessage("al_026", "alerts-data", "pagerduty",
        ":fire: [P3] snowflake warehouse `ANALYTICS_WH` — suspended automatically after 12h usage. human action required."))
    m.append(SeedMessage("al_026a", "alerts-data", "priya", 
        "this is the billing guardrail we set up post-Q1. I'll resume it and take a look at what's been running", parent_key="al_026"))

    m.append(SeedMessage("al_027", "alerts-data", "sentry",
        ":bug: [Regression] `fct_orders_daily` row count -40% vs 7-day avg"))
    m.append(SeedMessage("al_027a", "alerts-data", "priya", 
        "this IS concerning. investigating — the finance ETL migration yesterday may have lost data. started a thread in #incidents", parent_key="al_027"))

    # Unresolved — for user triage
    m.append(SeedMessage("al_028", "alerts-data", "datadog",
        ":rotating_light: [P2] reporting-worker queue length > 500 jobs (threshold: 100) for 15m. current: 842 pending. **unacked**"))
    m.append(SeedMessage("al_029", "alerts-data", "pagerduty",
        ":fire: [P3] dbt cloud — `int_users_daily` SLA missed (expected 04:00 UTC, ran 05:14 UTC). **unacked**"))

    # ------- #alerts-security ---------------------------------------------

    m.append(SeedMessage("al_040", "alerts-security", "pagerduty",
        ":fire: [P2] Cloudtrail — unusual root account login from IP 52.88.14.201 (not in office ranges). AWS account: acme-prod"))
    m.append(SeedMessage("al_040a", "alerts-security", "alex", 
        "acking. confirming that this is a planned vendor audit login, not an intrusion. vendor is aws support on a case we opened yesterday, confirmed with morgan out-of-band", parent_key="al_040"))
    m.append(SeedMessage("al_040b", "alerts-security", "alex", 
        "also: we should be rotating root access keys after this. adding to the quarterly review checklist", parent_key="al_040"))

    m.append(SeedMessage("al_041", "alerts-security", "datadog",
        ":warning: [P3] 15 failed SSO logins from user `contractor.jm@acme.co` in the last 5m"))
    m.append(SeedMessage("al_041a", "alerts-security", "alex", 
        "contractor is getting off an old laptop. talked to them directly. temp lockout expiring in 30m, they'll self-recover", parent_key="al_041"))

    m.append(SeedMessage("al_042", "alerts-security", "sentry",
        ":bug: [Regression] 3 requests returning 401 from `/api/admin/tenants` in user-service — unusual (baseline: 0)"))
    m.append(SeedMessage("al_042a", "alerts-security", "alex", 
        "looking — the admin API is only supposed to accept internal service tokens. 3 unauth requests is weird. pulling the access logs", parent_key="al_042"))
    m.append(SeedMessage("al_042b", "alerts-security", "alex", 
        "false alarm — it's the new tenant-migration script running from jordan's laptop on an expired session token. investigated, closed, no further action", parent_key="al_042"))

    m.append(SeedMessage("al_043", "alerts-security", "pagerduty",
        ":fire: [P2] cloudtrail — new IAM user created in acme-prod: `migration-temp-2026-04-11`. not in terraform state."))
    m.append(SeedMessage("al_043a", "alerts-security", "alex", 
        "this IS planned — sam is running the tenant-migration script that needs a temp IAM user for S3 access. planned to live for <2h. will clean up after, tracked in ticket SEC-412", parent_key="al_043"))

    # Unresolved — for user triage
    m.append(SeedMessage("al_044", "alerts-security", "datadog",
        ":rotating_light: [P2] 40+ password reset requests from single IP (104.22.18.55) in the last 10m. not a known office IP. **unacked**"))

    return m
