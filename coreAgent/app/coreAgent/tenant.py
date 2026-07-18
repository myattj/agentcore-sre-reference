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

import os
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, Field, field_validator
from pydantic_core import SchemaError, SchemaValidator, core_schema


# Tenant skill triggers are evaluated on every message, so their regex
# contract is deliberately narrower than Python's backtracking ``re`` engine.
# Pydantic Core's Rust regex engine guarantees linear-time matching and rejects
# unsupported constructs such as look-around and backreferences.  We also
# reject repeated groups as a conservative readability/safety rule: patterns
# such as ``(a+)+`` are the classic source of catastrophic backtracking when a
# trigger is copied into another regex implementation.
MAX_SKILL_TRIGGER_LENGTH = 512
MAX_SKILL_MATCH_TEXT_LENGTH = 8_192


def _has_repeated_group(pattern: str) -> bool:
    """Return whether a group is followed by ``*``, ``+``, or ``{...}``.

    Escaped closing parentheses and parentheses inside character classes are
    literals, not group boundaries. Optional groups (``(...)?``) remain
    allowed because they can match at most once.
    """
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
        next_char = pattern[index + 1]
        if next_char in {"*", "+", "{"}:
            return True
    return False


def compile_skill_trigger(trigger: str) -> SchemaValidator:
    """Validate and compile one non-slash skill trigger.

    Matching remains case-insensitive, preserving the previous ``re.IGNORECASE``
    behavior even when a trigger omits the optional inline ``(?i)`` flag.
    """
    if not trigger:
        raise ValueError("skill trigger must not be empty")
    if len(trigger) > MAX_SKILL_TRIGGER_LENGTH:
        raise ValueError(
            f"skill trigger must be at most {MAX_SKILL_TRIGGER_LENGTH} characters"
        )
    if _has_repeated_group(trigger):
        raise ValueError("skill trigger regex must not repeat a group")

    try:
        return SchemaValidator(
            core_schema.str_schema(
                pattern=f"(?i:{trigger})",
                regex_engine="rust-regex",
            )
        )
    except SchemaError as exc:
        raise ValueError("skill trigger must use the safe regex subset") from exc


def validate_skill_trigger(trigger: str) -> str:
    """Pydantic validator shared by tenant configuration models."""
    if trigger.startswith("/"):
        if len(trigger) > MAX_SKILL_TRIGGER_LENGTH:
            raise ValueError(
                f"skill trigger must be at most {MAX_SKILL_TRIGGER_LENGTH} characters"
            )
        return trigger
    compile_skill_trigger(trigger)
    return trigger


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
    # False is the safe default: conversations in one Slack channel must not
    # influence another channel unless an operator explicitly opts in.
    shared_across_channels: bool = False
    # When shared mode is enabled, these channels remain private silos.
    isolated_channels: list[str] = Field(default_factory=list)


class HeartbeatConfig(BaseModel):
    """Controls how the custom @app.ping handler reports HealthyBusy.

    busy_threshold: minimum number of in-flight background tasks to report
        HealthyBusy. >0 means the agent stays alive while work is pending.
    max_background_seconds: soft cap on how long any single background task
        is expected to run; tools should respect this when sizing work."""

    busy_threshold: int = 1
    max_background_seconds: int = 3600


class CostCapConfig(BaseModel):
    """Per-tenant monthly cost cap. Enforced at invocation time in main.py.

    When ``enabled`` is True, the agent checks accumulated spend for the
    current calendar month before building the Agent. If spend exceeds
    ``monthly_limit_dollars``, the invocation is blocked with a friendly
    message and no Bedrock tokens are consumed.

    The running spend counter lives on the DynamoDB tenant row as
    top-level attributes (``monthly_spend_cents``, ``spend_month``),
    NOT inside the config blob. See ``spend_tracker.py``.
    """

    monthly_limit_dollars: float = 50.0
    enabled: bool = True


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


class BotPolicyConfig(BaseModel):
    """Bot-to-bot interaction policy. Evaluated bridge-side BEFORE dispatch
    to prevent Bedrock spend on bot loops.

    Four tiers (evaluated in order):
      1. allow_all_bots — if True, ANY bot can trigger the agent. This is
         deliberately opt-in because bot messages can drive model spend
         and tool calls.
      2. trusted_bot_ids — always allowed (explicit whitelist)
      3. open_channels — any bot can trigger in these channels
      4. default — humans only (bot messages are dropped)
    """

    allow_all_bots: bool = False
    trusted_bot_ids: list[str] = Field(default_factory=list)
    open_channels: list[str] = Field(default_factory=list)


class ContextAssemblyConfig(BaseModel):
    """Controls the pre-LLM context assembly pipeline in context_assembler.py.

    Each flag enables/disables one assembly step. Depth params control how
    much context to fetch. All steps run in the agent before the Strands
    Agent is constructed.
    """

    resolve_permalinks: bool = True
    inject_thread_history: bool = True
    thread_history_depth: int = 25
    max_permalinks: int = 3


