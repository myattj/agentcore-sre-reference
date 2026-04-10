"""Slack channel listing helper.

Used by `GET /api/tenants/{tenant_id}/channels` to render the channels
page in the onboarding UI.

**Why `users.conversations` and not `conversations.list`?** Both methods
require `channels:read` (public) and `groups:read` (private), so the
scope cost is identical. `users.conversations` is preferable because it
returns only the channels the **bot** is a member of, which matches the
onboarding UX ("show me where I've added the bot") and gives a smaller
result set. `conversations.list` would return every public channel in
the workspace, most of which the bot can't actually post to.

Bot scopes required (manifest at `bridge/slack_manifest.json`):
  - users:read
  - channels:read   (for public channels)
  - groups:read     (for private channels)

If a tenant installed the app under an older scope set, this method
will fail with `missing_scope` and the route handler degrades to an
empty list with a `needs_reinstall=True` flag. The onboarding UI shows
a "re-install to grant new scopes" hint in that case.
"""
from __future__ import annotations

import logging
from typing import Any

from .api_models import ChannelInfo
from .slack_token_store import get_bot_token

log = logging.getLogger(__name__)


async def list_channels_for_tenant(tenant_id: str) -> list[ChannelInfo]:
    """Return public + private channels the bot is a member of.

    Raises:
      - `KeyError` if no bot token is configured for this tenant
        (the `/api` route translates to 404)
      - `slack_sdk.errors.SlackApiError` on Slack API failures
        (the route translates to 502)
    """
    token = get_bot_token(tenant_id)
    if not token:
        raise KeyError(f"No Slack bot token configured for tenant {tenant_id!r}")

    # Import lazily so tests don't pay the aiohttp import cost.
    from slack_sdk.web.async_client import AsyncWebClient

    client = AsyncWebClient(token=token)
    response = await client.users_conversations(
        types="public_channel,private_channel",
        exclude_archived=True,
        limit=200,
    )
    raw_channels: list[dict[str, Any]] = list(response.get("channels") or [])

    out: list[ChannelInfo] = []
    for ch in raw_channels:
        cid = ch.get("id")
        name = ch.get("name") or ch.get("name_normalized") or ""
        if not cid:
            continue
        out.append(
            ChannelInfo(
                id=str(cid),
                name=str(name),
                is_private=bool(ch.get("is_private", False)),
            )
        )
    log.info(
        "list_channels_for_tenant: tenant_id=%s returned %d channels",
        tenant_id,
        len(out),
    )
    return out
