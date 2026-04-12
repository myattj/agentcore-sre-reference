"""Channel discovery + join for the test env.

We don't create channels — that requires ``channels:manage`` which
broadens the OAuth scope surface. Instead, the README tells the user
to create the ten ``TESTENV_CHANNELS`` in their workspace once, and
this module:

  1. lists public channels the bot can see via ``conversations.list``
  2. maps each TESTENV_CHANNELS name → id
  3. joins each one via ``conversations.join`` (idempotent)
  4. warns about any missing channels

Returns the full name → id map for the config builder + seeder. Caches
results into ``SeederState`` so subsequent runs skip the listing call.
"""
from __future__ import annotations

import logging
from typing import Any

from ._state import SeederState
from .config import TESTENV_CHANNELS

log = logging.getLogger(__name__)


def discover_and_join(
    client: Any,
    state: SeederState,
    *,
    force_refresh: bool = False,
) -> tuple[dict[str, str], list[str]]:
    """Return ``(channel_map, missing)``.

    ``channel_map`` maps ``TESTENV_CHANNELS`` name → Slack channel id
    for every channel that exists in the workspace. ``missing`` is the
    list of expected channel names that aren't in the workspace yet —
    the caller should print these and tell the user to create them.

    Uses ``state.channel_ids`` as a cache. Pass ``force_refresh=True``
    to ignore the cache and re-list.
    """
    from slack_sdk.errors import SlackApiError

    if not force_refresh and state.channel_ids:
        cached = state.channel_ids
        missing = [name for name in TESTENV_CHANNELS if name not in cached]
        if not missing:
            log.info("using cached channel map (%d channels)", len(cached))
            return dict(cached), []
        log.info(
            "cached channel map is incomplete (%d missing), re-listing",
            len(missing),
        )

    # Fresh listing. Use pagination — workspaces with >200 channels
    # need it.
    name_to_id: dict[str, str] = {}
    cursor: str | None = None
    page = 0
    while True:
        page += 1
        log.debug("conversations.list page=%d", page)
        kwargs: dict[str, Any] = {
            "types": "public_channel",
            "exclude_archived": True,
            "limit": 200,
        }
        if cursor:
            kwargs["cursor"] = cursor
        try:
            resp = client.conversations_list(**kwargs)
        except SlackApiError as e:
            raise RuntimeError(
                f"conversations.list failed: {e.response.data.get('error')}. "
                f"Required scopes: channels:read."
            ) from e
        for ch in resp.get("channels", []):
            name = ch.get("name")
            ch_id = ch.get("id")
            if name and ch_id and name in TESTENV_CHANNELS:
                name_to_id[name] = ch_id
        cursor = (resp.get("response_metadata") or {}).get("next_cursor") or None
        if not cursor:
            break

    missing = [name for name in TESTENV_CHANNELS if name not in name_to_id]
    if missing:
        log.warning(
            "missing %d expected channels — create them manually in the "
            "workspace: %s",
            len(missing),
            ", ".join(f"#{n}" for n in missing),
        )

    # Join each channel we found. Idempotent: if the bot is already a
    # member, Slack returns ok=true.
    for name, ch_id in name_to_id.items():
        try:
            client.conversations_join(channel=ch_id)
            log.info("joined #%s (%s)", name, ch_id)
        except SlackApiError as e:
            err = e.response.data.get("error", "unknown")
            if err in {"already_in_channel", "method_not_supported_for_channel_type"}:
                log.debug("already in #%s", name)
            else:
                raise RuntimeError(
                    f"conversations.join failed for #{name}: {err}. "
                    f"Required scope: channels:join."
                ) from e
        state.set_channel_id(name, ch_id)

    return name_to_id, missing