class SkillDef(BaseModel):
    """A single skill/runbook definition.

    trigger: regex pattern OR exact slash-command. If it starts with "/",
        it's an exact prefix match against the message text. Otherwise
        it's compiled as a regex and searched against the full message.
    name: human-readable name for logging/audit.
    prompt_template: markdown prompt injected into the system prompt when
        the trigger matches. Supports {user_id}, {channel_id},
        {thread_id}, {workspace_id} placeholders resolved from ctx.
    required_tools: tools that MUST be available for this skill. Merged
        with the channel's effective tool list at runtime.
    """

    trigger: str = Field(min_length=1, max_length=MAX_SKILL_TRIGGER_LENGTH)
    name: str
    prompt_template: str
    required_tools: list[str] = Field(default_factory=list)
    channels: list[str] = Field(default_factory=list)

    @field_validator("trigger")
    @classmethod
    def _validate_trigger(cls, value: str) -> str:
        return validate_skill_trigger(value)


class EscalationRoute(BaseModel):
    """A single escalation routing entry."""

    team_name: str
    channel_id: str
    description: str = ""
    contacts: list[str] = Field(default_factory=list)


class EscalationConfig(BaseModel):
    """Escalation routing table. Used by the ``escalate`` catalog tool."""

    routes: list[EscalationRoute] = Field(default_factory=list)


class CodebaseBinding(BaseModel):
    """A single repo binding the tenant's agent knows about.

    The agent's discovery layer picks which binding to use for a given
    Slack message based on (channel, user, thread, message text). See
    ``context_assembler.resolve_codebase_context()`` for the rules.

    repo: GitHub ``owner/name`` slug (e.g. "acme/platform").
    default_branch: branch to read from when no ref is specified.
    aliases: informal names users might call this repo
        ("platform", "the gateway code"). Matched case-insensitively
        against message text as a discovery signal.
    channels: Slack channel IDs where this binding is the confirmed
        default. Populated from onboarding (explicit choice) or from
        learned ``codebase_affinity`` memory records being promoted
        to config.
    """

    repo: str
    default_branch: str = "main"
    aliases: list[str] = Field(default_factory=list)
    channels: list[str] = Field(default_factory=list)


class CodebasesConfig(BaseModel):
    """Per-tenant code access layer config.

    Drives the GitHub-App-backed code tools (``code_search``,
    ``code_read_file``, ``code_find_symbol``, ``code_list_commits``)
    and the context-injection layer that lists connected repos in the
    system prompt so the model can pick one for each tool call.

    enabled: master switch. When False the code tools are hidden from
        the agent even if they're in ``catalog.allowed_tools``.
    github_installation_id: numeric GitHub App installation ID for
        this tenant (stored as string for JSON compatibility).
        Populated during the GitHub App install handshake. Used by
        ``scm_github.get_installation_token()`` to mint access tokens.
    default_repo: fallback repo when discovery can't find a
        scope-specific binding. Typically the tenant's most-active
        repo, picked at install time.
    bindings: list of repos the agent knows about. Seeded from the
        GitHub App installation's repo list at install time and
        editable from the onboarding UI.
    allow_learning: whether the agent may use AgentCore Memory's
        SEMANTIC strategy to learn scope→repo mappings over time.
        True is the magical default; setting False disables the
        semantic-retrieval hint path in the resolver (the agent still
        reads explicit ``bindings.channels``).
    """

    enabled: bool = False
    github_installation_id: str | None = None
    default_repo: str | None = None
    bindings: list[CodebaseBinding] = Field(default_factory=list)
    allow_learning: bool = True


# ----------------------------------------------------------------------------
# Default system prompt for new tenants
# ----------------------------------------------------------------------------
#
# This prompt is the "magical default" for Agent: a new tenant gets a bot
# that's useful from minute one without any skill definitions, channel
# personas, or tool configuration. It bakes in the three core workflows
# (triage, Q&A, handoffs) as natural-language instructions so the agent
# doesn't need explicit skill triggers to act on them.
#
# **KEEP IN SYNC** with the duplicate copy in
# ``bridge/bridge/tenant_write.py:DEFAULT_SYSTEM_PROMPT`` — the bridge and
# agent have separate venvs and can't share constants. A divergence here
# surfaces as "OAuth-created tenants behave differently from seed-script
# tenants," which is subtle and hard to debug.
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


# ----------------------------------------------------------------------------
# Default catalog tools for new tenants
# ----------------------------------------------------------------------------
#
# Every new tenant gets the safe catalog tools enabled out of the box. The
# old default of just ``["echo"]`` forced users to manually enable each tool
# before the bot was useful, which contradicted the "zero-config magic"
# goal. Higher-risk tools remain available for explicit opt-in.
#
# **KEEP IN SYNC** with ``bridge/bridge/tenant_write.py:DEFAULT_CATALOG_TOOLS``.
#
# The read-only ``code_*`` tools are in the default whitelist so tenants
# that install the GitHub App get them automatically. ``main.py`` filters
# them out of the runtime effective_tools list when
# ``codebases.enabled=False`` so tenants without the App don't see tools
# they can't use. ``propose_pr`` is deliberately NOT a new-tenant default:
# it runs model-authored code and must be enabled explicitly after an
# operator reviews the sandbox and GitHub write-permission boundary.
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


