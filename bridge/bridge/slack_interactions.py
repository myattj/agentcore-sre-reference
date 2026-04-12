"""Slack interactivity payload parser + handlers.

POSTed to by Slack whenever a user interacts with a Block Kit element
(button click, select menu, etc.). Separate from ``/slack/events``
because Slack sends these to a different URL (configured on the App's
"Interactivity & Shortcuts" page) with a different body format
(form-urlencoded with a ``payload`` field containing a JSON string).

## The only action we handle today

``codebase_pick:<N>`` — fired by the ``ask_codebase_choice`` agent tool's
Block Kit buttons. When a user clicks, we:

  1. Edit the original message to remove the buttons and show the
     choice (uses the one-time ``response_url`` from the payload).
  2. Build a synthetic ``InboundMessage`` that looks like the user
     typed "Let's use <repo> for this channel going forward" into
     the same thread.
  3. Hand it to ``dispatch_async`` — the same path Slack Events take.
     The agent's SHORTLIST prompt block teaches it to acknowledge
     the pick with a scoped sentence, which AgentCore Memory's
     SEMANTIC strategy indexes for future invocations.

The synthetic-message approach keeps the dispatch path uniform: no
new agent-invocation surface, no special "button" turn type, no
ctx-flag plumbing. The button click is just another user turn.

## Not yet handled

- Idempotency / dedup. Slack interactivity doesn't retry the same way
  Events API does — a click either reaches us or it doesn't. We rely
  on user-level idempotency for now (a double-click sends two picks
  in the same thread, which the agent handles fine because the
  second pick will be a no-op acknowledgment).
- Other action types. When we add a second action (e.g. escalation
  approval, config edit), split the dispatch below into an
  action_id → handler mapping.
"""
from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from .adapters.core import InboundMessage

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Parsed payload shape — everything we care about from a Slack interactivity POST
# ---------------------------------------------------------------------------

@dataclass
class InteractivityPayload:
    """The subset of a Slack ``block_actions`` payload we use.

    Slack sends a LOT of fields; this shape captures only what any
    downstream handler needs. Unknown or malformed payloads return
    ``None`` from ``parse_interactivity_payload`` instead of a partial
    object, so the route handler can short-circuit cleanly.
    """
    action_id: str
    action_value: str
    team_id: str
    user_id: str
    channel_id: str
    message_ts: str
    thread_ts: str
    response_url: str
    trigger_id: str


def parse_interactivity_payload(
    raw_payload: dict[str, Any],
) -> InteractivityPayload | None:
    """Extract the fields we care about from a Slack interactivity body.

    Slack interactivity payloads are deeply nested. This function pulls
    out the minimum surface the handlers need, validates that the
    critical fields are all present, and returns ``None`` on anything
    missing — the caller treats ``None`` as "malformed, ignore".

    Pure: no IO, no mutation. Safe to unit-test directly.
    """
    if not isinstance(raw_payload, dict):
        return None

    # We only handle block_actions today. Other types (view_submission,
    # shortcut, etc.) fall through as None.
    if raw_payload.get("type") != "block_actions":
        return None

    actions = raw_payload.get("actions") or []
    if not actions:
        return None
    first_action = actions[0]
    if not isinstance(first_action, dict):
        return None

    action_id = first_action.get("action_id") or ""
    action_value = first_action.get("value") or ""
    if not action_id:
        return None

    team = raw_payload.get("team") or {}
    user = raw_payload.get("user") or {}
    channel = raw_payload.get("channel") or {}
    message = raw_payload.get("message") or {}

    team_id = team.get("id") or ""
    user_id = user.get("id") or ""
    channel_id = channel.get("id") or ""
    message_ts = message.get("ts") or ""
    # If the message is already in a thread, thread_ts is set; otherwise
    # the message's own ts becomes the thread root.
    thread_ts = message.get("thread_ts") or message_ts

    response_url = raw_payload.get("response_url") or ""
    trigger_id = raw_payload.get("trigger_id") or ""

    # Minimum viable: we need a team and channel to route the synthetic
    # message, and either a message_ts or thread_ts to keep it in-thread.
    if not team_id or not channel_id or not thread_ts:
        return None

    return InteractivityPayload(
        action_id=action_id,
        action_value=action_value,
        team_id=team_id,
        user_id=user_id,
        channel_id=channel_id,
        message_ts=message_ts,
        thread_ts=thread_ts,
        response_url=response_url,
        trigger_id=trigger_id,
    )


