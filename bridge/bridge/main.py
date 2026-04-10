"""Bridge FastAPI app: routes for each client adapter.

Routes:
  - POST /slack/events         — Slack Events API; ack within 3s, dispatch async
  - GET  /slack/install        — start the OAuth install flow
  - GET  /slack/oauth/callback — OAuth code exchange + tenant provisioning
  - POST /debug/message        — synchronous local debug (LOCAL_DEV=1 only)
  - GET  /healthz              — liveness probe

`/debug/message` is conditionally registered ONLY when `LOCAL_DEV=1`. The
production bridge has no debug route at all — zero attack surface.
"""
from __future__ import annotations

import json
import logging
import os
import time

# Configure root logger so our app-level log.warning/info calls
# actually appear in container stdout (uvicorn only configures its own).
logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s:%(name)s: %(message)s",
)

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request

from .adapters.debug import DebugAdapter
from .adapters.slack import SlackAdapter, SlackSignatureError
from .api import api_router
from .async_dispatcher import dispatch_async
from .client import AgentCoreClient
from .dedup import is_duplicate
from .gateway_jwt import get_jwks, get_oidc_configuration
from .slack_oauth import build_install_redirect, handle_oauth_callback
from .tenant_resolver import resolve_tenant_id
from .tenant_write import get_tenant_row

log = logging.getLogger(__name__)

# Slack app ID for self-message filtering. Set via env to avoid the bot
# processing its own messages in channels where it's a member.
_SLACK_APP_ID = os.getenv("SLACK_APP_ID", "")

LOCAL_DEV = os.getenv("LOCAL_DEV") == "1"

# No CORS middleware — all /api/tenants/* callers are server-side Next.js
# code in the onboarding service. The browser never talks to these routes
# directly. Add FastAPI CORSMiddleware here if/when a client-side caller
# (admin dashboard, etc.) needs direct access.
app = FastAPI(title="coreAgent bridge")

app.include_router(api_router)

slack = SlackAdapter(
    signing_secret=os.getenv("SLACK_SIGNING_SECRET"),
    bot_token=os.getenv("SLACK_BOT_TOKEN"),
)

client = AgentCoreClient(
    runtime_arn=os.getenv("AGENT_RUNTIME_ARN"),
    local_agent_url=os.getenv("LOCAL_AGENT_URL"),
)


# ---------------------------------------------------------------------------
# Bot policy helpers
# ---------------------------------------------------------------------------

_bot_policy_cache: dict[str, tuple[float, dict]] = {}
_BOT_POLICY_TTL = 60.0  # seconds


def _get_bot_policy(tenant_id: str) -> dict:
    """Return the bot_policy sub-dict from the tenant config, cached for 60s."""
    now = time.monotonic()
    cached = _bot_policy_cache.get(tenant_id)
    if cached and now - cached[0] < _BOT_POLICY_TTL:
        return cached[1]
    try:
        region = os.getenv("AWS_REGION", "us-west-2")
        config = get_tenant_row(tenant_id, region)
        policy = config.get("bot_policy", {})
    except KeyError:
        policy = {}
    _bot_policy_cache[tenant_id] = (now, policy)
    return policy


def _bot_allowed(policy: dict, bot_id: str, channel_id: str | None) -> bool:
    """Evaluate the four-tier bot policy. Returns True if the bot is allowed.

    Tier 0 (``allow_all_bots``) is the "magical default" for new tenants:
    any bot can trigger the agent without explicit configuration, so
    PagerDuty/Datadog alerts get auto-triaged. Missing-key defaults to
    ``False`` so existing tenants that pre-date this field keep the
    conservative posture until they explicitly opt in (their DDB row
    simply doesn't have the key).
    """
    # Tier 0: fully open — any bot allowed (new-tenant default)
    if policy.get("allow_all_bots", False):
        return True
    # Tier 1: explicitly trusted bots
    if bot_id in policy.get("trusted_bot_ids", []):
        return True
    # Tier 2: open channels where any bot can trigger
    if channel_id and channel_id in policy.get("open_channels", []):
        return True
    # Tier 3: default — block
    return False


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


# OIDC discovery + JWKS endpoints for AgentCore Gateway's CUSTOM_JWT
# authorizer. The Gateway is configured with `discoveryUrl` pointing at
# `<bridge>/.well-known/openid-configuration`; it then resolves the JWKS
# URL from there and uses it to verify per-invocation JWTs minted by
# `bridge/bridge/gateway_jwt.py`. Both routes are unauthenticated by
# design — discovery + JWKS are public per OIDC spec.
@app.get("/.well-known/openid-configuration")
async def oidc_configuration() -> dict[str, object]:
    return get_oidc_configuration()


