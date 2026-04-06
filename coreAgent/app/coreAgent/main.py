"""coreAgent entrypoint.

Multi-tenant agent. At invocation time, loads the tenant's config, builds a
Strands Agent with that tenant's allowed catalog tools + BYO MCP tools, and
streams the response. Memory extraction runs inline after the response.

DO NOT add blocking work to the entrypoint body — it stalls /ping and the
runtime will mark the agent unhealthy. Background work goes through
`tools.start_background_task` and the `app.add_async_task` lifecycle.
"""
from __future__ import annotations

from strands import Agent

from runtime import app
from tenant import load_tenant_config
from tools import build_catalog_tools
from mcp_client.client import build_byo_mcp_client
from model.load import load_model
from memory_store import InMemoryStore, extract_records

# Side-effect import: registers @app.ping handler. Keep this so the runtime
# uses our HealthyBusy logic for the heartbeat lifecycle.
import ping  # noqa: F401

log = app.logger

# Single in-process memory store for `agentcore dev`. Phase 8 swaps this for
# BatchCreateMemoryRecordsStore once the AgentCore Memory resource exists.
_memory = InMemoryStore()


@app.entrypoint
async def invoke(payload, context):
    """Per-invocation: hydrate tenant config, build agent, stream response,
    then run memory extraction inline."""
    tenant_id = payload.get("tenant_id", "demo")
    user_message = payload.get("prompt", "")
    # ctx is bridge-supplied: {user_id, channel_id, thread_id, workspace_id, ...}
    # Tools that need request-specific context read from here.
    ctx = payload.get("ctx", {})  # noqa: F841 -- forwarded to tools later

    log.info(f"Invoking tenant={tenant_id} prompt_len={len(user_message)}")

    config = load_tenant_config(tenant_id)

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
    # text for memory extraction below.
    response_chunks: list[str] = []
    stream = agent.stream_async(user_message)
    async for event in stream:
        if "data" in event and isinstance(event["data"], str):
            response_chunks.append(event["data"])
            yield event["data"]

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


if __name__ == "__main__":
    app.run()
