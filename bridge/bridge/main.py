"""Bridge FastAPI app: routes for each client adapter.

Routes:
  - POST /slack/events  — Slack Events API; ack within 3s, dispatch async
  - POST /debug/message — synchronous local debug; returns the agent reply
  - GET  /healthz       — liveness probe
"""
from __future__ import annotations

import os

from fastapi import BackgroundTasks, FastAPI, Request

from .adapters.debug import DebugAdapter
from .adapters.slack import SlackAdapter
from .async_dispatcher import dispatch_async
from .client import AgentCoreClient
from .tenant_resolver import resolve_tenant_id

app = FastAPI(title="coreAgent bridge")

slack = SlackAdapter(
    signing_secret=os.getenv("SLACK_SIGNING_SECRET"),
    bot_token=os.getenv("SLACK_BOT_TOKEN"),
)
debug = DebugAdapter()

client = AgentCoreClient(
    runtime_arn=os.getenv("AGENT_RUNTIME_ARN"),
    local_agent_url=os.getenv("LOCAL_AGENT_URL"),
)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/slack/events")
async def slack_events(request: Request, background: BackgroundTasks):
    """Slack Events API webhook. Must ack within 3 seconds.

    URL verification handshake is handled in slack.ack(). Normal events
    are dispatched to a BackgroundTask so the agent invocation (which can
    take minutes) doesn't block the response.
    """
    body = await request.json()
    # URL verification: short-circuit, no dispatch.
    if body.get("type") == "url_verification":
        return {"challenge": body["challenge"]}

    inbound = await slack.parse(request)
    background.add_task(dispatch_async, slack, inbound, client)
    return await slack.ack(request)


@app.post("/debug/message")
async def debug_message(request: Request) -> dict[str, str]:
    """Synchronous debug endpoint: invoke the agent and return its reply
    directly. No async dispatcher, no Slack creds needed."""
    inbound = await debug.parse(request)
    tenant_id = resolve_tenant_id(inbound.workspace_id)
    result = await client.invoke(
        tenant_id=tenant_id,
        prompt=inbound.text,
        ctx={
            "user_id": inbound.user_id,
            "thread_id": inbound.thread_id,
            "workspace_id": inbound.workspace_id,
        },
    )
    return {"tenant_id": tenant_id, "text": result}
