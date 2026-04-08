"""Per-tenant Slack bot token storage.

Model A (shared Slack app, per-workspace bot tokens). The bridge has ONE
shared signing secret (env var) that authenticates Slack → bridge, and
N bot tokens — one per installed workspace — used to authenticate
bridge → Slack when calling `chat.postMessage`.

Storage:
  - LOCAL_DEV=1: `EnvSlackTokenStore` returns `SLACK_BOT_TOKEN` for every
                 tenant. Lets the local loop work without Secrets Manager
                 or any real OAuth-installed token.
  - else:        `SecretsManagerSlackTokenStore` fetches
                 `agentcore/tenants/<tenant_id>/slack/bot_token` from
                 AWS Secrets Manager (lazy boto3 import, in-process LRU
                 cache).

The IAM managed policy `AgentCoreBridgeDataAccess` (see
`infra/data/lib/data-stack.ts`) grants the bridge role
`secretsmanager:GetSecretValue` AND `CreateSecret`/`PutSecretValue` on
`arn:aws:secretsmanager:*:*:secret:agentcore/tenants/*`. The OAuth callback
(week 2) writes new tokens; this module only reads them.
"""
from __future__ import annotations

import logging
import os
from functools import lru_cache
from typing import Any, Protocol

log = logging.getLogger(__name__)


class SlackTokenStore(Protocol):
    """Read contract for per-tenant Slack bot tokens.

    Implementations should raise `KeyError` for unknown tenants. Callers
    that want a stub-friendly fallback (e.g. the SlackAdapter's local-dev
    print mode) should catch KeyError explicitly.
    """

    def get(self, tenant_id: str) -> str: ...


class EnvSlackTokenStore:
    """LOCAL_DEV fallback: returns `SLACK_BOT_TOKEN` for every tenant.

    Returns an empty string if the env var is unset, which the
    SlackAdapter treats as "stub mode" (prints to stdout instead of
    posting to Slack). Never raises — local dev should work without any
    Slack credentials at all.
    """

    def get(self, tenant_id: str) -> str:
        return os.getenv("SLACK_BOT_TOKEN", "")


class SecretsManagerSlackTokenStore:
    """Production: fetches per-tenant bot tokens from AWS Secrets Manager.

    Secret naming convention: `agentcore/tenants/<tenant_id>/slack/bot_token`
    (matches the IAM policy prefix in `infra/data/lib/data-stack.ts`).

    Caches successful lookups in an in-process LRU (size 1024). The
    OAuth callback should call `invalidate(tenant_id)` after writing a
    new token so the cache picks it up on the next request.

    Failure modes:
      - Secret missing → `KeyError`
      - Secret present but empty / malformed → `KeyError`
      - AWS error → `KeyError` (treats network failures as "no token";
        the SlackAdapter falls back to its stub-print path so we never
        crash a request handler over a transient credentials failure)
    """

    def __init__(self, region: str | None = None) -> None:
        self.region = region or os.getenv("AWS_REGION", "us-west-2")
        self._client: Any | None = None
        # Wrap _fetch_uncached in an instance-bound lru_cache so we can
        # invalidate per-tenant entries from the OAuth callback path.
        self._cached_fetch = lru_cache(maxsize=1024)(self._fetch_uncached)

    def _get_client(self) -> Any:
        if self._client is None:
            import boto3

            self._client = boto3.client("secretsmanager", region_name=self.region)
        return self._client

    def _secret_id(self, tenant_id: str) -> str:
        return f"agentcore/tenants/{tenant_id}/slack/bot_token"

    def _fetch_uncached(self, tenant_id: str) -> str:
        secret_id = self._secret_id(tenant_id)
        try:
            response = self._get_client().get_secret_value(SecretId=secret_id)
        except Exception as e:  # noqa: BLE001 — convert all to KeyError below
            log.warning(
                "SecretsManagerSlackTokenStore: failed to fetch %s: %s",
                secret_id,
                e,
            )
            raise KeyError(tenant_id) from e

        # Tokens are stored as plain SecretString. JSON-encoded secrets
        # (e.g. `{"bot_token": "xoxb-..."}`) are NOT supported here on
        # purpose — the OAuth callback writes raw strings to keep this
        # path simple.
        token = response.get("SecretString", "")
        if not token:
            log.warning(
                "SecretsManagerSlackTokenStore: empty SecretString for %s",
                secret_id,
            )
            raise KeyError(tenant_id)
        return token

    def get(self, tenant_id: str) -> str:
        return self._cached_fetch(tenant_id)

    def invalidate(self, tenant_id: str) -> None:
        # lru_cache doesn't expose per-key invalidation. Cheap workaround:
        # clear the whole cache. Bot-token rotation is rare so the cost
        # is negligible.
        self._cached_fetch.cache_clear()


# ----------------------------------------------------------------------------
# Lazy singleton
# ----------------------------------------------------------------------------

_default_store: SlackTokenStore | None = None


def _store() -> SlackTokenStore:
    global _default_store
    if _default_store is None:
        if os.getenv("LOCAL_DEV") == "1":
            _default_store = EnvSlackTokenStore()
        else:
            _default_store = SecretsManagerSlackTokenStore()
    return _default_store


def get_bot_token(tenant_id: str) -> str:
    """Fetch the Slack bot token for `tenant_id`. Returns an empty string
    on LOCAL_DEV with no `SLACK_BOT_TOKEN` set; raises `KeyError` in
    production if the secret is missing.

    The SlackAdapter is the only intended caller. It treats an empty
    string as "stub mode" (print to stdout) and a `KeyError` as a real
    error worth bubbling up.
    """
    return _store().get(tenant_id)


def invalidate_token_cache(tenant_id: str) -> None:
    """Drop any cached entry for `tenant_id`. Called by the OAuth
    callback after writing a new token to Secrets Manager."""
    store = _store()
    if hasattr(store, "invalidate"):
        store.invalidate(tenant_id)


def reset_token_store_for_tests() -> None:
    """Test helper: clear the cached store so the next call re-reads env vars."""
    global _default_store
    _default_store = None
