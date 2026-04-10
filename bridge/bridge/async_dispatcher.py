"""Async dispatcher: invoke the agent in the background, then post the
reply back through the originating adapter.

This is the Slack-native ack-then-post pattern. The route handler returns
its 3-second ack immediately; this function runs as a FastAPI BackgroundTask
that may take minutes (especially if the agent is in HealthyBusy mode
running long tools).

`tenant_id` is resolved by the route handler, NOT here. That way unknown
workspaces fail fast with a 200 OK before we queue the background task —
saves us from having to surface "no tenant for this workspace" errors
from inside a fire-and-forget context.

Streaming path (when adapter supports it):
  1. set_thinking_status — Slack's native "is thinking…" indicator
  2. invoke_stream — yields text chunks as they arrive from the agent
  3. stream_reply — pushes chunks to Slack via chat.startStream/appendStream/stopStream
  4. Falls back to the buffered path on any error
"""
from __future__ import annotations

import logging

from .adapters.core import Adapter, InboundMessage, OutboundMessage
from .client import AgentCoreClient

log = logging.getLogger(__name__)


async def dispatch_async(
    adapter: Adapter,
    inbound: InboundMessage,
    client: AgentCoreClient,
    tenant_id: str,
) -> None:
    """Invoke the agent for `inbound` and post the result back via `adapter`."""
    # Show a native thinking indicator if the adapter supports it
    # (Slack's assistant.threads.setStatus). Best-effort — failures
    # are swallowed so the actual invocation still proceeds.
    if hasattr(adapter, "set_thinking_status"):
        try:
            await adapter.set_thinking_status(inbound)
        except Exception:
            pass  # UX polish, not load-bearing

    ctx = {
        "user_id": inbound.user_id,
        "channel_id": inbound.channel_id,
        "thread_id": inbound.thread_id,
        "workspace_id": inbound.workspace_id,
        "bot_id": inbound.metadata.get("bot_id"),
        "permalinks": inbound.metadata.get("permalinks", []),
    }

    # Streaming path: adapter supports stream_reply + client supports invoke_stream
    if hasattr(adapter, "stream_reply"):
        try:
            chunk_iter = client.invoke_stream(
                tenant_id=tenant_id,
                prompt=inbound.text,
                ctx=ctx,
            )
            await adapter.stream_reply(inbound, chunk_iter)
            return
        except Exception:
            log.warning("stream path failed for tenant=%s; falling back to buffered", tenant_id, exc_info=True)
            # Fall through to buffered path

    # Buffered path: existing behavior (also serves as fallback)
    try:
        result = await client.invoke(
            tenant_id=tenant_id,
            prompt=inbound.text,
            ctx=ctx,
        )
        await adapter.reply(
            inbound,
            OutboundMessage(text=result, thread_id=inbound.thread_id),
        )
    except Exception as e:
        await adapter.reply(
            inbound,
            OutboundMessage(
                text=f"Error invoking agent: {e}",
                thread_id=inbound.thread_id,
            ),
        )
