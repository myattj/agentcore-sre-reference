"""Runbook-lookup threads.

Ten threads where someone asks about a specific runbook and a teammate
either links the runbook or summarizes the key steps. These are
designed to feed the ``/runbook`` skill — when the user invokes it
later, the agent's ``search_team_history`` will surface these threads
as recent mentions of the runbook, alongside the real file content
from ``acme-runbooks``.

Every thread namespaces the runbook filename (e.g.
``rds-password-rotation.md``) so the agent can link to it from both
history and ``code_search`` results.
"""
from __future__ import annotations

from .._common import SeedMessage


def build() -> list[SeedMessage]:
    m: list[SeedMessage] = []

    # rb_001: RDS password rotation ------------------------------------------
    m.append(SeedMessage("rb_001", "ask-platform", "jordan",
        "where's the runbook for rotating the prod RDS password? I remember we talked about sequencing with the checkout-api deploy"))
    m.append(SeedMessage("rb_001a", "ask-platform", "morgan", 
        "`acme-runbooks/security/rds-password-rotation.md`. sequencing: (1) update secrets manager with new password and a NEW version stage, (2) trigger checkout-api rolling deploy so new pods pick up the new secret, (3) verify with a canary request, (4) promote the new secret version to AWSCURRENT, (5) wait 24h, (6) delete the old version", parent_key="rb_001"))
    m.append(SeedMessage("rb_001b", "ask-platform", "alex", 
        "+1 and remember to update terraform state AFTER step (4), not before — we had a drift issue last time", parent_key="rb_001"))

    # rb_002: EKS upgrade ----------------------------------------------------
    m.append(SeedMessage("rb_002", "ask-platform", "sam",
        "is there an eks version upgrade runbook somewhere? we're doing 1.28 → 1.29 next Tuesday"))
    m.append(SeedMessage("rb_002a", "ask-platform", "riley", 
        "`acme-runbooks/infra/eks-version-upgrade.md`. high-level: (1) read the k8s release notes, (2) check deprecated APIs via `kube-no-trouble`, (3) update the terraform for the control plane, (4) apply the control plane update (it's online, takes ~30 min), (5) node-group rolling update, (6) smoke test with the `platform-smoke` helm chart", parent_key="rb_002"))
    m.append(SeedMessage("rb_002b", "ask-platform", "riley", 
        "the runbook has the rollback procedure too in case any pod fails the smoke test", parent_key="rb_002"))

    # rb_003: Snowflake warehouse suspended ----------------------------------
    m.append(SeedMessage("rb_003", "ask-data", "jamie",
        "I just got a dbt run failure saying the warehouse is suspended — is there a runbook for that?"))
    m.append(SeedMessage("rb_003a", "ask-data", "priya", 
        "`acme-runbooks/data/snowflake-warehouse-suspended.md`. most common cause: auto-suspend kicked in and the auto-resume setting was off on that warehouse. fix: `ALTER WAREHOUSE <name> SET AUTO_RESUME = TRUE`. it's on for all our prod warehouses but someone flipped it for debugging once and forgot", parent_key="rb_003"))
    m.append(SeedMessage("rb_003b", "ask-data", "priya", 
        "the runbook also covers the credits-exhausted case (which is different and worse)", parent_key="rb_003"))

    # rb_004: Deploy rollback ------------------------------------------------
    m.append(SeedMessage("rb_004", "ask-platform", "taylor",
        "how do I rollback a checkout-api deploy if something breaks. asking for a friend who is me on oncall"))
    m.append(SeedMessage("rb_004a", "ask-platform", "morgan", 
        "`acme-runbooks/deploys/deploy-rollback.md`. two options: (1) GitHub Actions workflow `rollback-deploy` which re-tags the previous image and triggers a deploy (fast, ~3 min), (2) `kubectl rollout undo deployment/checkout-api -n prod` (faster, ~30 sec, but doesn't update the git tag so it's only a quick stopgap)", parent_key="rb_004"))
    m.append(SeedMessage("rb_004b", "ask-platform", "morgan", 
        "if it's bad enough to rollback, use option (1). option (2) is for 'oh shit' moments only", parent_key="rb_004"))

    # rb_005: On-call handoff ------------------------------------------------
    m.append(SeedMessage("rb_005", "oncall", "jordan",
        "first time on-call next week — is there a handoff runbook"))
    m.append(SeedMessage("rb_005a", "oncall", "morgan", 
        "`acme-runbooks/oncall/handoff.md`. monday morning checklist: (1) read the last week's oncall thread in this channel, (2) check datadog dashboards for open alerts, (3) ack your current pager assignment in pagerduty, (4) post the standard handoff message. the runbook has the full checklist", parent_key="rb_005"))
    m.append(SeedMessage("rb_005b", "oncall", "morgan", 
        "also: don't feel bad paging the secondary. that's what they're for", parent_key="rb_005"))

    # rb_006: Incident kickoff -----------------------------------------------
    m.append(SeedMessage("rb_006", "incidents", "priya",
        "for anyone new: `acme-runbooks/incidents/kickoff.md` is the incident kickoff runbook. it walks you through the first 10 minutes — who to page, what to post, where the status dashboard is. bookmarking this is worth your time"))

    # rb_007: Database slow query investigation ------------------------------
    m.append(SeedMessage("rb_007", "ask-platform", "sam",
        "slow query on rds-prod at 14:03, the datadog APM alert fired. runbook for how to investigate?"))
    m.append(SeedMessage("rb_007a", "ask-platform", "morgan", 
        "`acme-runbooks/infra/rds-slow-query.md`. start with `pg_stat_activity` to find the running query, then `pg_locks` to see what it's blocking on, then EXPLAIN on the query if it's not a lock issue. 90% of the time it's a missing index or a bad plan from a stale stats ANALYZE", parent_key="rb_007"))
    m.append(SeedMessage("rb_007b", "ask-platform", "morgan", 
        "also the runbook has the query that shows you the top 10 slowest queries from `pg_stat_statements`, that's the first thing I copy-paste when paged", parent_key="rb_007"))

    # rb_008: Ingest pipeline lag --------------------------------------------
    m.append(SeedMessage("rb_008", "ask-data", "jordan",
        "there's a runbook for the ingest-pipeline lag alert, right? I vaguely remember priya writing one"))
    m.append(SeedMessage("rb_008a", "ask-data", "priya", 
        "`acme-runbooks/data/ingest-pipeline-lag.md`. three things to check in order: (1) snowflake COPY INTO queue depth (most common — throttled copies stack up), (2) the kafka consumer lag, (3) the raw S3 bucket write rate. the runbook has the exact CloudWatch queries", parent_key="rb_008"))

    # rb_009: Secret leaked in logs ------------------------------------------
    m.append(SeedMessage("rb_009", "ask-security", "sam",
        "hypothetical — if a secret accidentally got logged and is in CloudWatch, what's the right process"))
    m.append(SeedMessage("rb_009a", "ask-security", "alex", 
        "`acme-runbooks/security/secret-leaked-in-logs.md`. tl;dr: ROTATE THE SECRET FIRST (don't wait). then file the cloudwatch log redaction ticket to security-ops (the team can purge specific log events). then check the secret-tracking spreadsheet for downstream usage. rotation before cleanup, always", parent_key="rb_009"))
    m.append(SeedMessage("rb_009b", "ask-security", "alex", 
        "if this is not hypothetical, page me immediately", parent_key="rb_009"))

    # rb_010: Dbt model failure ----------------------------------------------
    m.append(SeedMessage("rb_010", "ask-data", "jamie",
        "found the `dbt-model-failure.md` runbook — it references the `check_upstream_nulls` helper. where does that live"))
    m.append(SeedMessage("rb_010a", "ask-data", "priya", 
        "it's a macro in `acme-data-api/dbt/macros/debug_helpers.sql`. runs a row count and null count per column for any ref(). we use it in dbt tests but it's also callable from the dbt CLI as a run-operation", parent_key="rb_010"))

    return m
