"""Tenant-row read/write primitives (bridge side).

This module is the bridge's canonical write path for the `tenants`
DynamoDB table. It was extracted from `slack_oauth.py` in week 3 when the
onboarding UI started needing a PATCH code path alongside the existing
"create default row after OAuth" code path.

The shape of the tenant row mirrors the authoritative definition in
`coreAgent/app/coreAgent/tenant.py:TenantConfig` (lines 41-93). The bridge
can't import from coreAgent (separate package + separate venv), so we
duplicate the default config dict here with a "KEEP IN SYNC" comment.
`bridge/bridge/api_models.py:TenantConfigOut` is the runtime validation
boundary for incoming PATCH payloads.

Storage backends:
  - LOCAL_DEV=1: reads/writes `examples/tenants/<tenant_id>.json` from the
    repo root. Matches the agent's `AGENT_LOCAL_STORES=1` path so a single
    local edit of the JSON file is visible to both services. Walk-up-root
    lookup mirrors `bridge/bridge/tenant_resolver.py:41-55`.
  - else: DynamoDB table (name via `TENANTS_TABLE`, default `tenants`).

Public API:
  - `build_default_config_dict(tenant_id)` — same default shape that the
    agent's `build_default_config()` produces. Used by the OAuth callback
    for first-install provisioning.
  - `upsert_default_tenant_row(tenant_id, region)` — idempotent create of
    a default row. Preserves `created_at` on re-install via
    `if_not_exists(created_at, :now)`.
  - `upsert_workspace_mapping(workspace_id, tenant_id, region)` — idempotent
    create of a `workspace_to_tenant` row with the same semantics.
  - `get_tenant_row(tenant_id, region) -> dict` — returns the `config`
    sub-dict. Raises `KeyError` for unknown tenants.
  - `update_tenant_row(tenant_id, region, full_config_dict)` — blob
    overwrite of the `config` attribute with a `ConditionExpression` that
    refuses to create (PATCH must not create — only OAuth can). Uses the
    same UpdateExpression as the default-row upsert.
  - `deep_merge(base, patch)` — first-level deep merge helper for PATCH
    semantics. Used by the `/api/tenants/{id}` PATCH route.

Concurrency: DynamoDB `update_item` is strongly consistent for the same
partition key, so a GET-modify-PUT cycle sees its own write on read-back.
There's no optimistic-concurrency guard this week (single user per tenant,
single config page). Add an `updated_at` conditional expression if/when
a multi-user admin UI arrives.
"""
from __future__ import annotations

