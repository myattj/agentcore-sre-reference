"""Slack adapter — production-shaped, but happy in local dev.

What this module owns:
  - HMAC-SHA256 verification of Slack webhook signatures (`v0=` scheme)
  - Parsing Slack Events API payloads into the generic `InboundMessage`
  - Posting replies via `slack_sdk.web.async_client.AsyncWebClient`,
    using a per-tenant bot token fetched from `slack_token_store`

Local-dev mode:
  - The caller must explicitly pass `allow_unsigned_requests=True` (the bridge
    does this only under `LOCAL_DEV=1`) before an unset signing secret is
    accepted. Production construction fails fast instead of exposing an
    unsigned webhook.
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

import asyncio
import hashlib
import hmac
import json
import logging
import re
import time
from collections.abc import AsyncIterator
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
        *,
        allow_unsigned_requests: bool = False,
    ) -> None:
        """`bot_token` is retained for compatibility with LOCAL_DEV usage:
        if set, it's surfaced via the env-var path of the token store.
        Production reads from Secrets Manager — never from this constructor."""
        if not signing_secret and not allow_unsigned_requests:
            raise RuntimeError(
                "SLACK_SIGNING_SECRET is required unless LOCAL_DEV=1 explicitly "
                "enables unsigned local requests"
            )
        self.signing_secret = signing_secret
        self.allow_unsigned_requests = allow_unsigned_requests
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

        If `self.signing_secret` is unset after the explicit LOCAL_DEV-only
        constructor opt-in, this method logs a warning and returns silently.

        Reads the raw body via `await request.body()`. Starlette caches
        the body, so subsequent calls to `request.json()` / `request.body()`
        in the route handler still work — there's no double-read penalty.
        """
        if not self.signing_secret:
            if not self.allow_unsigned_requests:  # defensive; constructor rejects this
                raise SlackSignatureError("Slack signing secret is not configured")
            log.warning(
                "SlackAdapter.verify_signature: signing_secret not set; "
                "skipping HMAC verification (explicit LOCAL_DEV path)"
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
        text = event.get("text", "")

        # Extract Slack permalinks from message text for cross-channel
        # context assembly (resolved by the agent's context_assembler).
        _PERMALINK_RE = re.compile(
            r"https://[a-zA-Z0-9-]+\.slack\.com/archives/[A-Z0-9]+/p\d+"
        )
        permalinks = _PERMALINK_RE.findall(text)

        return InboundMessage(
            workspace_id=body.get("team_id", "T_LOCAL"),
            user_id=event.get("user", "U_LOCAL"),
            text=text,
            channel_id=event.get("channel"),
            thread_id=event.get("thread_ts") or event.get("ts"),
            metadata={
                "event_type": event.get("type"),
                "event_id": body.get("event_id"),
                "bot_id": event.get("bot_id"),
                "subtype": event.get("subtype"),
                "app_id": event.get("app_id"),
                "permalinks": permalinks,
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

    async def set_thinking_status(self, original: InboundMessage) -> None:
        """Show a native thinking indicator via Slack's assistant.threads.setStatus.

        Requires `chat:write` scope. The status auto-clears when we post
        the reply via `chat.postMessage` in the same thread. If the call
        fails (missing token, no thread_ts, API error), we log and move
        on — the thinking indicator is UX polish, not load-bearing.
        """
        if not original.channel_id or not original.thread_id:
            return

        try:
            tenant_id = resolve_tenant_id(original.workspace_id)
        except KeyError:
            return

        try:
            token = get_bot_token(tenant_id)
        except KeyError:
            return

        if not token:
            return

        from slack_sdk.web.async_client import AsyncWebClient

        client = AsyncWebClient(token=token)
        try:
            await client.assistant_threads_setStatus(
                channel_id=original.channel_id,
                thread_ts=original.thread_id,
                status="is thinking…",
            )
        except Exception:
            log.debug("set_thinking_status failed", exc_info=True)

    async def stream_reply(
        self,
        original: InboundMessage,
        chunks: AsyncIterator[str],
    ) -> str:
        """Stream agent response chunks to Slack via the streaming API.

        Uses a tick-based drip feed (~25 ticks/sec, ~15 chars/tick) for
        smooth typing animation.  `chat.startStream` on the first tick,
        `chat.appendStream` for subsequent ticks, `chat.stopStream` to
        finalize.  Adaptive: widens the release window when falling behind.

        Returns the full accumulated text (for audit logging).
        """
        try:
            tenant_id = resolve_tenant_id(original.workspace_id)
        except KeyError:
            log.warning(
                "SlackAdapter.stream_reply: no tenant for workspace_id=%s",
                original.workspace_id,
            )
            raise

        try:
            token = get_bot_token(tenant_id)
        except KeyError:
            log.warning(
                "SlackAdapter.stream_reply: no bot token for tenant=%s",
                tenant_id,
            )
            raise

        # LOCAL_DEV stub path: accumulate and print
        if not token:
            accumulated: list[str] = []
            async for chunk in chunks:
                accumulated.append(chunk)
                print(chunk, end="", flush=True)
            print()
            full_text = "".join(accumulated)
            channel = original.channel_id or "<unknown channel>"
            print(
                f"[slack-stream-stub] workspace={original.workspace_id} "
                f"tenant={tenant_id} channel={channel} "
                f"thread={original.thread_id}: {len(full_text)} chars streamed"
            )
            return full_text

        from slack_sdk.web.async_client import AsyncWebClient

        client = AsyncWebClient(token=token)
        channel = original.channel_id or ""

        # Decoupled producer/consumer: the agent produces chunks into a
        # queue; a separate flush loop drains the queue to Slack at a
        # steady pace. This prevents the "flash at the end" where tokens
        # pile up during slow appendStream calls and get dumped all at once.
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        accumulated_chunks: list[str] = []
        stream_ts: str | None = None
        producer_error: BaseException | None = None

        async def _produce() -> None:
            """Read agent chunks into the queue. None = done."""
            nonlocal producer_error
            try:
                async for chunk in chunks:
                    accumulated_chunks.append(chunk)
                    await queue.put(chunk)
            except Exception as exc:
                producer_error = exc
            finally:
                await queue.put(None)

        # ── Streaming pace constants ──
        # Smooth typing animation via a pipelined design:
        #   tick loop  → steady 30ms cadence, ~10-char slices → send_queue
        #   sender task → sequential appendStream calls (API-latency paced)
        # The tick loop is NEVER blocked by API latency, so chunk sizes
        # stay consistent even when Slack is slow.
        TICK = 0.03             # 30ms between releases (~33 ticks/sec)
        TARGET_CHARS = 10       # chars per release at steady state
        MAX_CHARS = 60          # per-release cap during catch-up
        CATCHUP_THRESHOLD = 100 # pending chars before we widen the window

        async def _flush_to_slack(text: str) -> None:
            """Send text to Slack, starting the stream if needed."""
            nonlocal stream_ts
            if stream_ts is None:
                resp = await client.chat_startStream(
                    channel=channel,
                    thread_ts=original.thread_id or "",
                    recipient_team_id=original.workspace_id,
                    recipient_user_id=original.user_id,
                    markdown_text=text,
                )
                stream_ts = resp.get("ts")
            else:
                await client.chat_appendStream(
                    channel=channel,
                    ts=stream_ts,
                    markdown_text=text,
                )

        def _pick_release(pending: str) -> int:
            """How many chars to release this tick (adaptive)."""
            target = TARGET_CHARS
            if len(pending) > CATCHUP_THRESHOLD:
                target = min(
                    MAX_CHARS,
                    TARGET_CHARS * len(pending) // CATCHUP_THRESHOLD,
                )
            return min(len(pending), target)

        try:
            producer_task = asyncio.create_task(_produce())

            # Pipelined sender: processes API calls sequentially so
            # ordering is preserved, but decoupled from the tick loop
            # so API latency doesn't distort the release cadence.
            send_queue: asyncio.Queue[str | None] = asyncio.Queue()
            sender_error: BaseException | None = None

            async def _sender() -> None:
                nonlocal sender_error
                try:
                    while True:
                        text = await send_queue.get()
                        if text is None:
                            break
                        await _flush_to_slack(text)
                except Exception as exc:
                    sender_error = exc

            sender_task = asyncio.create_task(_sender())

            pending = ""
            done = False
            last_release = 0.0

            while not done:
                # Bail early if the sender hit an error.
                if sender_task.done() and sender_error:
                    break

                # Wait for a chunk OR the tick interval, whichever first.
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=TICK)
                    if item is None:
                        done = True
                    else:
                        pending += item
                except asyncio.TimeoutError:
                    pass

                # Non-blocking drain of anything else already queued.
                while not queue.empty():
                    item = queue.get_nowait()
                    if item is None:
                        done = True
                        break
                    pending += item

                if not pending:
                    continue

                now = time.monotonic()
                if (now - last_release) < TICK and not done:
                    continue  # too soon since last release

                n = _pick_release(pending)
                release = pending[:n]
                pending = pending[n:]

                await send_queue.put(release)
                last_release = time.monotonic()

            # ── End of stream: drain remaining text into the sender. ──
            while pending:
                n = _pick_release(pending)
                release = pending[:n]
                pending = pending[n:]
                await send_queue.put(release)

            # Signal sender to finish and wait for all sends to complete.
            await send_queue.put(None)
            await sender_task

            if sender_error is not None:
                raise sender_error

            # Surface producer errors before falling back to postMessage,
            # so dispatch_async can retry via the buffered path.
            await producer_task
            if producer_error is not None:
                raise producer_error

            if stream_ts is None:
                # Zero text produced — fall back to postMessage.
                full_text = "".join(accumulated_chunks)
                await client.chat_postMessage(
                    channel=channel,
                    thread_ts=original.thread_id,
                    text=full_text or "(empty response)",
                )
                return full_text

            await client.chat_stopStream(
                channel=channel,
                ts=stream_ts,
            )

            return "".join(accumulated_chunks)

        except Exception:
            # Error mid-stream: stop the sender and finalize gracefully
            # so the Slack UI doesn't show a perpetual loading state.
            if "sender_task" in locals() and not sender_task.done():
                sender_task.cancel()
                try:
                    await sender_task
                except (asyncio.CancelledError, Exception):
                    pass
            if stream_ts is not None:
                try:
                    await client.chat_stopStream(
                        channel=channel,
                        ts=stream_ts,
                    )
                except Exception:
                    log.debug("stream_reply: stopStream cleanup failed", exc_info=True)
            raise

    async def fetch_message(
        self,
        workspace_id: str,
        channel_id: str,
        message_ts: str,
    ) -> dict[str, Any] | None:
        """Fetch a single Slack message by channel + timestamp.

        Calls `conversations.history` with `latest=ts, inclusive=True, limit=1`.
        Returns the message dict if found, None otherwise. Used by the
        reaction feedback handler to verify bot authorship and retrieve
        the message text.
        """
        try:
            tenant_id = resolve_tenant_id(workspace_id)
        except KeyError:
            return None

        try:
            token = get_bot_token(tenant_id)
        except KeyError:
            return None

        if not token:
            return None

        from slack_sdk.web.async_client import AsyncWebClient

        client = AsyncWebClient(token=token)
        try:
            resp = await client.conversations_history(
                channel=channel_id,
                latest=message_ts,
                inclusive=True,
                limit=1,
            )
            messages = resp.get("messages", [])
            return messages[0] if messages else None
        except Exception:
            log.debug("fetch_message failed for channel=%s ts=%s", channel_id, message_ts, exc_info=True)
            return None

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
