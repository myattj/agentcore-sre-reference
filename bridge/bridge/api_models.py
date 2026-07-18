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

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.json_schema import SkipJsonSchema
from pydantic_core import SchemaError, SchemaValidator, core_schema


# Keep this trigger contract aligned with
# ``coreAgent/app/coreAgent/tenant.py``. The services intentionally do not
# cross-import at runtime because they ship in separate containers.
MAX_SKILL_TRIGGER_LENGTH = 512


def _has_repeated_skill_group(pattern: str) -> bool:
    escaped = False
    in_character_class = False
    for index, char in enumerate(pattern):
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == "[":
            in_character_class = True
            continue
        if char == "]" and in_character_class:
            in_character_class = False
            continue
        if char != ")" or in_character_class or index + 1 >= len(pattern):
            continue
        if pattern[index + 1] in {"*", "+", "{"}:
            return True
    return False


def _validate_skill_trigger(value: str) -> str:
    if value.startswith("/"):
        return value
    if _has_repeated_skill_group(value):
        raise ValueError("skill trigger regex must not repeat a group")
    try:
        SchemaValidator(
            core_schema.str_schema(
                pattern=f"(?i:{value})",
                regex_engine="rust-regex",
            )
        )
    except SchemaError as exc:
        raise ValueError("skill trigger must use the safe regex subset") from exc
    return value


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
    shared_across_channels: bool = False
    isolated_channels: list[str] = Field(default_factory=list)


class HeartbeatConfigOut(BaseModel):
    busy_threshold: int = 1
    max_background_seconds: int = 3600


class CostCapConfigOut(BaseModel):
    monthly_limit_dollars: float = 50.0
    enabled: bool = True


class ChannelPersonaOut(BaseModel):
    """Per-channel overrides. Mirrors ``coreAgent.tenant.ChannelPersona``."""
    system_prompt: str | None = None
    allowed_tools: list[str] | None = None
    memory_rules: list[str] | None = None


class BotPolicyConfigOut(BaseModel):
    allow_all_bots: bool = False
    trusted_bot_ids: list[str] = Field(default_factory=list)
    open_channels: list[str] = Field(default_factory=list)


class ContextAssemblyConfigOut(BaseModel):
    resolve_permalinks: bool = True
    inject_thread_history: bool = True
    thread_history_depth: int = 25
    max_permalinks: int = 3


class SkillDefOut(BaseModel):
    trigger: str
    name: str
    prompt_template: str
    required_tools: list[str] = Field(default_factory=list)
    channels: list[str] = Field(default_factory=list)


class EscalationRouteOut(BaseModel):
    team_name: str
    channel_id: str
    description: str = ""
    contacts: list[str] = Field(default_factory=list)


class EscalationConfigOut(BaseModel):
    routes: list[EscalationRouteOut] = Field(default_factory=list)


class CodebaseBindingOut(BaseModel):
    """A single repo binding. Mirrors ``coreAgent.tenant.CodebaseBinding``."""
    repo: str
    default_branch: str = "main"
    aliases: list[str] = Field(default_factory=list)
    channels: list[str] = Field(default_factory=list)


class CodebasesConfigOut(BaseModel):
    """Per-tenant code access layer. Mirrors ``coreAgent.tenant.CodebasesConfig``.

    Drives the GitHub-App-backed code tools and the discovery layer that
    picks which repo a Slack message refers to.
    """
    enabled: bool = False
    github_installation_id: str | None = None
    default_repo: str | None = None
    bindings: list[CodebaseBindingOut] = Field(default_factory=list)
    allow_learning: bool = True


class TenantConfigOut(BaseModel):
    """Full tenant config returned by GET /api/tenants/{tenant_id}.

    Mirrors `coreAgent/app/coreAgent/tenant.py:TenantConfig`. All fields
    have defaults so that an incomplete DDB row (e.g. from an earlier
    schema version) still validates.
    """

    model_config = ConfigDict(extra="ignore")

    tenant_id: str
    model_id: str = "global.anthropic.claude-sonnet-4-6"
    # Fallback only — real new tenants get the full ``DEFAULT_SYSTEM_PROMPT``
    # from ``tenant_write.build_default_config_dict``. This one-liner is
    # just a safety net for legacy rows that somehow lost the field.
    system_prompt: str = "You are a helpful assistant."
    catalog: CatalogConfigOut = Field(default_factory=CatalogConfigOut)
    byo: ByoConfigOut = Field(default_factory=ByoConfigOut)
    memory: MemoryConfigOut = Field(default_factory=MemoryConfigOut)
    heartbeat: HeartbeatConfigOut = Field(default_factory=HeartbeatConfigOut)
    cost_cap: CostCapConfigOut = Field(default_factory=CostCapConfigOut)
    channels: dict[str, ChannelPersonaOut] = Field(default_factory=dict)
    admin_user_ids: list[str] = Field(default_factory=list)
    bot_policy: BotPolicyConfigOut = Field(default_factory=BotPolicyConfigOut)
    context_assembly: ContextAssemblyConfigOut = Field(default_factory=ContextAssemblyConfigOut)
    skills: list[SkillDefOut] = Field(default_factory=list)
    escalation: EscalationConfigOut = Field(default_factory=EscalationConfigOut)
    codebases: CodebasesConfigOut = Field(default_factory=CodebasesConfigOut)
    # Marks a tenant as an internal test/demo environment. See
    # ``coreAgent.tenant.TenantConfig.is_internal_testenv`` for semantics.
    is_internal_testenv: bool = False


