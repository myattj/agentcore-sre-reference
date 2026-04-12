"""GitHub App installation-token minting (bridge side).

Bridge-side mirror of ``coreAgent/app/coreAgent/scm_github.py``. The bridge
and coreAgent can't cross-import (separate venvs — gotcha #13), so the
token-minting logic exists in both packages. They share the same GitHub
App identity: same ``GITHUB_APP_ID`` env var, same Secrets Manager path
(``agentcore/platform/github_app/private_key``).

## Why the bridge needs this

The install-time warm-start (``github_install.py``) runs in the bridge's
OAuth callback flow after a tenant installs the AgentCore Reference GitHub App on their
org. The warm-start needs to call GitHub APIs as the installation (list
repos, read ``pushed_at``, rank them, pick a default), which requires an
installation access token.

## Auth flow

1. Sign an app-level JWT (RS256, ``iss=app_id``, ``exp=now+9min``).
2. ``POST /app/installations/{id}/access_tokens`` with the JWT as Bearer.
3. GitHub returns ``{token, expires_at, permissions}``. Use ``token`` as
   the Bearer for subsequent API calls on behalf of that installation.

## Secrets

- ``LOCAL_DEV=1`` (bridge's local-dev env var — note: NOT
  ``AGENT_LOCAL_STORES``, that's the agent's):
    - ``GITHUB_APP_ID`` — numeric App ID
    - ``GITHUB_APP_PRIVATE_KEY_PEM`` — inline PEM, OR
    - ``GITHUB_APP_PRIVATE_KEY_FILE`` — path to a ``.pem`` file
- production:
    - ``GITHUB_APP_ID`` — env var (non-secret, baked into the task def)
    - Secrets Manager: ``agentcore/platform/github_app/private_key``

The bridge's ``AgentCoreBridgeDataAccess`` IAM policy (see
``infra/data/lib/data-stack.ts``) grants ``secretsmanager:GetSecretValue``
on ``agentcore/platform/*`` as of the step-3 work.

## Caching

Installation tokens live for ~1 hour. Cache per ``installation_id`` and
reuse until 5 minutes before expiry (safety buffer for in-flight requests
that start near the edge). Process-local, thread-safe.

On 401/403 from a downstream caller that used a token from here, call
``invalidate_installation_token(installation_id)`` so the next call mints
fresh credentials without waiting for the TTL.
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


# JWT lifetime: GitHub enforces a 10-minute maximum. 9 minutes leaves
# headroom for clock skew between us and GitHub.
_JWT_EXP_SECONDS = 9 * 60

# GitHub recommends backdating ``iat`` by 60s to guard against a local
# clock that's slightly ahead of GitHub's. Without this, occasional
# "JWT issued in the future" 401s appear even with a correct clock.
_JWT_IAT_BACKDATE_SECONDS = 60

# Safety buffer before the installation token's GitHub-reported expiry.
# A request that starts 4 minutes before expiry can still return a valid
# response, but starting 30 seconds out is flirting with mid-flight
# invalidation on long calls.
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
# TTL-aware eviction. Dict + lock is the simplest thread-safe shape; the
# cache is tiny (one entry per tenant with GitHub connected) and reads
# dominate writes.
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
            "the AgentCore Reference GitHub App (visible on the App's settings page)."
        )
    return app_id


def _get_private_key_pem() -> str:
    """Return the GitHub App private key as a PEM string.

    LOCAL_DEV: read from ``GITHUB_APP_PRIVATE_KEY_PEM`` (inline) or
    ``GITHUB_APP_PRIVATE_KEY_FILE`` (path).

    Production: fetch from Secrets Manager at
    ``agentcore/platform/github_app/private_key``. The bridge's IAM
    policy grants read on ``agentcore/platform/*`` as of step 3.
    """
    if os.getenv("LOCAL_DEV") == "1":
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
            "LOCAL_DEV=1 but neither GITHUB_APP_PRIVATE_KEY_PEM nor "
            "GITHUB_APP_PRIVATE_KEY_FILE is set."
        )

    try:
        import boto3  # type: ignore[import-untyped]

        client = boto3.client(
            "secretsmanager",
            region_name=os.getenv("AWS_REGION", "us-west-2"),
        )
        resp = client.get_secret_value(SecretId=_GITHUB_APP_PRIVATE_KEY_SECRET_ID)
    except Exception as e:  # noqa: BLE001 — wrap any boto3/network failure
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

    This JWT is only valid for 9 minutes and only for calls to the App
    endpoints (most importantly, the installation-token exchange). It is
    NOT what you use as a Bearer for repo/code API calls — that's the
    installation token minted by exchanging this JWT.
    """
    now = int(time.time())
    payload = {
        "iat": now - _JWT_IAT_BACKDATE_SECONDS,
        "exp": now + _JWT_EXP_SECONDS,
        "iss": _get_app_id(),
    }
    # PyJWT 2.x returns str; our pyproject pins >= 2.8 so str is guaranteed.
    return jwt.encode(payload, _get_private_key_pem(), algorithm="RS256")


def _exchange_jwt_for_installation_token(
    app_jwt: str, installation_id: str
) -> InstallationToken:
    """POST to GitHub's installation-token exchange endpoint.

    Stdlib urllib to avoid a new HTTP dependency — httpx is already in
    the bridge, but staying on urllib keeps this module drop-in testable
    without async plumbing.
    """
    url = f"{_GITHUB_API_BASE}/app/installations/{installation_id}/access_tokens"
    req = urllib.request.Request(
        url,
        method="POST",
        headers={
            "Authorization": f"Bearer {app_jwt}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "AgentCore Reference-Bridge/1.0",
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
    # GitHub uses the trailing-Z form; normalize to +00:00 for fromisoformat.
    expires_at = datetime.fromisoformat(expires_at_raw.replace("Z", "+00:00"))
    return InstallationToken(token=token, expires_at=expires_at)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_installation_token(installation_id: str) -> str:
    """Return a valid access token for ``installation_id``.

    Caches per installation with a 5-minute safety buffer. First call for
    a new installation_id costs one JWT sign + one HTTPS round-trip;
    subsequent calls within ~55 minutes are free.

    Raises ``RuntimeError`` on any failure (missing config, AWS/network,
    or a non-2xx response from GitHub). Callers should treat RuntimeError
    as "the bridge cannot access GitHub for this installation right now"
    and surface a friendly message to the user.
    """
    if not installation_id:
        raise RuntimeError("installation_id is required")

    with _cache_lock:
        cached = _cache.get(installation_id)
        if cached and not cached.is_expired(
            safety_buffer_seconds=_TOKEN_SAFETY_BUFFER_SECONDS
        ):
            return cached.token

    # Mint outside the lock — JWT sign + HTTPS takes hundreds of ms and
    # we don't want to serialize every tenant's first-of-the-hour call
    # behind a single mutex. Cost of a race: two concurrent mints for the
    # same installation, one overwrites the other in the cache. Benign.
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
    minted here, so the next call re-mints fresh credentials. Cheaper
    than waiting for the 55-minute TTL to expire.
    """
    with _cache_lock:
        _cache.pop(installation_id, None)


def reset_github_app_cache_for_tests() -> None:
    """Test helper: empty the whole cache so a fresh mint is forced."""
    with _cache_lock:
        _cache.clear()
