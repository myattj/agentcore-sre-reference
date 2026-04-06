"""Async dispatcher: invoke the agent in the background, then post the
reply back through the originating adapter.

This is the Slack-native ack-then-post pattern. The route handler returns
its 3-second ack immediately; this function runs as a FastAPI BackgroundTask
that may take minutes (especially if the agent is in HealthyBusy mode
running long tools).
"""
from __future__ import annotations

from .adapters.core import Adapter, InboundMessage, OutboundMessage
from .client import AgentCoreClient
from .tenant_resolver import resolve_tenant_id


async def dispatch_async(
    adapter: Adapter,
    inbound: InboundMessage,
    client: AgentCoreClient,
) -> None:
    """Invoke the agent for `inbound` and post the result back via `adapter`."""
    tenant_id = resolve_tenant_id(inbound.workspace_id)
    try:
        result = await client.invoke(
            tenant_id=tenant_id,
            prompt=inbound.text,
            ctx={
                "user_id": inbound.user_id,
                "thread_id": inbound.thread_id,
                "workspace_id": inbound.workspace_id,
                **inbound.metadata,
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
