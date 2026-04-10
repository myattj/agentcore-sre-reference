"""Slack Web API helpers for catalog tools.

The agent runs in a separate venv from the bridge and does NOT import
``slack_sdk``. Instead, these helpers call the Slack Web API directly via
``urllib.request`` (stdlib — zero new dependencies).

Bot token retrieval:
  - AGENT_LOCAL_STORES=1: reads ``SLACK_BOT_TOKEN`` env var (mirrors the
    bridge's ``EnvSlackTokenStore``)
  - else: fetches from AWS Secrets Manager at
    ``agentcore/tenants/<tenant_id>/slack/bot_token`` (same path the bridge
    wrote during OAuth). The ``AgentCoreDataAccess`` IAM policy already
    grants ``secretsmanager:GetSecretValue`` on this prefix
    (data-stack.ts:186-191).

Token caching: per-process LRU so we don't hit Secrets Manager on every
tool call within the same invocation. The cache is bounded at 64 entries
(tenants) which is plenty for single-process ``agentcore dev`` and for the
AgentCore Runtime's per-session process model.
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.parse
import urllib.request
from functools import lru_cache
from typing import Any

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Token retrieval
# ---------------------------------------------------------------------------

@lru_cache(maxsize=64)
def get_bot_token(tenant_id: str) -> str | None:
    """Return the Slack bot token for *tenant_id*, or ``None`` if unavailable.

    Cached per process so repeated calls within a single invocation (or
    across invocations in ``agentcore dev``) don't re-fetch from Secrets
    Manager.
    """
    if os.getenv("AGENT_LOCAL_STORES") == "1":
        token = os.getenv("SLACK_BOT_TOKEN")
        if token:
            return token
        log.warning("AGENT_LOCAL_STORES=1 but SLACK_BOT_TOKEN not set")
        return None

    try:
        import boto3  # type: ignore[import-untyped]

        client = boto3.client(
            "secretsmanager",
            region_name=os.getenv("AWS_REGION", "us-west-2"),
        )
        secret_name = f"agentcore/tenants/{tenant_id}/slack/bot_token"
        resp = client.get_secret_value(SecretId=secret_name)
        return resp["SecretString"]
    except Exception:
        log.exception("Failed to fetch bot token for tenant_id=%s", tenant_id)
        return None


# ---------------------------------------------------------------------------
# Slack API calls
# ---------------------------------------------------------------------------

def _slack_get(token: str, method: str, params: dict[str, Any]) -> dict[str, Any]:
    """Call a Slack Web API method via GET and return the parsed JSON."""
    qs = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    url = f"https://slack.com/api/{method}?{qs}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data: dict[str, Any] = json.loads(resp.read())
    if not data.get("ok"):
        log.warning("Slack API %s returned ok=false: %s", method, data.get("error"))
    return data


def _slack_post(token: str, method: str, data: dict[str, Any]) -> dict[str, Any]:
    """Call a Slack Web API method via POST with a JSON body."""
    url = f"https://slack.com/api/{method}"
    body = json.dumps(data).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        result: dict[str, Any] = json.loads(resp.read())
    if not result.get("ok"):
        log.warning("Slack POST %s returned ok=false: %s", method, result.get("error"))
    return result


# ---------------------------------------------------------------------------
# Permalink parsing
# ---------------------------------------------------------------------------

_PERMALINK_RE = re.compile(r"https://[^/]+/archives/([A-Z0-9]+)/p(\d+)")


def parse_permalink(url: str) -> tuple[str, str] | None:
    """Extract (channel_id, thread_ts) from a Slack permalink URL.

    Format: ``https://<workspace>.slack.com/archives/<C_ID>/p<TS_NO_DOT>``
    The thread_ts is reconstructed by inserting a dot 6 chars from the end.

    Returns None if the URL doesn't match the expected format.
    """
    m = _PERMALINK_RE.match(url)
    if not m:
        return None
    channel_id = m.group(1)
    ts_raw = m.group(2)
    if len(ts_raw) <= 6:
        return None
    thread_ts = ts_raw[:-6] + "." + ts_raw[-6:]
    return (channel_id, thread_ts)


def fetch_channel_history(
    token: str,
    channel_id: str,
    query: str,
    limit: int = 20,
) -> str:
    """Fetch recent messages from a channel, filter by keyword, return markdown.

    Uses ``conversations.history`` (requires ``channels:history`` /
    ``groups:history`` scopes, already in the manifest). Client-side
    keyword filtering because the Slack API doesn't support server-side
    text search on ``conversations.history``.
    """
    # Fetch up to 100 recent messages to filter from
    data = _slack_get(token, "conversations.history", {
        "channel": channel_id,
        "limit": 100,
    })
    if not data.get("ok"):
        return f"Error fetching channel history: {data.get('error', 'unknown')}"

    messages = data.get("messages", [])
    query_lower = query.lower()
    matches = [
        m for m in messages
        if query_lower in (m.get("text", "")).lower()
    ][:limit]

    if not matches:
        return f"No messages matching '{query}' found in recent channel history."

    lines = []
    for m in matches:
        user = m.get("user", "unknown")
        text = m.get("text", "")
        ts = m.get("ts", "")
        lines.append(f"- **{user}** ({ts}): {text}")
    return f"Found {len(matches)} message(s) matching '{query}':\n\n" + "\n".join(lines)


def fetch_thread_replies(
    token: str,
    channel_id: str,
    thread_ts: str,
) -> str:
    """Fetch all replies in a Slack thread, return as formatted conversation.

    Uses ``conversations.replies`` (requires ``channels:history`` /
    ``groups:history`` scopes).
    """
    data = _slack_get(token, "conversations.replies", {
        "channel": channel_id,
        "ts": thread_ts,
        "limit": 100,
    })
    if not data.get("ok"):
        return f"Error fetching thread: {data.get('error', 'unknown')}"

    messages = data.get("messages", [])
    if not messages:
        return "Thread is empty or not found."

    lines = []
    for m in messages:
        user = m.get("user", "unknown")
        text = m.get("text", "")
        lines.append(f"**{user}**: {text}")
    return f"Thread ({len(messages)} messages):\n\n" + "\n\n".join(lines)


def fetch_thread_replies_raw(
    token: str,
    channel_id: str,
    thread_ts: str,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Fetch raw reply dicts from a Slack thread. Used by context_assembler
    which needs the raw message list (to filter, truncate, etc.) rather
    than a pre-formatted string."""
    data = _slack_get(token, "conversations.replies", {
        "channel": channel_id,
        "ts": thread_ts,
        "limit": limit,
    })
    if not data.get("ok"):
        return []
    return data.get("messages", [])


def post_message(
    token: str,
    channel_id: str,
    text: str,
    thread_ts: str | None = None,
) -> str:
    """Post a message to a Slack channel. Returns confirmation or error."""
    payload: dict[str, Any] = {"channel": channel_id, "text": text}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    result = _slack_post(token, "chat.postMessage", payload)
    if result.get("ok"):
        return f"Message posted to <#{channel_id}>"
    return f"Failed to post: {result.get('error', 'unknown')}"
