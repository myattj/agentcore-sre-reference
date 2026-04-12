"""coreAgent entrypoint.

Multi-tenant agent. At invocation time, loads the tenant's config, builds a
Strands Agent with that tenant's allowed catalog tools + BYO MCP tools, and
streams the response. Memory extraction runs inline after the response.

DO NOT add blocking work to the entrypoint body — it stalls /ping and the
runtime will mark the agent unhealthy. Background work goes through
`tools.start_background_task` and the `app.add_async_task` lifecycle.

## Audit wiring (week 1)

Every invocation sets a `request_context` at the top and clears it in a
`finally:`. The context carries tenant_id, user_id, channel_id, and a
uuid4 `invocation_id` that links tool-call audit rows (written from
`tools.py`'s audit wrapper) back to the parent invocation row (written
here after the stream completes).

Token usage is pulled from the final Strands stream event when available.
The extraction is defensive — if the event shape changes or the key isn't
present, tokens default to 0 and the audit row still writes.
"""
from __future__ import annotations

import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from strands import Agent

import request_context
from audit import build_audit_store
from mcp_client.client import build_byo_mcp_client
from memory_store import build_memory_store, extract_records
from metrics import build_metrics_emitter
from model.load import load_model
from pricing import compute_cost_cents
from runtime import app
from spend_tracker import build_spend_tracker
from tenant import TenantConfig, load_tenant_config
from tools import CATALOG, build_catalog_tools

# Side-effect import: registers @app.ping handler. Keep this so the runtime
# uses our HealthyBusy logic for the heartbeat lifecycle.
import ping  # noqa: F401

log = app.logger

# Single in-process memory store for local dev (AGENT_LOCAL_STORES=1).
# When AGENTCORE_MEMORY_ID is set and AGENT_LOCAL_STORES is not 1, the
# real AgentCore Memory resource is used via AgentCoreMemorySessionManager
# and this store is unused. Singleton shared with tools.py so
# record_feedback writes land in the same dict.
_memory = build_memory_store()

# Memory resource ID, set after running provision_memory.py.
# When set, the agent uses the real AgentCore Memory resource
# instead of InMemoryStore.
_MEMORY_ID = os.getenv("AGENTCORE_MEMORY_ID", "")
_SEMANTIC_STRATEGY_ID = os.getenv("AGENTCORE_SEMANTIC_STRATEGY_ID", "")
_USER_PREF_STRATEGY_ID = os.getenv("AGENTCORE_USER_PREF_STRATEGY_ID", "")

# Audit store, respects AGENT_LOCAL_STORES / LOCAL_AUDIT env vars.
# Shared with tools.py (same factory call, same backend configuration) so
# tool-level and invocation-level rows land in the same place.
_audit = build_audit_store()

# Spend tracker for per-tenant cost caps. Same env-var wiring as audit.
_spend = build_spend_tracker()

# CloudWatch metrics emitter (EMF via stdout in production). Same env-var
# wiring as audit/spend, and shared with tools.py via the factory singleton
# so invocation and tool_call records land in the same backend.
_metrics = build_metrics_emitter()


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _truncate(value: str, max_bytes: int = 1024) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[: max_bytes - 3].decode("utf-8", errors="ignore") + "..."


def _build_byo_auth(
    static_auth: dict[str, Any] | None,
    gateway_jwt: str | None,
) -> dict[str, Any] | None:
    """Merge per-tenant static auth with the per-invocation Gateway JWT.

    `static_auth` is the tenant config's `byo.gateway_auth` (or None);
    today its only honored field is `headers`. The bridge-supplied
    `gateway_jwt` (minted per invocation) is added as a Bearer
    Authorization header. If both are None we return None so
    `build_byo_mcp_client` skips constructing an MCPClient at all.
    """
    if not gateway_jwt and not static_auth:
        return None

    headers: dict[str, str] = {}
    if isinstance(static_auth, dict):
        existing = static_auth.get("headers")
        if isinstance(existing, dict):
            headers.update({str(k): str(v) for k, v in existing.items()})

    if gateway_jwt:
        headers["Authorization"] = f"Bearer {gateway_jwt}"

    if not headers:
        return None
    return {"headers": headers}


