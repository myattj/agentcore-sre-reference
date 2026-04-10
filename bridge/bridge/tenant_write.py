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
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


# ----------------------------------------------------------------------------
# Default tenant config dict
# ----------------------------------------------------------------------------

def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def build_default_config_dict(tenant_id: str) -> dict[str, Any]:
    """Build the default tenant config dict for a brand-new tenant.

    **KEEP IN SYNC with `coreAgent/app/coreAgent/tenant.py:build_default_config()`.**
    The two packages have separate venvs so we can't import; this is the
    minimal duplication required to provision a new tenant from the
    bridge. If you change the agent's default config shape, mirror it
    here and in `bridge/bridge/api_models.py:TenantConfigOut`.
    """
    return {
        "tenant_id": tenant_id,
        "model_id": "global.anthropic.claude-sonnet-4-6",
        # MUST be non-empty — Bedrock Converse rejects empty system blocks
        # (`system[0].text min length: 1`). Mirror of the same default in
        # coreAgent.tenant.build_default_config().
        "system_prompt": "You are a helpful assistant.",
        "catalog": {
            "allowed_tools": ["echo"],
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
        },
        "heartbeat": {
            "busy_threshold": 1,
            "max_background_seconds": 3600,
        },
        "channels": {},
    }


# ----------------------------------------------------------------------------
# Deep-merge helper for PATCH semantics
# ----------------------------------------------------------------------------

# Top-level fields that should be deep-merged one level down rather than
# wholesale-replaced. A PATCH like `{"catalog": {"allowed_tools": [...]}}`
# should preserve `catalog.tool_config`; Pydantic's `model_copy(update=...)`
# is SHALLOW and would drop the sibling field.
_DEEP_MERGE_FIELDS = frozenset({"catalog", "byo", "memory", "heartbeat", "channels"})


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
            ":config": build_default_config_dict(tenant_id),
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
                ":config": full_config,
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
