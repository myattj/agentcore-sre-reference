#!/usr/bin/env python3
"""Linear seeder.

Connects Linear to the test tenant + seeds ~15 issues in the first
team on the account (queried via GraphQL). Content mirrors the Jira
seed so if you link both, they tell the same story.

Secret shape at ``agentcore/testenv/linear``:

    {"api_key": "<linear personal api key>"}

Get an API key from: https://linear.app/ → Settings → API → Personal
API keys. Scope: full workspace access (seeder needs read+write).

Usage:
    python -m scripts.testenv.integrations.seed_linear --tenant slack-t0xxxxxxxxx
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


_BASE_URL = "https://api.linear.app"


# Issue definitions mirror seed_jira.py but trimmed. Linear workflow
# states are workspace-dependent, so instead of hardcoding statuses
# we use priority (0=None, 1=Urgent, 2=High, 3=Medium, 4=Low).
_ISSUES: list[dict[str, Any]] = [
    {
        "title": "Implement exponential backoff with jitter on checkout-api retries",
        "description": "Post-Feb incident fix. Fixed 1s retry interval → exponential backoff with ±20% jitter. Acme-data-api/app/http_client.py.",
        "priority": 2,  # High
        "labels": ["checkout-api", "incident:feb-retry-storm"],
    },
    {
        "title": "Split INGEST_WH into stream + batch warehouses",
        "description": "acme-infra PR #92. Isolate event stream from finance extract workload. Unblocks ingest-pipeline lag contention.",
        "priority": 2,
        "labels": ["ingest-pipeline", "infrastructure"],
    },
    {
        "title": "Remove silent null-drop in stg_orders",
        "description": "Root cause of the Apr fct_orders_daily regression. Silent null-drop in a dbt filter dropped rows where upstream columns went null. Replaced with explicit `not_null` tests.",
        "priority": 1,  # Urgent
        "labels": ["dbt", "data-quality", "incident:orders-regression"],
    },
    {
        "title": "Write Feb 2026 checkout incident postmortem",
        "description": "Still in draft. Timeline, root cause (retry storm amplification), action items, thundering-herd lessons.",
        "priority": 2,
        "labels": ["postmortem"],
    },
    {
        "title": "Audit all dbt filters for silent-drop patterns",
        "description": "Output of orders regression postmortem. Any dbt filter that silently drops bad rows is a canary-killer.",
        "priority": 2,
        "labels": ["dbt", "data-quality"],
    },
    {
        "title": "EKS 1.28 → 1.29 upgrade",
        "description": "Next Tuesday 2pm. Runbook: acme-runbooks/infra/eks-version-upgrade.md. Pre-flight with kube-no-trouble.",
        "priority": 2,
        "labels": ["eks", "upgrade"],
    },
    {
        "title": "Rotate prod RDS password (quarterly)",
        "description": "Runbook: security/rds-password-rotation.md. 24h hold between promote and delete.",
        "priority": 3,
        "labels": ["security", "rotation"],
    },
    {
        "title": "Reduce auto-suspend timeout on all prod Snowflake warehouses",
        "description": "10min → 60s. Biggest contributor to Q1 cost remediation.",
        "priority": 3,
        "labels": ["snowflake", "cost-optimization"],
    },
    {
        "title": "Split ANALYTICS_WH into scheduled vs ad-hoc",
        "description": "Prevent ad-hoc query blast radius on scheduled dbt runs.",
        "priority": 3,
        "labels": ["snowflake", "cost-optimization"],
    },
    {
        "title": "Fix checkout-api RDS pool_size override",
        "description": "pool_size=25 in checkout-api config > 15 team standard. Caused 'remaining connection slots reserved' errors.",
        "priority": 2,
        "labels": ["checkout-api", "rds"],
    },
    {
        "title": "Datadog monitor flapping: ingest-pipeline lag",
        "description": "Threshold set too tight (p95 > 30s) without excluding Sunday batch window. Adjusting to > 60s excluding 01-04 UTC Sundays.",
        "priority": 4,
        "labels": ["ingest-pipeline", "datadog"],
    },
    {
        "title": "Terraform drift on EKS cluster tags",
        "description": "aws provider tag casing inconsistency. Workaround: ignore_changes on tags['auto-generated']. Real fix deferred.",
        "priority": 4,
        "labels": ["eks", "terraform", "tech-debt"],
    },
    {
        "title": "Cross-team change mgmt: finance schema migrations",
        "description": "Output of orders regression postmortem. Finance must notify data-eng before production schema changes. Establish Linear project for these.",
        "priority": 3,
        "labels": ["process", "cross-team"],
    },
    {
        "title": "Q2 OKR: zero SEV-1 incidents",
        "description": "Requires retry-backoff fix, ingest workload split, dbt silent-drop audit to all land cleanly.",
        "priority": 2,
        "labels": ["okr", "q2"],
    },
    {
        "title": "Q2 OKR: Snowflake monthly spend under $15k",
        "description": "After Q1 remediation we're on track. Monitor weekly.",
        "priority": 3,
        "labels": ["okr", "q2"],
    },
]


# ----------------------------------------------------------------------------
# GraphQL helpers
# ----------------------------------------------------------------------------

def _graphql(
    client: RateLimitedClient, query: str, variables: dict[str, Any] | None = None
) -> dict[str, Any]:
    r = client.post(
        "/graphql",
        json={"query": query, "variables": variables or {}},
    )
    if r.status_code != 200:
        raise RuntimeError(f"linear graphql HTTP {r.status_code}: {r.text[:300]}")
    data = r.json() or {}
    if data.get("errors"):
        raise RuntimeError(f"linear graphql errors: {data['errors']}")
    return data.get("data") or {}


_TEAMS_QUERY = """
query Teams {
  teams(first: 5) {
    nodes { id name key }
  }
}
"""

_CREATE_ISSUE_MUTATION = """
mutation CreateIssue(
  $teamId: String!,
  $title: String!,
  $description: String,
  $priority: Int,
  $labelIds: [String!]
) {
  issueCreate(input: {
    teamId: $teamId,
    title: $title,
    description: $description,
    priority: $priority,
    labelIds: $labelIds
  }) {
    success
    issue { id identifier title }
  }
}
"""

_LABELS_QUERY = """
query TeamLabels($teamId: String!) {
  team(id: $teamId) {
    labels(first: 100) { nodes { id name } }
  }
}
"""

_CREATE_LABEL_MUTATION = """
mutation CreateLabel($teamId: String!, $name: String!) {
  issueLabelCreate(input: { teamId: $teamId, name: $name }) {
    success
    issueLabel { id name }
  }
}
"""


def _ensure_labels(
    client: RateLimitedClient, team_id: str, names: list[str]
) -> dict[str, str]:
    """Return a map of label name → id, creating any missing ones."""
    existing = _graphql(client, _LABELS_QUERY, {"teamId": team_id})
    by_name: dict[str, str] = {}
    for node in (existing.get("team") or {}).get("labels", {}).get("nodes", []):
        by_name[node["name"]] = node["id"]

    for name in names:
        if name in by_name:
            continue
        data = _graphql(
            client, _CREATE_LABEL_MUTATION, {"teamId": team_id, "name": name}
        )
        label = (data.get("issueLabelCreate") or {}).get("issueLabel") or {}
        if label.get("id"):
            by_name[name] = label["id"]

    return by_name


def run_seed(
    tenant_id: str,
    *,
    region: str | None = None,
    bridge_url: str | None = None,
    skip_connect: bool = False,
    skip_seed: bool = False,
    force: bool = False,
) -> int:
    step("Loading Linear credentials from Secrets Manager")
    try:
        creds = load_integration_secret(
            "linear", region=region, required_keys=["api_key"]
        )
    except RuntimeError as e:
        err(str(e))
        return 1
    ok("creds loaded")

    if skip_connect:
        warn("--skip-connect: skipping bridge integration connect")
    else:
        step(f"Connecting Linear to tenant {tenant_id} via bridge")
        try:
            resp = bridge_connect_integration(
                tenant_id,
                "linear",
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

    step("Seeding Linear issues")
    client = RateLimitedClient(
        base_url=_BASE_URL,
        headers={
            "Authorization": creds["api_key"],
            "Content-Type": "application/json",
        },
        min_interval_s=0.5,
    )

    try:
        # Query the first team on the workspace
        data = _graphql(client, _TEAMS_QUERY)
        teams = (data.get("teams") or {}).get("nodes") or []
        if not teams:
            err("no teams found on the Linear workspace — create a team first")
            return 1
        team = teams[0]
        team_id = team["id"]
        grey(f"  using team: {team['name']} ({team['key']})")

        state = load_seeded_state("linear")
        if state.get("issues") and not force:
            warn(f"found existing state ({len(state['issues'])} seeded issues) — pass --force to re-seed")
            return 0

        # Collect all unique labels from the issues and ensure they exist
        all_labels: set[str] = set()
        for issue in _ISSUES:
            all_labels.update(issue.get("labels") or [])
        step("Ensuring labels exist")
        label_map = _ensure_labels(client, team_id, sorted(all_labels))
        grey(f"  {len(label_map)} labels ready")

        created: list[str] = []
        step("Creating issues")
        for i, issue in enumerate(_ISSUES, 1):
            label_ids = [
                label_map[n] for n in (issue.get("labels") or []) if n in label_map
            ]
            variables = {
                "teamId": team_id,
                "title": issue["title"],
                "description": issue["description"],
                "priority": issue["priority"],
                "labelIds": label_ids,
            }
            try:
                result = _graphql(client, _CREATE_ISSUE_MUTATION, variables)
            except RuntimeError as e:
                warn(f"  issue {i} failed: {e}")
                continue
            create_result = result.get("issueCreate") or {}
            if not create_result.get("success"):
                warn(f"  issue {i} returned success=false: {issue['title'][:60]}")
                continue
            identifier = (create_result.get("issue") or {}).get("identifier", "")
            created.append(identifier)
            grey(f"  {identifier}: {issue['title'][:60]}")

        ok(f"{len(created)}/{len(_ISSUES)} issues created")

        state["team_id"] = team_id
        state["team_key"] = team["key"]
        state["issues"] = created
        state["last_run"] = int(time.time())
        save_seeded_state("linear", state)

    finally:
        client.close()

    step("Linear seed complete")
    grey(f"  open https://linear.app/ → {team['name']} team to verify")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed Linear for the Agent test env.")
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
