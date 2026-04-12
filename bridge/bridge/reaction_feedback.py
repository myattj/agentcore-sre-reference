"""Reaction feedback: capture Slack reactions on bot replies as feedback signals.

When a user reacts to a bot message with a feedback-signal emoji (thumbsup,
thumbsdown, etc.), this module:
  1. Verifies the reacted message is from our bot
  2. Fetches the thread parent (the user's original question)
  3. Invokes the agent with ``event_type="reaction_feedback"`` so the agent
     can write an audit row + memory record without an LLM call

Non-feedback emojis (random reactions, custom emoji) are silently dropped.
Reactions on non-bot messages are also dropped — we only learn from feedback
on our own replies.

The agent-side handler is in ``coreAgent/app/coreAgent/main.py``: it
short-circuits the Strands Agent path and writes directly to audit + memory.
"""
from __future__ import annotations

import logging
from typing import Any

from .adapters.slack import SlackAdapter
from .client import AgentCoreClient

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Sentiment mapping
# ---------------------------------------------------------------------------
#
# Only reactions that are unambiguous feedback signals. Everything else is
# ignored — people react with all sorts of emojis that don't mean "good
# answer" or "bad answer".

_POSITIVE_REACTIONS: frozenset[str] = frozenset({
    "+1",
    "thumbsup",
    "white_check_mark",
    "heavy_check_mark",
    "heart",
    "tada",
    "100",
    "star",
    "pray",
    "raised_hands",
})

_NEGATIVE_REACTIONS: frozenset[str] = frozenset({
    "-1",
    "thumbsdown",
    "x",
    "no_entry",
    "no_entry_sign",
    "confused",
    "thinking_face",
    "warning",
})


def classify_reaction(emoji_name: str) -> str | None:
    """Return ``"positive"``, ``"negative"``, or ``None`` (not a feedback signal)."""
    if emoji_name in _POSITIVE_REACTIONS:
        return "positive"
    if emoji_name in _NEGATIVE_REACTIONS:
        return "negative"
    return None


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

async def dispatch_reaction_feedback(
    adapter: SlackAdapter,
    event: dict[str, Any],
    client: AgentCoreClient,
    tenant_id: str,
    slack_app_id: str,
) -> None:
    """Process a ``reaction_added`` event and forward feedback to the agent.

    Called as a FastAPI BackgroundTask from the ``/slack/events`` route.
    Runs after the 3-second ack, so latency here is fine.

    Steps:
      1. Classify the reaction emoji → positive/negative/None
      2. Fetch the reacted message from Slack (``conversations.history``)
      3. Verify it's from our bot (check ``app_id`` or ``bot_id``)
      4. Fetch the thread parent (the user's original question)
      5. Invoke the agent with ``event_type="reaction_feedback"``
    """
    reaction = event.get("reaction", "")
    sentiment = classify_reaction(reaction)
    if sentiment is None:
        return  # not a feedback-signal emoji

    item = event.get("item", {})
    if item.get("type") != "message":
        return  # only handle message reactions

    channel_id = item.get("channel", "")
    message_ts = item.get("ts", "")
    reactor_user_id = event.get("user", "")
    workspace_id = event.get("team_id", "")

    if not channel_id or not message_ts:
        return

    # ── Step 2: Fetch the reacted message ──
    bot_message = await adapter.fetch_message(workspace_id, channel_id, message_ts)
    if bot_message is None:
        log.debug(
            "reaction_feedback: could not fetch message channel=%s ts=%s",
            channel_id, message_ts,
        )
        return

    # ── Step 3: Verify it's our bot's message ──
    msg_app_id = bot_message.get("app_id", "")
    msg_bot_id = bot_message.get("bot_id", "")
    if not msg_bot_id and not msg_app_id:
        return  # not a bot message at all
    if slack_app_id and msg_app_id and msg_app_id != slack_app_id:
        return  # different bot's message

    bot_answer = bot_message.get("text", "")
    thread_ts = bot_message.get("thread_ts", "")

    # ── Step 4: Fetch the thread parent (user's question) ──
    user_question = ""
    if thread_ts and thread_ts != message_ts:
        parent_msg = await adapter.fetch_message(workspace_id, channel_id, thread_ts)
        if parent_msg is not None:
            user_question = parent_msg.get("text", "")

    # ── Step 5: Invoke agent with feedback payload ──
    log.info(
        "reaction_feedback: tenant=%s channel=%s reaction=%s sentiment=%s",
        tenant_id, channel_id, reaction, sentiment,
    )

    try:
        await client.invoke(
            tenant_id=tenant_id,
            prompt="",  # no user prompt — this is a system event
            ctx={
                "user_id": reactor_user_id,
                "channel_id": channel_id,
                "thread_id": thread_ts or message_ts,
                "workspace_id": workspace_id,
                "event_type": "reaction_feedback",
                "feedback": {
                    "reaction": reaction,
                    "sentiment": sentiment,
                    "bot_message_ts": message_ts,
                    "bot_answer": bot_answer,
                    "user_question": user_question,
                    "reactor_user_id": reactor_user_id,
                },
            },
        )
    except Exception:
        # Feedback is best-effort. Never break the bridge over a failed
        # feedback write. Diagnose via CloudWatch logs.
        log.warning(
            "reaction_feedback: agent invocation failed for tenant=%s",
            tenant_id,
            exc_info=True,
        )
