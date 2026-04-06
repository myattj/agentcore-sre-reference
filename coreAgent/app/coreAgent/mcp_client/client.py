"""BYO tools via AgentCore Gateway (MCP).

When a tenant has BYO enabled in their config, this module returns a
Strands MCPClient connected to their AgentCore Gateway endpoint. The
agent appends the MCPClient directly to its tools list — Strands handles
listing remote tools and exposing them to the model.

This is the only file in the agent that knows about MCP; the rest of the
agent treats catalog tools and BYO tools identically.
"""
from __future__ import annotations

from typing import Any

from mcp.client.streamable_http import streamablehttp_client
from strands.tools.mcp.mcp_client import MCPClient


def build_byo_mcp_client(
    endpoint: str | None,
    auth: dict[str, Any] | None = None,
) -> MCPClient | None:
    """Return a Strands-compatible MCPClient for the tenant's Gateway endpoint.

    Args:
        endpoint: AgentCore Gateway HTTP/streamable endpoint URL. None means
            BYO is disabled for this tenant.
        auth: Optional auth dict. Currently supports {"headers": {...}} for
            bearer tokens or other arbitrary headers. AgentCore Gateway
            handles ingress auth via the headers passed here.

    Returns:
        MCPClient instance or None if endpoint is not provided.
    """
    if not endpoint:
        return None

    headers = (auth or {}).get("headers", {})
    # streamablehttp_client is a context manager factory; MCPClient calls it
    # lazily when the agent first uses a tool from this collection.
    return MCPClient(lambda: streamablehttp_client(endpoint, headers=headers))