def _extract_usage_from_event(event: Any) -> tuple[int, int] | None:
    """Pull (input_tokens, output_tokens) from a Strands stream event.

    Strands' exact event shape for usage metadata is not formally documented
    and has changed across releases. We probe a few known shapes:
      - event["usage"] = {"inputTokens": N, "outputTokens": M}
      - event["metadata"]["usage"] = {...}
      - event["event"]["metadata"]["usage"] = {...}   (nested under SDK wrapper)
      - camelCase OR snake_case keys

    Returns None if nothing was found; caller uses 0/0 as a safe default.
    """
    if not isinstance(event, dict):
        return None

    candidates = [
        event.get("usage"),
        (event.get("metadata") or {}).get("usage") if isinstance(event.get("metadata"), dict) else None,
        ((event.get("event") or {}).get("metadata") or {}).get("usage")
            if isinstance(event.get("event"), dict) else None,
    ]
    for usage in candidates:
        if not isinstance(usage, dict):
            continue
        input_tokens = (
            usage.get("inputTokens")
            or usage.get("input_tokens")
            or usage.get("prompt_tokens")
            or 0
        )
        output_tokens = (
            usage.get("outputTokens")
            or usage.get("output_tokens")
            or usage.get("completion_tokens")
            or 0
        )
        if input_tokens or output_tokens:
            return int(input_tokens), int(output_tokens)
    return None


def _build_memory_session_manager(
    tenant_id: str,
    ctx: dict[str, Any],
    invocation_id: str,
    config: TenantConfig | None = None,
) -> Any | None:
    """Build an AgentCoreMemorySessionManager for the real memory resource.

    Returns None when AGENTCORE_MEMORY_ID is not set (no memory resource
    provisioned). When the env var IS set, real memory is used regardless
    of AGENT_LOCAL_STORES — that flag only controls tenant config + audit
    stores, not memory.

    Namespace mapping (shared-by-default):
      - Channels: actorId = {tenant_id} (shared brain across all channels)
      - Isolated channels: actorId = {tenant_id}_{channel_id} (opt-in silo)
      - DMs:      actorId = {tenant_id}_{user_id}    (per-user)
      - sessionId = thread_id (groups a conversation thread) or invocation_id
    """
    if not _MEMORY_ID:
        return None

    from bedrock_agentcore.memory.integrations.strands.session_manager import AgentCoreMemorySessionManager
    from bedrock_agentcore.memory.integrations.strands.config import AgentCoreMemoryConfig

    channel_id = ctx.get("channel_id", "")
    isolated = (
        config is not None
        and channel_id
        and channel_id in config.memory.isolated_channels
    )

    if isolated:
        actor_id = f"{tenant_id}_{channel_id}"
    elif channel_id:
        actor_id = tenant_id
    else:
        actor_id = f"{tenant_id}_{ctx.get('user_id', 'anon')}"

    from bedrock_agentcore.memory.integrations.strands.config import RetrievalConfig

    # Build retrieval config — query both SEMANTIC and USER_PREFERENCE
    # namespaces so the agent sees facts + preferences from prior conversations.
    # Each strategy resolves to a different namespace path via {memoryStrategyId},
    # so we use pre-resolved paths as dict keys to avoid key collisions.
    retrieval: dict[str, RetrievalConfig] = {}
    if _SEMANTIC_STRATEGY_ID:
        ns = f"/strategies/{_SEMANTIC_STRATEGY_ID}/actors/{{actorId}}/"
        retrieval[ns] = RetrievalConfig(
            top_k=10,
            relevance_score=0.2,
        )
    if _USER_PREF_STRATEGY_ID:
        ns = f"/strategies/{_USER_PREF_STRATEGY_ID}/actors/{{actorId}}/"
        retrieval[ns] = RetrievalConfig(
            top_k=10,
            relevance_score=0.2,
        )

    mem_config = AgentCoreMemoryConfig(
        memory_id=_MEMORY_ID,
        actor_id=actor_id,
        # Slack thread IDs contain dots (e.g. "1712345678.123456") which
        # violate AgentCore's sessionId regex [a-zA-Z0-9][a-zA-Z0-9-_]*.
        # Replace dots with underscores to keep them unique and valid.
        session_id=(ctx.get("thread_id") or invocation_id).replace(".", "_"),
        retrieval_config=retrieval if retrieval else None,
    )
    region = os.getenv("AWS_REGION", "us-west-2")
    return AgentCoreMemorySessionManager(
        agentcore_memory_config=mem_config,
        region_name=region,
    )


