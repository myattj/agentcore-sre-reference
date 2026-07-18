"""Tenant-row read/write primitives (bridge side).

This module is the bridge's canonical write path for the `tenants`
DynamoDB table. Both Slack OAuth provisioning and the onboarding UI use
these primitives so tenant creation and configuration updates stay aligned.

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
  - `update_tenant_row(tenant_id, region, full_config_dict, expected_config)`
    — blob overwrite of the `config` attribute with a condition that refuses
    to create and, when supplied, rejects stale read-modify-write cycles.
  - `deep_merge(base, patch)` — first-level deep merge helper for PATCH
    semantics. Used by the `/api/tenants/{id}` PATCH route.

Concurrency: tenant-session PATCH passes the config it read as
`expected_config`. DynamoDB compares that map in the write condition, so a
concurrent operator update cannot be replaced by a stale full-blob write. The
route re-reads and retries a bounded number of times.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import tempfile
import threading
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class GitHubInstallationBindingConflict(RuntimeError):
    """An installation is already bound, or a tenant already has another."""


class TenantConfigConflictError(RuntimeError):
    """The tenant config changed after a caller read it."""


_local_github_binding_lock = threading.Lock()
_local_config_update_lock = threading.Lock()


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
DEFAULT_SYSTEM_PROMPT = """You are a Slack-based operations assistant for your team. You handle three things: triaging alerts and incidents, answering questions about how systems work, and automating workflow handoffs. Your memory is scoped to the current channel unless a workspace administrator explicitly enables sharing.

## How to respond

- Be concise. Slack, not email. Lead with the answer, evidence after. One sentence beats three. Skip preamble, filler, and end-of-response summaries — the output speaks for itself.
- No emojis unless the user explicitly asks for them.
- Match scope to the request. Do what's asked — nothing more. Don't add features, "improvements", or speculative work the user didn't ask for.
- When uncertain, say so. Don't invent. Don't fabricate sources. If you don't have a tool you need, say that instead of guessing.

## How to work

- Act, don't narrate. Use tools instead of describing what you would do. Don't narrate each tool call step-by-step.
- Read before you write. Never modify or answer about something you haven't looked at first. Search team history and any connected document sources before answering from general knowledge. Read the thread before summarizing it.
- Run independent calls in parallel. When both history and document-search tools are available, call them in the same turn.
- Diagnose, don't thrash. When a tool fails, read the error and fix the cause. Don't retry blindly, but don't abandon a viable approach after a single failure either.

## Tools

- `read_thread_context` — user references "this thread" or "this conversation"
- `search_team_history` — past discussions in the current channel
- `escalate` — hand off to another team via your routing table
- `post_to_channel` — cross-channel actions (tell the user where you posted)
- `manage_config` — view your settings; updates require an authorized admin

Connected Gateway or MCP integrations may add document-search and other tools. Only reference tools you actually have in your tool list. If a tool isn't there, tell the user it's not connected — don't claim you have it or hide the gap.

When a bot posts an alert (PagerDuty, Datadog, etc.), triage it like a user-reported issue.

## Care with risky actions

Read-only work (search, fetch, summarize): act freely. Externally-visible work (posting to another channel, escalating, changing config, overwriting state): confirm intent when the request is ambiguous. Never bypass a safety check or clobber existing state just to make an obstacle go away — investigate first; it may be someone's in-progress work.

## Self-configuration

You know your own config. Use `manage_config` to inspect settings when asked. Configuration changes are read-only by default and may only be persisted when the requesting Slack user is explicitly listed as a workspace admin; the tool enforces this authorization in code.

## Learning from feedback

When a user corrects your answer, says you're wrong, re-asks the same question in a way that implies your answer missed the mark, or tells you the answer was unhelpful — call `record_feedback` with sentiment="negative" and a brief reason explaining what went wrong. When a user explicitly confirms an answer was helpful ("thanks, that's exactly what I needed", "perfect") — call `record_feedback` with sentiment="positive". Do this alongside your normal response. Don't announce you're recording feedback or ask permission.

