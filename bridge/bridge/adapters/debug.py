"""Debug adapter — synchronous local-dev path.

POST /debug/message
{
  "workspace_id": "demo-ws",
  "user_id": "u1",
  "text": "echo hello"
}

Unlike the Slack adapter, this is fully synchronous: the bridge invokes the
agent and returns the reply in the HTTP response. Useful for curl tests
and local iteration without any client SDK setup.
"""
from __future__ import annotations

from typing import Any

from .core import InboundMessage, OutboundMessage


class DebugAdapter:
    name: str = "debug"

    async def parse(self, request: Any) -> InboundMessage:
        body = await request.json()
        return InboundMessage(
            workspace_id=body.get("workspace_id", "demo-ws"),
            user_id=body.get("user_id", "u1"),
            text=body.get("text", ""),
            thread_id=body.get("thread_id"),
        )

    async def ack(self, request: Any) -> Any:
        # No ack needed — synchronous flow returns the agent reply directly.
        return None

    async def reply(self, original: InboundMessage, out: OutboundMessage) -> None:
        # The route handler returns the reply itself; this is a no-op so
        # the dispatcher contract still works if we ever switch to async.
        pass