def _write_feedback_audit(
    tenant_id: str,
    invocation_id: str,
    ctx: dict[str, Any],
    feedback: dict[str, Any],
) -> None:
    """Write a feedback audit row. Best-effort — never throws."""
    try:
        _audit.write({
            "row_type": "feedback",
            "tenant_id": tenant_id,
            "sk": f"FB#{_iso_now()}#{invocation_id}",
            "invocation_id": invocation_id,
            "timestamp": _iso_now(),
            "created_at": _iso_now(),
            "user_id": feedback.get("reactor_user_id", ""),
            "channel_id": ctx.get("channel_id", ""),
            "thread_id": ctx.get("thread_id", ""),
            "workspace_id": ctx.get("workspace_id", ""),
            "reaction": feedback.get("reaction", ""),
            "sentiment": feedback.get("sentiment", ""),
            "bot_message_ts": feedback.get("bot_message_ts", ""),
            "question_summary": _truncate(feedback.get("user_question", "")),
            "answer_summary": _truncate(feedback.get("bot_answer", "")),
        })
    except Exception as e:
        log.warning("feedback audit write dropped: %s", e)


def _write_feedback_memory(
    tenant_id: str,
    ctx: dict[str, Any],
    invocation_id: str,
    config: TenantConfig,
    feedback: dict[str, Any],
) -> None:
    """Write a feedback memory record so the bot learns from reactions.

    Local dev: writes directly to InMemoryStore.
    Production: creates a conversation event via AgentCoreMemorySessionManager.
    The SEMANTIC strategy extracts and indexes it asynchronously.
    """
    sentiment = feedback.get("sentiment", "")
    reaction = feedback.get("reaction", "")
    question = feedback.get("user_question", "")
    answer = feedback.get("bot_answer", "")

    record = {
        "type": "user_feedback",
        "sentiment": sentiment,
        "reaction": reaction,
        "question": _truncate(question),
        "answer": _truncate(answer),
        "extracted_via": "reaction_feedback_v0",
    }

    if not _MEMORY_ID:
        # Local dev: write to InMemoryStore
        namespace = config.memory.namespace or f"tenants/{tenant_id}"
        _memory.write_records(namespace, [record])
        log.info("Wrote feedback memory record to namespace=%s", namespace)
        return

    # Production: create a conversation event via the memory session manager.
    # The session manager hooks into AgentCore Memory; the built-in SEMANTIC
    # strategy extracts and indexes the feedback asynchronously.
    try:
        session_mgr = _build_memory_session_manager(
            tenant_id, ctx, invocation_id, config,
        )
        if session_mgr is None:
            return

        # Encode the feedback as a conversation turn so the SEMANTIC strategy
        # can extract and index it. The user "message" is the original question;
        # the assistant "message" includes the answer + reaction annotation.
        sentiment_label = "positive" if sentiment == "positive" else "negative"
        annotated_answer = (
            f"{answer}\n\n"
            f"[User feedback: :{reaction}: — {sentiment_label}. "
            f"{'The user found this answer helpful.' if sentiment == 'positive' else 'The user indicated this answer was unhelpful or incorrect.'}]"
        )

        # The Strands session manager API is tightly coupled to the Agent
        # lifecycle. Try the direct session methods; fall back gracefully
        # if the exact method names differ across SDK versions.
        session_mgr.session_start(
            session_id=(ctx.get("thread_id") or invocation_id).replace(".", "_"),
        )
        session_mgr.add_user_message(question or "(no question captured)")
        session_mgr.add_agent_message(annotated_answer)
        session_mgr.session_end()
        log.info("Wrote feedback memory event for tenant=%s", tenant_id)

    except Exception:
        # Production memory write is best-effort. The audit row is the
        # durable record; memory enhances it for future conversations.
        log.warning(
            "feedback memory write failed for tenant=%s", tenant_id,
            exc_info=True,
        )


