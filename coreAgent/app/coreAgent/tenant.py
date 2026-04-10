"""Tenant configuration: the source of truth for per-customer agent behavior.

Each customer gets a TenantConfig that drives:
  - which model to use
  - the system prompt
  - which catalog tools are available (whitelist)
  - whether BYO tools via AgentCore Gateway are wired up
  - memory rules (triggers, namespace, extraction rules)
  - heartbeat thresholds

Storage:
  - AGENT_LOCAL_STORES=1: JSON files at examples/tenants/<id>.json
  - else:                 DynamoDB table (name via TENANTS_TABLE, default "tenants")

The env var is deliberately NOT named `LOCAL_DEV`: the AgentCore CLI
hardcodes `LOCAL_DEV=1` into every `agentcore dev` subprocess (to flag
local-credential mode), which would override any value we set and force
the JSON path even in production-mode-locally smoke tests.

The `TenantStore` Protocol mirrors `memory_store.MemoryStore` — pick an
impl via env var, load lazily on first use, keep `load_tenant_config()`'s
signature unchanged so callers don't care which backend is live.

`create_default(tenant_id)` is the shared write path used by:
  - the seed script `infra/data/scripts/seed_tenants.py`
  - the Slack OAuth callback (week 2)
  - the onboarding UI (week 3)
all of which need consistent `if_not_exists(created_at, :now)` semantics.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, Field


class CatalogConfig(BaseModel):
    """Which platform-shipped tools the tenant can use, and per-tool config."""
    allowed_tools: list[str] = Field(default_factory=list)
    tool_config: dict[str, dict[str, Any]] = Field(default_factory=dict)


class ByoConfig(BaseModel):
    """BYO tools via AgentCore Gateway (MCP). When enabled, the agent connects
    to the tenant's Gateway endpoint as an MCP client at invocation time and
    exposes the remote tools alongside the catalog tools."""
    enabled: bool = False
    gateway_endpoint: str | None = None
    gateway_auth: dict[str, Any] | None = None
    connected_integrations: list[str] = Field(default_factory=list)


class MemoryTriggers(BaseModel):
    """Triggers that fire AgentCore Memory's self-managed extraction pipeline.
    These map directly to AgentCore Memory selfManagedConfiguration triggers."""
    message_count: int = 6
    token_count: int = 1000
    idle_timeout_seconds: int = 1800


class MemoryExtraction(BaseModel):
    enabled: bool = True
    rules: list[str] = Field(default_factory=list)


class MemoryConfig(BaseModel):
    triggers: MemoryTriggers = Field(default_factory=MemoryTriggers)
    namespace: str = ""
    extraction: MemoryExtraction = Field(default_factory=MemoryExtraction)


class HeartbeatConfig(BaseModel):
    """Controls how the custom @app.ping handler reports HealthyBusy.

    busy_threshold: minimum number of in-flight background tasks to report
        HealthyBusy. >0 means the agent stays alive while work is pending.
    max_background_seconds: soft cap on how long any single background task
        is expected to run; tools should respect this when sizing work."""
    busy_threshold: int = 1
    max_background_seconds: int = 3600


class ChannelPersona(BaseModel):
    """Per-channel overrides. When present, these fields REPLACE (not merge)
    the tenant-level defaults for invocations in this channel. ``None``
    means "inherit from tenant base."

    Used by the channel-persona merge in ``main.py``: after loading the
    tenant's base ``TenantConfig``, the entrypoint checks
    ``config.channels[channel_id]`` and overrides ``system_prompt``,
    ``allowed_tools``, and ``memory_rules`` if set.
    """
    system_prompt: str | None = None
    allowed_tools: list[str] | None = None
    memory_rules: list[str] | None = None


class TenantConfig(BaseModel):
    tenant_id: str
    model_id: str = "global.anthropic.claude-sonnet-4-6"
    system_prompt: str = "You are a helpful assistant."
    catalog: CatalogConfig = Field(default_factory=CatalogConfig)
    byo: ByoConfig = Field(default_factory=ByoConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    heartbeat: HeartbeatConfig = Field(default_factory=HeartbeatConfig)
    channels: dict[str, ChannelPersona] = Field(default_factory=dict)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def build_default_config(tenant_id: str) -> TenantConfig:
    """Build a TenantConfig with sane defaults for a brand-new tenant.

    Used by the OAuth callback and onboarding UI when a customer first
    connects. Customers customize their config from the onboarding UI
    after this initial row exists.

    Defaults:
      - Model: the platform default (Claude Sonnet 4.6)
      - System prompt: a generic helpful-assistant default. **Must be
        non-empty** — Bedrock's Converse API rejects empty system blocks
        with a `system[0].text min length: 1` validation error. Customers
        override this in the onboarding UI (week 3).
      - Catalog: only `echo` enabled (proves the bot works without
        exposing anything sensitive)
      - BYO: disabled
      - Memory: extraction enabled with the default rules; namespace
        scoped to the tenant
      - Heartbeat: defaults
    """
    return TenantConfig(
        tenant_id=tenant_id,
        system_prompt="You are a helpful assistant.",
        catalog=CatalogConfig(allowed_tools=["echo"]),
        memory=MemoryConfig(
            namespace=f"tenants/{tenant_id}",
            extraction=MemoryExtraction(enabled=True, rules=["user_preferences", "facts"]),
        ),
    )


# ----------------------------------------------------------------------------
# Storage contract
# ----------------------------------------------------------------------------

class TenantStore(Protocol):
    """Storage contract for tenant rows.

    Two impls:
      - `JsonFileTenantStore` for AGENT_LOCAL_STORES=1
      - `DynamoTenantStore` for production

    Write semantics:
      - `upsert(config)` is the base primitive. Idempotent: re-running
        against an existing tenant_id refreshes `updated_at` and the
        config blob, but preserves `created_at` (`if_not_exists` semantics).
      - `create_default(tenant_id)` is a thin wrapper that builds the
        default config and calls `upsert`. It's the shared entry point
        for the OAuth callback, the seed script (when seeding a new
        tenant with no JSON file yet), and the onboarding UI.
    """

    def get(self, tenant_id: str) -> TenantConfig: ...

    def upsert(self, config: TenantConfig) -> None: ...

    def create_default(self, tenant_id: str) -> TenantConfig: ...


class JsonFileTenantStore:
    """Reads `examples/tenants/<tenant_id>.json` from the repo root.

    Walks up from this file to find `examples/tenants/`. Used by
    `agentcore dev` and unit tests so there is zero AWS dependency in the
    local loop.
    """

    def __init__(self) -> None:
        self._root: Path | None = None

    def _find_root(self) -> Path:
        if self._root is not None:
            return self._root
        current = Path(__file__).resolve()
        for parent in current.parents:
            candidate = parent / "examples" / "tenants"
            if candidate.is_dir():
                self._root = candidate
                return self._root
        raise FileNotFoundError(
            f"Could not find examples/tenants/ above {current}"
        )

    def get(self, tenant_id: str) -> TenantConfig:
        path = self._find_root() / f"{tenant_id}.json"
        if not path.exists():
            raise KeyError(f"No tenant config for tenant_id={tenant_id!r} at {path}")
        return TenantConfig.model_validate_json(path.read_text())

    def upsert(self, config: TenantConfig) -> None:
        """Write a config to disk, overwriting any existing file. JSON
        files don't carry metadata fields, so there's no created_at to
        preserve here — the on-disk shape is just the TenantConfig."""
        path = self._find_root() / f"{config.tenant_id}.json"
        path.write_text(config.model_dump_json(indent=2) + "\n")

    def create_default(self, tenant_id: str) -> TenantConfig:
        """Idempotent default-row creation. If the file already exists,
        it's left untouched and the existing config is returned (matches
        the Dynamo `if_not_exists` semantics)."""
        path = self._find_root() / f"{tenant_id}.json"
        if path.exists():
            return TenantConfig.model_validate_json(path.read_text())
        config = build_default_config(tenant_id)
        self.upsert(config)
        return config


class DynamoTenantStore:
    """Reads tenant rows from a DynamoDB table.

    Item shape:
        {
            tenant_id: str,                # partition key
            config:    dict,               # full TenantConfig as a nested map
            created_at: str,               # ISO8601, GDPR/audit hedge
            updated_at: str,               # ISO8601
        }

    Only ever calls `get_item(Key={tenant_id: X})` — never scans. This is
    the application-level enforcement for the shared-table isolation model:
    every caller has to pass a tenant_id and we only ever query that one.
    """

    def __init__(self, table_name: str, region: str | None = None) -> None:
        self.table_name = table_name
        self.region = region or os.getenv("AWS_REGION", "us-west-2")
        self._table: Any | None = None

    def _get_table(self) -> Any:
        if self._table is None:
            import boto3

            resource = boto3.resource("dynamodb", region_name=self.region)
            self._table = resource.Table(self.table_name)
        return self._table

    def get(self, tenant_id: str) -> TenantConfig:
        response = self._get_table().get_item(Key={"tenant_id": tenant_id})
        item = response.get("Item")
        if not item:
            raise KeyError(
                f"No tenant config for tenant_id={tenant_id!r} in "
                f"table={self.table_name!r}"
            )
        # The row carries metadata fields (created_at, updated_at) alongside
        # the config. Strip them before Pydantic validation. If the config
        # is nested under a "config" key, use that; otherwise treat the
        # whole item as the config (minus metadata).
        if "config" in item and isinstance(item["config"], dict):
            config_data = item["config"]
        else:
            config_data = {k: v for k, v in item.items()
                           if k not in {"created_at", "updated_at"}}
        return TenantConfig.model_validate(config_data)

    def upsert(self, config: TenantConfig) -> None:
        """Idempotent write of a tenant row.

        UpdateExpression:
            SET #config = :config,
                updated_at = :now,
                created_at = if_not_exists(created_at, :now)

        Re-running for an existing tenant_id refreshes the config blob
        and `updated_at` but preserves the original `created_at`.

        This is the canonical write primitive. The seed script and the
        OAuth callback (via `create_default`) both flow through here so
        the row shape and timestamp semantics are guaranteed identical.

        Note: this OVERWRITES the config blob on every call. Per-field
        merge updates (e.g. "user changed system_prompt in the
        onboarding UI") need a separate code path — TBD in week 3.
        """
        now = _iso_now()
        self._get_table().update_item(
            Key={"tenant_id": config.tenant_id},
            UpdateExpression=(
                "SET #config = :config, "
                "updated_at = :now, "
                "created_at = if_not_exists(created_at, :now)"
            ),
            ExpressionAttributeNames={"#config": "config"},
            ExpressionAttributeValues={
                ":config": config.model_dump(),
                ":now": now,
            },
        )

    def create_default(self, tenant_id: str) -> TenantConfig:
        """Idempotent default-row creation. Builds the default config and
        writes it via `upsert`."""
        config = build_default_config(tenant_id)
        self.upsert(config)
        return config


# ----------------------------------------------------------------------------
# Lazy singleton
# ----------------------------------------------------------------------------

_default_store: TenantStore | None = None


def _store() -> TenantStore:
    global _default_store
    if _default_store is None:
        if os.getenv("AGENT_LOCAL_STORES") == "1":
            _default_store = JsonFileTenantStore()
        else:
            _default_store = DynamoTenantStore(
                table_name=os.getenv("TENANTS_TABLE", "tenants"),
            )
    return _default_store


def load_tenant_config(tenant_id: str) -> TenantConfig:
    """Load a tenant's config, dispatching to JSON (AGENT_LOCAL_STORES=1) or DynamoDB.

    Signature unchanged from v0 so `main.py:invoke` keeps working. The
    backing store is chosen lazily on first call.
    """
    return _store().get(tenant_id)


def create_default_tenant(tenant_id: str) -> TenantConfig:
    """Idempotently create a default tenant row, dispatching to JSON
    (AGENT_LOCAL_STORES=1) or DynamoDB.

    Used by:
      - `infra/data/scripts/seed_tenants.py` (bulk seeding from local files)
      - the Slack OAuth callback in `bridge/bridge/main.py` (week 2)
      - the onboarding UI (week 3)

    All three callers share this function so the row shape and the
    `if_not_exists(created_at, :now)` semantics are guaranteed identical.
    """
    return _store().create_default(tenant_id)


def reset_store_for_tests() -> None:
    """Test helper: clear the cached store so the next `load_tenant_config`
    call re-reads env vars. Not used by production code."""
    global _default_store
    _default_store = None
