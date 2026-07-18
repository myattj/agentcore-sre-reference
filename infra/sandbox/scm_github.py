"""GitHub App installation-token minting.

============================================================================
MIRROR of coreAgent/app/coreAgent/scm_github.py — KEEP IN SYNC.

The agent, the bridge, and the sandbox container each run in a separate
venv (gotcha #13: bridge/agent/sandbox can't cross-import). The bridge
has its own copy at bridge/bridge/github_app.py; this file is the
sandbox's copy. When you change the source-of-truth file in coreAgent,
update both mirrors in the same commit and re-deploy.

The Dockerfile copies this file into the sandbox image, so the
sandbox's version of the file is whatever was committed when the image
was last built. `cdk deploy` rebuilds the image whenever any file in
infra/sandbox/ changes.
============================================================================

Auth primitive for the codebase-access layer. Agent uses ONE GitHub App;
customers install it on their GitHub org. Each install gets an
``installation_id`` which we store on the tenant row
(``codebases.github_installation_id``). This module exchanges that id for a
short-lived access token (~1h TTL) usable as a Bearer on ``api.github.com``.

## Auth flow

1. Sign an app-level JWT (RS256, ``iss=app_id``, ``exp=now+9min``).
2. ``POST /app/installations/{id}/access_tokens`` with that JWT as Bearer.
3. GitHub returns ``{token, expires_at, permissions}``. Use ``token`` as the
   Bearer for subsequent API calls on behalf of that installation.

## Secrets

- ``AGENT_LOCAL_STORES=1``:
    - ``GITHUB_APP_ID`` — numeric App ID (env var)
    - ``GITHUB_APP_PRIVATE_KEY_PEM`` — inline PEM, OR
    - ``GITHUB_APP_PRIVATE_KEY_FILE`` — path to a ``.pem`` file
- production:
    - ``GITHUB_APP_ID`` — env var (non-secret, baked into the task def)
    - Secrets Manager: ``agentcore/platform/github_app/private_key``

The private key lives OUTSIDE the ``agentcore/tenants/*`` prefix because
the GitHub App is a platform-level resource, not a tenant-level one.

**IAM:** the ``AgentCoreDataAccess`` and ``AgentCoreBridgeDataAccess``
managed policies in ``infra/data/lib/data-stack.ts`` grant
``secretsmanager:GetSecretValue`` on ``agentcore/platform/*`` (the
``PlatformSecretsRead`` statement). After a ``cdk deploy`` of the data
stack, the agent and bridge roles can both read this secret. The
secret itself must still be created out-of-band by an operator —
store the private key directly in Secrets Manager and never commit it.

## Caching

Installation tokens live for ~1 hour. We cache per ``installation_id`` and
reuse the token until 5 minutes before expiry (safety buffer for in-flight
requests that start near the edge). Cache is process-local and thread-safe.
Call ``invalidate_installation_token(id)`` on 401/403 from any downstream
caller that used a token minted here, and the next call will re-mint.

## Why PyJWT

Adding ``pyjwt[crypto]`` (~200KB) saves ~50 lines of hand-rolled
base64url + ``cryptography.hazmat`` JWT construction. RS256 requires the
``[crypto]`` extra which pulls in the ``cryptography`` backend.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone

import jwt

log = logging.getLogger(__name__)


# JWT lifetime: GitHub enforces a 10-minute maximum. Use 9 minutes to
# leave room for clock skew between us and GitHub.
_JWT_EXP_SECONDS = 9 * 60

# GitHub recommends backdating ``iat`` by 60s to guard against a local
# clock that's slightly ahead of GitHub's. Without this, you occasionally
# get "JWT issued in the future" 401s even though your clock is fine.
_JWT_IAT_BACKDATE_SECONDS = 60

# Safety buffer before the installation token's GitHub-reported expiry.
# A request that starts 4 minutes before expiry can still return a valid
# response, but a request that starts 30 seconds before expiry is flirting
# with a mid-flight invalidation.
_TOKEN_SAFETY_BUFFER_SECONDS = 5 * 60

_GITHUB_API_BASE = "https://api.github.com"
_GITHUB_APP_PRIVATE_KEY_SECRET_ID = "agentcore/platform/github_app/private_key"


@dataclass(frozen=True)
class InstallationToken:
    """A GitHub installation access token with its GitHub-reported expiry."""

    token: str
    expires_at: datetime  # UTC

    def is_expired(self, safety_buffer_seconds: int = 0) -> bool:
        now = datetime.now(timezone.utc)
        return now.timestamp() + safety_buffer_seconds >= self.expires_at.timestamp()


# ---------------------------------------------------------------------------
# Per-process cache
# ---------------------------------------------------------------------------

# installation_id -> InstallationToken. Not lru_cache because we need
# TTL-aware eviction (lru_cache has no concept of expiry). Dict + lock is
# the simplest thread-safe shape; the cache is tiny (one entry per
# tenant that has GitHub connected) and reads dominate writes.
_cache: dict[str, InstallationToken] = {}
_cache_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Secrets / config
# ---------------------------------------------------------------------------

def _get_app_id() -> str:
    """Return the numeric GitHub App ID from the ``GITHUB_APP_ID`` env var."""
    app_id = os.getenv("GITHUB_APP_ID")
    if not app_id:
        raise RuntimeError(
            "GITHUB_APP_ID env var is not set. Set it to the numeric ID of "
            "the Agent GitHub App (visible on the App's settings page)."
        )
    return app_id


def _get_private_key_pem() -> str:
    """Return the GitHub App private key as a PEM string.

    Local dev: read from ``GITHUB_APP_PRIVATE_KEY_PEM`` (inline) or
    ``GITHUB_APP_PRIVATE_KEY_FILE`` (path).

    Production: fetch from Secrets Manager at
    ``agentcore/platform/github_app/private_key``. Requires the IAM grant
    described in this module's ``Secrets`` section.
    """
    if os.getenv("AGENT_LOCAL_STORES") == "1":
        inline = os.getenv("GITHUB_APP_PRIVATE_KEY_PEM")
        if inline:
            return inline
        file_path = os.getenv("GITHUB_APP_PRIVATE_KEY_FILE")
        if file_path:
            try:
                with open(file_path) as f:
                    return f.read()
            except OSError as e:
                raise RuntimeError(
                    f"GITHUB_APP_PRIVATE_KEY_FILE={file_path} could not be "
                    f"read: {e}"
                ) from e
        raise RuntimeError(
            "AGENT_LOCAL_STORES=1 but neither GITHUB_APP_PRIVATE_KEY_PEM nor "
            "GITHUB_APP_PRIVATE_KEY_FILE is set."
        )

    try:
        import boto3  # type: ignore[import-untyped]

        client = boto3.client(
            "secretsmanager",
            region_name=os.getenv("AWS_REGION", "us-west-2"),
        )
        resp = client.get_secret_value(SecretId=_GITHUB_APP_PRIVATE_KEY_SECRET_ID)
    except Exception as e:  # noqa: BLE001 — wrap anything from boto3
        raise RuntimeError(
            f"Failed to fetch GitHub App private key from Secrets Manager "
            f"({_GITHUB_APP_PRIVATE_KEY_SECRET_ID}): {e}"
        ) from e

    pem = resp.get("SecretString", "")
    if not pem:
        raise RuntimeError(
            f"Secrets Manager returned an empty SecretString for "
            f"{_GITHUB_APP_PRIVATE_KEY_SECRET_ID}"
        )
    return pem


# ---------------------------------------------------------------------------
# JWT + token exchange
# ---------------------------------------------------------------------------

def _mint_app_jwt() -> str:
    """Sign an app-level JWT authenticating as the GitHub App itself.

    This JWT is only good for 9 minutes and only for calls to the App
    endpoints (most importantly, the installation-token exchange). It is
    NOT the thing you use as a Bearer for repo/code API calls — that's
    the installation token, minted by exchanging this JWT.
    """
    now = int(time.time())
    payload = {
        "iat": now - _JWT_IAT_BACKDATE_SECONDS,
        "exp": now + _JWT_EXP_SECONDS,
        "iss": _get_app_id(),
    }
    # PyJWT 2.x returns str; earlier versions returned bytes. Our
    # pyproject pins >= 2.8.0 so str is guaranteed.
    return jwt.encode(payload, _get_private_key_pem(), algorithm="RS256")


def _exchange_jwt_for_installation_token(
    app_jwt: str, installation_id: str
) -> InstallationToken:
    """POST to GitHub's installation-token exchange endpoint.

    Uses stdlib urllib to avoid a new HTTP dependency — matches the
    zero-new-deps pattern in ``slack_api.py``.
    """
    url = f"{_GITHUB_API_BASE}/app/installations/{installation_id}/access_tokens"
    req = urllib.request.Request(
        url,
        method="POST",
        headers={
            "Authorization": f"Bearer {app_jwt}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "Agent-Sandbox/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        # GitHub returns useful JSON errors — read the body for diagnostics.
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"GitHub installation-token exchange failed: HTTP {e.code} for "
            f"installation_id={installation_id}: {body}"
        ) from e
    except urllib.error.URLError as e:
        raise RuntimeError(
            f"GitHub installation-token exchange failed: network error for "
            f"installation_id={installation_id}: {e}"
        ) from e

    token = data.get("token")
    expires_at_raw = data.get("expires_at")
    if not token or not expires_at_raw:
        raise RuntimeError(
            f"GitHub returned an unexpected payload shape "
            f"(missing token or expires_at): {data!r}"
        )
    # GitHub's timestamps use the trailing-Z form; Python's fromisoformat
    # accepts ``+00:00`` but not ``Z`` on all versions, so normalize.
    expires_at = datetime.fromisoformat(expires_at_raw.replace("Z", "+00:00"))
    return InstallationToken(token=token, expires_at=expires_at)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_installation_token(installation_id: str) -> str:
    """Return a valid access token for ``installation_id``.

    Caches per installation with a 5-minute safety buffer. First call for
    a new installation_id costs one JWT sign + one HTTPS round-trip;
    subsequent calls within ~55 minutes are free (dict lookup).

    Raises ``RuntimeError`` on any failure (missing config, AWS/network,
    or a non-2xx response from GitHub). Callers should treat RuntimeError
    as "the agent cannot access this tenant's code right now" and surface
    a friendly message to the user.
    """
    if not installation_id:
        raise RuntimeError("installation_id is required")

    with _cache_lock:
        cached = _cache.get(installation_id)
        if cached and not cached.is_expired(
            safety_buffer_seconds=_TOKEN_SAFETY_BUFFER_SECONDS
        ):
            return cached.token

    # Mint a new token OUTSIDE the lock — the JWT sign + HTTPS call takes
    # hundreds of ms and we don't want to serialize every tenant's first
    # call of the hour behind a single mutex. Cost of a race: two tenants
    # mint concurrently and one of the tokens is immediately overwritten.
    # That's a cheap, benign duplicate API call, not a correctness bug.
    app_jwt = _mint_app_jwt()
    token_obj = _exchange_jwt_for_installation_token(app_jwt, installation_id)

    with _cache_lock:
        _cache[installation_id] = token_obj
    log.info(
        "github_app: minted installation token for installation_id=%s, "
        "expires_at=%s",
        installation_id,
        token_obj.expires_at.isoformat(),
    )
    return token_obj.token


def invalidate_installation_token(installation_id: str) -> None:
    """Drop the cached token for an installation.

    Call this on 401/403 responses from downstream code that used a token
    minted here, so the next call re-mints fresh credentials. Cheaper than
    waiting for the 55-minute TTL to expire.
    """
    with _cache_lock:
        _cache.pop(installation_id, None)


def reset_github_app_cache_for_tests() -> None:
    """Test helper: empty the whole cache so a fresh mint is forced."""
    with _cache_lock:
        _cache.clear()