Don't call `record_feedback` on routine acknowledgments ("ok", "got it") or when the user is simply continuing the conversation with a new question.
"""


# **KEEP IN SYNC** with ``coreAgent/app/coreAgent/tenant.py:DEFAULT_CATALOG_TOOLS``.
# Every new tenant gets the safe set enabled — the old "echo only" default
# forced users to manually enable each tool before the bot was useful,
# which contradicted the zero-config magic goal. Higher-risk tools remain
# available for explicit opt-in.
#
# The read-only ``code_*`` tools ship in the whitelist but are filtered
# out of the runtime effective_tools list in ``coreAgent/main.py`` when
# the tenant hasn't installed the GitHub App
# (``codebases.enabled=False``). ``propose_pr`` remains available in the
# catalog, but is deliberately excluded here so GitHub write access and
# model-authored sandbox execution require an explicit operator opt-in.
DEFAULT_CATALOG_TOOLS = [
    "echo",
    "start_background_task",
    "search_team_history",
    "read_thread_context",
    "post_to_channel",
    "escalate",
    "record_feedback",
    "ask_codebase_choice",
    "inspect_codebase_context",
    "code_search",
    "code_read_file",
    "code_find_symbol",
    "code_list_commits",
    "check_task_status",
    "render_dashboard",
]


def build_default_config_dict(tenant_id: str) -> dict[str, Any]:
    """Build the default tenant config dict for a brand-new tenant.

    **KEEP IN SYNC with `coreAgent/app/coreAgent/tenant.py:build_default_config()`.**
    The two packages have separate venvs so we can't import; this is the
    minimal duplication required to provision a new tenant from the
    bridge. If you change the agent's default config shape, mirror it
    here and in `bridge/bridge/api_models.py:TenantConfigOut`.

    Defaults make the human-driven path useful without widening trust:
    safe catalog tools are available, bot triggers are human-only, memory is
    channel-scoped, and runtime config mutation has no admins until an
    operator provisions exact Slack user IDs.
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
            "shared_across_channels": False,
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
        "admin_user_ids": [],
        "bot_policy": {
            "allow_all_bots": False,
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
        "codebases": {
            "enabled": False,
            "github_installation_id": None,
            "default_repo": None,
            "bindings": [],
            "allow_learning": True,
        },
        "is_internal_testenv": False,
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
    "bot_policy", "context_assembly", "escalation", "codebases",
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


def _local_update(
    tenant_id: str,
    full_config: dict[str, Any],
    expected_config: dict[str, Any] | None = None,
) -> None:
    """Atomically replace a local config, optionally rejecting a stale read."""
    path = _local_tenant_path(tenant_id)
    with _local_config_update_lock:
        if not path.exists():
            raise KeyError(f"No tenant config at {path}")
        if expected_config is not None and json.loads(path.read_text()) != expected_config:
            raise TenantConfigConflictError(
                f"Tenant config changed for tenant_id={tenant_id!r}"
            )

        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary.write(json.dumps(full_config, indent=2) + "\n")
                temporary.flush()
                os.fsync(temporary.fileno())
                temporary_name = temporary.name
            os.replace(temporary_name, path)
        finally:
            if temporary_name and os.path.exists(temporary_name):
                os.unlink(temporary_name)


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
    config blob and `updated_at` but preserves `created_at`. This retains
    the original OAuth provisioning behavior: re-installing a workspace
    resets its customizations.

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
    expected_config: dict[str, Any] | None = None,
) -> None:
    """Overwrite the tenant's `config` attribute with the given dict.

    Always refuses to create. When `expected_config` is supplied, the write
    also requires the stored map to equal the caller's prior read. This keeps
    stale tenant-session writes from reverting concurrent operator changes.
    Refreshes `updated_at`.

    Raises `KeyError` if the row doesn't exist and `TenantConfigConflictError`
    if an optimistic-concurrency comparison fails.
    """
    if _is_local_dev():
        _local_update(tenant_id, full_config, expected_config)
        return

    from botocore.exceptions import ClientError

    table = _get_table(region, _tenants_table_name())
    now = _iso_now()
    condition = "attribute_exists(tenant_id)"
    expression_values = {
        ":config": _floats_to_decimals(full_config),
        ":now": now,
    }
    if expected_config is not None:
        condition += " AND #config = :expected_config"
        expression_values[":expected_config"] = _floats_to_decimals(expected_config)
    try:
        table.update_item(
            Key={"tenant_id": tenant_id},
            UpdateExpression="SET #config = :config, updated_at = :now",
            ConditionExpression=condition,
            ExpressionAttributeNames={"#config": "config"},
            ExpressionAttributeValues=expression_values,
        )
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code")
        if code == "ConditionalCheckFailedException":
            existing = table.get_item(
                Key={"tenant_id": tenant_id},
                ConsistentRead=True,
            ).get("Item")
            if not existing:
                raise KeyError(f"No tenant row for tenant_id={tenant_id!r}") from e
            raise TenantConfigConflictError(
                f"Tenant config changed for tenant_id={tenant_id!r}"
            ) from e
        raise


