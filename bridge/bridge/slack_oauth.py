"""Slack OAuth install + callback flow.

Model A onboarding (see CLAUDE.md / BUILD_PLAN.md): one shared Slack app
on the marketplace, OAuth into many workspaces, per-workspace bot tokens
stored in Secrets Manager.

Two responsibilities:

1. **`/slack/install`** — Build Slack's authorize URL (with our scopes,
   client ID, redirect URI, and a signed state token) and redirect the
   user there. The user is then taken through Slack's consent screen.

2. **`/slack/oauth/callback`** — Validate the state token, exchange the
   `code` query param for a bot token via Slack's `oauth.v2.access`
   endpoint, then provision the tenant: write a default tenant row,
   store the bot token in Secrets Manager, and add the workspace
   mapping. Finally, mint a **session token** and 302-redirect to the
   onboarding UI (`${ONBOARDING_BASE_URL}/onboarding/<id>/welcome?t=...`).

State vs session tokens (both HMAC-SHA256 over the same secret):

    State token   — `{nonce}.{ts}.{hmac}` (3 parts, 10-min TTL)
                    Used for the Slack consent redirect only; bound to
                    a single install click.
    Session token — `{tenant_id}.{nonce}.{ts}.{hmac}` (4 parts, 60-min TTL)
                    Bound to a specific tenant. Issued by the OAuth
                    callback, stored as an HttpOnly cookie on the Next.js
                    onboarding origin, and forwarded as `Authorization:
                    Bearer <token>` when Next.js's server calls bridge
                    `/api/tenants/*` routes. The `/api` router's
                    `require_session_token` dependency asserts the
                    embedded tenant_id matches the URL path — this is
                    our cross-tenant isolation.

The state secret comes from `BRIDGE_OAUTH_STATE_SECRET` if set, otherwise
falls back to `SLACK_SIGNING_SECRET`. Both token types share the same
secret — rotating it invalidates all in-flight installs AND all active
onboarding sessions.

Tenant IDs minted by `_tenant_id_for_workspace` are guaranteed period-free
(`slack-<team_id.lower()>`), which is what lets the session token use `.`
as a safe delimiter. `make_session_token` asserts this.

DynamoDB write paths:
  - Tenant + workspace mapping writes live in `bridge/bridge/tenant_write.py`
    (shared with the `/api/tenants` PATCH route). We re-import the two
    helpers here.
  - Secrets Manager bot-token storage is still inlined here because no
    other code path writes tokens.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets as py_secrets
import time
from urllib.parse import quote, urlencode

from fastapi.responses import RedirectResponse

from .slack_token_store import invalidate_token_cache
from .tenant_write import (
    upsert_default_tenant_row,
    upsert_workspace_mapping,
)

log = logging.getLogger(__name__)


# OAuth scopes for the shared Slack app (Model A). Match BUILD_PLAN.md
# week-2 entry. Add new scopes here as features land.
_SCOPES = ",".join([
    "app_mentions:read",
    "assistant:write",  # required for assistant.threads.setStatus (thinking indicator)
    "chat:write",
    "chat:write.customize",  # post with per-message username/icon (scripts/testenv seeder)
    "channels:history",
    "channels:read",  # required for users.conversations on public channels
    "channels:join",  # conversations.join on public channels (scripts/testenv seeder)
    "groups:history",
    "groups:read",    # required for users.conversations on private channels
    "im:history",
    "mpim:history",
    "users:read",
    "team:read",
])

# State token validity window. 10 minutes is comfortable for users
# clicking through Slack's consent screen.
_STATE_TTL_SECONDS = 600

# Session token validity window. 60 minutes is enough for a single
# onboarding sitting; if the user idles out, they re-run /slack/install.
# See CLAUDE.md gotcha #22.
_SESSION_TTL_SECONDS = 3600


# ----------------------------------------------------------------------------
# Shared HMAC secret
# ----------------------------------------------------------------------------

def _state_secret() -> str:
    """Get the secret used to sign state + session tokens.

    Prefers `BRIDGE_OAUTH_STATE_SECRET`; falls back to
    `SLACK_SIGNING_SECRET` if unset (so first-time deployments don't
    need a new env var). Raises if neither is set."""
    secret = os.getenv("BRIDGE_OAUTH_STATE_SECRET") or os.getenv("SLACK_SIGNING_SECRET")
    if not secret:
        raise RuntimeError(
            "OAuth state signing requires BRIDGE_OAUTH_STATE_SECRET "
            "(or SLACK_SIGNING_SECRET as fallback). Neither is set."
        )
    return secret


# ----------------------------------------------------------------------------
# State token: signed, no DB needed
# ----------------------------------------------------------------------------

def _sign_state(nonce: str, ts: int) -> str:
    """Compute HMAC-SHA256 over `{nonce}.{ts}` using the state secret."""
    return hmac.new(
        _state_secret().encode("utf-8"),
        f"{nonce}.{ts}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def make_state_token() -> str:
    """Generate a fresh signed state token. Format: `{nonce}.{ts}.{hmac}`."""
    nonce = py_secrets.token_hex(16)
    ts = int(time.time())
    sig = _sign_state(nonce, ts)
    return f"{nonce}.{ts}.{sig}"


def verify_state_token(token: str) -> bool:
    """Validate a state token: signature matches AND it isn't stale."""
    if not token:
        return False
    parts = token.split(".")
    if len(parts) != 3:
        return False
    nonce, ts_str, sig = parts
    try:
        ts = int(ts_str)
    except ValueError:
        return False
    if abs(time.time() - ts) > _STATE_TTL_SECONDS:
        return False
    expected = _sign_state(nonce, ts)
    return hmac.compare_digest(expected, sig)


