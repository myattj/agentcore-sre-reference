"""Pydantic request/response models for `/api/tenants/*` routes.

These models are the bridge's validation boundary for the onboarding
UI. The Next.js server sends JSON; FastAPI + Pydantic validate it;
invalid payloads return 422 before `tenant_write.update_tenant_row` is
called. This is why the onboarding UI doesn't need to replicate the
full validation logic — it can hand-roll form state against
`onboarding/lib/types.ts` and let the bridge reject bad shapes.

**KEEP IN SYNC with `coreAgent/app/coreAgent/tenant.py:TenantConfig`
(lines 41-93) and `bridge/bridge/tenant_write.py:build_default_config_dict`.**
The three copies exist because bridge / coreAgent / Next.js are three
separate packages with separate venvs. See CLAUDE.md gotcha #21.

Two variants of each nested model:
  - `*Out`: full shape with defaults; used for GET responses
  - `*Patch`: all fields optional; used for PATCH bodies. The bridge's
    PATCH handler calls `model_dump(exclude_unset=True)` to get a
    sparse dict and feeds it through `tenant_write.deep_merge`.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


# ----------------------------------------------------------------------------
# Full shape (GET response, matches DDB config blob)
# ----------------------------------------------------------------------------

class CatalogConfigOut(BaseModel):
    allowed_tools: list[str] = Field(default_factory=list)
    tool_config: dict[str, dict[str, Any]] = Field(default_factory=dict)


class ByoConfigOut(BaseModel):
    enabled: bool = False
    gateway_endpoint: str | None = None
    gateway_auth: dict[str, Any] | None = None
    connected_integrations: list[str] = Field(default_factory=list)


class MemoryTriggersOut(BaseModel):
    message_count: int = 6
    token_count: int = 1000
    idle_timeout_seconds: int = 1800


class MemoryExtractionOut(BaseModel):
    enabled: bool = True
    rules: list[str] = Field(default_factory=list)


class MemoryConfigOut(BaseModel):
    triggers: MemoryTriggersOut = Field(default_factory=MemoryTriggersOut)
    namespace: str = ""
    extraction: MemoryExtractionOut = Field(default_factory=MemoryExtractionOut)


class HeartbeatConfigOut(BaseModel):
    busy_threshold: int = 1
    max_background_seconds: int = 3600


class ChannelPersonaOut(BaseModel):
    """Per-channel overrides. Mirrors ``coreAgent.tenant.ChannelPersona``."""
    system_prompt: str | None = None
    allowed_tools: list[str] | None = None
    memory_rules: list[str] | None = None


class TenantConfigOut(BaseModel):
    """Full tenant config returned by GET /api/tenants/{tenant_id}.

    Mirrors `coreAgent/app/coreAgent/tenant.py:TenantConfig`. All fields
    have defaults so that an incomplete DDB row (e.g. from an earlier
    schema version) still validates.
    """

    model_config = ConfigDict(extra="ignore")

    tenant_id: str
    model_id: str = "global.anthropic.claude-sonnet-4-6"
    system_prompt: str = "You are a helpful assistant."
    catalog: CatalogConfigOut = Field(default_factory=CatalogConfigOut)
    byo: ByoConfigOut = Field(default_factory=ByoConfigOut)
    memory: MemoryConfigOut = Field(default_factory=MemoryConfigOut)
    heartbeat: HeartbeatConfigOut = Field(default_factory=HeartbeatConfigOut)
    channels: dict[str, ChannelPersonaOut] = Field(default_factory=dict)


# ----------------------------------------------------------------------------
# Patch shape (PATCH request body — all fields optional)
# ----------------------------------------------------------------------------

class CatalogConfigPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    allowed_tools: list[str] | None = None
    tool_config: dict[str, dict[str, Any]] | None = None


class ByoConfigPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool | None = None
    gateway_endpoint: str | None = None
    gateway_auth: dict[str, Any] | None = None
    connected_integrations: list[str] | None = None


class MemoryTriggersPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message_count: int | None = None
    token_count: int | None = None
    idle_timeout_seconds: int | None = None


class MemoryExtractionPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool | None = None
    rules: list[str] | None = None


class MemoryConfigPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    triggers: MemoryTriggersPatch | None = None
    namespace: str | None = None
    extraction: MemoryExtractionPatch | None = None


class HeartbeatConfigPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    busy_threshold: int | None = None
    max_background_seconds: int | None = None


class ChannelPersonaPatch(BaseModel):
    """Per-channel persona overrides for PATCH. Mirrors ``ChannelPersonaOut``."""
    model_config = ConfigDict(extra="forbid")
    system_prompt: str | None = None
    allowed_tools: list[str] | None = None
    memory_rules: list[str] | None = None


class TenantConfigPatch(BaseModel):
    """Partial TenantConfig for PATCH /api/tenants/{tenant_id}.

    All fields optional. Use `model_dump(exclude_unset=True)` to get a
    sparse dict that matches only fields the client actually sent, then
    feed it into `tenant_write.deep_merge` to layer on top of the
    existing config.
    """

    model_config = ConfigDict(extra="forbid")

    model_id: str | None = None
    system_prompt: str | None = Field(default=None, min_length=1)
    catalog: CatalogConfigPatch | None = None
    byo: ByoConfigPatch | None = None
    memory: MemoryConfigPatch | None = None
    heartbeat: HeartbeatConfigPatch | None = None
    channels: dict[str, ChannelPersonaPatch] | None = None


# ----------------------------------------------------------------------------
# Integration connect (week 4+5)
# ----------------------------------------------------------------------------

class DatadogConnectRequest(BaseModel):
    """POST /api/tenants/{id}/integrations/datadog body."""
    model_config = ConfigDict(extra="forbid")
    api_key: str = Field(..., min_length=1, description="Datadog API key")
    app_key: str = Field(..., min_length=1, description="Datadog Application key (required for most read endpoints)")
    site: str = Field(
        default="datadoghq.com",
        description="Datadog site (e.g. datadoghq.com, datadoghq.eu, us5.datadoghq.com)",
    )


class ConfluenceConnectRequest(BaseModel):
    """POST /api/tenants/{id}/integrations/confluence body."""
    model_config = ConfigDict(extra="forbid")
    email: str = Field(..., min_length=1, description="Atlassian account email")
    api_token: str = Field(..., min_length=1, description="Atlassian API token")
    domain: str = Field(..., min_length=1, description="Atlassian domain (e.g. 'mycompany' for mycompany.atlassian.net)")


class NotionConnectRequest(BaseModel):
    """POST /api/tenants/{id}/integrations/notion body."""
    model_config = ConfigDict(extra="forbid")
    integration_token: str = Field(..., min_length=1, description="Notion internal integration token")


class JiraConnectRequest(BaseModel):
    """POST /api/tenants/{id}/integrations/jira body."""
    model_config = ConfigDict(extra="forbid")
    email: str = Field(..., min_length=1, description="Atlassian account email")
    api_token: str = Field(..., min_length=1, description="Atlassian API token")
    domain: str = Field(..., min_length=1, description="Atlassian domain (e.g. 'mycompany' for mycompany.atlassian.net)")


class LinearConnectRequest(BaseModel):
    """POST /api/tenants/{id}/integrations/linear body."""
    model_config = ConfigDict(extra="forbid")
    api_key: str = Field(..., min_length=1, description="Linear API key")


class PagerDutyConnectRequest(BaseModel):
    """POST /api/tenants/{id}/integrations/pagerduty body."""
    model_config = ConfigDict(extra="forbid")
    api_key: str = Field(..., min_length=1, description="PagerDuty REST API key (v2)")


class GitHubConnectRequest(BaseModel):
    """POST /api/tenants/{id}/integrations/github body."""
    model_config = ConfigDict(extra="forbid")
    personal_access_token: str = Field(..., min_length=1, description="GitHub personal access token (fine-grained or classic)")
    org: str = Field(default="", description="GitHub org to scope queries to (optional)")


class IntegrationConnectResponse(BaseModel):
    """Response from POST /api/tenants/{id}/integrations/{integration}."""
    ok: bool
    integration: str
    target_name: str | None = None
    gateway_url: str | None = None
    error: str | None = None


# ----------------------------------------------------------------------------
# Channels
# ----------------------------------------------------------------------------

class ChannelInfo(BaseModel):
    id: str
    name: str
    is_private: bool = False


class ChannelsResponse(BaseModel):
    channels: list[ChannelInfo]
    # True when the bot token is valid but is missing one of the scopes
    # needed to list channels (channels:read / groups:read). The
    # onboarding UI uses this to show a "re-install to grant the new
    # scopes" hint instead of an error banner. Only set on the
    # graceful-degrade path; happy-path responses leave it false.
    needs_reinstall: bool = False
