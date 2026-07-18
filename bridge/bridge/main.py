"""Bridge FastAPI app: routes for each client adapter.

Routes:
  - POST /slack/events         — Slack Events API; ack within 3s, dispatch async
  - POST /slack/interactions   — Slack Interactivity API (Block Kit button clicks)
  - GET  /slack/install        — start the OAuth install flow
  - GET  /slack/oauth/callback — OAuth code exchange + tenant provisioning
  - POST /debug/message        — synchronous local debug (LOCAL_DEV=1 only)
  - GET  /healthz              — liveness probe

`/debug/message` is conditionally registered ONLY when `LOCAL_DEV=1`. The
production bridge has no debug route at all — zero attack surface.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response

from .adapters.debug import DebugAdapter
from .adapters.slack import SlackAdapter, SlackSignatureError
from .api import api_router
from .async_dispatcher import dispatch_async
from .client import AgentCoreClient
from .dedup import is_duplicate
from .reaction_feedback import classify_reaction, dispatch_reaction_feedback
from .rate_limit import TokenBucketRateLimiter
from .gateway_jwt import get_jwks, get_oidc_configuration
from .sandbox_callback import handle_sandbox_complete, verify_callback_auth
from .sandbox_progress import handle_sandbox_progress
from .slack_interactions import (
    build_codebase_pick_synthetic_message,
    extract_payload_json,
    is_codebase_pick_action,
    parse_interactivity_payload,
    post_response_url_update,
)
from .slack_oauth import build_install_redirect, handle_oauth_callback
from .tenant_resolver import resolve_tenant_id
from .tenant_write import get_tenant_row

# Configure root logger so our app-level log.warning/info calls
# actually appear in container stdout (uvicorn only configures its own).
logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s:%(name)s: %(message)s",
)

log = logging.getLogger(__name__)

# Slack app ID for self-message filtering. Set via env to avoid the bot
# processing its own messages in channels where it's a member.
_SLACK_APP_ID = os.getenv("SLACK_APP_ID", "")

LOCAL_DEV = os.getenv("LOCAL_DEV") == "1"


def _bounded_int_env(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, "")
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        log.warning("Ignoring invalid %s=%r; using %d", name, raw, default)
        return default
    if not minimum <= value <= maximum:
        log.warning("Ignoring out-of-range %s=%r; using %d", name, raw, default)
        return default
    return value


_DASHBOARD_RATE_LIMIT = TokenBucketRateLimiter(
    capacity=_bounded_int_env(
        "DASHBOARD_READS_PER_MINUTE",
        60,
        minimum=1,
        maximum=10_000,
    )
)
_DASHBOARD_READ_SLOTS = threading.BoundedSemaphore(
    _bounded_int_env("DASHBOARD_MAX_CONCURRENT_READS", 16, minimum=1, maximum=256)
)

# No CORS middleware — all /api/tenants/* callers are server-side Next.js
# code in the onboarding service. The browser never talks to these routes
# directly. Add FastAPI CORSMiddleware here if/when a client-side caller
# (admin dashboard, etc.) needs direct access.
app = FastAPI(title="Agent bridge")

app.include_router(api_router)

slack = SlackAdapter(
    signing_secret=os.getenv("SLACK_SIGNING_SECRET"),
    bot_token=os.getenv("SLACK_BOT_TOKEN"),
    allow_unsigned_requests=LOCAL_DEV,
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

    Tier 0 (``allow_all_bots``) is an explicit high-trust opt-in. Missing-key
    defaults to ``False`` so new and legacy tenants remain humans-only until
    an operator trusts a bot or opens a channel.
    """
    # Tier 0: fully open — any bot allowed (explicit opt-in)
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


# ---------------------------------------------------------------------------
# PR sandbox -> bridge callbacks
# ---------------------------------------------------------------------------
#
# The Fargate sandbox container POSTs here when its propose_pr work
# finishes (success or error). Auth is a Bearer-token shared secret —
# SANDBOX_CALLBACK_SECRET is read from env (injected by services-stack.ts
# from the agentcore/services/sandbox Secrets Manager secret) and the
# sandbox uses the same value for its outbound POST.
#
# The actual orchestration (DDB read + Slack post) lives in
# bridge/sandbox_callback.py so it can be unit-tested without spinning
# up FastAPI. This handler is a thin auth + JSON shim.
#
# ALB routing: /internal/* is added to the bridge target group's
# path-pattern listener rule in services-stack.ts.
@app.post("/internal/sandbox_complete")
async def sandbox_complete(request: Request) -> dict[str, object]:
    """Receive a completion notification from a Fargate sandbox task."""
    auth = request.headers.get("Authorization", "")
    if not verify_callback_auth(auth):
        log.warning("sandbox_complete: rejected request with bad auth")
        raise HTTPException(status_code=401, detail="invalid callback auth")
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload must be a JSON object")
    return await handle_sandbox_complete(payload)


