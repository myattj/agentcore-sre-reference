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
   mapping. Finally, return a tiny placeholder HTML page (the real
   onboarding UI lands in week 3).

State token format:
    "{nonce}.{ts}.{hmac_hex}"
where nonce is 16 random hex chars, ts is the issue time (epoch seconds),
and hmac_hex is HMAC-SHA256(state_secret, f"{nonce}.{ts}").

The state secret comes from `BRIDGE_OAUTH_STATE_SECRET` if set, otherwise
falls back to `SLACK_SIGNING_SECRET` (which the bridge already needs for
inbound HMAC). State tokens expire after 10 minutes.

DynamoDB write paths:
  - `tenants` row: same UpdateExpression as
    `coreAgent.tenant.DynamoTenantStore.upsert` — kept in sync by
    convention. **Edit the row shape in coreAgent/tenant.py first**, then
    mirror here. (The two packages have separate venvs, so we can't import.)
  - `workspace_to_tenant` row: same UpdateExpression as
    `infra/data/scripts/seed_tenants.py:put_workspace`.

Secrets Manager:
  - Path: `agentcore/tenants/<tenant_id>/slack/bot_token`
  - Try CreateSecret; fall back to PutSecretValue on ResourceExistsException.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets as py_secrets
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

from fastapi.responses import HTMLResponse, RedirectResponse

from .slack_token_store import invalidate_token_cache

log = logging.getLogger(__name__)


# OAuth scopes for the shared Slack app (Model A). Match BUILD_PLAN.md
# week-2 entry. Add new scopes here as features land.
_SCOPES = ",".join([
    "app_mentions:read",
    "chat:write",
    "channels:history",
    "groups:history",
    "im:history",
    "mpim:history",
    "users:read",
    "team:read",
])

# State token validity window. 10 minutes is comfortable for users
# clicking through Slack's consent screen.
_STATE_TTL_SECONDS = 600


# ----------------------------------------------------------------------------
# State token: signed, no DB needed
# ----------------------------------------------------------------------------

def _state_secret() -> str:
    """Get the secret used to sign OAuth state tokens.

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
# /slack/oauth/callback — code exchange + tenant provisioning
# ----------------------------------------------------------------------------

def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _tenant_id_for_workspace(team_id: str) -> str:
    """Mint a tenant_id from a Slack team_id.

    Convention: `slack-<team_id.lower()>`. Stable (re-installing the
    same workspace produces the same tenant_id, so config persists),
    debuggable (easy to identify the source workspace from the tenant_id),
    and namespaced so a future Discord adapter can use a different prefix.
    """
    return f"slack-{team_id.lower()}"


def _build_default_config_dict(tenant_id: str) -> dict[str, Any]:
    """Build the default tenant config dict.

    **Keep in sync with `coreAgent.tenant.build_default_config()`.**
    The two packages have separate venvs so we can't import; this is the
    minimal duplication required to provision a new tenant from the
    bridge. If you change the agent's default config shape, mirror it
    here.
    """
    return {
        "tenant_id": tenant_id,
        "model_id": "global.anthropic.claude-sonnet-4-6",
        # MUST be non-empty — Bedrock Converse rejects empty system blocks
        # (`system[0].text min length: 1`). Mirror of the same default in
        # coreAgent.tenant.build_default_config().
        "system_prompt": "You are a helpful assistant.",
        "catalog": {
            "allowed_tools": ["echo"],
            "tool_config": {},
        },
        "byo": {
            "enabled": False,
            "gateway_endpoint": None,
            "gateway_auth": None,
        },
        "memory": {
            "triggers": {
                "message_count": 6,
                "token_count": 1000,
                "idle_timeout_seconds": 1800,
            },
            "namespace": f"tenants/{tenant_id}",
            "extraction": {
                "enabled": True,
                "rules": ["user_preferences", "facts"],
            },
        },
        "heartbeat": {
            "busy_threshold": 1,
            "max_background_seconds": 3600,
        },
    }


def _upsert_tenant_row(tenant_id: str, region: str) -> None:
    """Write the default tenant row to DynamoDB.

    UpdateExpression matches `coreAgent.tenant.DynamoTenantStore.upsert`
    and `infra/data/scripts/seed_tenants.py:put_tenant`. Idempotent:
    re-running for the same tenant_id refreshes `updated_at` but
    preserves `created_at`."""
    import boto3

    table_name = os.getenv("TENANTS_TABLE", "tenants")
    dynamodb = boto3.resource("dynamodb", region_name=region)
    table = dynamodb.Table(table_name)
    now = _iso_now()
    table.update_item(
        Key={"tenant_id": tenant_id},
        UpdateExpression=(
            "SET #config = :config, "
            "updated_at = :now, "
            "created_at = if_not_exists(created_at, :now)"
        ),
        ExpressionAttributeNames={"#config": "config"},
        ExpressionAttributeValues={
            ":config": _build_default_config_dict(tenant_id),
            ":now": now,
        },
    )


def _upsert_workspace_mapping(workspace_id: str, tenant_id: str, region: str) -> None:
    """Write the workspace_id → tenant_id mapping. Same idempotent
    semantics as `_upsert_tenant_row`."""
    import boto3

    table_name = os.getenv("WORKSPACE_TO_TENANT_TABLE", "workspace_to_tenant")
    dynamodb = boto3.resource("dynamodb", region_name=region)
    table = dynamodb.Table(table_name)
    now = _iso_now()
    table.update_item(
        Key={"workspace_id": workspace_id},
        UpdateExpression=(
            "SET tenant_id = :tid, "
            "updated_at = :now, "
            "created_at = if_not_exists(created_at, :now)"
        ),
        ExpressionAttributeValues={":tid": tenant_id, ":now": now},
    )


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


async def handle_oauth_callback(code: str, state: str) -> HTMLResponse:
    """Exchange the OAuth `code` for a bot token, provision the tenant,
    and return a placeholder success page.

    Returns an HTMLResponse so the user lands somewhere reasonable in
    their browser. The real onboarding UI in week 3 will replace this
    placeholder.

    Errors are logged internally and surface as a generic error page —
    we deliberately don't echo exception text to the browser.
    """
    if not verify_state_token(state):
        log.warning("oauth_callback: invalid or expired state token")
        return _error_page("Invalid or expired install link. Please try again.")

    client_id = os.getenv("SLACK_CLIENT_ID")
    client_secret = os.getenv("SLACK_CLIENT_SECRET")
    redirect_uri = os.getenv("SLACK_REDIRECT_URI")
    if not client_id or not client_secret or not redirect_uri:
        log.error(
            "oauth_callback: missing SLACK_CLIENT_ID/SLACK_CLIENT_SECRET/SLACK_REDIRECT_URI"
        )
        return _error_page("Server is not configured for Slack install.")

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
        return _error_page("Slack install failed. Please try again.")

    if not oauth_response.get("ok"):
        log.warning("oauth_callback: oauth.v2.access returned not-ok: %s", oauth_response.data)
        return _error_page("Slack install failed.")

    team = oauth_response.get("team") or {}
    team_id = team.get("id")
    bot_token = oauth_response.get("access_token")
    if not team_id or not bot_token:
        log.warning("oauth_callback: response missing team.id or access_token")
        return _error_page("Slack install returned an unexpected response.")

    tenant_id = _tenant_id_for_workspace(team_id)
    region = os.getenv("AWS_REGION", "us-west-2")

    try:
        _upsert_tenant_row(tenant_id, region)
        _store_bot_token(tenant_id, bot_token, region)
        _upsert_workspace_mapping(team_id, tenant_id, region)
        invalidate_token_cache(tenant_id)
    except Exception as e:  # noqa: BLE001
        log.exception(
            "oauth_callback: tenant provisioning failed for tenant_id=%s: %s",
            tenant_id,
            e,
        )
        return _error_page("Could not finish install. Please contact support.")

    log.info("oauth_callback: provisioned tenant_id=%s for team_id=%s", tenant_id, team_id)
    return _success_page(team.get("name") or team_id)


def _success_page(team_name: str) -> HTMLResponse:
    body = f"""<!doctype html>
<html><head><title>Installed</title></head>
<body style="font-family: system-ui; max-width: 600px; margin: 4em auto; padding: 0 1em;">
  <h1>Installed in {team_name}</h1>
  <p>The bot is ready. Mention it in any channel and it will reply.</p>
  <p><em>This page is a placeholder. The full onboarding UI lands in week 3.</em></p>
</body></html>"""
    return HTMLResponse(content=body, status_code=200)


def _error_page(message: str) -> HTMLResponse:
    body = f"""<!doctype html>
<html><head><title>Install failed</title></head>
<body style="font-family: system-ui; max-width: 600px; margin: 4em auto; padding: 0 1em;">
  <h1>Install failed</h1>
  <p>{message}</p>
</body></html>"""
    return HTMLResponse(content=body, status_code=400)