@app.get("/jwks.json")
async def jwks() -> dict[str, object]:
    return get_jwks()


@app.post("/slack/events")
async def slack_events(request: Request, background: BackgroundTasks):
    """Slack Events API webhook. Must ack within 3 seconds.

    Order of operations (matters for security AND for cost control):
      1. HMAC verification — reject unsigned/forged requests with 401
      2. JSON parse the body
      3. URL verification handshake — return the challenge token
      4. Retry dedup — drop Slack retries before they hit Bedrock
      5. Parse into InboundMessage + dispatch to background task
      6. 200 OK (ack within Slack's 3-second window)

    Long agent work happens in the BackgroundTask, NOT inline.
    """
    # 1. HMAC verification. Skipped silently in LOCAL_DEV when
    #    SLACK_SIGNING_SECRET is unset; logged at WARNING.
    try:
        await slack.verify_signature(request)
    except SlackSignatureError as e:
        log.warning("slack_events: signature verification failed: %s", e)
        raise HTTPException(status_code=401, detail="invalid Slack signature")

    # 2. Parse JSON from raw body bytes (cached by Starlette).
    raw_body = await request.body()
    body = json.loads(raw_body) if raw_body else {}

    # 3. URL verification handshake.
    if body.get("type") == "url_verification":
        return {"challenge": body["challenge"]}

    # 4. Slack retry dedup. Must happen BEFORE dispatch_async or any
    #    agent invocation. event_id is provided by Slack on every
    #    Events API delivery; if missing, we let it through.
    event_id = body.get("event_id")
    if event_id and is_duplicate(event_id):
        log.info("slack_events: dropping duplicate event_id=%s", event_id)
        return {"ok": True}

    # 5. Parse into InboundMessage + resolve tenant + dispatch.
    inbound = await slack.parse(request)
    try:
        tenant_id = resolve_tenant_id(inbound.workspace_id)
    except KeyError:
        # Unknown workspace (e.g. uninstalled, or never OAuth'd).
        # Return 200 OK with no dispatch so Slack stops retrying.
        # We do NOT post a "please install" message because we don't
        # have a bot token for this workspace.
        log.info(
            "slack_events: no tenant mapping for workspace_id=%s; ack and drop",
            inbound.workspace_id,
        )
        return {"ok": True}

    # 5b. Bot policy filtering — must happen before dispatch to save Bedrock
    #     spend on bot loops. Also filter our own app's messages.
    bot_id = inbound.metadata.get("bot_id")
    if bot_id:
        app_id = inbound.metadata.get("app_id")
        if _SLACK_APP_ID and app_id == _SLACK_APP_ID:
            log.debug("slack_events: dropping self-message app_id=%s", app_id)
            return {"ok": True}
        policy = _get_bot_policy(tenant_id)
        if not _bot_allowed(policy, bot_id, inbound.channel_id):
            log.info(
                "slack_events: bot_id=%s blocked by policy for tenant=%s channel=%s",
                bot_id, tenant_id, inbound.channel_id,
            )
            return {"ok": True}

    background.add_task(dispatch_async, slack, inbound, client, tenant_id)
    return await slack.ack(request)


@app.get("/slack/install")
async def slack_install():
    """Start the Slack OAuth install flow.

    Generates a signed state token and redirects the user to Slack's
    consent screen. The callback (below) finishes the install.
    """
    try:
        return build_install_redirect()
    except RuntimeError as e:
        log.error("slack_install: %s", e)
        raise HTTPException(status_code=500, detail="Slack install not configured")


@app.get("/slack/oauth/callback")
async def slack_oauth_callback(code: str = "", state: str = ""):
    """OAuth callback: exchange `code` for a bot token, provision the
    tenant, and return a placeholder onboarding page."""
    return await handle_oauth_callback(code=code, state=state)


# `/debug/message` is registered ONLY in LOCAL_DEV. Production builds
# don't expose it — keeps the public surface to /healthz and /slack/*.
if LOCAL_DEV:
    debug = DebugAdapter()

    @app.post("/debug/message")
    async def debug_message(request: Request) -> dict[str, str]:
        """Synchronous debug endpoint: invoke the agent and return its
        reply directly. LOCAL_DEV-only — no Slack creds needed."""
        inbound = await debug.parse(request)
        tenant_id = resolve_tenant_id(inbound.workspace_id)
        result = await client.invoke(
            tenant_id=tenant_id,
            prompt=inbound.text,
            ctx={
                "user_id": inbound.user_id,
                "channel_id": inbound.channel_id,
                "thread_id": inbound.thread_id,
                "workspace_id": inbound.workspace_id,
            },
        )
        return {"tenant_id": tenant_id, "text": result}