@app.post("/internal/sandbox_progress")
async def sandbox_progress(request: Request) -> dict[str, object]:
    """Receive a progress update from a Fargate sandbox task."""
    auth = request.headers.get("Authorization", "")
    if not verify_callback_auth(auth):
        log.warning("sandbox_progress: rejected request with bad auth")
        raise HTTPException(status_code=401, detail="invalid callback auth")
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload must be a JSON object")
    return await handle_sandbox_progress(payload)


# ---------------------------------------------------------------------------
# Ephemeral dashboards — serve specs to the onboarding service
# ---------------------------------------------------------------------------
#
# Unauthenticated by design — the token IS the access control (UUID,
# unguessable). The onboarding Next.js app calls this server-side from
# its /d/[token] route, which renders the dashboard with Recharts.
@app.get("/internal/dashboard")
async def get_dashboard(request: Request, response: Response) -> dict[str, object]:
    """Return a dashboard spec using a bearer token kept out of access-log URLs."""
    from .dashboard_store import (
        DashboardStoreError,
        get_dashboard_spec,
        is_valid_dashboard_token,
    )

    token = request.headers.get("x-dashboard-token", "")
    no_store_headers = {"Cache-Control": "no-store", "X-Robots-Tag": "noindex"}
    if not is_valid_dashboard_token(token):
        raise HTTPException(
            status_code=404,
            detail="dashboard not found or expired",
            headers=no_store_headers,
        )
    # In the reference topology, the trusted ALB appends the real peer address
    # to X-Forwarded-For. Reading the rightmost value prevents a caller-supplied
    # prefix from choosing a fresh bucket. Other deployments use the socket peer
    # unless they explicitly opt into the same trusted-proxy contract.
    forwarded_for = (
        request.headers.get("x-forwarded-for", "")
        if os.getenv("DASHBOARD_TRUST_X_FORWARDED_FOR") == "1"
        else ""
    )
    source = forwarded_for.rsplit(",", 1)[-1].strip()
    if not source:
        source = request.client.host if request.client else "unknown"
    retry_after = _DASHBOARD_RATE_LIMIT.retry_after(source)
    if retry_after is not None:
        raise HTTPException(
            status_code=429,
            detail="dashboard request rate exceeded",
            headers={**no_store_headers, "Retry-After": str(retry_after)},
        )
    if not _DASHBOARD_READ_SLOTS.acquire(blocking=False):
        raise HTTPException(
            status_code=429,
            detail="dashboard service is busy",
            headers={**no_store_headers, "Retry-After": "1"},
        )
    try:
        # boto3 is synchronous. Keep its network I/O off the FastAPI event loop.
        try:
            spec = await asyncio.to_thread(get_dashboard_spec, token)
        finally:
            _DASHBOARD_READ_SLOTS.release()
    except DashboardStoreError as exc:
        log.error("get_dashboard: dashboard store unavailable: %s", exc)
        raise HTTPException(
            status_code=503,
            detail="dashboard service temporarily unavailable",
            headers={**no_store_headers, "Retry-After": "5"},
        ) from exc
    if spec is None:
        raise HTTPException(
            status_code=404,
            detail="dashboard not found or expired",
            headers=no_store_headers,
        )
    response.headers.update(no_store_headers)
    response.headers["Referrer-Policy"] = "no-referrer"
    return spec


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

    # 5. Reaction feedback: detect reaction_added events before normal
    #    message parsing. These go through a separate path that fetches
    #    the bot message from Slack and invokes the agent with a feedback
    #    payload (no LLM call, no Slack reply).
    event = body.get("event", {})
    if event.get("type") == "reaction_added":
        reaction = event.get("reaction", "")
        if classify_reaction(reaction) is None:
            # Not a feedback-signal emoji — drop silently.
            return {"ok": True}
        workspace_id = body.get("team_id", "")
        try:
            tenant_id = resolve_tenant_id(workspace_id)
        except KeyError:
            return {"ok": True}
        # Pass the full event + workspace_id to the background handler.
        # The handler fetches the message from Slack and invokes the agent.
        event_with_team = dict(event)
        event_with_team["team_id"] = workspace_id
        event_with_team["api_app_id"] = body.get("api_app_id", "")
        background.add_task(
            dispatch_reaction_feedback, slack, event_with_team, client,
            tenant_id, _SLACK_APP_ID,
        )
        return {"ok": True}

    # 6. Parse into InboundMessage + resolve tenant + dispatch.
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

    # 6b. Bot policy filtering — must happen before dispatch to save Bedrock
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