def _build_self_awareness_block(
    config: TenantConfig,
    effective_skills: list | None = None,
) -> str:
    """Generate a system prompt appendix describing the bot's non-default config.

    Returns an empty string for zero-config tenants — the base system prompt
    stands alone and the LLM reasons about the defaults from the tool schemas
    it already sees. The block only fires when there's something the LLM
    can't infer from its tools: custom skills, a non-default bot policy
    (allow_all_bots is True by default), configured escalation routes, or
    disconnected integrations the bot should be transparent about.

    When any of those are present, we also append the ``manage_config``
    guidance so the LLM knows it can modify these settings at user request.
    Enabled tools and context assembly flags are deliberately omitted — the
    former is duplicative with the tool schemas the LLM already has, and the
    latter is bridge-side plumbing the LLM can't act on.

    ``effective_skills`` is the merged list of built-in + tenant-custom
    skills. If None, falls back to ``config.skills`` for backward compat.
    """
    sections: list[str] = []

    # Integration status — tell the model what's NOT connected so it
    # doesn't claim capabilities it can't deliver or hide real blockers.
    not_connected: list[str] = []
    if not config.codebases.enabled:
        not_connected.append(
            "GitHub (code tools: `code_search`, `code_read_file`, etc.)"
        )
    if not config.byo.connected_integrations:
        not_connected.append(
            "Documentation sources (Confluence, Notion, etc.)"
        )
    if not_connected:
        sections.append(
            "**Not Connected:** The following integrations are not set up "
            "for this workspace. If a user asks for something that requires "
            "them, say plainly that the integration isn't connected yet — "
            "don't pretend you have the tool or hide the blocker.\n"
            + "\n".join(f"- {nc}" for nc in not_connected)
        )

    skills = effective_skills if effective_skills is not None else config.skills
    if skills:
        tenant_skill_names = {s.name for s in config.skills}
        skill_lines: list[str] = []
        for s in skills:
            kind = "slash command" if s.trigger.startswith("/") else "regex"
            label = "custom" if s.name in tenant_skill_names else "built-in"
            line = f"- `{s.trigger}` ({kind}, {label}) -> {s.name}"
            if s.channels:
                line += f" [only in: {', '.join(s.channels)}]"
            skill_lines.append(line)
        sections.append(
            f"**Skills:** {len(skills)} available\n"
            + "\n".join(skill_lines)
        )

    # Bot policy: only surface when it diverges from the zero-config default
    # (allow_all_bots=True, empty trusted_bot_ids, empty open_channels).
    # When surfaced, accurately reflects the four-tier evaluation from
    # ``BotPolicyConfig`` — the old implementation incorrectly said
    # "humans only" whenever the lists were empty, ignoring ``allow_all_bots``.
    bp = config.bot_policy
    is_default_policy = (
        bp.allow_all_bots is True
        and not bp.trusted_bot_ids
        and not bp.open_channels
    )
    if not is_default_policy:
        descriptors: list[str] = []
        if bp.allow_all_bots:
            descriptors.append("all bots allowed")
        if bp.trusted_bot_ids:
            descriptors.append(f"trusted bots: {', '.join(bp.trusted_bot_ids)}")
        if bp.open_channels:
            descriptors.append(f"open channels: {', '.join(bp.open_channels)}")
        if not descriptors:
            # allow_all_bots=False and both lists empty: humans only
            descriptors.append("humans only")
        sections.append("**Bot Policy:** " + "; ".join(descriptors))

    if config.escalation.routes:
        route_lines = [
            f"- {r.team_name} -> {r.channel_id}"
            for r in config.escalation.routes
        ]
        sections.append("**Escalation Routes:**\n" + "\n".join(route_lines))

    if not sections:
        return ""

    sections.append(
        "**Updating Settings:** Users can ask you to add skills, change "
        "bot policy, update escalation routes, or modify other settings at "
        "any time. Use the `manage_config` tool to view or update your "
        "configuration. Changes persist and take effect on the next message."
    )

    return "\n\n## Your Configuration\n\n" + "\n\n".join(sections)