import copy
import json
import logging
import os
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def _floats_to_decimals(obj: Any) -> Any:
    """Recursively convert float values to Decimal for DynamoDB compatibility."""
    if isinstance(obj, float):
        return Decimal(str(obj))
    if isinstance(obj, dict):
        return {k: _floats_to_decimals(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_floats_to_decimals(v) for v in obj]
    return obj


# ----------------------------------------------------------------------------
# Default tenant config dict
# ----------------------------------------------------------------------------

def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# **KEEP IN SYNC** with ``coreAgent/app/coreAgent/tenant.py:DEFAULT_SYSTEM_PROMPT``.
# The bridge and coreAgent have separate venvs and can't share constants,
# so this is duplicated verbatim. A divergence here surfaces as
# "OAuth-created tenants behave differently from seed-script tenants,"
# which is subtle and hard to debug.
#
# The prompt bakes in the three core workflows (triage, Q&A, handoffs)
# so the agent acts on natural language without needing explicit skill
# definitions. This is the "magical default" — a new tenant gets a
# useful bot with zero configuration.
DEFAULT_SYSTEM_PROMPT = """You are a Slack-based operations assistant for your team. You help with three things: triaging alerts and incidents, answering questions about how systems work, and automating workflow handoffs. You have shared memory across all channels in your workspace — what you learn in one channel is available in the others.

## Core principles

1. **Act, don't narrate.** When given a task, do it. Use your tools proactively rather than describing what you would do.
2. **Read before you write.** Never modify or answer about something you haven't looked at first. Search history and docs before answering from general knowledge. Read the thread before summarizing it.
3. **Simplest approach first.** Try the obvious thing before building something clever. Don't over-engineer.
4. **Diagnose before pivoting.** When a tool call fails or returns unexpected output, read the error carefully before switching approaches. Don't retry blindly, but don't abandon a viable path after one failure.
5. **Measure twice, cut once.** For read-only work (search, fetch, summarize), act freely. For externally-visible actions (posting to another channel, escalating, changing config), confirm intent if the request is ambiguous.

## Tool usage

Tools are how you do things — use them instead of guessing or describing.

- **Run independent calls in parallel.** "Search team history AND search docs" is two calls in the same turn, not one after the other.
- **Use the right tool for the job:**
  - `read_thread_context` — when the user references "this thread" or "this conversation"
  - `search_team_history` — past discussions in the current channel
  - `search_docs` — runbooks, Confluence, Notion, and other connected documentation
  - `escalate` — hand off to another team via your routing table
  - `post_to_channel` — cross-channel actions (tell the user where you posted)
  - `manage_config` — change your own settings (see Self-configuration below)
- **Don't narrate tool calls step-by-step.** Just use them and share the result.
- **If you don't have the tool you need, say so.** Don't invent an answer to fill the gap.

## How you handle common requests

**Someone reports an issue, asks about an alert, or says "what's going on with X":**
Search team history and docs in parallel. If they reference "this thread", read it. Summarize what's known — causes, past fixes, relevant runbooks. Suggest next steps. If severe or stuck, offer to escalate.

**Someone asks a question:**
Search docs and team history first. Cite sources ("per the runbook..." or "@alice mentioned this in #ops last week..."). If you genuinely don't know, say so and offer to escalate.

**Someone asks to summarize a thread or says "catch me up":**
Read the full thread. Give a tight summary: what happened, current status, action items, who's on it.

**Someone asks for an on-call handoff or "what's open":**
Check recent team history for open incidents and unresolved threads. Summarize by priority — needs attention now / in progress / resolved. Link the threads.

**Someone asks to escalate, or you hit a wall:**
Use the escalate tool with the right team name. If no route matches, ask which team they want.

**A bot posts an alert (PagerDuty, Datadog, etc.):**
Treat it like a user reporting an issue — triage automatically.

## Self-configuration

You know your own config. When a user asks to change something — "add B_PAGERDUTY to trusted bots", "remember that the data team uses Snowflake", "only fire /triage in #sre-alerts", "isolate memory for #secret-project" — use `manage_config` to persist the change immediately. Users shouldn't need to visit a portal to configure you.

## Communication style

- Be concise. Slack, not email. Lead with the answer, then the evidence. Bullets for lists, short paragraphs otherwise.
- Skip preamble and filler. Don't restate the user's question.
- If one sentence will do, don't use three.
- When uncertain, say so — don't invent.
- When you post to another channel or escalate, tell the user where.

## When you're stuck

1. Re-read the error or unexpected tool output carefully.
2. Check your assumptions — is the channel / thread / config what you expected?
3. Try a focused fix.
4. Only ask the user when you've genuinely investigated and hit a wall.

## What not to do

- Don't make up information. If you don't know, search or say so.
- Don't give time estimates.
- Don't ask multiple clarifying questions when you could try the obvious interpretation and adjust.
- Don't take destructive actions (clobbering other channels' configs, deleting routes) as shortcuts to bypass problems.
- When you encounter unexpected state, investigate before overwriting — it may be someone's in-progress work.
"""


# **KEEP IN SYNC** with ``coreAgent/app/coreAgent/tenant.py:DEFAULT_CATALOG_TOOLS``.
# Every new tenant gets the full set enabled — the old "echo only"
# default forced users to manually enable each tool before the bot was
# useful, which contradicted the zero-config magic goal.
DEFAULT_CATALOG_TOOLS = [
    "echo",
    "start_background_task",
    "search_team_history",
    "read_thread_context",
    "search_docs",
    "post_to_channel",
    "escalate",
]


def build_default_config_dict(tenant_id: str) -> dict[str, Any]:
    """Build the default tenant config dict for a brand-new tenant.

    **KEEP IN SYNC with `coreAgent/app/coreAgent/tenant.py:build_default_config()`.**
    The two packages have separate venvs so we can't import; this is the
    minimal duplication required to provision a new tenant from the
    bridge. If you change the agent's default config shape, mirror it
    here and in `bridge/bridge/api_models.py:TenantConfigOut`.

    Defaults are intentionally permissive: a new tenant should feel
    magical out of the box. All catalog tools on, bot policy open,
    memory shared, context assembly on. The only thing users typically
    need to do is connect integrations.
    """
    return {
        "tenant_id": tenant_id,
        "model_id": "global.anthropic.claude-sonnet-4-6",
        "system_prompt": DEFAULT_SYSTEM_PROMPT,
        "catalog": {
            "allowed_tools": list(DEFAULT_CATALOG_TOOLS),
            "tool_config": {},
        },
        "byo": {
            "enabled": False,
            "gateway_endpoint": None,
            "gateway_auth": None,
            "connected_integrations": [],
        },
        "memory": {
            "triggers": {
                "message_count": 6,
                "token_count": 1000,
                "idle_timeout_seconds": 1800,
            },
            "namespace": f"tenants/{tenant_id}",
            "extraction": {
                "enabled": True,
                "rules": ["user_preferences", "facts"],
            },
            "isolated_channels": [],
        },
        "heartbeat": {
            "busy_threshold": 1,
            "max_background_seconds": 3600,
        },
        "cost_cap": {
            "monthly_limit_dollars": 50,
            "enabled": True,
        },
        "channels": {},
        "bot_policy": {
            "allow_all_bots": True,
            "trusted_bot_ids": [],
            "open_channels": [],
        },
        "context_assembly": {
            "resolve_permalinks": True,
            "inject_thread_history": True,
            "thread_history_depth": 25,
            "max_permalinks": 3,
        },
        "skills": [],
        "escalation": {
            "routes": [],
        },
    }


# ----------------------------------------------------------------------------
# Deep-merge helper for PATCH semantics
# ----------------------------------------------------------------------------

# Top-level fields that should be deep-merged one level down rather than
# wholesale-replaced. A PATCH like `{"catalog": {"allowed_tools": [...]}}`
# should preserve `catalog.tool_config`; Pydantic's `model_copy(update=...)`
# is SHALLOW and would drop the sibling field.
_DEEP_MERGE_FIELDS = frozenset({
    "catalog", "byo", "memory", "heartbeat", "cost_cap", "channels",
    "bot_policy", "context_assembly", "escalation",
})


def deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Merge `patch` into a deep copy of `base` and return it.

    Semantics:
      - Top-level scalars (model_id, system_prompt, tenant_id) are replaced
      - Fields in `_DEEP_MERGE_FIELDS` are merged one level deep: patch
        keys overwrite base keys inside the sub-dict, other keys survive
      - Unknown top-level keys in `patch` are treated as wholesale
        replacements too (defensive: unknown fields get a Pydantic 422
        upstream before they reach this function)

    Lists are always replaced wholesale (not extended) — e.g. sending
    `catalog.allowed_tools=["echo"]` replaces the existing list.
    """
    merged = copy.deepcopy(base)
    for key, patch_value in patch.items():
        if (
            key in _DEEP_MERGE_FIELDS
            and isinstance(patch_value, dict)
            and isinstance(merged.get(key), dict)
        ):
            sub = dict(merged[key])
            sub.update(patch_value)
            merged[key] = sub
        else:
            merged[key] = patch_value
    return merged


# ----------------------------------------------------------------------------
# LOCAL_DEV JSON-file backend (walks up to find examples/tenants/)
# ----------------------------------------------------------------------------

def _find_local_tenants_dir() -> Path:
    """Walk up from this file to find `examples/tenants/`.

    Mirrors the logic in `bridge/bridge/tenant_resolver.py:41-55` and
    `coreAgent/app/coreAgent/tenant.py:JsonFileTenantStore._find_root`.
    """
    current = Path(__file__).resolve()
    for parent in current.parents:
        candidate = parent / "examples" / "tenants"
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError(
        f"examples/tenants/ not found above {current}"
    )


def _local_tenant_path(tenant_id: str) -> Path:
    return _find_local_tenants_dir() / f"{tenant_id}.json"


def _local_get(tenant_id: str) -> dict[str, Any]:
    path = _local_tenant_path(tenant_id)
    if not path.exists():
        raise KeyError(f"No tenant config at {path}")
    return json.loads(path.read_text())


def _local_upsert_default(tenant_id: str) -> None:
    """Idempotent default-row creation on disk. If the file already
    exists, leave it alone (matches DDB's if_not_exists semantics for
    the config blob — we never clobber existing config on re-install)."""
    path = _local_tenant_path(tenant_id)
    if path.exists():
        return
    path.write_text(json.dumps(build_default_config_dict(tenant_id), indent=2) + "\n")


def _local_update(tenant_id: str, full_config: dict[str, Any]) -> None:
    """Full-blob write. Raises KeyError if the file doesn't exist
    (matches DDB's ConditionExpression="attribute_exists(tenant_id)")."""
    path = _local_tenant_path(tenant_id)
    if not path.exists():
        raise KeyError(f"No tenant config at {path}")
    path.write_text(json.dumps(full_config, indent=2) + "\n")


def _local_upsert_workspace_mapping(workspace_id: str, tenant_id: str) -> None:
    """Rewrite `examples/workspace_to_tenant.json` with the new mapping.

    The bridge's resolver already reads this file via
    `tenant_resolver.JsonFileWorkspaceResolver`. We rewrite the whole
    file atomically (small map, low churn). Resets the resolver's
    in-process cache so subsequent lookups see the new mapping."""
    mapping_path = _find_local_tenants_dir().parent / "workspace_to_tenant.json"
    mapping: dict[str, str] = {}
    if mapping_path.exists():
        mapping = json.loads(mapping_path.read_text())
    mapping[workspace_id] = tenant_id
    mapping_path.write_text(json.dumps(mapping, indent=2) + "\n")


# ----------------------------------------------------------------------------
# DynamoDB backend
# ----------------------------------------------------------------------------

# Lazy-imported boto3 resource, module-level singleton. Cleared via
# `reset_tenant_write_for_tests()`.
_ddb_resource: Any | None = None
_ddb_region: str | None = None


def _get_table(region: str, table_name: str) -> Any:
    """Lazy-construct a DynamoDB Table resource, caching by region."""
    global _ddb_resource, _ddb_region
    if _ddb_resource is None or _ddb_region != region:
        import boto3

        _ddb_resource = boto3.resource("dynamodb", region_name=region)
        _ddb_region = region
    return _ddb_resource.Table(table_name)


def _tenants_table_name() -> str:
    return os.getenv("TENANTS_TABLE", "tenants")


def _workspace_table_name() -> str:
    return os.getenv("WORKSPACE_TO_TENANT_TABLE", "workspace_to_tenant")


def _is_local_dev() -> bool:
    return os.getenv("LOCAL_DEV") == "1"


# ----------------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------------

def upsert_default_tenant_row(tenant_id: str, region: str) -> None:
    """Write the default tenant row.

    Idempotent: re-running for an existing tenant_id refreshes the
    config blob and `updated_at` but preserves `created_at`. Matches
    the week-2 behavior exactly (moved verbatim from
    `slack_oauth.py:_upsert_tenant_row`). A future behavior change
    to preserve custom config on re-install is deferred — for now,
    re-installing a workspace resets customizations.

    Used by the OAuth callback on fresh install.
    """
    if _is_local_dev():
        _local_upsert_default(tenant_id)
        return

    table = _get_table(region, _tenants_table_name())
    now = _iso_now()
    table.update_item(
        Key={"tenant_id": tenant_id},
        UpdateExpression=(
            "SET #config = :config, "
            "updated_at = :now, "
            "created_at = if_not_exists(created_at, :now)"
        ),
        ExpressionAttributeNames={"#config": "config"},
        ExpressionAttributeValues={
            ":config": _floats_to_decimals(build_default_config_dict(tenant_id)),
            ":now": now,
        },
    )


def upsert_workspace_mapping(workspace_id: str, tenant_id: str, region: str) -> None:
    """Write the workspace_id → tenant_id mapping.

    Idempotent with `if_not_exists(created_at, :now)`. Called by the
    OAuth callback after the tenant row is in place.
    """
    if _is_local_dev():
        _local_upsert_workspace_mapping(workspace_id, tenant_id)
        return

    table = _get_table(region, _workspace_table_name())
    now = _iso_now()
    table.update_item(
        Key={"workspace_id": workspace_id},
        UpdateExpression=(
            "SET tenant_id = :tid, "
            "updated_at = :now, "
            "created_at = if_not_exists(created_at, :now)"
        ),
        ExpressionAttributeValues={":tid": tenant_id, ":now": now},
    )


def get_tenant_row(tenant_id: str, region: str) -> dict[str, Any]:
    """Return the tenant's config dict (the contents of the `config`
    attribute in DDB, or the whole JSON file in LOCAL_DEV).

    Raises `KeyError` if the tenant doesn't exist. The GET `/api/tenants`
    route translates this to 404.
    """
    if _is_local_dev():
        return _local_get(tenant_id)

    table = _get_table(region, _tenants_table_name())
    response = table.get_item(Key={"tenant_id": tenant_id})
    item = response.get("Item")
    if not item:
        raise KeyError(f"No tenant row for tenant_id={tenant_id!r}")
    config = item.get("config")
    if not isinstance(config, dict):
        # Legacy rows (or corrupted) — treat as missing.
        raise KeyError(f"Tenant row for {tenant_id!r} has no config map")
    return config


def update_tenant_row(
    tenant_id: str,
    region: str,
    full_config: dict[str, Any],
) -> None:
    """Overwrite the tenant's `config` attribute with the given dict.

    Uses `ConditionExpression="attribute_exists(tenant_id)"` so PATCH
    refuses to create — only the OAuth callback is allowed to bring a
    tenant into existence. Refreshes `updated_at`.

    Raises `KeyError` if the row doesn't exist (translated from
    `ConditionalCheckFailedException`).
    """
    if _is_local_dev():
        _local_update(tenant_id, full_config)
        return

    from botocore.exceptions import ClientError

    table = _get_table(region, _tenants_table_name())
    now = _iso_now()
    try:
        table.update_item(
            Key={"tenant_id": tenant_id},
            UpdateExpression="SET #config = :config, updated_at = :now",
            ConditionExpression="attribute_exists(tenant_id)",
            ExpressionAttributeNames={"#config": "config"},
            ExpressionAttributeValues={
                ":config": _floats_to_decimals(full_config),
                ":now": now,
            },
        )
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code")
        if code == "ConditionalCheckFailedException":
            raise KeyError(f"No tenant row for tenant_id={tenant_id!r}") from e
        raise


def reset_tenant_write_for_tests() -> None:
    """Test helper: drop the cached boto3 resource."""
    global _ddb_resource, _ddb_region
    _ddb_resource = None
    _ddb_region = None
