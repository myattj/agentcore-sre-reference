"""Pre-LLM context assembly pipeline.

Runs between the channel-persona merge and Agent construction in main.py.
Assembles additional context that the LLM needs to produce a good response:

  1. **Permalink resolution** — detects Slack permalink URLs passed in
     ``ctx["permalinks"]`` (extracted by the bridge), fetches the referenced
     threads via Slack API, and prepends them as context blocks.

  2. **Thread history injection** — fetches the current Slack thread's
     recent messages so the agent has conversational continuity even for
     messages sent before it was mentioned.

  3. **Skill matching** — checks the user message against the tenant's
     configured skills (slash-command prefix or regex trigger). If matched,
     the skill's prompt template is appended to the system prompt and its
     required tools are merged into the effective tool list.

  4. **Codebase context** — reads ``TenantConfig.codebases`` + queries
     AgentCore Memory's SEMANTIC namespace for a "preferred codebase
     for this channel" hint, then calls ``resolve_codebase_context``
     to pick the primary repo. Appends a prompt block telling the
     agent either to use the confirmed repo, or to ask from a ranked
     shortlist before using any code tools. See
     ``codebase_resolver.py`` for the resolution rules and
     ``codebase_memory.py`` for the retrieval wrapper.

Each step is independently toggleable via ``TenantConfig.context_assembly``.
All Slack API calls are best-effort: failures are logged and skipped so the
agent still processes the message (just with less context).
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any

# Absolute imports — main.py and every other module here are loaded as
# top-level scripts by the AgentCore Runtime, not as a package. Relative
# imports (`from .slack_api ...`) raise ImportError at invocation time.
import slack_api
from codebase_memory import retrieve_codebase_affinity_hint
from codebase_resolver import CodebaseContext, resolve_codebase_context
from tenant import ByoConfig, CodebasesConfig, ContextAssemblyConfig, SkillDef

log = logging.getLogger(__name__)

# Reuse a small thread pool for parallel permalink fetching.
_executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="ctx-asm")

# Per-message character limit in injected context to avoid token bloat.
_MAX_MSG_CHARS = 500


@dataclass
class SkillMatch:
    """Result of a successful skill trigger match."""
    name: str
    prompt_addition: str
    required_tools: list[str] = field(default_factory=list)


@dataclass
class AssembledContext:
    """Output of the context assembly pipeline."""
    enriched_message: str
    system_prompt: str
    extra_tools: list[str] = field(default_factory=list)
    matched_skill: str | None = None
    codebase: CodebaseContext | None = None


def assemble_context(
    user_message: str,
    ctx: dict[str, Any],
    assembly_config: ContextAssemblyConfig,
    skills: list[SkillDef],
    effective_prompt: str,
    tenant_id: str,
    codebases: CodebasesConfig | None = None,
    memory_isolated_channels: list[str] | None = None,
    byo: ByoConfig | None = None,
) -> AssembledContext:
    """Run the full context assembly pipeline.

    Called from ``main.py`` between the channel-persona merge and the
    ``Agent()`` constructor. Returns an ``AssembledContext`` with the
    enriched message, possibly-modified system prompt, and any extra
    tools required by a matched skill.

    ``codebases`` is optional (defaults to None for backward
    compatibility with callers that haven't migrated to the new
    signature yet). When provided and ``codebases.enabled=True``, the
    resolver appends a codebase-context block to the system prompt.

    ``memory_isolated_channels`` mirrors the tenant's
    ``memory.isolated_channels`` list. Needed by the codebase-affinity
    semantic lookup to pick the same actor_id that the session manager
    uses for writes — otherwise the retrieve and write sides query
    different namespaces and the hint never returns anything.

    ``byo`` is optional (defaults to None for backward compatibility).
    When provided and ``byo.enabled=True``, connected integrations are
    injected as a prompt block so the model knows which Gateway tools
    are available (e.g. Datadog metrics, PagerDuty alerts).
    """
    context_blocks: list[str] = []

    # Step 1: Resolve permalinks
    if assembly_config.resolve_permalinks:
        block = _resolve_permalinks(ctx, assembly_config, tenant_id)
        if block:
            context_blocks.append(block)

    # Step 2: Thread history injection
    if assembly_config.inject_thread_history:
        block = _inject_thread_history(ctx, assembly_config, tenant_id)
        if block:
            context_blocks.append(block)

    # Step 3: Skill matching
    skill_match = _match_skill(user_message, skills, ctx)
    if skill_match:
        effective_prompt += (
            f"\n\n## Active Skill: {skill_match.name}\n\n"
            f"{skill_match.prompt_addition}"
        )

    # Step 4: Codebase context. Two sub-steps:
    #   4a. Query AgentCore Memory's SEMANTIC namespace for a
    #       "most recently used repo in this scope" hint. This is a
    #       synchronous ~200-500ms API call that only fires when
    #       codebases.enabled AND allow_learning are both True. The
    #       hint is informative — the resolver surfaces it as a soft
    #       default in the prompt block, but the model still reads
    #       the user message and picks on its own.
    #   4b. Call the pure resolver with the hint. Returns a single
    #       CodebaseContext with a ``disabled`` flag and a prompt
    #       block listing every connected repo.
    #
    # The hint layer is IO; the resolver is pure. This split keeps
    # resolver unit tests fast and lets the bridge test harness
    # exercise the resolver without mocking boto3.
    codebase_ctx: CodebaseContext | None = None
    if codebases is not None:
        semantic_hint: str | None = None
        if codebases.enabled and codebases.allow_learning:
            known_repos = [b.repo for b in codebases.bindings]
            channel_id = ctx.get("channel_id", "") or ""
            isolated_list = memory_isolated_channels or []
            is_isolated = bool(channel_id and channel_id in isolated_list)
            # retrieve_codebase_affinity_hint never raises — it logs
            # and returns None on any error — so we don't need a
            # try/except here.
            semantic_hint = retrieve_codebase_affinity_hint(
                tenant_id=tenant_id,
                channel_id=channel_id,
                known_repos=known_repos,
                isolated=is_isolated,
                user_id=ctx.get("user_id", "") or "",
            )
        codebase_ctx = resolve_codebase_context(
            ctx, codebases, semantic_hint=semantic_hint
        )
        if not codebase_ctx.disabled and codebase_ctx.prompt_block:
            effective_prompt += "\n\n" + codebase_ctx.prompt_block

    # Step 5: Integration injection — tell the model which monitoring /
    # observability tools are available via connected BYO integrations.
    # This fires for ALL skills, not just incident-response, so any
    # skill prompt that says "if your tools include query_metrics" gets
    # the right signal.
    integration_block = _build_integration_block(byo)
    if integration_block:
        effective_prompt += "\n\n" + integration_block

    # Assemble enriched message
    if context_blocks:
        enriched = "\n\n".join(context_blocks) + "\n\n---\n\n" + user_message
    else:
        enriched = user_message

    return AssembledContext(
        enriched_message=enriched,
        system_prompt=effective_prompt,
        extra_tools=skill_match.required_tools if skill_match else [],
        matched_skill=skill_match.name if skill_match else None,
        codebase=codebase_ctx,
    )


# ---------------------------------------------------------------------------
# Step 1: Permalink resolution
# ---------------------------------------------------------------------------

def _resolve_permalinks(
    ctx: dict[str, Any],
    config: ContextAssemblyConfig,
    tenant_id: str,
) -> str:
    """Resolve permalink URLs into thread content blocks."""
    permalinks: list[str] = ctx.get("permalinks", [])
    if not permalinks:
        return ""

    token = slack_api.get_bot_token(tenant_id)
    if not token:
        return ""

    # Cap the number of permalinks to resolve.
    permalinks = permalinks[: config.max_permalinks]

    # Parse permalink URLs into (channel_id, thread_ts) pairs.
    targets: list[tuple[str, str, str]] = []  # (url, channel_id, thread_ts)
    for url in permalinks:
        parsed = slack_api.parse_permalink(url)
        if parsed:
            targets.append((url, parsed[0], parsed[1]))

    if not targets:
        return ""

    # Fetch threads in parallel.
    blocks: list[str] = []
    futures = {
        _executor.submit(
            slack_api.fetch_thread_replies, token, channel_id, thread_ts
        ): url
        for url, channel_id, thread_ts in targets
    }
    for future in as_completed(futures):
        url = futures[future]
        try:
            thread_text = future.result()
            if thread_text and "error" not in thread_text.lower()[:20]:
                blocks.append(
                    f"## Referenced Thread\n**Source:** {url}\n\n{thread_text}"
                )
        except Exception:
            log.warning("Failed to resolve permalink %s", url, exc_info=True)

    return "\n\n".join(blocks)


# ---------------------------------------------------------------------------
# Step 2: Thread history injection
# ---------------------------------------------------------------------------

def _inject_thread_history(
    ctx: dict[str, Any],
    config: ContextAssemblyConfig,
    tenant_id: str,
) -> str:
    """Fetch the current thread's recent messages for conversational continuity."""
    channel_id = ctx.get("channel_id")
    thread_id = ctx.get("thread_id")
    if not channel_id or not thread_id:
        return ""

    token = slack_api.get_bot_token(tenant_id)
    if not token:
        return ""

    try:
        messages = slack_api.fetch_thread_replies_raw(
            token, channel_id, thread_id,
            limit=config.thread_history_depth,
        )
    except Exception:
        log.warning("Failed to fetch thread history", exc_info=True)
        return ""

    if not messages or len(messages) <= 1:
        # Single message = the current message itself, no history to inject.
        return ""

    # Format all messages except the last (which is the current message
    # the user just sent — avoid duplicating it).
    lines: list[str] = []
    for m in messages[:-1]:
        user = m.get("user", m.get("bot_id", "unknown"))
        text = m.get("text", "")
        if len(text) > _MAX_MSG_CHARS:
            text = text[:_MAX_MSG_CHARS] + "..."
        lines.append(f"**{user}**: {text}")

    if not lines:
        return ""

    return (
        f"## Current Thread History ({len(lines)} prior messages)\n\n"
        + "\n\n".join(lines)
    )