@app.entrypoint
async def invoke(payload, context):
    """Per-invocation: hydrate tenant config, build agent, stream response,
    then run memory extraction + audit logging inline."""
    tenant_id = payload.get("tenant_id", "demo")
    user_message = payload.get("prompt", "")
    # ctx is bridge-supplied: {user_id, channel_id, thread_id, workspace_id, ...}
    # Tools that need request-specific context read from here. Use `or {}`
    # rather than the dict default so an explicit `"ctx": null` from the
    # caller still produces an empty dict instead of a None.
    ctx = payload.get("ctx") or {}

    invocation_id = uuid.uuid4().hex
    start = time.time()

    log.info(f"Invoking tenant={tenant_id} invocation_id={invocation_id} prompt_len={len(user_message)}")

    # Reaction feedback short-circuit: no LLM call, no Strands Agent.
    # The bridge sends event_type="reaction_feedback" when a user reacts
    # to a bot message with a feedback-signal emoji (thumbsup/thumbsdown).
    # We write audit + memory and return immediately.
    if ctx.get("event_type") == "reaction_feedback":
        feedback = ctx.get("feedback", {})
        log.info(
            "Feedback event: tenant=%s reaction=%s sentiment=%s invocation_id=%s",
            tenant_id, feedback.get("reaction"), feedback.get("sentiment"),
            invocation_id,
        )
        try:
            config = load_tenant_config(tenant_id)
        except Exception:
            log.warning("feedback: could not load config for tenant=%s", tenant_id)
            return
        _write_feedback_audit(tenant_id, invocation_id, ctx, feedback)
        _write_feedback_memory(tenant_id, ctx, invocation_id, config, feedback)
        yield ""  # entrypoint must yield at least once
        return

    success = True
    error_text: str | None = None
    response_chunks: list[str] = []
    input_tokens = 0
    output_tokens = 0
    model_id = ""
    config = None  # set inside try; guarded in finally for spend tracking

    try:
        config = load_tenant_config(tenant_id)
        model_id = config.model_id

        # Set the request context after config is loaded so escalation_routes
        # is available. The finally block calls clear_context() which is safe
        # even if set_context wasn't called (sets to None, a no-op).
        request_context.set_context(
            tenant_id=tenant_id,
            invocation_id=invocation_id,
            user_id=ctx.get("user_id", ""),
            channel_id=ctx.get("channel_id", ""),
            thread_id=ctx.get("thread_id", ""),
            workspace_id=ctx.get("workspace_id", ""),
            escalation_routes=[r.model_dump() for r in config.escalation.routes],
        )

        # Cost-cap pre-flight check: reject the invocation before any
        # Bedrock spend if the tenant has exceeded their monthly cap.
        if config.cost_cap.enabled:
            cap_cents = int(config.cost_cap.monthly_limit_dollars * 100)
            allowed, current_spend = _spend.check_budget(tenant_id, cap_cents)
            if not allowed:
                cap_msg = (
                    f"Your organization has reached its monthly usage cap "
                    f"(${config.cost_cap.monthly_limit_dollars:.2f}). "
                    f"Please contact your administrator to increase the limit "
                    f"or wait until the next billing period."
                )
                log.info(
                    "Cost cap exceeded for tenant=%s spend=%d cap=%d",
                    tenant_id, current_spend, cap_cents,
                )
                yield cap_msg
                success = True  # not an error — deliberate block
                return

        # Channel persona merge: override system_prompt, allowed_tools, and
        # memory rules when a ChannelPersona is configured for this channel.
        channel_id = ctx.get("channel_id", "")
        effective_prompt = config.system_prompt
        effective_tools = config.catalog.allowed_tools
        effective_rules = config.memory.extraction.rules

        if channel_id and channel_id in config.channels:
            cp = config.channels[channel_id]
            if cp.system_prompt is not None:
                effective_prompt = cp.system_prompt
            if cp.allowed_tools is not None:
                effective_tools = cp.allowed_tools
            if cp.memory_rules is not None:
                effective_rules = cp.memory_rules

        # Merge built-in skills with any tenant-configured overrides.
        # Built-ins are always available; tenant skills with the same
        # name replace the built-in version. Computed once and used by
        # both the self-awareness block and the context assembler.
        from builtin_skills import merge_skills
        effective_skills = merge_skills(config.skills)

        # Context assembly: resolve permalinks, inject thread history,
        # match skills. Runs AFTER channel-persona merge so effective_prompt
        # and effective_tools reflect per-channel overrides.
        # Absolute import (not `from .context_assembler`) because main.py
        # is loaded as a top-level script by the AgentCore Runtime, not
        # as a package module — relative imports raise ImportError.
        from context_assembler import assemble_context

        assembled = assemble_context(
            user_message=user_message,
            ctx=ctx,
            assembly_config=config.context_assembly,
            skills=effective_skills,
            effective_prompt=effective_prompt,
            tenant_id=tenant_id,
            codebases=config.codebases,
            memory_isolated_channels=config.memory.isolated_channels,
        )
        effective_prompt = assembled.system_prompt
        user_message = assembled.enriched_message
        effective_tools = list(set(effective_tools) | set(assembled.extra_tools))
        if assembled.matched_skill:
            log.info("Skill matched: %s for tenant=%s", assembled.matched_skill, tenant_id)
        if assembled.codebase is not None:
            log.info(
                "Codebase resolution for tenant=%s channel=%s: "
                "disabled=%s bindings=%d",
                tenant_id,
                ctx.get("channel_id", ""),
                assembled.codebase.disabled,
                len(assembled.codebase.bindings),
            )
            # The resolver no longer provides a silent ``primary_repo``
            # fallback — the model must pass ``repo='owner/name'``
            # explicitly on every code_* call, picking from the list
            # of connected repos in the prompt block. We still stash
            # ``github_installation_id`` so the tools can mint tokens.
            request_context.merge_context(
                github_installation_id=config.codebases.github_installation_id or "",
            )

        # Drop code_* tools from the effective list when codebases are
        # disabled for this tenant. The resolver already emits a DISABLED
        # state (no prompt injection), but we also need to hide the tools
        # from the model so it doesn't attempt to call them and get a
        # "not configured" error back.
        if not config.codebases.enabled:
            _CODE_TOOLS = {
                "code_search",
                "code_read_file",
                "code_find_symbol",
                "code_list_commits",
                "inspect_codebase_context",
                "ask_codebase_choice",
                "propose_pr",
            }
            removed = [t for t in effective_tools if t in _CODE_TOOLS]
            if removed:
                log.info(
                    "Filtered %d code tools for tenant=%s "
                    "(codebases.enabled=False): %s",
                    len(removed), tenant_id, removed,
                )
            effective_tools = [t for t in effective_tools if t not in _CODE_TOOLS]

        # Self-awareness: the bot knows its own config so it can explain
        # itself and help users modify settings via manage_config. Returns
        # an empty string for zero-config tenants — the base prompt stands
        # alone when there's nothing non-default to announce.
        effective_prompt += _build_self_awareness_block(config, effective_skills)

        catalog_tools = build_catalog_tools(
            effective_tools,
            config.catalog.tool_config,
        )

        # BYO MCP client picks up the Gateway endpoint from the tenant
        # config and the per-invocation JWT from the bridge-supplied ctx.
        # The bridge mints `ctx.gateway_jwt` in `client.py:invoke` for
        # every call; the agent forwards it as a Bearer header to the
        # AgentCore Gateway's CUSTOM_JWT authorizer (week 4 chunk A).
        # Static per-tenant headers from `byo.gateway_auth.headers`
        # (e.g. an x-tenant-id for the interceptor's defense-in-depth)
        # are merged in alongside.
        byo_auth = _build_byo_auth(config.byo.gateway_auth, ctx.get("gateway_jwt"))
        byo_client = build_byo_mcp_client(
            config.byo.gateway_endpoint if config.byo.enabled else None,
            byo_auth,
        )

        tools = list(catalog_tools)
        # manage_config is a system tool — always available so the bot can
        # explain and modify its own settings at the user's request.
        _manage_config = CATALOG.get("manage_config")
        if _manage_config:
            tools.append(_manage_config)
        if byo_client is not None:
            # Strands accepts an MCPClient as a tool collection; it lists and
            # exposes the remote tools to the model lazily.
            tools.append(byo_client)

        # Build the memory session manager (real AgentCore Memory when
        # AGENTCORE_MEMORY_ID is set, None for local dev).
        session_mgr = _build_memory_session_manager(tenant_id, ctx, invocation_id, config)

        # FAQ injection: local dev only (when session_mgr is None).
        # With real memory, channel-scoped FAQ is handled automatically
        # by the SEMANTIC strategy + workspace-per-channel actorId mapping.
        if session_mgr is None and "faq_in_channel" in effective_rules and channel_id:
            faq_ns = f"{config.memory.namespace or f'tenants/{tenant_id}'}/channels/{channel_id}/faq"
            faq_records = _memory.query(faq_ns, user_message, limit=5)
            faq_lines = [
                f"Q: {r.get('question', '')}\nA: {r.get('answer', '')}"
                for r in faq_records
                if r.get("type") == "faq"
            ]
            if faq_lines:
                effective_prompt += (
                    "\n\n## Frequently Asked Questions (from this channel's history)\n\n"
                    + "\n\n---\n\n".join(faq_lines)
                    + "\n\n---\n\nUse these FAQs to answer if relevant. If the question is new, answer normally.\n\n"
                )

        agent_kwargs: dict[str, Any] = {
            "model": load_model(config.model_id),
            "system_prompt": effective_prompt,
            "tools": tools,
        }
        if session_mgr is not None:
            agent_kwargs["session_manager"] = session_mgr

        agent = Agent(**agent_kwargs)

        # Stream the response back to the caller while accumulating the full
        # text for memory extraction + audit below. Also probe each event
        # for usage metadata (Strands emits it on the final event).
        stream = agent.stream_async(user_message)
        async for event in stream:
            if isinstance(event, dict):
                if "data" in event and isinstance(event["data"], str):
                    response_chunks.append(event["data"])
                    yield event["data"]
                # Probe usage on every event; the last one wins.
                usage = _extract_usage_from_event(event)
                if usage is not None:
                    input_tokens, output_tokens = usage

        # Memory extraction: when using real AgentCore Memory (session_mgr
        # is not None), the session manager handles event creation and
        # AgentCore's built-in strategies handle extraction automatically.
        # The inline path only runs for local dev (AGENT_LOCAL_STORES=1).
        if session_mgr is None and config.memory.extraction.enabled:
            records = extract_records(
                {"user": user_message, "assistant": "".join(response_chunks)},
                rules=effective_rules,
            )
            if records:
                base_ns = config.memory.namespace or f"tenants/{tenant_id}"
                if "faq_in_channel" in effective_rules and channel_id:
                    namespace = f"{base_ns}/channels/{channel_id}/faq"
                else:
                    namespace = base_ns
                _memory.write_records(namespace, records)
                log.info(f"Wrote {len(records)} memory records to namespace={namespace}")

    except Exception as e:  # noqa: BLE001 — audit, re-raise below
        success = False
        error_text = f"{type(e).__name__}: {e}"
        log.exception("invoke failed for tenant=%s invocation_id=%s", tenant_id, invocation_id)
        raise
    finally:
        # Compute shared values once so both the audit write and the
        # metrics emit can use them — and so that the metrics path stays
        # alive even if the audit PutItem body raises inside its try block.
        full_response = "".join(response_chunks)
        duration_ms = int((time.time() - start) * 1000)

        # Write the invocation-level audit row. Best-effort — the audit
        # store swallows exceptions, but wrap in try anyway.
        try:
            _audit.write({
                "row_type": "invocation",
                "tenant_id": tenant_id,
                "sk": f"INV#{_iso_now()}#{invocation_id}",
                "invocation_id": invocation_id,
                "timestamp": _iso_now(),
                "created_at": _iso_now(),
                "user_id": ctx.get("user_id", ""),
                "channel_id": ctx.get("channel_id", ""),
                "thread_id": ctx.get("thread_id", ""),
                "workspace_id": ctx.get("workspace_id", ""),
                "model_id": model_id,
                "input_summary": _truncate(user_message),
                "output_summary": _truncate(full_response if success else (error_text or "")),
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "duration_ms": duration_ms,
                "success": success,
            })
        except Exception as audit_exc:  # pragma: no cover
            log.warning("invoke: audit write dropped for invocation_id=%s: %s", invocation_id, audit_exc)

        # CloudWatch metrics via EMF. Separate try/except so a metrics
        # failure doesn't suppress the audit row above or the spend record
        # below. The EMFMetricsEmitter also swallows its own exceptions —
        # this is belt-and-braces.
        try:
            _metrics.emit_invocation(
                tenant_id=tenant_id,
                model_id=model_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                duration_ms=duration_ms,
                success=success,
                invocation_id=invocation_id,
                channel_id=ctx.get("channel_id", ""),
                workspace_id=ctx.get("workspace_id", ""),
            )
        except Exception as metrics_exc:  # pragma: no cover
            log.warning("invoke: metrics emit dropped for invocation_id=%s: %s", invocation_id, metrics_exc)

        # Record spend for cost-cap tracking (post-invocation).
        # Only records when tokens were actually consumed. Best-effort —
        # the tracker swallows exceptions so a DDB hiccup doesn't break
        # the response that already streamed.
        # Guard on `config` existing — if load_tenant_config raised, skip.
        try:
            if (
                config is not None
                and config.cost_cap.enabled
                and success
                and (input_tokens or output_tokens)
            ):
                cost_cents = compute_cost_cents(model_id, input_tokens, output_tokens)
                _spend.record_spend(tenant_id, cost_cents)
        except Exception as spend_exc:  # pragma: no cover
            log.warning("invoke: spend recording failed for invocation_id=%s: %s", invocation_id, spend_exc)

        request_context.clear_context()


if __name__ == "__main__":
    app.run()
