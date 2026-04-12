#!/usr/bin/env python3
"""Jira seeder.

Connects Jira Cloud to the test tenant + seeds a realistic project:

  - Project ACME (software, kanban-style)
  - ~25 issues across types (Bug, Task, Story, Spike)
  - Varied priorities + statuses (To Do / In Progress / Done)
  - References the same services + incidents as the Slack seed

Secret shape at ``agentcore/testenv/jira``:

    {
      "email":     "<your atlassian account email>",
      "api_token": "<atlassian api token from id.atlassian.com>",
      "domain":    "<your-subdomain>"
    }

``domain`` is the subdomain, e.g. ``acme-testenv`` for
``acme-testenv.atlassian.net``.

Signup: https://www.atlassian.com/software/jira/free
API token: https://id.atlassian.com/manage-profile/security/api-tokens

Usage:
    python -m scripts.testenv.integrations.seed_jira --tenant slack-t0xxxxxxxxx
"""
from __future__ import annotations

import argparse
import base64
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


_PROJECT_KEY = "ACME"
_PROJECT_NAME = "Acme Data Co Engineering"


# Issues to create. Each entry is (summary, description, issuetype,
# priority, target_status). target_status drives the post-create
# transition to make the project look lived-in.
_ISSUES: list[dict[str, Any]] = [
    # ----- Feb incident action items -----
    {
        "summary": "Implement exponential backoff with jitter on checkout-api retries",
        "description": "Output of the Feb retry storm incident. Current: fixed 1s retry interval. Target: exponential backoff starting at 500ms with ±20% jitter. File changed: acme-data-api/app/http_client.py.",
        "issuetype": "Bug",
        "priority": "High",
        "labels": ["checkout-api", "incident:feb-retry-storm", "sev2"],
        "target_status": "Done",
    },
    {
        "summary": "Lower circuit breaker timeout on payment provider calls",
        "description": "Post-incident action: 5s → 3s default. Update acme-data-api/app/config/payment.yaml and deploy via checkout-api release.",
        "issuetype": "Task",
        "priority": "Medium",
        "labels": ["checkout-api", "incident:feb-retry-storm"],
        "target_status": "Done",
    },
    {
        "summary": "Add chaos test: payment provider recovery",
        "description": "Quarterly game day scenario. Simulate 90s payment provider outage followed by sudden recovery. Assert retry policy doesn't tip them over.",
        "issuetype": "Task",
        "priority": "Medium",
        "labels": ["checkout-api", "chaos-testing"],
        "target_status": "To Do",
    },
    {
        "summary": "Write Feb 2026 checkout incident postmortem",
        "description": "Still in draft (Morgan). Include: timeline, root cause (retry storm amplification), action items, lessons on thundering-herd patterns.",
        "issuetype": "Task",
        "priority": "High",
        "labels": ["postmortem", "incident:feb-retry-storm"],
        "target_status": "In Progress",
    },
    # ----- Ingest pipeline contention (ongoing) -----
    {
        "summary": "Split INGEST_WH into stream + batch warehouses",
        "description": "acme-infra PR #92. New INGEST_STREAM_WH for high-frequency event ingest, INGEST_BATCH_WH for finance extract. Cost: +$50/mo, benefit: workload isolation.",
        "issuetype": "Task",
        "priority": "High",
        "labels": ["ingest-pipeline", "incident:ingest-contention"],
        "target_status": "In Progress",
    },
    {
        "summary": "Migrate dbt profiles to new warehouse split",
        "description": "Once acme-infra PR #92 merges, update dbt profile.yml and reschedule the cutover during low-traffic window (10am PT).",
        "issuetype": "Task",
        "priority": "High",
        "labels": ["ingest-pipeline", "dbt"],
        "target_status": "To Do",
    },
    {
        "summary": "Write runbook: ingest-pipeline-lag-contention.md",
        "description": "After the workload-split fix ships and is verified, document the diagnosis path (query history by warehouse, pipe queue depth, finance extract correlation).",
        "issuetype": "Task",
        "priority": "Low",
        "labels": ["ingest-pipeline", "runbook"],
        "target_status": "To Do",
    },
    # ----- Orders dbt regression (resolved) -----
    {
        "summary": "Remove silent null-drop in stg_orders",
        "description": "Root cause of the Apr fct_orders_daily regression. The data-quality filter was dropping rows where upstream columns went null, masking upstream schema changes. Replaced with `dbt test not_null` that fails loudly.",
        "issuetype": "Bug",
        "priority": "Highest",
        "labels": ["reporting-worker", "dbt", "incident:orders-regression"],
        "target_status": "Done",
    },
    {
        "summary": "Audit all dbt filters for silent-drop patterns",
        "description": "Output of the orders regression postmortem. Any dbt filter that silently drops bad rows is a canary-killer. Replace with explicit tests.",
        "issuetype": "Story",
        "priority": "High",
        "labels": ["dbt", "data-quality"],
        "target_status": "In Progress",
    },
    {
        "summary": "Cross-team change mgmt: finance notifies data-eng on schema migrations",
        "description": "Output of orders regression postmortem. Finance team pushed a column rename without notice; our ingest silently dropped rows. Establish: finance creates a Jira ticket on this project before any production schema change.",
        "issuetype": "Story",
        "priority": "Medium",
        "labels": ["process", "cross-team"],
        "target_status": "To Do",
    },
    # ----- Snowflake cost -----
    {
        "summary": "Reduce auto-suspend timeout on ANALYTICS_WH",
        "description": "10min → 60s. Biggest single contributor to the Q1 cost spike remediation. See acme-runbooks/data/snowflake-cost-optimization.md.",
        "issuetype": "Task",
        "priority": "Medium",
        "labels": ["snowflake", "cost-optimization"],
        "target_status": "Done",
    },
    {
        "summary": "Split reporting warehouse: scheduled vs ad-hoc",
        "description": "ANALYTICS_WH was carrying both scheduled dbt runs AND ad-hoc human queries. Split to prevent ad-hoc blast radius.",
        "issuetype": "Task",
        "priority": "Medium",
        "labels": ["snowflake", "cost-optimization"],
        "target_status": "Done",
    },
    {
        "summary": "Monthly credit guardrails via resource monitors",
        "description": "Every warehouse gets a resource monitor with a monthly credit limit. Pages at 100% of quota, suspends warehouse. Acts as a stop-loss for runaway bills.",
        "issuetype": "Task",
        "priority": "Medium",
        "labels": ["snowflake", "cost-optimization"],
        "target_status": "Done",
    },
    # ----- EKS upgrade -----
    {
        "summary": "EKS 1.28 → 1.29 upgrade",
        "description": "Scheduled for next Tuesday 2pm. Runbook: acme-runbooks/infra/eks-version-upgrade.md. Pre-flight: kube-no-trouble for deprecated APIs.",
        "issuetype": "Task",
        "priority": "High",
        "labels": ["eks", "upgrade"],
        "target_status": "In Progress",
    },
    {
        "summary": "Update terraform for EKS 1.29 control plane",
        "description": "acme-infra/terraform/prod/eks/eks.tf — bump cluster_version. Apply is online, takes ~30 min.",
        "issuetype": "Task",
        "priority": "High",
        "labels": ["eks", "terraform"],
        "target_status": "Done",
    },
    # ----- Routine tasks -----
    {
        "summary": "Rotate prod RDS password (quarterly)",
        "description": "Runbook: acme-runbooks/security/rds-password-rotation.md. Sequence is the tricky part — 24h hold between steps 4 and 5.",
        "issuetype": "Task",
        "priority": "Medium",
        "labels": ["rds", "security", "rotation"],
        "target_status": "To Do",
    },
    {
        "summary": "Fix checkout-api RDS connection pool override",
        "description": "Sam had pool_size=25 in the checkout-api config, above the 15 team standard. Caused 'remaining connection slots reserved' errors intermittently.",
        "issuetype": "Bug",
        "priority": "High",
        "labels": ["checkout-api", "rds"],
        "target_status": "Done",
    },
    {
        "summary": "Refactor User model to acme-data-api/app/models/",
        "description": "The pre-refactor location acme-data-api/models/user.py is gone. Everything should live under `app/` as the canonical namespace.",
        "issuetype": "Task",
        "priority": "Low",
        "labels": ["acme-data-api", "refactor"],
        "target_status": "Done",
    },
    # ----- Q2 planning / spikes -----
    {
        "summary": "Spike: AgentCore Gateway interceptor for tenant isolation",
        "description": "Evaluate the interceptor Lambda pattern for per-tenant tool scoping. Week 4 chunk B groundwork.",
        "issuetype": "Spike",
        "priority": "Medium",
        "labels": ["spike", "platform"],
        "target_status": "In Progress",
    },
    {
        "summary": "Spike: evaluate Linear vs Jira for eng tickets",
        "description": "Jamie's ask. Neither is strictly better; Linear has nicer UX, Jira has better cross-team integration with Confluence. Decision by Q2 end.",
        "issuetype": "Spike",
        "priority": "Low",
        "labels": ["spike", "process"],
        "target_status": "To Do",
    },
    {
        "summary": "Q2 OKR: zero SEV-1 incidents",
        "description": "Stretch goal. Requires the retry-backoff fix, the ingest workload split, and the dbt silent-drop audit to all land cleanly.",
        "issuetype": "Story",
        "priority": "High",
        "labels": ["okr", "q2"],
        "target_status": "In Progress",
    },
    {
        "summary": "Q2 OKR: Snowflake monthly spend under $15k",
        "description": "After Q1 remediation (auto-suspend, workload split, resource monitors), we're on track. Monitor weekly.",
        "issuetype": "Story",
        "priority": "Medium",
        "labels": ["okr", "q2"],
        "target_status": "In Progress",
    },
    # ----- Open bugs -----
    {
        "summary": "Datadog monitor flapping: ingest-pipeline lag",
        "description": "Monitor threshold set too tight (p95 > 30s) without excluding the Sunday batch window. Priya's fix: adjust to p95 > 60s excluding Sun 01-04 UTC.",
        "issuetype": "Bug",
        "priority": "Low",
        "labels": ["ingest-pipeline", "datadog"],
        "target_status": "In Progress",
    },
    {
        "summary": "Terraform drift on EKS cluster tags",
        "description": "aws provider not respecting tag casing. Workaround: ignore_changes on tags['auto-generated']. Real fix deferred.",
        "issuetype": "Bug",
        "priority": "Low",
        "labels": ["eks", "terraform", "tech-debt"],
        "target_status": "To Do",
    },
    {
        "summary": "user-service: IntegrityError on duplicate email POST /users",
        "description": "Front-end retrying on success. Sentry regression. Not a backend bug but worth tracking for the frontend fix.",
        "issuetype": "Bug",
        "priority": "Low",
        "labels": ["user-service", "frontend"],
        "target_status": "To Do",
    },
]


