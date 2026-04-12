"""Shared helpers for the integration seeders.

Each seeder needs a few common things:

  - Load credentials from Secrets Manager at ``agentcore/testenv/<name>``
  - Mint a bridge session token (to call the /api/tenants/*/integrations/* routes)
  - A rate-limited HTTP client
  - State tracking for idempotency (so re-runs don't duplicate content)

Credentials layout:

  agentcore/testenv/datadog    -> {"api_key", "app_key", "site"}
  agentcore/testenv/pagerduty  -> {"api_key"}
  agentcore/testenv/jira       -> {"email", "api_token", "domain"}
  agentcore/testenv/linear     -> {"api_key"}
  agentcore/testenv/sentry     -> {"auth_token", "organization", "project"}

``domain`` for Jira is the subdomain (e.g. "acme-testenv" for
``acme-testenv.atlassian.net``).

All seeders assume you have AWS credentials that can read the
``agentcore/testenv/*`` namespace in the same account that runs
the bridge (typically: your dev profile).
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

# Make ``bridge.slack_oauth.make_session_token`` importable without
# installing the bridge package. Same sys.path hack as bootstrap.py.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_BRIDGE_DIR = _REPO_ROOT / "bridge"
if str(_BRIDGE_DIR) not in sys.path:
    sys.path.insert(0, str(_BRIDGE_DIR))

log = logging.getLogger(__name__)

DEFAULT_REGION = "us-west-2"
DEFAULT_BRIDGE_URL = "https://agent.example.com"


# ----------------------------------------------------------------------------
# Secrets Manager
# ----------------------------------------------------------------------------

def _secret_id(integration: str) -> str:
    return f"agentcore/testenv/{integration}"


def load_integration_secret(
    integration: str,
    *,
    region: str | None = None,
    required_keys: list[str] | None = None,
) -> dict[str, Any]:
    """Load and parse the JSON secret for a given integration.

    Raises ``RuntimeError`` with a specific, actionable message if:
      - the secret doesn't exist (user needs to run the aws secretsmanager
        create-secret command from README.md)
      - the secret isn't valid JSON
      - any ``required_keys`` are missing

    The error messages quote the README so the user always has a next
    step without needing to grep.
    """
    import boto3
    from botocore.exceptions import ClientError

    region = region or os.getenv("AWS_REGION", DEFAULT_REGION)
    client = boto3.client("secretsmanager", region_name=region)
    secret_id = _secret_id(integration)

    try:
        response = client.get_secret_value(SecretId=secret_id)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code == "ResourceNotFoundException":
            raise RuntimeError(
                f"No secret at {secret_id!r} in region {region}.\n"
                f"  Create it with the command in "
                f"scripts/testenv/integrations/README.md "
                f"(search for '{integration}')."
            ) from e
        raise

    blob = response.get("SecretString") or "{}"
    try:
        data = json.loads(blob)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"Secret {secret_id!r} is not valid JSON: {e}. "
            f"Expected shape per README.md for {integration!r}."
        ) from e

    if not isinstance(data, dict):
        raise RuntimeError(
            f"Secret {secret_id!r} must be a JSON object, got {type(data).__name__}."
        )

    missing = [k for k in (required_keys or []) if k not in data or not data.get(k)]
    if missing:
        raise RuntimeError(
            f"Secret {secret_id!r} is missing required keys: {missing}. "
            f"See README.md section for {integration!r}."
        )

    return data


# ----------------------------------------------------------------------------
# Bridge session token
# ----------------------------------------------------------------------------

def _find_bridge_secret_name(region: str) -> str:
    """Find the bridge state-secret in Secrets Manager by prefix.

    Same logic as bootstrap.py — the bridge secret has a random suffix
    so we list+filter by prefix.
    """
    import boto3

    client = boto3.client("secretsmanager", region_name=region)
    paginator = client.get_paginator("list_secrets")
    for page in paginator.paginate(
        Filters=[{"Key": "name", "Values": ["agentcore/services/bridge"]}]
    ):
        for entry in page.get("SecretList", []):
            name = entry.get("Name") or ""
            if name.startswith("agentcore/services/bridge"):
                return name
    raise RuntimeError(
        "Could not find a secret named agentcore/services/bridge* in "
        f"region {region}."
    )


def load_bridge_state_secret(region: str) -> str:
    """Fetch BRIDGE_OAUTH_STATE_SECRET, same as bootstrap.py."""
    env_override = os.getenv("BRIDGE_OAUTH_STATE_SECRET")
    if env_override:
        return env_override

    import boto3

    name = _find_bridge_secret_name(region)
    client = boto3.client("secretsmanager", region_name=region)
    resp = client.get_secret_value(SecretId=name)
    blob = resp.get("SecretString") or "{}"
    data = json.loads(blob)
    secret = data.get("BRIDGE_OAUTH_STATE_SECRET")
    if not secret:
        raise RuntimeError(
            f"Secret {name} has no BRIDGE_OAUTH_STATE_SECRET key."
        )
    return secret


def mint_bridge_session_token(tenant_id: str, region: str | None = None) -> str:
    """Mint a bridge session token locally using the bridge's own helper."""
    region = region or os.getenv("AWS_REGION", DEFAULT_REGION)
    state_secret = load_bridge_state_secret(region)
    os.environ["BRIDGE_OAUTH_STATE_SECRET"] = state_secret
    from bridge.slack_oauth import make_session_token  # type: ignore

    return make_session_token(tenant_id)