@app.post("/slack/interactions")
async def slack_interactions(request: Request, background: BackgroundTasks):
    """Slack Interactivity API webhook.

    Receives Block Kit action payloads (button clicks, select menus).
    Today we only handle the ``codebase_pick`` action fired by the
    agent's ``ask_codebase_choice`` tool.

    Same 3-second ack contract as /slack/events — we verify, parse,
    dispatch to a background task, then ack immediately.

    Order of operations:
      1. HMAC verification — same ``v0=`` scheme as Events API
      2. Extract the JSON ``payload`` field from the form-urlencoded body
      3. Parse into our ``InteractivityPayload`` shape
      4. Dispatch by action_id (currently only codebase_pick)
      5. 200 OK (ack within Slack's 3-second window)

    On any parse/validation failure we still return 200 OK — Slack
    retries interactivity on non-2xx responses and a malformed
    payload shouldn't block the user's next click.
    """
    # 1. HMAC verification. Slack interactivity uses the exact same
    # v0=hmac(secret, "v0:ts:body") scheme as /slack/events, so the
    # adapter's verify_signature works unchanged.
    try:
        await slack.verify_signature(request)
    except SlackSignatureError as e:
        log.warning("slack_interactions: signature verification failed: %s", e)
        raise HTTPException(status_code=401, detail="invalid Slack signature")

    # 2. Body is form-urlencoded with a single `payload` field. Read
    # the raw bytes and parse — don't use request.form() because
    # signature verification already consumed request.body() (Starlette
    # caches but we want to be explicit about which bytes we're reading).
    raw_body = await request.body()
    payload_dict = extract_payload_json(raw_body)
    if payload_dict is None:
        log.warning(
            "slack_interactions: could not extract payload JSON from body"
        )
        return {"ok": True}

    # 3. Parse into our narrow shape.
    parsed = parse_interactivity_payload(payload_dict)
    if parsed is None:
        log.info("slack_interactions: unsupported or malformed payload type")
        return {"ok": True}

    # 4. Dispatch by action_id. Add an action_id → handler map here when
    # we support more than one interactivity action.
    if is_codebase_pick_action(parsed.action_id):
        # Resolve the tenant from the team_id — 200 OK and drop if the
        # workspace is unknown (same pattern as /slack/events).
        try:
            tenant_id = resolve_tenant_id(parsed.team_id)
        except KeyError:
            log.info(
                "slack_interactions: no tenant mapping for team_id=%s; "
                "ack and drop",
                parsed.team_id,
            )
            return {"ok": True}

        picked_repo = parsed.action_value
        log.info(
            "slack_interactions: codebase_pick tenant=%s channel=%s "
            "repo=%s user=%s",
            tenant_id,
            parsed.channel_id,
            picked_repo,
            parsed.user_id,
        )

        # 4a. Best-effort: replace the original button message with a
        # confirmation. Runs inline because it's fast and fire-and-forget
        # (function swallows all errors internally).
        post_response_url_update(parsed.response_url, picked_repo)

        # 4b. Build a synthetic InboundMessage and dispatch to the agent
        # via the same path /slack/events uses. The agent sees a normal
        # user turn and responds per the SHORTLIST prompt block's
        # acknowledgment coaching.
        synthetic = build_codebase_pick_synthetic_message(parsed)
        background.add_task(dispatch_async, slack, synthetic, client, tenant_id)
        return {"ok": True}

    # Unrecognized action_id — log and ignore. When we add more
    # interactivity actions, replace this with a dispatch map.
    log.info(
        "slack_interactions: unknown action_id=%s, ignoring",
        parsed.action_id,
    )
    return {"ok": True}


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
async def slack_oauth_callback(request: Request, code: str = "", state: str = ""):
    """Exchange `code` for a bot token, provision the tenant, and
    redirect into the onboarding UI."""
    return await handle_oauth_callback(
        code=code,
        state=state,
        browser_nonce=request.cookies.get("slack_oauth_state", ""),
    )


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