# ---------------------------------------------------------------------------
# Step 3: Skill matching
# ---------------------------------------------------------------------------

# Cache compiled regex patterns to avoid recompiling on every invocation.
_compiled_triggers: dict[str, re.Pattern[str]] = {}


def _match_skill(
    user_message: str,
    skills: list[SkillDef],
    ctx: dict[str, Any],
) -> SkillMatch | None:
    """Match the user message against configured skills. First match wins.

    Skills with a non-empty ``channels`` list only fire in those channels.
    Skills with an empty ``channels`` list fire everywhere.
    """
    text = user_message.strip()
    if not text or not skills:
        return None

    channel_id = ctx.get("channel_id", "")

    for skill in skills:
        # Channel whitelist: skip if this skill is restricted and we're
        # not in one of the whitelisted channels.
        if skill.channels and channel_id not in skill.channels:
            continue

        trigger = skill.trigger
        if trigger.startswith("/"):
            # Slash-command: exact prefix match
            if text.lower().startswith(trigger.lower()):
                return _build_skill_match(skill, ctx)
        else:
            # Regex match
            pattern = _compiled_triggers.get(trigger)
            if pattern is None:
                try:
                    pattern = re.compile(trigger, re.IGNORECASE)
                except re.error:
                    log.warning("Invalid skill trigger regex: %s", trigger)
                    continue
                _compiled_triggers[trigger] = pattern
            if pattern.search(text):
                return _build_skill_match(skill, ctx)

    return None


