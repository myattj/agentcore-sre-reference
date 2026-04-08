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
from runtime import app
from tenant import load_tenant_config
from tools import build_catalog_tools

# Side-effect import: registers @app.ping handler. Keep this so the runtime
# uses our HealthyBusy logic for the heartbeat lifecycle.
import ping  # noqa: F401

log = app.logger

# Single in-process memory store for `agentcore dev`. Phase 8 swaps this for
# BatchCreateMemoryRecordsStore once the AgentCore Memory resource exists.
_memory = InMemoryStore()

# Audit store, respects AGENT_LOCAL_STORES / LOCAL_AUDIT env vars.
# Shared with tools.py (same factory call, same backend configuration) so
# tool-level and invocation-level rows land in the same place.
_audit = build_audit_store()


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _truncate(value: str, max_bytes: int = 1024) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[: max_bytes - 3].decode("utf-8", errors="ignore") + "..."


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

    try:
        config = load_tenant_config(tenant_id)
        model_id = config.model_id

        catalog_tools = build_catalog_tools(
            config.catalog.allowed_tools,
            config.catalog.tool_config,
        )

        byo_client = build_byo_mcp_client(
            config.byo.gateway_endpoint if config.byo.enabled else None,
            config.byo.gateway_auth,
        )

        tools = list(catalog_tools)
        if byo_client is not None:
            # Strands accepts an MCPClient as a tool collection; it lists and
            # exposes the remote tools to the model lazily.
            tools.append(byo_client)

        agent = Agent(
            model=load_model(config.model_id),
            system_prompt=config.system_prompt,
            tools=tools,
        )

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

        # Memory extraction runs inline for now. Phase 8 moves this into a Lambda
        # triggered by AgentCore Memory's SNS notifications.
        if config.memory.extraction.enabled:
            records = extract_records(
                {"user": user_message, "assistant": "".join(response_chunks)},
                rules=config.memory.extraction.rules,
            )
            if records:
                namespace = config.memory.namespace or f"tenants/{tenant_id}"
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

        request_context.clear_context()


if __name__ == "__main__":
    app.run()
