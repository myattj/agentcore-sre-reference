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
from memory_store import InMemoryStore, extract_records
from model.load import load_model
from pricing import compute_cost_cents
from runtime import app
from spend_tracker import build_spend_tracker
from tenant import load_tenant_config
from tools import build_catalog_tools

# Side-effect import: registers @app.ping handler. Keep this so the runtime
# uses our HealthyBusy logic for the heartbeat lifecycle.
import ping  # noqa: F401

log = app.logger

# Single in-process memory store for local dev (AGENT_LOCAL_STORES=1).
# When AGENTCORE_MEMORY_ID is set and AGENT_LOCAL_STORES is not 1, the
# real AgentCore Memory resource is used via AgentCoreMemorySessionManager
# and this store is unused.
_memory = InMemoryStore()

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
) -> Any | None:
    """Build an AgentCoreMemorySessionManager for the real memory resource.

    Returns None when AGENTCORE_MEMORY_ID is not set (no memory resource
    provisioned). When the env var IS set, real memory is used regardless
    of AGENT_LOCAL_STORES — that flag only controls tenant config + audit
    stores, not memory.

    Namespace mapping:
      - Channels: actorId = {tenant_id}_{channel_id} (workspace-per-channel)
      - DMs:      actorId = {tenant_id}_{user_id}    (per-user)
      - sessionId = thread_id (groups a conversation thread) or invocation_id
    """
    if not _MEMORY_ID:
        return None

    from bedrock_agentcore.memory.integrations.strands.session_manager import AgentCoreMemorySessionManager
    from bedrock_agentcore.memory.integrations.strands.config import AgentCoreMemoryConfig

    channel_id = ctx.get("channel_id", "")
    if channel_id:
        actor_id = f"{tenant_id}_{channel_id}"
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

    # Set the request context BEFORE building the agent so any tool whose
    # construction happens to call into the audit path (unlikely but
    # possible) sees a valid context.
    request_context.set_context(
        tenant_id=tenant_id,
        invocation_id=invocation_id,
        user_id=ctx.get("user_id", ""),
        channel_id=ctx.get("channel_id", ""),
        thread_id=ctx.get("thread_id", ""),
        workspace_id=ctx.get("workspace_id", ""),
    )

    log.info(f"Invoking tenant={tenant_id} invocation_id={invocation_id} prompt_len={len(user_message)}")

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
        if byo_client is not None:
            # Strands accepts an MCPClient as a tool collection; it lists and
            # exposes the remote tools to the model lazily.
            tools.append(byo_client)

        # Build the memory session manager (real AgentCore Memory when
        # AGENTCORE_MEMORY_ID is set, None for local dev).
        session_mgr = _build_memory_session_manager(tenant_id, ctx, invocation_id)

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
        # Write the invocation-level audit row. Best-effort — the audit
        # store swallows exceptions, but wrap in try anyway.
        try:
            full_response = "".join(response_chunks)
            duration_ms = int((time.time() - start) * 1000)
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