def _build_skill_match(skill: SkillDef, ctx: dict[str, Any]) -> SkillMatch:
    """Build a SkillMatch with resolved placeholders in the prompt template."""
    placeholders = defaultdict(
        str,
        {
            "user_id": ctx.get("user_id", ""),
            "channel_id": ctx.get("channel_id", ""),
            "thread_id": ctx.get("thread_id", ""),
            "workspace_id": ctx.get("workspace_id", ""),
        },
    )
    try:
        resolved = skill.prompt_template.format_map(placeholders)
    except (KeyError, ValueError):
        # If the template has bad placeholders, use it as-is.
        resolved = skill.prompt_template

    return SkillMatch(
        name=skill.name,
        prompt_addition=resolved,
        required_tools=list(skill.required_tools),
    )


# ---------------------------------------------------------------------------
# Step 5: Integration injection
# ---------------------------------------------------------------------------

# Maps integration name (as stored in byo.connected_integrations) to the
# tools it exposes via the Gateway MCP target. Used to build a prompt hint
# so the model knows which monitoring/observability tools are available.
# Only integrations that provide tools relevant to investigation are listed
# here — doc sources (Confluence, Notion) and issue trackers (Jira, Linear)
# are already covered by search_docs and don't need explicit hints.
_INTEGRATION_TOOLS: dict[str, tuple[str, list[str]]] = {
    "datadog": (
        "Datadog",
        ["query_metrics", "get_recent_alerts", "search_logs"],
    ),
    "pagerduty": (
        "PagerDuty",
        ["list_incidents", "get_incident", "list_oncalls"],
    ),
}


def _build_integration_block(byo: ByoConfig | None) -> str:
    """Build a prompt block listing connected monitoring integrations.

    Returns an empty string when BYO is disabled or no monitoring
    integrations are connected. The block explicitly lists tool names
    so the model can reference them in investigation prompts like
    "if your tools include query_metrics, use them."
    """
    if byo is None or not byo.enabled or not byo.connected_integrations:
        return ""

    lines: list[str] = []
    for integration in byo.connected_integrations:
        entry = _INTEGRATION_TOOLS.get(integration)
        if entry:
            display_name, tools = entry
            lines.append(
                f"- **{display_name}**: {', '.join(f'`{t}`' for t in tools)}"
            )

    if not lines:
        return ""

    return (
        "## Connected Monitoring Integrations\n\n"
        "The following monitoring tools are available via connected integrations. "
        "Use them when investigating incidents, triaging alerts, or answering "
        "questions about production health.\n\n"
        + "\n".join(lines)
    )
