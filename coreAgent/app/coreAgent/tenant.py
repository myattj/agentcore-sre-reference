"""Tenant configuration: the source of truth for per-customer agent behavior.

Each customer gets a TenantConfig that drives:
  - which model to use
  - the system prompt
  - which catalog tools are available (whitelist)
  - whether BYO tools via AgentCore Gateway are wired up
  - memory rules (triggers, namespace, extraction rules)
  - heartbeat thresholds

v0 loader reads JSON from examples/tenants/<id>.json. Replace with
DynamoDB or Postgres when there are more than a handful of tenants.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

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


class TenantConfig(BaseModel):
    tenant_id: str
    model_id: str = "global.anthropic.claude-sonnet-4-6"
    system_prompt: str = "You are a helpful assistant."
    catalog: CatalogConfig = Field(default_factory=CatalogConfig)
    byo: ByoConfig = Field(default_factory=ByoConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    heartbeat: HeartbeatConfig = Field(default_factory=HeartbeatConfig)


def load_tenant_config(tenant_id: str) -> TenantConfig:
    """Load a tenant's config from examples/tenants/<tenant_id>.json.

    v0 only. Walks up from this file to find the repo root containing
    `examples/tenants/`. Replace with a DynamoDB/Postgres-backed loader
    when tenant count grows.
    """
    current = Path(__file__).resolve()
    for parent in current.parents:
        candidate = parent / "examples" / "tenants" / f"{tenant_id}.json"
        if candidate.exists():
            return TenantConfig.model_validate_json(candidate.read_text())
    raise FileNotFoundError(
        f"No tenant config found for tenant_id={tenant_id!r}. "
        f"Searched parents of {current}"
    )