# ----------------------------------------------------------------------------
# Jira API helpers
# ----------------------------------------------------------------------------

def _jira_base_url(domain: str) -> str:
    return f"https://{domain}.atlassian.net"


def _basic_auth_header(email: str, api_token: str) -> str:
    raw = f"{email}:{api_token}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def _get_myself(client: RateLimitedClient) -> dict[str, Any] | None:
    """GET /rest/api/3/myself — used to resolve the current account id,
    which is required as the leadAccountId when creating a project."""
    r = client.get("/rest/api/3/myself")
    if r.status_code != 200:
        return None
    return r.json()


def _find_project(client: RateLimitedClient, key: str) -> dict[str, Any] | None:
    r = client.get(f"/rest/api/3/project/{key}")
    if r.status_code == 200:
        return r.json()
    return None


def _create_project(
    client: RateLimitedClient,
    *,
    key: str,
    name: str,
    lead_account_id: str,
) -> dict[str, Any] | None:
    body = {
        "key": key,
        "name": name,
        "projectTypeKey": "software",
        "projectTemplateKey": "com.pyxis.greenhopper.jira:gh-simplified-kanban-classic",
        "leadAccountId": lead_account_id,
        "description": "Acme Data Co engineering — test-env seed project.",
        "assigneeType": "PROJECT_LEAD",
    }
    r = client.post("/rest/api/3/project", json=body)
    if r.status_code in (200, 201):
        return r.json()
    return None