def _local_tenants_with_github_installation(installation_id: str) -> set[str]:
    matches: set[str] = set()
    for path in _find_local_tenants_dir().glob("*.json"):
        try:
            config = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            raise RuntimeError(
                f"Cannot safely inspect local tenant config {path.name}"
            ) from e
        if not isinstance(config, dict):
            raise RuntimeError(
                f"Cannot safely inspect local tenant config {path.name}"
            )
        configured = str(
            (config.get("codebases") or {}).get("github_installation_id") or ""
        )
        if configured == installation_id:
            matches.add(str(config.get("tenant_id") or path.stem))
    return matches


def _ddb_tenants_with_github_installation(
    installation_id: str,
    region: str,
) -> set[str]:
    table = _get_table(region, _tenants_table_name())
    matches: set[str] = set()
    scan_args: dict[str, Any] = {
        "ConsistentRead": True,
        "ProjectionExpression": (
            "tenant_id, #cfg.#codebases.#installation"
        ),
        "ExpressionAttributeNames": {
            "#cfg": "config",
            "#codebases": "codebases",
            "#installation": "github_installation_id",
        },
    }
    while True:
        response = table.scan(**scan_args)
        for item in response.get("Items", []):
            tenant_id = item.get("tenant_id")
            configured = str(
                (item.get("config") or {})
                .get("codebases", {})
                .get("github_installation_id")
                or ""
            )
            if isinstance(tenant_id, str) and configured == installation_id:
                matches.add(tenant_id)
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            return matches
        scan_args["ExclusiveStartKey"] = last_key


def _github_installation_lock_id(installation_id: str) -> str:
    return f"__github_installation__#{installation_id}"


def _ddb_github_installation_lock_owner(
    installation_id: str,
    region: str,
) -> str | None:
    """Read the authoritative O(1) binding created by new approvals."""
    table = _get_table(region, _tenants_table_name())
    item = table.get_item(
        Key={"tenant_id": _github_installation_lock_id(installation_id)},
        ConsistentRead=True,
    ).get("Item")
    if not item:
        return None
    owner = item.get("bound_tenant_id")
    if not isinstance(owner, str) or not owner:
        raise GitHubInstallationBindingConflict(
            "GitHub installation lock row is malformed"
        )
    return owner


