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
"""
from __future__ import annotations

from .adapters.core import Adapter, InboundMessage, OutboundMessage
from .client import AgentCoreClient


async def dispatch_async(
    adapter: Adapter,
    inbound: InboundMessage,
    client: AgentCoreClient,
    tenant_id: str,
) -> None:
    """Invoke the agent for `inbound` and post the result back via `adapter`."""
    try:
        result = await client.invoke(
            tenant_id=tenant_id,
            prompt=inbound.text,
            ctx={
                "user_id": inbound.user_id,
                "channel_id": inbound.channel_id,
                "thread_id": inbound.thread_id,
                "workspace_id": inbound.workspace_id,
            },
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