# ----------------------------------------------------------------------------
# Session token: tenant-scoped, used by the onboarding UI
# ----------------------------------------------------------------------------

def _sign_session(tenant_id: str, nonce: str, ts: int) -> str:
    """Compute HMAC-SHA256 over `{tenant_id}.{nonce}.{ts}` using the
    state secret. Same secret as state tokens (see module docstring)."""
    return hmac.new(
        _state_secret().encode("utf-8"),
        f"{tenant_id}.{nonce}.{ts}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def make_session_token(tenant_id: str) -> str:
    """Mint a session token bound to `tenant_id`.

    Format: `{tenant_id}.{nonce}.{ts}.{hmac_hex}` — four dot-separated
    parts, hex-encoded HMAC. Asserts `tenant_id` is period-free so the
    verifier's `split(".")` parses unambiguously. Tenant IDs from the
    OAuth flow are always `slack-<team_id.lower()>` and Slack team IDs
    are alphanumeric, so this holds today.
    """
    if "." in tenant_id:
        raise ValueError(
            f"tenant_id must not contain '.' for session tokens; got {tenant_id!r}"
        )
    nonce = py_secrets.token_hex(16)
    ts = int(time.time())
    sig = _sign_session(tenant_id, nonce, ts)
    return f"{tenant_id}.{nonce}.{ts}.{sig}"


def verify_session_token(token: str) -> str | None:
    """Validate a session token and return its embedded `tenant_id`, or
    `None` if the token is invalid / expired / malformed.

    The caller (`bridge.api:require_session_token`) is responsible for
    asserting that the returned tenant_id matches the URL path.
    """
    if not token:
        return None
    parts = token.split(".")
    if len(parts) != 4:
        return None
    tenant_id, nonce, ts_str, sig = parts
    if not tenant_id:
        return None
    try:
        ts = int(ts_str)
    except ValueError:
        return None
    if abs(time.time() - ts) > _SESSION_TTL_SECONDS:
        return None
    expected = _sign_session(tenant_id, nonce, ts)
    if not hmac.compare_digest(expected, sig):
        return None
    return tenant_id


# ----------------------------------------------------------------------------
# /slack/install
# ----------------------------------------------------------------------------

def build_install_redirect() -> RedirectResponse:
    """Build a 302 redirect to Slack's `oauth/v2/authorize` endpoint.

    Required env vars:
      - SLACK_CLIENT_ID    (the shared app's client ID)
      - SLACK_REDIRECT_URI (the public URL of /slack/oauth/callback)
    """
    client_id = os.getenv("SLACK_CLIENT_ID")
    redirect_uri = os.getenv("SLACK_REDIRECT_URI")
    if not client_id or not redirect_uri:
        raise RuntimeError(
            "Slack install requires SLACK_CLIENT_ID and SLACK_REDIRECT_URI env vars."
        )

    params = {
        "client_id": client_id,
        "scope": _SCOPES,
        "redirect_uri": redirect_uri,
        "state": make_state_token(),
    }
    url = "https://slack.com/oauth/v2/authorize?" + urlencode(params)
    return RedirectResponse(url=url, status_code=302)


# ----------------------------------------------------------------------------
# /slack/oauth/callback — code exchange + tenant provisioning + redirect
# ----------------------------------------------------------------------------

def _onboarding_base_url() -> str:
    """Public base URL of the onboarding Next.js service.

    Default `http://localhost:3000` for LOCAL_DEV. Production deployments
    must set `ONBOARDING_BASE_URL` to the real onboarding origin (e.g.
    `https://onboarding.example.com`)."""
    url = os.getenv("ONBOARDING_BASE_URL", "http://localhost:3000")
    return url.rstrip("/")


def _onboarding_welcome_url(tenant_id: str, token: str) -> str:
    # `tenant_id` is path-safe (we control the format) but `token` is
    # hex/dot only — still percent-encode both as a belt-and-suspenders
    # measure.
    return (
        f"{_onboarding_base_url()}/onboarding/{quote(tenant_id, safe='')}"
        f"/welcome?t={quote(token, safe='')}"
    )


def _onboarding_error_url(reason: str) -> str:
    return f"{_onboarding_base_url()}/onboarding/error?reason={quote(reason, safe='')}"


def _tenant_id_for_workspace(team_id: str) -> str:
    """Mint a tenant_id from a Slack team_id.

    Convention: `slack-<team_id.lower()>`. Stable (re-installing the
    same workspace produces the same tenant_id, so config persists),
    debuggable (easy to identify the source workspace from the tenant_id),
    and namespaced so a future Discord adapter can use a different prefix.
    """
    return f"slack-{team_id.lower()}"


def _store_bot_token(tenant_id: str, bot_token: str, region: str) -> None:
    """Store the per-tenant bot token in Secrets Manager.

    Path: `agentcore/tenants/<tenant_id>/slack/bot_token`. Tries
    CreateSecret first; on ResourceExistsException (re-install of an
    existing workspace), falls back to PutSecretValue.
    """
    import boto3
    from botocore.exceptions import ClientError

    client = boto3.client("secretsmanager", region_name=region)
    secret_id = f"agentcore/tenants/{tenant_id}/slack/bot_token"
    try:
        client.create_secret(
            Name=secret_id,
            SecretString=bot_token,
            Description=f"Slack bot token for tenant {tenant_id} (Model A OAuth install)",
        )
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code")
        if code == "ResourceExistsException":
            client.put_secret_value(SecretId=secret_id, SecretString=bot_token)
        else:
            raise


async def handle_oauth_callback(code: str, state: str) -> RedirectResponse:
    """Exchange the OAuth `code` for a bot token, provision the tenant,
    and 302-redirect to the onboarding UI welcome page.

    Success → `{ONBOARDING_BASE_URL}/onboarding/{tenant_id}/welcome?t=<session_token>`
    Any error → `{ONBOARDING_BASE_URL}/onboarding/error?reason=<slug>`

    Errors are logged internally and surface as a human-readable slug —
    we deliberately don't echo exception text to the browser.
    """
    if not verify_state_token(state):
        log.warning("oauth_callback: invalid or expired state token")
        return RedirectResponse(_onboarding_error_url("invalid_state"), status_code=302)

    client_id = os.getenv("SLACK_CLIENT_ID")
    client_secret = os.getenv("SLACK_CLIENT_SECRET")
    redirect_uri = os.getenv("SLACK_REDIRECT_URI")
    if not client_id or not client_secret or not redirect_uri:
        log.error(
            "oauth_callback: missing SLACK_CLIENT_ID/SLACK_CLIENT_SECRET/SLACK_REDIRECT_URI"
        )
        return RedirectResponse(_onboarding_error_url("not_configured"), status_code=302)

    # Exchange code → tokens. slack-sdk's AsyncWebClient handles this.
    try:
        from slack_sdk.web.async_client import AsyncWebClient

        slack_client = AsyncWebClient()
        oauth_response = await slack_client.oauth_v2_access(
            client_id=client_id,
            client_secret=client_secret,
            code=code,
            redirect_uri=redirect_uri,
        )
    except Exception as e:  # noqa: BLE001
        log.exception("oauth_callback: code exchange failed: %s", e)
        return RedirectResponse(_onboarding_error_url("exchange_failed"), status_code=302)

    if not oauth_response.get("ok"):
        log.warning("oauth_callback: oauth.v2.access returned not-ok: %s", oauth_response.data)
        return RedirectResponse(_onboarding_error_url("exchange_failed"), status_code=302)

    team = oauth_response.get("team") or {}
    team_id = team.get("id")
    bot_token = oauth_response.get("access_token")
    if not team_id or not bot_token:
        log.warning("oauth_callback: response missing team.id or access_token")
        return RedirectResponse(_onboarding_error_url("missing_fields"), status_code=302)

    tenant_id = _tenant_id_for_workspace(team_id)
    region = os.getenv("AWS_REGION", "us-west-2")

    try:
        upsert_default_tenant_row(tenant_id, region)
        _store_bot_token(tenant_id, bot_token, region)
        upsert_workspace_mapping(team_id, tenant_id, region)
        invalidate_token_cache(tenant_id)
    except Exception as e:  # noqa: BLE001
        log.exception(
            "oauth_callback: tenant provisioning failed for tenant_id=%s: %s",
            tenant_id,
            e,
        )
        return RedirectResponse(_onboarding_error_url("provisioning_failed"), status_code=302)

    log.info("oauth_callback: provisioned tenant_id=%s for team_id=%s", tenant_id, team_id)
    session_token = make_session_token(tenant_id)
    return RedirectResponse(
        _onboarding_welcome_url(tenant_id, session_token),
        status_code=302,
    )