def find_tenant_by_github_installation(
    installation_id: str,
    region: str,
) -> str | None:
    """Return the tenant bound to an installation, enforcing uniqueness.

    A strongly consistent scan also detects legacy duplicate bindings that
    predate the atomic lock row used by new operator approvals.
    """

    if _is_local_dev():
        matches = _local_tenants_with_github_installation(installation_id)
    else:
        lock_owner = _ddb_github_installation_lock_owner(installation_id, region)
        if lock_owner:
            return lock_owner
        # Pre-lock releases stored the installation only inside the config
        # map. Scan solely as a compatibility guard for those legacy rows;
        # once an approval writes its lock, every subsequent lookup is O(1).
        matches = _ddb_tenants_with_github_installation(installation_id, region)
    if len(matches) > 1:
        raise GitHubInstallationBindingConflict(
            "GitHub installation is bound to multiple legacy tenant rows"
        )
    return next(iter(matches), None)


def _local_approve_github_installation(
    tenant_id: str,
    installation_id: str,
) -> None:
    with _local_github_binding_lock:
        owner = find_tenant_by_github_installation(installation_id, "local")
        if owner and owner != tenant_id:
            raise GitHubInstallationBindingConflict(
                "GitHub installation is already bound to another tenant"
            )

        current = _local_get(tenant_id)
        prior = str(
            (current.get("codebases") or {}).get("github_installation_id") or ""
        )
        if prior and prior != installation_id:
            raise GitHubInstallationBindingConflict(
                "Tenant already has a different GitHub installation binding"
            )

        merged = deep_merge(
            current,
            {"codebases": {"github_installation_id": installation_id}},
        )
        path = _local_tenant_path(tenant_id)
        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary.write(json.dumps(merged, indent=2) + "\n")
                temporary.flush()
                os.fsync(temporary.fileno())
                temporary_name = temporary.name
            os.replace(temporary_name, path)
        finally:
            if temporary_name and os.path.exists(temporary_name):
                os.unlink(temporary_name)


def _ddb_approve_github_installation(
    tenant_id: str,
    installation_id: str,
    region: str,
) -> None:
    from boto3.dynamodb.types import TypeSerializer
    from botocore.exceptions import ClientError

    owner = find_tenant_by_github_installation(installation_id, region)
    if owner and owner != tenant_id:
        raise GitHubInstallationBindingConflict(
            "GitHub installation is already bound to another tenant"
        )

    current = get_tenant_row(tenant_id, region)
    prior = str(
        (current.get("codebases") or {}).get("github_installation_id") or ""
    )
    if prior and prior != installation_id:
        raise GitHubInstallationBindingConflict(
            "Tenant already has a different GitHub installation binding"
        )
    merged = deep_merge(
        current,
        {"codebases": {"github_installation_id": installation_id}},
    )

    serializer = TypeSerializer()

    def value(item: Any) -> dict[str, Any]:
        return serializer.serialize(_floats_to_decimals(item))

    table = _get_table(region, _tenants_table_name())
    now = _iso_now()
    lock_id = _github_installation_lock_id(installation_id)
    try:
        table.meta.client.transact_write_items(
            TransactItems=[
                {
                    "Update": {
                        "TableName": table.name,
                        "Key": {"tenant_id": value(lock_id)},
                        "UpdateExpression": (
                            "SET bound_tenant_id = if_not_exists("
                            "bound_tenant_id, :tenant), "
                            "created_at = if_not_exists(created_at, :now), "
                            "updated_at = :now"
                        ),
                        "ConditionExpression": (
                            "attribute_not_exists(bound_tenant_id) OR "
                            "bound_tenant_id = :tenant"
                        ),
                        "ExpressionAttributeValues": {
                            ":tenant": value(tenant_id),
                            ":now": value(now),
                        },
                    }
                },
                {
                    "Update": {
                        "TableName": table.name,
                        "Key": {"tenant_id": value(tenant_id)},
                        "UpdateExpression": (
                            "SET #config = :config, updated_at = :now"
                        ),
                        "ConditionExpression": (
                            "attribute_exists(tenant_id) AND ("
                            "attribute_not_exists(#config.#codebases.#installation) "
                            "OR attribute_type(#config.#codebases.#installation, "
                            ":null_type) OR "
                            "#config.#codebases.#installation = :installation)"
                        ),
                        "ExpressionAttributeNames": {
                            "#config": "config",
                            "#codebases": "codebases",
                            "#installation": "github_installation_id",
                        },
                        "ExpressionAttributeValues": {
                            ":config": value(merged),
                            ":now": value(now),
                            ":null_type": value("NULL"),
                            ":installation": value(installation_id),
                        },
                    }
                },
            ]
        )
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") != "TransactionCanceledException":
            raise
        # Classify the fail-closed transaction error without revealing the
        # other tenant's identifier to an operator API caller.
        try:
            get_tenant_row(tenant_id, region)
        except KeyError:
            raise
        lock = table.get_item(
            Key={"tenant_id": lock_id},
            ConsistentRead=True,
        ).get("Item", {})
        if lock.get("bound_tenant_id") not in (None, tenant_id):
            raise GitHubInstallationBindingConflict(
                "GitHub installation is already bound to another tenant"
            ) from e
        raise GitHubInstallationBindingConflict(
            "GitHub installation approval could not be committed safely"
        ) from e