def _create_issue(
    client: RateLimitedClient,
    *,
    project_key: str,
    summary: str,
    description: str,
    issuetype: str,
    priority: str,
    labels: list[str],
) -> str | None:
    # Description uses Atlassian Document Format (ADF) on Cloud v3 API.
    adf = {
        "type": "doc",
        "version": 1,
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": description}]}
        ],
    }
    body = {
        "fields": {
            "project": {"key": project_key},
            "summary": summary,
            "description": adf,
            "issuetype": {"name": issuetype},
            "labels": labels,
            "priority": {"name": priority},
        }
    }
    r = client.post("/rest/api/3/issue", json=body)
    if r.status_code not in (200, 201):
        return None
    return (r.json() or {}).get("key")


def _get_transitions(client: RateLimitedClient, issue_key: str) -> list[dict[str, Any]]:
    r = client.get(f"/rest/api/3/issue/{issue_key}/transitions")
    if r.status_code != 200:
        return []
    return (r.json() or {}).get("transitions") or []


def _transition_issue(
    client: RateLimitedClient,
    *,
    issue_key: str,
    target_status: str,
) -> bool:
    """Transition an issue to the named status by looking up the
    transition that resolves to that status."""
    transitions = _get_transitions(client, issue_key)
    target_id: str | None = None
    for t in transitions:
        to_status = (t.get("to") or {}).get("name", "")
        if to_status.lower() == target_status.lower():
            target_id = t.get("id")
            break
    if not target_id:
        return False
    r = client.post(
        f"/rest/api/3/issue/{issue_key}/transitions",
        json={"transition": {"id": target_id}},
    )
    return r.status_code in (200, 204)


