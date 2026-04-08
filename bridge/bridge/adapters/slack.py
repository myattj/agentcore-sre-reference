"""Slack adapter — production-shaped, but happy in local dev.

What this module owns:
  - HMAC-SHA256 verification of Slack webhook signatures (`v0=` scheme)
  - Parsing Slack Events API payloads into the generic `InboundMessage`
  - Posting replies via `slack_sdk.web.async_client.AsyncWebClient`,
    using a per-tenant bot token fetched from `slack_token_store`

Local-dev mode:
  - If `signing_secret` is None (env var unset under `LOCAL_DEV=1`), HMAC
    verification is SKIPPED and a one-time warning is logged. The local
    debug loop and synthetic curl tests can post unsigned bodies.
  - If the per-tenant token store returns an empty string (no
    `SLACK_BOT_TOKEN` env var, no real OAuth-installed token in Secrets
    Manager), `reply()` falls back to printing to stdout instead of
    posting to Slack. Lets the local loop run end-to-end without any
    Slack credentials.

Production wiring:
  - Bridge sets `SLACK_SIGNING_SECRET` (one global secret, shared across
    all tenants — Model A: shared Slack app)
  - Bridge sets `SLACK_BOT_TOKEN` only as a fallback for tenants whose
    OAuth install hasn't yet stored a real token in Secrets Manager
    (typically only used by smoke tests against fresh deployments)
  - Per-tenant tokens live at `agentcore/tenants/<tenant_id>/slack/bot_token`
    and are fetched on demand by `slack_token_store.get_bot_token()`

Authentication and replay protection:
  - The `v0=` HMAC scheme is documented at https://api.slack.com/authentication/verifying-requests-from-slack
  - We reject any request whose `X-Slack-Request-Timestamp` is more than
    5 minutes old (Slack's recommended replay window).
  - Comparison uses `hmac.compare_digest` (constant-time).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from typing import Any

from ..tenant_resolver import resolve_tenant_id
from ..slack_token_store import get_bot_token
from .core import InboundMessage, OutboundMessage

log = logging.getLogger(__name__)

# Slack's recommended replay window. The signed timestamp must be within
# this many seconds of the current time, in either direction.
_REPLAY_WINDOW_SECONDS = 60 * 5


class SlackSignatureError(Exception):
    """Raised when an inbound Slack request fails HMAC verification.
    The route handler should catch this and return 401."""


class SlackAdapter:
    name: str = "slack"

    def __init__(
        self,
        signing_secret: str | None = None,
        bot_token: str | None = None,
    ) -> None:
        """`bot_token` is retained for compatibility with LOCAL_DEV usage:
        if set, it's surfaced via the env-var path of the token store.
        Production reads from Secrets Manager — never from this constructor."""
        self.signing_secret = signing_secret
        # Kept for legacy LOCAL_DEV callers that still pass it. Token
        # lookup goes through `slack_token_store.get_bot_token()` which
        # respects LOCAL_DEV / SLACK_BOT_TOKEN env vars.
        self.bot_token = bot_token

    # ------------------------------------------------------------------
    # Inbound
    # ------------------------------------------------------------------

    async def verify_signature(self, request: Any) -> None:
        """Verify the inbound request's `X-Slack-Signature` header.

        Raises `SlackSignatureError` on failure. Returns silently on success.

        If `self.signing_secret` is unset (LOCAL_DEV without env var), this
        method LOGS a warning and returns silently. Production deployments
        MUST set `SLACK_SIGNING_SECRET` or every Slack request will be
        accepted regardless of provenance.

        Reads the raw body via `await request.body()`. Starlette caches
        the body, so subsequent calls to `request.json()` / `request.body()`
        in the route handler still work — there's no double-read penalty.
        """
        if not self.signing_secret:
            log.warning(
                "SlackAdapter.verify_signature: signing_secret not set; "
                "skipping HMAC verification (LOCAL_DEV path)"
            )
            return

        timestamp = request.headers.get("x-slack-request-timestamp", "")
        signature = request.headers.get("x-slack-signature", "")
        if not timestamp or not signature:
            raise SlackSignatureError("missing X-Slack-Request-Timestamp or X-Slack-Signature header")

        # Reject stale requests (replay protection).
        try:
            ts_int = int(timestamp)
        except ValueError as e:
            raise SlackSignatureError(f"invalid timestamp: {timestamp!r}") from e
        if abs(time.time() - ts_int) > _REPLAY_WINDOW_SECONDS:
            raise SlackSignatureError(
                f"timestamp {ts_int} outside replay window of {_REPLAY_WINDOW_SECONDS}s"
            )

        # Compute expected signature: v0=hmac_sha256(secret, "v0:{ts}:{body}")
        raw_body = await request.body()
        basestring = b"v0:" + timestamp.encode("ascii") + b":" + raw_body
        digest = hmac.new(
            self.signing_secret.encode("utf-8"),
            basestring,
            hashlib.sha256,
        ).hexdigest()
        expected = "v0=" + digest

        if not hmac.compare_digest(expected, signature):
            raise SlackSignatureError("HMAC signature mismatch")

    async def parse(self, request: Any) -> InboundMessage:
        """Parse a Slack Events API payload into an InboundMessage.

        Does NOT call `verify_signature()` — the route handler is
        responsible for calling that BEFORE parse, so a failed signature
        short-circuits with a 401 before any further processing happens.
        """
        # Use raw body bytes + json.loads rather than `request.json()` to
        # guarantee we parse the EXACT bytes the signature was computed
        # against. (Starlette's caching makes this redundant in practice,
        # but the explicit form is harder to break accidentally.)
        raw_body = await request.body()
        body = json.loads(raw_body) if raw_body else {}

        event = body.get("event", {})
        return InboundMessage(
            workspace_id=body.get("team_id", "T_LOCAL"),
            user_id=event.get("user", "U_LOCAL"),
            text=event.get("text", ""),
            channel_id=event.get("channel"),
            thread_id=event.get("thread_ts") or event.get("ts"),
            metadata={
                "event_type": event.get("type"),
                "event_id": body.get("event_id"),
            },
        )

    async def ack(self, request: Any) -> Any:
        """Slack's 3-second ack contract.

        - URL verification handshake: echo back the challenge token.
        - Normal events: 200 OK with empty body within 3 seconds.

        Real work happens in async_dispatcher after this returns.
        """
        raw_body = await request.body()
        body = json.loads(raw_body) if raw_body else {}
        if body.get("type") == "url_verification":
            return {"challenge": body["challenge"]}
        return {"ok": True}

    # ------------------------------------------------------------------
    # Outbound
    # ------------------------------------------------------------------

    async def reply(self, original: InboundMessage, out: OutboundMessage) -> None:
        """Post a reply via Slack `chat.postMessage`.

        Resolves the tenant_id from `original.workspace_id`, fetches the
        per-tenant bot token from `slack_token_store`, and calls Slack's
        AsyncWebClient. If the token store returns an empty string
        (LOCAL_DEV with no SLACK_BOT_TOKEN), falls back to printing to
        stdout — keeps the local loop functional with zero credentials.
        """
        try:
            tenant_id = resolve_tenant_id(original.workspace_id)
        except KeyError:
            # Unknown workspace — bridge slack_events handler should
            # have caught this earlier, but reply() is also called from
            # the async dispatcher path so guard here too.
            log.warning(
                "SlackAdapter.reply: no tenant for workspace_id=%s; dropping reply",
                original.workspace_id,
            )
            return

        try:
            token = get_bot_token(tenant_id)
        except KeyError:
            log.warning(
                "SlackAdapter.reply: no bot token for tenant=%s; dropping reply",
                tenant_id,
            )
            return

        if not token:
            channel = original.channel_id or "<unknown channel>"
            print(
                f"[slack-stub] would post to workspace={original.workspace_id} "
                f"tenant={tenant_id} channel={channel} thread={original.thread_id}: {out.text}"
            )
            return

        # Lazy import — slack_sdk is only required when we're actually
        # posting to Slack, so the local-dev stub path doesn't pull it.
        from slack_sdk.web.async_client import AsyncWebClient

        client = AsyncWebClient(token=token)
        response = await client.chat_postMessage(
            channel=original.channel_id or "",
            thread_ts=original.thread_id,
            text=out.text,
        )
        if not response.get("ok"):
            log.warning(
                "SlackAdapter.reply: chat.postMessage returned not-ok for tenant=%s: %s",
                tenant_id,
                response.data if hasattr(response, "data") else response,
            )