class TenantConfig(BaseModel):
    tenant_id: str
    model_id: str = "global.anthropic.claude-sonnet-4-6"
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    catalog: CatalogConfig = Field(default_factory=CatalogConfig)
    byo: ByoConfig = Field(default_factory=ByoConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    heartbeat: HeartbeatConfig = Field(default_factory=HeartbeatConfig)
    cost_cap: CostCapConfig = Field(default_factory=CostCapConfig)
    channels: dict[str, ChannelPersona] = Field(default_factory=dict)
    # Exact Slack user IDs allowed to mutate configuration through the
    # in-agent ``manage_config`` tool. An empty list makes it read-only.
    admin_user_ids: list[str] = Field(default_factory=list)
    bot_policy: BotPolicyConfig = Field(default_factory=BotPolicyConfig)
    context_assembly: ContextAssemblyConfig = Field(
        default_factory=ContextAssemblyConfig
    )
    skills: list[SkillDef] = Field(default_factory=list)
    escalation: EscalationConfig = Field(default_factory=EscalationConfig)
    codebases: CodebasesConfig = Field(default_factory=CodebasesConfig)
    # Marks a tenant as an internal test/demo environment (e.g. the
    # agent-testenv manual-testing rig). The ops dashboard filters
    # these out of cross-tenant leaderboards by default so they don't
    # pollute real-customer metrics. Purely a presentation flag — the
    # agent itself ignores it.
    is_internal_testenv: bool = False


def _iso_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def build_default_config(tenant_id: str) -> TenantConfig:
    """Build a TenantConfig with sane defaults for a brand-new tenant.

    Used by the OAuth callback and onboarding UI when a customer first
    connects. Customers customize their config from the onboarding UI
    after this initial row exists.

    The defaults make the human-driven investigation path useful without
    silently widening trust boundaries. External bots and integrations are
    explicit opt-ins.

    Defaults:
      - Model: the platform default (Claude Sonnet 4.6)
      - System prompt: the full ``DEFAULT_SYSTEM_PROMPT`` that teaches
        the agent triage, Q&A, handoff, and self-configuration workflows.
        No skill definitions needed — the workflows are baked into the
        prompt.
      - Catalog: all catalog tools enabled so the bot can search, read
        threads, escalate, and post cross-channel from minute one.
      - Bot policy: humans only. Operators explicitly trust alert bots or
        open specific channels after reviewing the spend and tool boundary.
      - Memory: channel-scoped by default. Workspace-wide sharing is an
        explicit opt-in, with ``isolated_channels`` available as exceptions;
        extraction enabled with default rules; namespace scoped to the tenant.
      - Runtime config mutation: disabled until exact Slack admin user IDs
        are configured in ``admin_user_ids``.
      - Context assembly: permalink resolution and thread history
        injection both on (already the BaseModel defaults).
      - BYO: disabled (users opt in via the integrations page).
      - Heartbeat: defaults.
    """
    return TenantConfig(
        tenant_id=tenant_id,
        system_prompt=DEFAULT_SYSTEM_PROMPT,
        catalog=CatalogConfig(allowed_tools=list(DEFAULT_CATALOG_TOOLS)),
        memory=MemoryConfig(
            namespace=f"tenants/{tenant_id}",
            extraction=MemoryExtraction(
                enabled=True, rules=["user_preferences", "facts"]
            ),
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
        raise FileNotFoundError(f"Could not find examples/tenants/ above {current}")

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


def _floats_to_decimal(obj: Any) -> Any:
    """Recursively convert float values to Decimal for DynamoDB compatibility.

    DynamoDB's boto3 resource layer rejects Python floats and requires
    ``decimal.Decimal``. Pydantic's ``model_dump()`` emits native floats
    for any float-typed field (e.g. ``CostCapConfig.monthly_limit_dollars``).
    """
    if isinstance(obj, float):
        return Decimal(str(obj))
    if isinstance(obj, dict):
        return {k: _floats_to_decimal(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_floats_to_decimal(v) for v in obj]
    return obj


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
            config_data = {
                k: v for k, v in item.items() if k not in {"created_at", "updated_at"}
            }
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
                ":config": _floats_to_decimal(config.model_dump()),
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


def save_tenant_config(config: TenantConfig) -> None:
    """Save a modified tenant config. Read-modify-write callers should
    ``load_tenant_config()``, modify the returned object, then call this
    to persist. Used by the ``manage_config`` catalog tool after it verifies
    the requesting Slack user against the tenant's explicit admin allowlist."""
    _store().upsert(config)


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