# ----------------------------------------------------------------------------
# Top-level seeder
# ----------------------------------------------------------------------------

def run_seed(
    tenant_id: str,
    *,
    region: str | None = None,
    bridge_url: str | None = None,
    skip_connect: bool = False,
    skip_seed: bool = False,
    force: bool = False,
) -> int:
    step("Loading Jira credentials from Secrets Manager")
    try:
        creds = load_integration_secret(
            "jira",
            region=region,
            required_keys=["email", "api_token", "domain"],
        )
    except RuntimeError as e:
        err(str(e))
        return 1
    ok(f"creds loaded (domain: {creds['domain']}.atlassian.net)")

    if skip_connect:
        warn("--skip-connect: skipping bridge integration connect")
    else:
        step(f"Connecting Jira to tenant {tenant_id} via bridge")
        try:
            resp = bridge_connect_integration(
                tenant_id,
                "jira",
                body={
                    "email": creds["email"],
                    "api_token": creds["api_token"],
                    "domain": creds["domain"],
                },
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

    step("Seeding Jira project + issues")
    client = RateLimitedClient(
        base_url=_jira_base_url(creds["domain"]),
        headers={
            "Authorization": _basic_auth_header(creds["email"], creds["api_token"]),
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        min_interval_s=0.3,
    )

    try:
        # Current user id (for project leadAccountId)
        me = _get_myself(client)
        if not me:
            err("could not GET /rest/api/3/myself — check email/api_token")
            return 1
        account_id = me.get("accountId")
        grey(f"  current user: {me.get('displayName')} ({account_id})")

        # Project (idempotent by key)
        project = _find_project(client, _PROJECT_KEY)
        if project and not force:
            grey(f"  project {_PROJECT_KEY} exists (reusing)")
        elif not project:
            project = _create_project(
                client,
                key=_PROJECT_KEY,
                name=_PROJECT_NAME,
                lead_account_id=account_id,
            )
            if not project:
                err(f"failed to create project {_PROJECT_KEY}")
                return 1
            ok(f"project {_PROJECT_KEY} created")

        state = load_seeded_state("jira")
        if state.get("issues") and not force:
            warn(f"found existing state ({len(state['issues'])} seeded issues) — pass --force to re-seed")
            return 0

        created: list[dict[str, Any]] = []
        for i, issue in enumerate(_ISSUES, 1):
            key = _create_issue(
                client,
                project_key=_PROJECT_KEY,
                summary=issue["summary"],
                description=issue["description"],
                issuetype=issue["issuetype"],
                priority=issue["priority"],
                labels=issue.get("labels", []),
            )
            if not key:
                warn(f"  issue {i} failed: {issue['summary'][:60]}")
                continue
            created.append({"key": key, "target_status": issue["target_status"]})
            grey(f"  {key} [{issue['issuetype']}]: {issue['summary'][:60]}")

        ok(f"{len(created)}/{len(_ISSUES)} issues created")

        # Transition issues to their target statuses
        step("Transitioning issues to target statuses")
        transition_fails = 0
        for entry in created:
            target = entry["target_status"]
            if target == "To Do":
                continue  # default state, skip
            ok_transition = _transition_issue(
                client, issue_key=entry["key"], target_status=target
            )
            if not ok_transition:
                transition_fails += 1
                grey(f"  {entry['key']} → {target}: FAILED (workflow may not allow)")
            else:
                grey(f"  {entry['key']} → {target}")
        if transition_fails:
            warn(f"{transition_fails} transitions failed — often OK if Jira workflow has different names")

        state["project"] = _PROJECT_KEY
        state["issues"] = [e["key"] for e in created]
        state["last_run"] = int(time.time())
        save_seeded_state("jira", state)

    finally:
        client.close()

    step("Jira seed complete")
    grey(f"  open https://{creds['domain']}.atlassian.net/jira/software/projects/{_PROJECT_KEY}/board to verify")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed Jira for the AgentCore Reference test env.")
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
