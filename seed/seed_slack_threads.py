#!/usr/bin/env python3
"""
Seed Slack with realistic incident threads for testing the Agent investigation bot.

Creates three threads:
  1. Datadog alert (P1 high latency)
  2. Customer escalation (Acme Corp enterprise)
  3. Engineer asking for help (N+1 suspicion)

Usage:
  uv run --with requests python seed/seed_slack_threads.py
  SLACK_SEEDER_BOT_TOKEN=... SLACK_SEED_CHANNEL_ID=... \
    uv run --with requests python seed/seed_slack_threads.py --apply

Optional:
  --bot-user-id U123456   (Agent's bot user ID, for @mentions in replies)
"""

import argparse
import os
import re
import sys
import time
from dataclasses import dataclass

import requests

# ---------------------------------------------------------------------------
# Slack helpers
# ---------------------------------------------------------------------------

SLACK_BASE = "https://slack.com/api"
SLACK_CHANNEL_ID_RE = re.compile(r"[CGD][A-Z0-9]{8,14}")
SLACK_USER_ID_RE = re.compile(r"[UW][A-Z0-9]{8,14}")


@dataclass(frozen=True)
class ThreadSpec:
    title: str
    root: str
    replies: tuple[str, ...]


class SlackApiError(RuntimeError):
    """Slack accepted the HTTP request but rejected the API operation."""


def validate_slack_channel_id(channel: str) -> str:
    """Validate a Slack conversation ID without accepting names or URLs."""
    normalized = channel.strip().upper()
    if not SLACK_CHANNEL_ID_RE.fullmatch(normalized):
        raise ValueError("Slack channel must be an ID like C0123456789 or G0123456789")
    return normalized


def validate_slack_user_id(user_id: str) -> str:
    """Validate a Slack bot/user ID used in a mention."""
    normalized = user_id.strip().upper()
    if not SLACK_USER_ID_RE.fullmatch(normalized):
        raise ValueError("Agent bot user must be an ID like U0123456789")
    return normalized


def validate_seeder_token(token: str) -> str:
    """Require a bot token while rejecting whitespace and pasted shell syntax."""
    if not token.startswith("xoxb-") or any(character.isspace() for character in token):
        raise ValueError(
            "SLACK_SEEDER_BOT_TOKEN must be a Slack bot token beginning with xoxb-"
        )
    return token