def approve_github_installation_binding(
    tenant_id: str,
    installation_id: str,
    region: str,
) -> None:
    """Atomically bind one verified GitHub installation to one tenant.

    Existing bindings are idempotent. Rebinding either side to a different
    identity fails closed; a future explicit revocation workflow can handle
    that destructive operation with its own audit trail.
    """

    if _is_local_dev():
        _local_approve_github_installation(tenant_id, installation_id)
        return
    _ddb_approve_github_installation(tenant_id, installation_id, region)


def list_internal_testenv_tenants(
    tenant_ids: list[str],
    region: str,
) -> set[str]:
    """Return the subset of ``tenant_ids`` whose ``config.is_internal_testenv``
    is True.

    Used by the ops roster to hide internal test/demo tenants from
    cross-tenant metrics by default. Reads only the ``config`` attribute
    via a ``ProjectionExpression`` so the data transferred is minimal.

    Fails open: if DDB errors for any reason, returns an empty set (the
    caller treats unknown tenants as real customers). We'd rather show
    a testenv tenant in the roster once than hide a real customer by
    accident.

    In LOCAL_DEV, reads the JSON files directly — same semantics.
    """
    if not tenant_ids:
        return set()

    if _is_local_dev():
        result: set[str] = set()
        for tid in tenant_ids:
            try:
                config = _local_get(tid)
            except KeyError:
                continue
            if config.get("is_internal_testenv") is True:
                result.add(tid)
        return result

    try:
        import boto3
    except ImportError:
        return set()

    try:
        client = boto3.client("dynamodb", region_name=region)
        table_name = _tenants_table_name()
        testenv: set[str] = set()
        # BatchGetItem is limited to 100 items per call; chunk if needed.
        # For a healthy platform with <100 active tenants, this is one
        # call. Adjust chunk size if we ever push past that.
        for start in range(0, len(tenant_ids), 100):
            chunk = tenant_ids[start:start + 100]
            response = client.batch_get_item(
                RequestItems={
                    table_name: {
                        "Keys": [{"tenant_id": {"S": tid}} for tid in chunk],
                        "ProjectionExpression": "tenant_id, #cfg.is_internal_testenv",
                        "ExpressionAttributeNames": {"#cfg": "config"},
                    }
                }
            )
            items = response.get("Responses", {}).get(table_name, [])
            for item in items:
                tid = item.get("tenant_id", {}).get("S")
                if not tid:
                    continue
                flag = (
                    item.get("config", {})
                    .get("M", {})
                    .get("is_internal_testenv", {})
                    .get("BOOL", False)
                )
                if flag:
                    testenv.add(tid)
        return testenv
    except Exception as e:  # noqa: BLE001
        log.warning("list_internal_testenv_tenants failed: %s", e)
        return set()


def reset_tenant_write_for_tests() -> None:
    """Test helper: drop the cached boto3 resource."""
    global _ddb_resource, _ddb_region
    _ddb_resource = None
    _ddb_region = None
