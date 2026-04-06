"""Generic adapter protocol.

The bridge is client-agnostic at its core. Each transport (Slack, Discord,
Teams, web chat, etc.) implements an Adapter that translates inbound
client events into a normalized InboundMessage and posts replies back
through the same client SDK.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class InboundMessage:
    """Normalized inbound message from any client transport."""
    workspace_id: str
    user_id: str
    text: str
    thread_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class OutboundMessage:
    """Normalized outbound message to send back through a client adapter."""
    text: str
    thread_id: str | None = None


class Adapter(Protocol):
    """Each client transport implements this protocol."""

    name: str

    async def parse(self, request: Any) -> InboundMessage:
        """Parse a client-specific request body into an InboundMessage."""
        ...

    async def ack(self, request: Any) -> Any:
        """Return the immediate ack response (e.g. Slack's 3-second rule).

        For synchronous adapters (debug), return None and use reply() instead.
        """
        ...

    async def reply(self, original: InboundMessage, out: OutboundMessage) -> None:
        """Post a reply back to the client. For Slack: chat.postMessage."""
        ...
