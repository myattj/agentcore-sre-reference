"""Shared helpers for the scripts/testenv rig.

Seeder-token loading, persona registry, rate-limited Slack posting, and the
SeedMessage dataclass that every pack returns lists of.

This module is deliberately standalone. Test data is posted through a separate,
disposable Slack seeder app so the customer-facing Agent app never needs the
high-trust ``chat:write.customize`` or ``channels:join`` scopes.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)


# ----------------------------------------------------------------------------
# Seeder-token loader
# ----------------------------------------------------------------------------

def load_seeder_bot_token() -> str:
    """Load the disposable test seeder's bot token from the environment.

    Never fall back to the tenant Agent token: doing so would make production
    OAuth request test-only identity-customization and channel-join scopes.
    """
    token = os.getenv("SLACK_SEEDER_BOT_TOKEN", "")
    if not token:
        raise RuntimeError(
            "SLACK_SEEDER_BOT_TOKEN is required. Install the separate test "
            "seeder app from scripts/testenv/slack_seeder_manifest.json, then "
            "export its xoxb token in your shell."
        )
    if not token.startswith("xoxb-") or any(char.isspace() for char in token):
        raise RuntimeError(
            "SLACK_SEEDER_BOT_TOKEN must be a Slack bot token beginning with "
            "xoxb- and containing no whitespace."
        )
    return token


# ----------------------------------------------------------------------------
# Personas — used by chat:write.customize to vary author names per message
# ----------------------------------------------------------------------------

@dataclass(frozen=True)
class Persona:
    """A fake author identity used by chat:write.customize.

    ``slug`` is the key packs reference. ``username`` and ``icon_emoji``
    are what Slack users see in the channel sidebar. ``kind`` is 'bot'
    for fake external services (PagerDuty, Datadog...) and 'human' for
    fake teammates (Morgan, Priya...).

    The bot personas make the agent's ``bot_policy.allow_all_bots``
    behavior observable — even though all these messages post through
    a single bot token, their Slack-visible username reads as a bot
    service, which is the scenario customers hit in production.
    """
    slug: str
    username: str
    icon_emoji: str
    kind: str  # 'bot' or 'human'


PERSONAS: dict[str, Persona] = {
    # Fake bot services -------------------------------------------------------
    "pagerduty": Persona("pagerduty", "PagerDuty", ":fire:", "bot"),
    "datadog":   Persona("datadog",   "Datadog",   ":dog2:", "bot"),
    "sentry":    Persona("sentry",    "Sentry",    ":bug:", "bot"),
    "github":    Persona("github",    "GitHub",    ":octocat:", "bot"),
    "statuspage": Persona("statuspage", "Statuspage", ":traffic_light:", "bot"),

    # Fake teammates ----------------------------------------------------------
    "morgan":   Persona("morgan",   "Morgan Chen",    ":woman_technologist:", "human"),
    "priya":    Persona("priya",    "Priya Ramanathan", ":bar_chart:", "human"),
    "alex":     Persona("alex",     "Alex Diaz",      ":lock:", "human"),
    "jamie":    Persona("jamie",    "Jamie Park",     ":clipboard:", "human"),
    "sam":      Persona("sam",      "Sam O'Brien",    ":man_technologist:", "human"),
    "taylor":   Persona("taylor",   "Taylor Kim",     ":pager:", "human"),
    "riley":    Persona("riley",    "Riley Novak",    ":gear:", "human"),
    "jordan":   Persona("jordan",   "Jordan Webb",    ":computer:", "human"),
}


def persona(slug: str) -> Persona:
    try:
        return PERSONAS[slug]
    except KeyError as e:
        raise KeyError(
            f"Unknown persona slug {slug!r}. Known: {sorted(PERSONAS)}"
        ) from e


# ----------------------------------------------------------------------------
# SeedMessage — the unit of seeded content
# ----------------------------------------------------------------------------

@dataclass(frozen=True)
class SeedMessage:
    """One Slack message to seed.

    ``key`` is a stable idempotency key (must be unique across the whole
    run). The state tracker skips keys it's already posted so re-running
    the seeder is safe and resumable.

    ``parent_key`` is the key of a previously-posted SeedMessage in the
    same run — when set, this message is posted as a thread reply to the
    parent (the seeder resolves parent_key → parent's posted thread_ts
    at post time, so ordering matters: parents must come before replies
    in the pack list).

    ``channel`` is the **channel name** (e.g. ``alerts-sre``), not the
    ID. The runner resolves to an ID via the channel map.
    """
    key: str
    channel: str
    persona_slug: str
    text: str
    parent_key: str | None = None
    # Optional Slack blocks (for rich PagerDuty/Datadog-style layouts).
    # Most seed messages use plain text; blocks are for the alert packs.
    blocks: list[dict[str, Any]] | None = None


# ----------------------------------------------------------------------------
# Rate-limited Slack sender
# ----------------------------------------------------------------------------

# Slack's chat.postMessage is tier-4 by workspace but enforces a
# per-channel 1/second soft limit. 1.2s between posts is safe for
# sustained single-channel bursts; faster runs risk random 429s.
_MIN_POST_INTERVAL_SECONDS = 1.2


class RateLimitedPoster:
    """Wraps a slack_sdk.WebClient to sleep between chat.postMessage calls
    and retry once on HTTP 429. Not thread-safe — one poster per seeder
    run."""

    def __init__(self, client: Any) -> None:
        self._client = client
        self._last_post_ts: float = 0.0

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_post_ts
        remaining = _MIN_POST_INTERVAL_SECONDS - elapsed
        if remaining > 0:
            time.sleep(remaining)

    def post(
        self,
        *,
        channel: str,
        text: str,
        username: str,
        icon_emoji: str,
        thread_ts: str | None = None,
        blocks: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Post one message, honoring the rate limit. Returns the Slack
        chat.postMessage response (a dict-like SlackResponse)."""
        from slack_sdk.errors import SlackApiError

        self._throttle()
        attempt = 0
        while True:
            try:
                kwargs: dict[str, Any] = {
                    "channel": channel,
                    "text": text,
                    "username": username,
                    "icon_emoji": icon_emoji,
                }
                if thread_ts:
                    kwargs["thread_ts"] = thread_ts
                if blocks:
                    kwargs["blocks"] = blocks
                response = self._client.chat_postMessage(**kwargs)
                self._last_post_ts = time.monotonic()
                return response.data  # type: ignore[no-any-return]
            except SlackApiError as e:
                status = getattr(e.response, "status_code", None)
                retry_after_hdr = (
                    getattr(e.response, "headers", {}) or {}
                ).get("Retry-After")
                if status == 429 and attempt < 3:
                    retry_after = int(retry_after_hdr) if retry_after_hdr else 2
                    log.warning("slack 429, sleeping %ds", retry_after)
                    time.sleep(retry_after + 1)
                    attempt += 1
                    continue
                raise


def make_slack_client(bot_token: str) -> Any:
    """Construct a sync slack_sdk.WebClient bound to the bot token."""
    from slack_sdk import WebClient
    return WebClient(token=bot_token)


# ----------------------------------------------------------------------------
# Logging
# ----------------------------------------------------------------------------

def configure_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        level=logging.DEBUG if verbose else logging.INFO,
        datefmt="%H:%M:%S",
    )
    # slack_sdk is chatty on INFO — keep it quiet unless verbose.
    if not verbose:
        logging.getLogger("slack_sdk").setLevel(logging.WARNING)
        logging.getLogger("botocore").setLevel(logging.WARNING)
        logging.getLogger("boto3").setLevel(logging.WARNING)
        logging.getLogger("urllib3").setLevel(logging.WARNING)
