#!/usr/bin/env python3
"""Slack history seeder for the Agent test env.

Loads all seed packs, iterates each SeedMessage in order, and posts to
the test workspace via the bot token. Idempotent via SeederState —
messages already posted (by ``key``) are skipped, so re-running is
safe and resumable.

Usage (run via scripts/testenv-seed.sh which sets up the venv):

  python -m scripts.testenv.seed_slack_history --tenant slack-T12345 [-v]

Or import ``seed_all()`` and call from the bootstrap orchestrator.
"""
from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from typing import Callable

from ._channels import discover_and_join
from ._common import (
    RateLimitedPoster,
    SeedMessage,
    configure_logging,
    load_seeder_bot_token,
    make_slack_client,
    persona,
)
from ._state import SeederState

log = logging.getLogger(__name__)


# ----------------------------------------------------------------------------
# Pack registry
# ----------------------------------------------------------------------------

@dataclass(frozen=True)
class Pack:
    name: str
    loader: Callable[[], list[SeedMessage]]


def _load_packs() -> list[Pack]:
    """Import each pack module lazily and return a list of loaders.

    Order matters for parent_key resolution — packs that reference
    earlier packs' messages need to come later. Within a pack, parents
    must come before replies.
    """
    from .packs import (
        alert_stream,
        casual_chatter,
        incident_timelines,
        qa_history,
        runbook_threads,
    )
    return [
        Pack("casual_chatter", casual_chatter.build),
        Pack("qa_history", qa_history.build),
        Pack("runbook_threads", runbook_threads.build),
        Pack("alert_stream", alert_stream.build),
        Pack("incident_timelines", incident_timelines.build),
    ]


# ----------------------------------------------------------------------------
# Post one message
# ----------------------------------------------------------------------------

def _post_one(
    msg: SeedMessage,
    *,
    channel_map: dict[str, str],
    poster: RateLimitedPoster,
    state: SeederState,
) -> bool:
    """Post a single SeedMessage. Returns True on post, False on skip.
    Raises on hard failure."""
    if state.is_posted(msg.key):
        log.debug("skip %s (already posted)", msg.key)
        return False

    channel_id = channel_map.get(msg.channel)
    if not channel_id:
        log.warning(
            "skip %s: channel #%s not in workspace",
            msg.key,
            msg.channel,
        )
        return False

    p = persona(msg.persona_slug)

    # Resolve parent_key → thread_ts if this is a threaded reply. The
    # parent must have been posted earlier in this run (or a previous
    # resumed run). If not found, log and skip — this is almost always
    # a pack authoring bug.
    thread_ts: str | None = None
    if msg.parent_key:
        thread_ts = state.get_thread_ts(msg.parent_key)
        if not thread_ts:
            log.warning(
                "skip %s: parent %s not posted yet (authoring bug? "
                "parents must precede replies)",
                msg.key,
                msg.parent_key,
            )
            return False
        # Slack requires replies to post to the same channel as the
        # parent. Override the channel from state.
        parent_channel = state.get_channel_of(msg.parent_key)
        if parent_channel:
            channel_id = parent_channel

    response = poster.post(
        channel=channel_id,
        text=msg.text,
        username=p.username,
        icon_emoji=p.icon_emoji,
        thread_ts=thread_ts,
        blocks=msg.blocks,
    )
    ts = response.get("ts", "")
    if not ts:
        raise RuntimeError(f"chat.postMessage returned no ts for {msg.key}: {response}")
    state.mark_posted(msg.key, channel_id, ts)
    log.info(
        "posted %s to #%s ts=%s (%s)",
        msg.key,
        msg.channel,
        ts,
        p.username,
    )
    return True


# ----------------------------------------------------------------------------
# Main entry point
# ----------------------------------------------------------------------------

def seed_all(
    tenant_id: str,
    *,
    region: str | None = None,
    dry_run: bool = False,
) -> dict[str, int]:
    """Seed all packs for a given tenant. Returns counts dict.

    Raises RuntimeError on hard failures (missing seeder token, required
    channels missing, Slack API errors that aren't rate limits).
    """
    bot_token = load_seeder_bot_token()
    client = make_slack_client(bot_token)
    state = SeederState(tenant_id)

    channel_map, missing = discover_and_join(client, state)
    if missing:
        raise RuntimeError(
            f"Workspace is missing {len(missing)} expected channels: "
            f"{', '.join('#' + m for m in missing)}. "
            f"Create them manually in the Slack UI (see "
            f"scripts/testenv/README.md), then re-run."
        )
    log.info("channel map ready: %d channels", len(channel_map))

    packs = _load_packs()
    poster = RateLimitedPoster(client)
    counts = {"posted": 0, "skipped": 0, "failed": 0}

    for pack in packs:
        log.info("=== pack: %s ===", pack.name)
        try:
            messages = pack.loader()
        except Exception as e:  # noqa: BLE001
            log.error("pack %s failed to load: %s", pack.name, e)
            counts["failed"] += 1
            continue
        log.info("pack %s: %d messages", pack.name, len(messages))

        for msg in messages:
            if dry_run:
                log.info("[dry-run] would post %s to #%s", msg.key, msg.channel)
                counts["skipped"] += 1
                continue
            try:
                posted = _post_one(
                    msg,
                    channel_map=channel_map,
                    poster=poster,
                    state=state,
                )
                if posted:
                    counts["posted"] += 1
                else:
                    counts["skipped"] += 1
            except Exception as e:  # noqa: BLE001
                log.error("post failed for %s: %s", msg.key, e)
                counts["failed"] += 1

    return counts


def _main() -> int:
    parser = argparse.ArgumentParser(
        description="Seed an Agent test-env Slack workspace with realistic history.",
    )
    parser.add_argument(
        "--tenant",
        required=True,
        help="tenant_id (typically slack-<team_id>); the separate seeder token "
        "comes from SLACK_SEEDER_BOT_TOKEN.",
    )
    parser.add_argument(
        "--region",
        default=None,
        help="AWS region (defaults to AWS_REGION env var or us-west-2).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List what would be posted without hitting Slack.",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Verbose logging.",
    )
    args = parser.parse_args()

    configure_logging(args.verbose)
    try:
        counts = seed_all(
            tenant_id=args.tenant,
            region=args.region,
            dry_run=args.dry_run,
        )
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    print(
        f"\nseed complete: "
        f"{counts['posted']} posted, "
        f"{counts['skipped']} skipped, "
        f"{counts['failed']} failed"
    )
    return 0 if counts["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(_main())