# ----------------------------------------------------------------------------
# Patch shape (PATCH request body — all fields optional)
# ----------------------------------------------------------------------------

class CatalogConfigPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    allowed_tools: list[str] | None = None
    tool_config: dict[str, dict[str, Any]] | None = None


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
    # namespace is a storage-isolation boundary and is intentionally
    # operator-managed. A tenant bearer must not select another tenant's
    # memory namespace.
    extraction: MemoryExtractionPatch | None = None
    # ``None`` is an internal omission sentinel, not an API value. Keep it out
    # of OpenAPI; an explicitly supplied null reaches full-config validation,
    # which returns a generic 422 without reflecting request or stored input.
    shared_across_channels: bool | SkipJsonSchema[None] = None
    isolated_channels: list[str] | None = None


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


class BotPolicyConfigPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    allow_all_bots: bool | None = None
    trusted_bot_ids: list[str] | None = None
    open_channels: list[str] | None = None


class ContextAssemblyConfigPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    resolve_permalinks: bool | None = None
    inject_thread_history: bool | None = None
    thread_history_depth: int | None = None
    max_permalinks: int | None = None


class SkillDefPatch(BaseModel):
    """Skill definition for PATCH. Skills list is replaced wholesale."""
    model_config = ConfigDict(extra="forbid")
    trigger: str = Field(min_length=1, max_length=MAX_SKILL_TRIGGER_LENGTH)
    name: str
    prompt_template: str
    required_tools: list[str] = Field(default_factory=list)
    channels: list[str] = Field(default_factory=list)

    @field_validator("trigger")
    @classmethod
    def _safe_trigger(cls, value: str) -> str:
        return _validate_skill_trigger(value)


class EscalationRoutePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    team_name: str
    channel_id: str
    description: str = ""
    contacts: list[str] = Field(default_factory=list)


class EscalationConfigPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    routes: list[EscalationRoutePatch] | None = None


class CodebaseBindingPatch(BaseModel):
    """A single codebase binding for PATCH. Bindings list is replaced wholesale."""
    model_config = ConfigDict(extra="forbid")
    repo: str
    default_branch: str = "main"
    aliases: list[str] = Field(default_factory=list)
    channels: list[str] = Field(default_factory=list)


class CodebasesConfigPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool | None = None
    # github_installation_id is intentionally not patchable through the
    # tenant session API. It is an operator-approved trust binding; allowing a
    # tenant to set it would let them claim another GitHub App installation.
    default_repo: str | None = None
    bindings: list[CodebaseBindingPatch] | None = None
    allow_learning: bool | None = None


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
    # ``byo`` is intentionally absent. Gateway endpoints, authentication,
    # enablement, and connection markers are operator/connector-managed trust
    # configuration, never tenant-session writable.
    memory: MemoryConfigPatch | None = None
    heartbeat: HeartbeatConfigPatch | None = None
    # cost_cap is platform enforcement policy and is intentionally absent.
    channels: dict[str, ChannelPersonaPatch] | None = None
    # admin_user_ids is intentionally operator-managed and not patchable by a
    # tenant session; otherwise any bearer could grant itself runtime admin.
    bot_policy: BotPolicyConfigPatch | None = None
    context_assembly: ContextAssemblyConfigPatch | None = None
    skills: list[SkillDefPatch] | None = None
    escalation: EscalationConfigPatch | None = None
    codebases: CodebasesConfigPatch | None = None
    # is_internal_testenv is an operator accounting/visibility marker and is
    # intentionally absent from the tenant-session PATCH surface.


# ----------------------------------------------------------------------------
# Integration connection requests
# ----------------------------------------------------------------------------

DATADOG_SITES = frozenset(
    {
        "datadoghq.com",
        "us3.datadoghq.com",
        "us5.datadoghq.com",
        "datadoghq.eu",
        "ap1.datadoghq.com",
    }
)
_ATLASSIAN_LABEL_RE = re.compile(
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z"
)
_GITHUB_LOGIN_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?\Z")
_RESERVED_ATLASSIAN_LABELS = frozenset({"localhost", "metadata"})