def _post_message(
    token: str,
    channel: str,
    text: str,
    thread_ts: str | None = None,
    unfurl_links: bool = False,
) -> dict:
    """Post a message to Slack. Returns the API response dict."""
    url = f"{SLACK_BASE}/chat.postMessage"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
    }
    payload: dict = {
        "channel": channel,
        "text": text,
        "unfurl_links": unfurl_links,
        "unfurl_media": False,
    }
    if thread_ts:
        payload["thread_ts"] = thread_ts

    resp = requests.post(url, json=payload, headers=headers, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        error = data.get("error", "unknown")
        if error == "invalid_auth":
            raise SlackApiError("invalid Slack token")
        if error == "channel_not_found":
            raise SlackApiError(
                "channel not found; make sure the seeder bot is invited to the channel"
            )
        raise SlackApiError(f"Slack API error: {error}")
    return data


def _thread_url(channel: str, ts: str) -> str:
    """Build a Slack deep-link for a thread."""
    ts_clean = ts.replace(".", "")
    return f"https://slack.com/archives/{channel}/p{ts_clean}"


def _mention_or_fallback(bot_user_id: str | None, fallback: str) -> str:
    if bot_user_id:
        return f"<@{bot_user_id}>"
    return fallback


def build_thread_specs(bot_user_id: str | None) -> tuple[ThreadSpec, ...]:
    """Build the complete synthetic incident without contacting Slack."""
    if bot_user_id is not None:
        bot_user_id = validate_slack_user_id(bot_user_id)

    datadog_root = (
        ":rotating_light: *[P1] High Latency — acme-data-api*\n"
        "\n"
        "*Metric:* `acme.api.request.duration` > 2000ms\n"
        "*Endpoint:* `/api/v1/items/export`\n"
        "*Service:* `acme-data-api` | *Env:* `production`\n"
        "*Duration:* 45 minutes and counting\n"
        "*Current value:* p99 = 4,832ms (threshold: 2,000ms)\n"
        "\n"
        "<https://app.datadoghq.com/metric/explorer?exp_metric=acme.api.request.duration&exp_group=endpoint|View in Datadog>"
    )
    datadog_replies = (
        "Looking into this — seems specific to the export endpoint. Other items routes are fine.",
        "Checked the deploy log — we shipped a new export endpoint about an hour before this started. cc @morgan",
        "Also seeing connection pool exhaustion in the logs: `sqlalchemy.exc.TimeoutError: QueuePool limit of 5 overflow 3 reached, connection timed out, timeout 3`",
        (
            f"{_mention_or_fallback(bot_user_id, 'Can someone investigate this?')} "
            "can you investigate this? Check the recent commits to acme-data-api and correlate with the latency spike."
            if bot_user_id
            else "Can someone investigate this? Check the recent commits to acme-data-api and correlate with the latency spike."
        ),
    )

    escalation_root = (
        "*Customer Escalation — Acme Corp (Enterprise)*\n"
        "\n"
        "Their integration team reports the `/api/v1/items/export` endpoint is returning "
        "500 errors intermittently. Started ~1 hour ago. They use this endpoint for their "
        "nightly data sync pipeline and it's blocking their ETL.\n"
        "\n"
        "Error they're seeing:\n"
        "```\n"
        "HTTP 500 Internal Server Error\n"
        '{"detail": "Internal server error"}\n'
        "```\n"
        "\n"
        "Timeline from their side:\n"
        "• 2 hours ago: first timeout\n"
        "• 1.5 hours ago: ~30% of requests failing\n"
        "• Now: ~50% failure rate, pipeline halted\n"
        "\n"
        "They're asking for an RCA by EOD."
    )
    escalation_mention = _mention_or_fallback(bot_user_id, "Can someone")
    escalation_replies = (
        "This is probably related to the P1 in #sre-alerts. Same endpoint.",
        "Can we check what changed in the acme-data-api repo recently? I think someone shipped a new export feature.",
        f"{escalation_mention} investigate the acme-data-api latency issue — customer is blocked",
    )

    engineer_root = (
        "Something is off with the items export endpoint in acme-data-api. "
        "I added it this morning and it worked fine in staging but production is showing p99 > 5s.\n"
        "\n"
        "The endpoint loads items and enriches them with owner details. "
        "Could be an N+1 but I used `session.get()` which should hit the identity map... right?\n"
        "\n"
        "Relevant code is in `backend/app/api/routes/items.py` — the `export_items` function.\n"
        "\n"
        "Also seeing this in the connection pool stats — it's fully saturated:\n"
        "```\n"
        "pool size: 5, max overflow: 3, checked out: 8, queue: 12\n"
        "```\n"
        "\n"
        "Someone also pushed a pool config change to `backend/app/core/db.py` — "
        "reduced pool_timeout to 3s. Could that be making this worse?"
    )
    engineer_mention = _mention_or_fallback(bot_user_id, "Can someone")
    engineer_replies = (
        (
            "The pool_timeout=3 is way too aggressive with an N+1 on the export route. "
            "Each export request holds a connection while it does hundreds of individual user lookups."
        ),
        (
            f"{engineer_mention} can you look at the recent commits to acme-labs/data-api "
            "and tell us what's causing the latency spike on the export endpoint? "
            "Check the Datadog metrics too."
        ),
    )

    return (
        ThreadSpec("Datadog Alert (P1 High Latency)", datadog_root, datadog_replies),
        ThreadSpec(
            "Customer Escalation (Acme Corp)", escalation_root, escalation_replies
        ),
        ThreadSpec("Engineer Asking for Help", engineer_root, engineer_replies),
    )


# ---------------------------------------------------------------------------
# Thread definitions
# ---------------------------------------------------------------------------


def post_thread(token: str, channel: str, spec: ThreadSpec, number: int) -> None:
    """Post one synthetic thread, pacing replies to resemble a real conversation."""
    print(f"\n--- Thread {number}: {spec.title} ---")
    resp = _post_message(token, channel, spec.root)
    thread_ts = resp["ts"]
    print(f"  Root message posted: {_thread_url(channel, thread_ts)}")

    time.sleep(2)
    for index, text in enumerate(spec.replies, 1):
        _post_message(token, channel, text, thread_ts=thread_ts)
        print(f"  Reply {index}/{len(spec.replies)} posted")
        if index < len(spec.replies):
            time.sleep(5)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Seed Slack with realistic incident threads for testing the Agent investigation bot."
    )
    parser.add_argument(
        "--channel",
        default=os.environ.get("SLACK_SEED_CHANNEL_ID"),
        help="destination channel ID (or set SLACK_SEED_CHANNEL_ID)",
    )
    parser.add_argument(
        "--bot-user-id",
        default=os.environ.get("AGENT_BOT_USER_ID"),
        help="Agent bot's Slack user ID for @mentions (optional)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="post threads; without this flag the script only generates a preview",
    )
    args = parser.parse_args()

    token = os.environ.get("SLACK_SEEDER_BOT_TOKEN")
    if args.apply and not token:
        parser.error(
            "--apply requires SLACK_SEEDER_BOT_TOKEN from a separate seeder app"
        )
    if args.apply and not args.channel:
        parser.error("--apply requires --channel or SLACK_SEED_CHANNEL_ID")

    try:
        if token:
            token = validate_seeder_token(token)
        channel = (
            validate_slack_channel_id(args.channel) if args.channel else "C0123456789"
        )
        bot_user_id = (
            validate_slack_user_id(args.bot_user_id) if args.bot_user_id else None
        )
    except ValueError as exc:
        parser.error(str(exc))

    specs = build_thread_specs(bot_user_id)

    action = "Seeding" if args.apply else "Previewing"
    print(f"{action} Slack threads in channel {channel}")
    if bot_user_id:
        print(f"Bot @mentions will target user ID: {bot_user_id}")
    else:
        print("No --bot-user-id provided; @mentions will use generic fallback text.")
    print("=" * 60)

    if args.apply:
        try:
            for number, spec in enumerate(specs, 1):
                post_thread(token, channel, spec, number)
        except (requests.RequestException, SlackApiError) as exc:
            print(f"\nSlack seed failed: {exc}", file=sys.stderr)
            return 1
    else:
        for number, spec in enumerate(specs, 1):
            print(f"  {number}. {spec.title}: 1 root + {len(spec.replies)} replies")

    print(f"\n{'=' * 60}")
    result = "created" if args.apply else "generated"
    print(f"Done. 3 threads {result} with replies.")
    if not args.apply:
        print(
            "No network requests were made. Re-run with --apply to post these threads."
        )
    print("Threads reference repos:")
    print("  - acme-labs/data-api")
    print("  - acme-labs/platform-infra")
    print("  - acme-labs/incident-runbooks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
