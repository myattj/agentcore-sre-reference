"""Slack adapter — stubbed for local dev.

What's stubbed and why:
  - **Signing secret verification**: production needs HMAC-SHA256 verification
    against `SLACK_SIGNING_SECRET`. Stubbed to keep local dev frictionless.
  - **chat.postMessage**: production uses slack-sdk's WebClient. Stubbed to
    print to stdout so the local loop works without Slack credentials.

To productionize:
  1. `uv sync --extra slack`
  2. Set SLACK_SIGNING_SECRET and SLACK_BOT_TOKEN
  3. Implement verify_signature() (HMAC-SHA256 of body + timestamp header)
  4. Replace the print() in reply() with WebClient(...).chat_postMessage(...)
"""
from __future__ import annotations

from typing import Any

from .core import Adapter, InboundMessage, OutboundMessage


class SlackAdapter:
    name: str = "slack"

    def __init__(
        self,
        signing_secret: str | None = None,
        bot_token: str | None = None,
    ) -> None:
        self.signing_secret = signing_secret
        self.bot_token = bot_token

    async def parse(self, request: Any) -> InboundMessage:
        """Parse a Slack Events API payload into an InboundMessage.

        Stub: does NOT verify the signing secret. Real impl must verify
        before parsing or Slack will reject your app at install time.
        """
        body = await request.json()
        event = body.get("event", {})
        return InboundMessage(
            workspace_id=body.get("team_id", "T_LOCAL"),
            user_id=event.get("user", "U_LOCAL"),
            text=event.get("text", ""),
            thread_id=event.get("thread_ts") or event.get("ts"),
            metadata={
                "channel": event.get("channel"),
                "event_type": event.get("type"),
            },
        )

    async def ack(self, request: Any) -> Any:
        """Slack's 3-second ack contract.

        - URL verification handshake: echo back the challenge token.
        - Normal events: 200 OK with empty body within 3 seconds.

        Real work happens in async_dispatcher after this returns.
        """
        body = await request.json()
        if body.get("type") == "url_verification":
            return {"challenge": body["challenge"]}
        return {"ok": True}

    async def reply(self, original: InboundMessage, out: OutboundMessage) -> None:
        """Post a reply via chat.postMessage. Stubbed: prints to stdout
        unless SLACK_BOT_TOKEN is set."""
        if not self.bot_token:
            channel = original.metadata.get("channel", "<unknown channel>")
            print(
                f"[slack-stub] would post to workspace={original.workspace_id} "
                f"channel={channel} thread={original.thread_id}: {out.text}"
            )
            return
        # Production:
        # from slack_sdk.web.async_client import AsyncWebClient
        # client = AsyncWebClient(token=self.bot_token)
        # await client.chat_postMessage(
        #     channel=original.metadata["channel"],
        #     thread_ts=original.thread_id,
        #     text=out.text,
        # )
        raise NotImplementedError(
            "Real Slack reply not implemented yet. See module docstring."
        )