def _validate_atlassian_domain(value: str) -> str:
    domain = value.strip().lower()
    if (
        not _ATLASSIAN_LABEL_RE.fullmatch(domain)
        or domain in _RESERVED_ATLASSIAN_LABELS
    ):
        raise ValueError("domain must be one safe Atlassian DNS label")
    return domain


def _validate_installation_id_input(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError("installation_id must be a positive integer")
    raw = str(value).strip()
    if not raw.isascii() or not raw.isdecimal():
        raise ValueError("installation_id must be a positive integer")
    number = int(raw, 10)
    if number <= 0 or number > 2**63 - 1:
        raise ValueError("installation_id must be a positive 64-bit integer")
    return number

class DatadogConnectRequest(BaseModel):
    """Fail-closed Datadog request; secret material is never accepted."""
    # This one model captures extras so the route can return a generic 422.
    # Pydantic's normal extra="forbid" error includes the rejected input value
    # and would reflect a mistakenly submitted API key back in the response.
    model_config = ConfigDict(extra="allow")
    site: str = Field(
        default="datadoghq.com",
        description="Datadog site (e.g. datadoghq.com, datadoghq.eu, us5.datadoghq.com)",
    )

    @field_validator("site")
    @classmethod
    def validate_site(cls, value: str) -> str:
        site = value.strip().lower()
        if site not in DATADOG_SITES:
            raise ValueError("unsupported Datadog site")
        return site


class ConfluenceConnectRequest(BaseModel):
    """POST /api/tenants/{id}/integrations/confluence body."""
    model_config = ConfigDict(extra="forbid")
    email: str = Field(..., min_length=1, description="Atlassian account email")
    api_token: str = Field(..., min_length=1, description="Atlassian API token")
    domain: str = Field(..., min_length=1, description="Atlassian domain (e.g. 'mycompany' for mycompany.atlassian.net)")

    @field_validator("domain")
    @classmethod
    def validate_domain(cls, value: str) -> str:
        return _validate_atlassian_domain(value)


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

    @field_validator("domain")
    @classmethod
    def validate_domain(cls, value: str) -> str:
        return _validate_atlassian_domain(value)


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


class GitHubAppInstallRequest(BaseModel):
    """POST /api/tenants/{id}/codebases/github/install body.

    Distinct from ``GitHubConnectRequest`` (the BYO PAT flow that
    provisions a Gateway target). This is the GitHub App install flow:
    the onboarding UI redirects the user to install the Agent GitHub
    App on their org, GitHub redirects back with an ``installation_id``,
    and the UI POSTs that id here to trigger the warm-start (list repos,
    rank, seed the ``codebases`` block on the tenant row).
    """
    model_config = ConfigDict(extra="forbid")
    installation_id: int = Field(
        ...,
        gt=0,
        le=2**63 - 1,
        description="Numeric GitHub App installation ID from the install callback",
    )

    @field_validator("installation_id", mode="before")
    @classmethod
    def validate_installation_id(cls, value: Any) -> int:
        return _validate_installation_id_input(value)


class GitHubAppApprovalRequest(BaseModel):
    """Operator-confirmed GitHub installation identity."""

    model_config = ConfigDict(extra="forbid")
    installation_id: int = Field(..., gt=0, le=2**63 - 1)
    expected_account_login: str = Field(..., min_length=1, max_length=39)

    @field_validator("installation_id", mode="before")
    @classmethod
    def validate_installation_id(cls, value: Any) -> int:
        return _validate_installation_id_input(value)

    @field_validator("expected_account_login")
    @classmethod
    def validate_account_login(cls, value: str) -> str:
        login = value.strip()
        if not _GITHUB_LOGIN_RE.fullmatch(login) or "--" in login:
            raise ValueError("expected_account_login must be a valid GitHub login")
        return login


class CodebaseBindingBrief(BaseModel):
    """Compact binding shape returned from the install endpoint."""
    repo: str
    default_branch: str


class GitHubAppInstallResponse(BaseModel):
    """Response from POST /api/tenants/{id}/codebases/github/install."""
    ok: bool
    installation_id: str
    default_repo: str | None = None
    bindings: list[CodebaseBindingBrief] = Field(default_factory=list)
    total_repos_available: int = 0
    pending_approval: bool = False
    error: str | None = None


class GitHubAppApprovalResponse(BaseModel):
    """Response from the operator-only installation approval endpoint."""

    approved: bool = True
    tenant_id: str
    installation_id: str
    account_login: str


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