# ---------------------------------------------------------------------------
# codebase_pick handler — synthetic InboundMessage builder
# ---------------------------------------------------------------------------

def is_codebase_pick_action(action_id: str) -> bool:
    """Match the action_id shape the agent's ``ask_codebase_choice`` tool
    emits. Action IDs look like ``codebase_pick:0``, ``codebase_pick:1``
    etc. (index suffix required because Slack needs unique action_ids
    within a block)."""
    return action_id.startswith("codebase_pick")


def build_codebase_pick_synthetic_message(
    payload: InteractivityPayload,
) -> InboundMessage:
    """Translate a ``codebase_pick`` button click into a synthetic user
    message for the agent.

    The exact phrasing matters for two reasons:
      1. It matches the "Let's use <repo> for this channel going
         forward" template the SHORTLIST prompt block tells the model
         to expect, so the model knows to acknowledge with the
         semantically-indexable response.
      2. It names the full repo slug so AgentCore Memory's SEMANTIC
         strategy has a clean token to extract.
    """
    repo = payload.action_value or "the selected codebase"
    text = (
        f"Let's use {repo} for this channel going forward. "
        "Please continue with my original question."
    )
    return InboundMessage(
        workspace_id=payload.team_id,
        user_id=payload.user_id,
        text=text,
        channel_id=payload.channel_id,
        thread_id=payload.thread_ts,
        metadata={
            "event_type": "codebase_pick",
            "event_id": payload.trigger_id,
            "bot_id": None,
            "subtype": None,
            "app_id": None,
            "permalinks": [],
        },
    )


# ---------------------------------------------------------------------------
# response_url post — updates the original Slack message in place
# ---------------------------------------------------------------------------

def post_response_url_update(
    response_url: str,
    picked_repo: str,
) -> None:
    """Replace the original button message with a "✓ Chose <repo>" text.

    ``response_url`` is a one-time URL Slack includes in every
    interactivity payload. POSTing JSON to it edits the message the
    user just interacted with — no bot token needed.

    Best-effort: any failure is logged and swallowed. The agent's
    re-invocation is the load-bearing work; tidying up the original
    message is polish.
    """
    if not response_url:
        return

    body = {
        "replace_original": True,
        "text": f":white_check_mark: Chose *{picked_repo}* — working on it…",
    }
    try:
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            response_url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            # Slack returns "ok" as plain text on success, a JSON error
            # body otherwise. We just check the HTTP status.
            if resp.status >= 300:
                log.warning(
                    "slack_interactions: response_url returned HTTP %s",
                    resp.status,
                )
    except Exception as e:  # noqa: BLE001 — swallow everything, it's polish
        log.warning(
            "slack_interactions: response_url update failed: %s", e
        )


# ---------------------------------------------------------------------------
# Top-level body parser — Slack sends us form-urlencoded with a JSON blob
# ---------------------------------------------------------------------------

def extract_payload_json(raw_body: bytes) -> dict[str, Any] | None:
    """Pull the ``payload`` field out of a form-urlencoded Slack body
    and JSON-parse it.

    Slack interactivity is delivered as
    ``application/x-www-form-urlencoded`` with a single ``payload``
    parameter containing the JSON. This normalizes that into a dict.

    Returns ``None`` on any decode/parse failure so the caller can
    treat it as "malformed, ignore".
    """
    try:
        decoded = raw_body.decode("utf-8")
    except UnicodeDecodeError:
        return None

    parsed_form = urllib.parse.parse_qs(decoded)
    payload_list = parsed_form.get("payload") or []
    if not payload_list:
        return None
    try:
        result = json.loads(payload_list[0])
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(result, dict):
        return None
    return result


__all__ = [
    "InteractivityPayload",
    "build_codebase_pick_synthetic_message",
    "extract_payload_json",
    "is_codebase_pick_action",
    "parse_interactivity_payload",
    "post_response_url_update",
]