# ----------------------------------------------------------------------------
# Bridge integrations/* POST (connect step)
# ----------------------------------------------------------------------------

def bridge_connect_integration(
    tenant_id: str,
    integration: str,
    body: dict[str, Any],
    *,
    bridge_url: str | None = None,
    region: str | None = None,
    timeout_s: float = 60.0,
) -> dict[str, Any]:
    """POST to the bridge's ``/api/tenants/{id}/integrations/{integration}``.

    Raises ``RuntimeError`` on non-200. On 200, returns the parsed JSON
    response (``IntegrationConnectResponse``). The bridge handles
    credential validation, Gateway target provisioning, and flipping
    ``byo.enabled`` on the tenant row.

    Be patient — provisioning can take 20-40s per integration because
    AgentCore Gateway target creation is slow.
    """
    import httpx

    bridge_url = (bridge_url or os.getenv("BRIDGE_BASE_URL", DEFAULT_BRIDGE_URL)).rstrip("/")
    url = f"{bridge_url}/api/tenants/{tenant_id}/integrations/{integration}"
    token = mint_bridge_session_token(tenant_id, region=region)

    log.info("POST %s", url)
    with httpx.Client(timeout=timeout_s) as client:
        response = client.post(
            url,
            json=body,
            headers={"Authorization": f"Bearer {token}"},
        )
    if response.status_code != 200:
        raise RuntimeError(
            f"bridge connect failed for {integration!r}: "
            f"HTTP {response.status_code} — {response.text[:500]}"
        )
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(
            f"bridge connect returned ok=false for {integration!r}: "
            f"{data.get('error', 'no error message')}"
        )
    return data


# ----------------------------------------------------------------------------
# Rate-limited HTTP client (simple minimum-interval)
# ----------------------------------------------------------------------------

class RateLimitedClient:
    """Thin wrapper over httpx.Client with a min-interval between calls.

    Not thread-safe. Reuses one httpx.Client across calls. Respects a
    per-second floor so seeders don't hammer free-tier APIs.
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        headers: dict[str, str] | None = None,
        min_interval_s: float = 0.3,
        timeout_s: float = 30.0,
    ) -> None:
        import httpx

        self._min_interval = min_interval_s
        self._last: float = 0.0
        self._client = httpx.Client(
            base_url=base_url or "",
            headers=headers or {},
            timeout=timeout_s,
        )

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last
        remaining = self._min_interval - elapsed
        if remaining > 0:
            time.sleep(remaining)

    def get(self, path: str, **kwargs: Any) -> Any:
        self._throttle()
        r = self._client.get(path, **kwargs)
        self._last = time.monotonic()
        return r

    def post(self, path: str, **kwargs: Any) -> Any:
        self._throttle()
        r = self._client.post(path, **kwargs)
        self._last = time.monotonic()
        return r

    def put(self, path: str, **kwargs: Any) -> Any:
        self._throttle()
        r = self._client.put(path, **kwargs)
        self._last = time.monotonic()
        return r

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "RateLimitedClient":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


# ----------------------------------------------------------------------------
# State tracking — skip re-seeding on repeat runs
# ----------------------------------------------------------------------------

def _state_path(integration: str) -> Path:
    return Path(__file__).resolve().parent / f".{integration}-seeded.json"


def load_seeded_state(integration: str) -> dict[str, Any]:
    """Return the on-disk state blob for an integration, or empty dict."""
    path = _state_path(integration)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}


def save_seeded_state(integration: str, state: dict[str, Any]) -> None:
    path = _state_path(integration)
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


# ----------------------------------------------------------------------------
# Logging
# ----------------------------------------------------------------------------

def configure_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        level=logging.DEBUG if verbose else logging.INFO,
        datefmt="%H:%M:%S",
    )
    if not verbose:
        for name in ("botocore", "boto3", "urllib3", "httpx"):
            logging.getLogger(name).setLevel(logging.WARNING)


# ----------------------------------------------------------------------------
# Tiny print helpers — same style as bootstrap.py
# ----------------------------------------------------------------------------

class C:
    GREEN = "\033[92m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    GREY = "\033[90m"
    BOLD = "\033[1m"
    RESET = "\033[0m"


def step(msg: str) -> None:
    print(f"\n{C.BOLD}{C.BLUE}▶ {msg}{C.RESET}")


def ok(msg: str) -> None:
    print(f"  {C.GREEN}✓{C.RESET} {msg}")


def warn(msg: str) -> None:
    print(f"  {C.YELLOW}!{C.RESET} {msg}")


def err(msg: str) -> None:
    print(f"  {C.RED}✗ {msg}{C.RESET}")


def grey(msg: str) -> None:
    print(f"  {C.GREY}{msg}{C.RESET}")
