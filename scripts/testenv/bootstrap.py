#!/usr/bin/env python3
"""One-shot orchestrator for the Agent test-env rig.

Idempotent. Safe to re-run. Expects the manual steps from
``scripts/testenv/README.md`` to be done first (Slack workspace
created, Agent app installed, channels created, GitHub App
installed optionally).

What it does, in order:

  1. Verify the tenant row exists in the configured ``tenants`` DynamoDB
     table. If not, print the install URL and exit.
  2. Mark only ``config.is_internal_testenv`` through a scoped DynamoDB update.
  3. Verify the separate Slack seeder token is present in the environment.
  4. Discover + join the expected test channels via Slack API.
  5. If GitHub is configured, approve the installation through the narrow
     operator endpoint before enabling its tenant-visible repo bindings.
  6. Load ``BRIDGE_OAUTH_STATE_SECRET`` from the environment or the bridge
     secret and PATCH the tenant-safe ``build_testenv_config()`` dict via
     ``/api/tenants/{id}`` — this exercises the real validation path.
  7. Run all the seed packs (``seed_slack_history.seed_all``).
  8. Print a ready-to-test summary.

Run via ``scripts/testenv-bootstrap.sh`` (bridge venv launcher).
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

# Ensure the bridge package is importable (for make_session_token).
# Mirrors scripts/smoke.py's sys.path hack.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_BRIDGE_DIR = _REPO_ROOT / "bridge"
if str(_BRIDGE_DIR) not in sys.path:
    sys.path.insert(0, str(_BRIDGE_DIR))

from ._channels import discover_and_join  # noqa: E402
from ._common import (  # noqa: E402
    configure_logging,
    load_seeder_bot_token,
    make_slack_client,
)
from ._state import SeederState  # noqa: E402
from .config import TESTENV_CHANNELS, build_testenv_config  # noqa: E402
from .seed_slack_history import seed_all  # noqa: E402

log = logging.getLogger(__name__)


DEFAULT_BRIDGE_URL = "http://localhost:8000"
DEFAULT_REGION = "us-west-2"
_GITHUB_LOGIN_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?\Z")


# ----------------------------------------------------------------------------
# Color helpers (match smoke.py style)
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


# ----------------------------------------------------------------------------
# Deployment secrets + state
# ----------------------------------------------------------------------------


def _find_bridge_secret_name(region: str) -> str:
    """Find the bridge secret's full Secrets Manager name.

    The secret is created manually outside CDK with a randomized
    suffix, so the exact name is ``agentcore/services/bridge-<6 chars>``.
    We list secrets matching the prefix and take the first one.
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
        f"region {region}. Either the bridge isn't provisioned in this "
        f"account or you lack secretsmanager:ListSecrets. Set the needed "
        f"BRIDGE_OAUTH_STATE_SECRET or ADMIN_SECRET environment variable "
        f"to skip this lookup."
    )


def _load_bridge_secret_json(region: str) -> tuple[str, dict[str, Any]]:
    """Return the bridge Secrets Manager name and parsed JSON object."""
    import boto3

    name = _find_bridge_secret_name(region)
    log.info("fetching bridge secret from %s", name)
    client = boto3.client("secretsmanager", region_name=region)
    resp = client.get_secret_value(SecretId=name)
    blob = resp.get("SecretString") or "{}"
    try:
        data = json.loads(blob)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Secret {name} is not JSON. Expected a JSON object.") from e
    if not isinstance(data, dict):
        raise RuntimeError(f"Secret {name} must contain a JSON object.")
    return name, data


def _load_bridge_secret_value(region: str, key: str) -> str:
    """Load one named bridge secret value without logging its contents."""
    name, data = _load_bridge_secret_json(region)
    secret = data.get(key)
    if not isinstance(secret, str) or not secret:
        raise RuntimeError(
            f"Secret {name} JSON has no non-empty {key} key. Got keys: {sorted(data)}"
        )
    return secret


def load_state_secret(region: str) -> str:
    """Fetch BRIDGE_OAUTH_STATE_SECRET from env or Secrets Manager.

    The secret at ``agentcore/services/bridge-<suffix>`` has a JSON
    blob; we extract the ``BRIDGE_OAUTH_STATE_SECRET`` key. Falls back
    to the env var if set, which is useful for local dev.
    """
    env_override = os.getenv("BRIDGE_OAUTH_STATE_SECRET")
    if env_override:
        log.info("using BRIDGE_OAUTH_STATE_SECRET from env")
        return env_override

    return _load_bridge_secret_value(region, "BRIDGE_OAUTH_STATE_SECRET")


def load_admin_secret(region: str) -> str:
    """Fetch ADMIN_SECRET without ever printing or embedding it in a URL."""
    env_override = os.getenv("ADMIN_SECRET")
    if env_override:
        log.info("using ADMIN_SECRET from env")
        return env_override

    return _load_bridge_secret_value(region, "ADMIN_SECRET")


# ----------------------------------------------------------------------------
# Tenant verification + PATCH
# ----------------------------------------------------------------------------


def verify_tenant_exists(tenant_id: str, region: str) -> dict[str, Any]:
    """GetItem against the configured ``tenants`` table. Returns the config dict.
    Raises with a friendly message if missing."""
    import boto3

    table_name = os.getenv("TENANTS_TABLE", "tenants")
    resource = boto3.resource("dynamodb", region_name=region)
    table = resource.Table(table_name)
    response = table.get_item(Key={"tenant_id": tenant_id})
    item = response.get("Item")
    if not item:
        raise RuntimeError(
            f"No tenant row for {tenant_id!r} in table {table_name!r}. "
            f"Install the Agent Slack app to your test workspace first: "
            f"{os.getenv('BRIDGE_BASE_URL', DEFAULT_BRIDGE_URL)}/slack/install"
        )
    config = item.get("config") or {}
    return dict(config)  # type: ignore[return-value]


def mark_internal_testenv(tenant_id: str, region: str) -> None:
    """Set only ``config.is_internal_testenv`` on an existing tenant row."""
    import boto3

    table_name = os.getenv("TENANTS_TABLE", "tenants")
    resource = boto3.resource("dynamodb", region_name=region)
    table = resource.Table(table_name)
    table.update_item(
        Key={"tenant_id": tenant_id},
        UpdateExpression="SET #config.#internal = :true",
        ConditionExpression=(
            "attribute_exists(#tenant_id) AND attribute_exists(#config)"
        ),
        ExpressionAttributeNames={
            "#tenant_id": "tenant_id",
            "#config": "config",
            "#internal": "is_internal_testenv",
        },
        ExpressionAttributeValues={":true": True},
        ReturnValues="NONE",
    )


def approve_github_installation(
    tenant_id: str,
    installation_id: int,
    expected_account_login: str,
    *,
    bridge_url: str,
    admin_secret: str,
) -> None:
    """Approve a GitHub trust binding through the operator-only endpoint."""
    parsed = urlsplit(bridge_url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise RuntimeError("bridge URL must be an HTTP(S) origin")
    if parsed.scheme == "http":
        hostname = parsed.hostname.lower()
        is_loopback = hostname == "localhost"
        if not is_loopback:
            try:
                is_loopback = ipaddress.ip_address(hostname).is_loopback
            except ValueError:
                is_loopback = False
        if not is_loopback:
            raise RuntimeError("plain HTTP is allowed only for a loopback bridge")

    bridge_origin = f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
    safe_tenant_id = quote(tenant_id, safe="")
    url = f"{bridge_origin}/api/ops/tenants/{safe_tenant_id}/codebases/github/approve"
    try:
        import httpx

        with httpx.Client(timeout=30.0) as client:
            response = client.post(
                url,
                headers={"X-Admin-Token": admin_secret},
                json={
                    "installation_id": installation_id,
                    "expected_account_login": expected_account_login,
                },
            )
    except Exception as e:  # noqa: BLE001 -- sanitize transport failures
        raise RuntimeError("GitHub installation approval request failed") from e
    if response.status_code != 200:
        # Deliberately omit the response body: upstream/proxy errors must not
        # be allowed to reflect the admin secret into terminal output.
        raise RuntimeError(
            f"GitHub installation approval failed (HTTP {response.status_code})"
        )
    try:
        result = response.json()
    except Exception as e:  # noqa: BLE001 -- do not reflect response contents
        raise RuntimeError("GitHub installation approval returned invalid JSON") from e
    if (
        not isinstance(result, dict)
        or result.get("approved") is not True
        or result.get("tenant_id") != tenant_id
        or str(result.get("installation_id")) != str(installation_id)
        or str(result.get("account_login", "")).lower()
        != expected_account_login.lower()
    ):
        raise RuntimeError(
            "GitHub installation approval response did not match request"
        )


def validate_github_setup(
    github_org: str | None,
    github_installation_id: str | None,
) -> tuple[str, int] | None:
    """Validate the all-or-nothing optional GitHub bootstrap arguments."""
    if bool(github_org) != bool(github_installation_id):
        raise RuntimeError(
            "--github-org and --github-installation-id must be provided together"
        )
    if not github_org or not github_installation_id:
        return None

    account_login = github_org.strip()
    if not _GITHUB_LOGIN_RE.fullmatch(account_login) or "--" in account_login:
        raise RuntimeError("--github-org must be a valid GitHub account login")
    try:
        installation_id = int(github_installation_id, 10)
    except ValueError as e:
        raise RuntimeError("--github-installation-id must be numeric") from e
    if installation_id <= 0 or installation_id > 2**63 - 1:
        raise RuntimeError("--github-installation-id must be a positive 64-bit integer")
    return account_login, installation_id


def patch_tenant_config(
    tenant_id: str,
    config_dict: dict[str, Any],
    *,
    bridge_url: str,
    state_secret: str,
) -> None:
    """PATCH the rich config via the bridge's real API. Exercises the
    full Pydantic validation + deep-merge path, same as the onboarding UI."""
    import httpx

    # Mint a session token locally using the bridge's own helper.
    os.environ["BRIDGE_OAUTH_STATE_SECRET"] = state_secret
    from bridge.slack_oauth import make_session_token  # type: ignore

    token = make_session_token(tenant_id)
    url = f"{bridge_url.rstrip('/')}/api/tenants/{quote(tenant_id, safe='')}"

    log.info("PATCH %s", url)
    with httpx.Client(timeout=30.0) as client:
        response = client.patch(
            url,
            json=config_dict,
            headers={"Authorization": f"Bearer {token}"},
        )
    if response.status_code != 200:
        raise RuntimeError(
            f"PATCH failed: {response.status_code} — {response.text[:500]}"
        )
    log.info("PATCH ok")


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

_BOOTSTRAP_INTEGRATIONS = ("pagerduty", "jira", "linear", "sentry")


def parse_integrations(value: str) -> list[str]:
    """Parse the bootstrap integration selector.

    Datadog is deliberately absent. Its API requires two independent secrets,
    while a direct AgentCore Gateway target supports one credential provider.
    The bridge therefore rejects direct Datadog provisioning. Developers can
    still populate a disposable Datadog account with the content-only seeder,
    which requires an explicit ``--skip-connect`` flag.
    """
    selector = value.strip().lower()
    if not selector:
        return []
    if selector == "all":
        return list(_BOOTSTRAP_INTEGRATIONS)

    integrations = [item.strip().lower() for item in value.split(",") if item.strip()]
    if "datadog" in integrations:
        raise ValueError(
            "Datadog is content-seed-only and cannot be selected by bootstrap; "
            "run `python -m scripts.testenv.integrations.seed_datadog "
            "--tenant <id> --skip-connect` explicitly"
        )
    unknown = [item for item in integrations if item not in _BOOTSTRAP_INTEGRATIONS]
    if unknown:
        raise ValueError(
            f"unknown integration(s): {unknown}. Valid: {list(_BOOTSTRAP_INTEGRATIONS)}"
        )
    return integrations


def _run_integration_seeders(
    tenant_id: str,
    *,
    region: str,
    bridge_url: str,
    integrations: list[str],
) -> dict[str, int]:
    """Run each selected integration seeder in sequence.

    Returns a ``{integration: exit_code}`` map. Doesn't abort the
    whole bootstrap on integration failures — they're optional and
    often blocked on free-tier credential issues we can't diagnose
    from here. The bootstrap prints per-integration status and the
    user can retry individual ones via
    ``python -m scripts.testenv.integrations.seed_<name>``.
    """
    results: dict[str, int] = {}
    for name in integrations:
        step(f"Running integration seeder: {name}")
        try:
            if name == "pagerduty":
                from .integrations.seed_pagerduty import run_seed as run_pd

                results[name] = run_pd(tenant_id, region=region, bridge_url=bridge_url)
            elif name == "jira":
                from .integrations.seed_jira import run_seed as run_jira

                results[name] = run_jira(
                    tenant_id, region=region, bridge_url=bridge_url
                )
            elif name == "linear":
                from .integrations.seed_linear import run_seed as run_linear

                results[name] = run_linear(
                    tenant_id, region=region, bridge_url=bridge_url
                )
            elif name == "sentry":
                from .integrations.seed_sentry import run_seed as run_sentry

                results[name] = run_sentry(
                    tenant_id, region=region, bridge_url=bridge_url
                )
            else:
                err(f"unknown integration: {name}")
                results[name] = 1
        except Exception as e:  # noqa: BLE001
            err(f"{name} seeder crashed: {e}")
            results[name] = 1
    return results


def run_bootstrap(
    tenant_id: str,
    *,
    region: str,
    bridge_url: str,
    github_org: str | None,
    github_installation_id: str | None,
    skip_seed: bool,
    skip_patch: bool,
    integrations: list[str],
) -> int:
    configure_logging(verbose=False)

    # ----- Step 1: tenant exists? -----
    step("Verifying tenant row in DynamoDB")
    try:
        existing = verify_tenant_exists(tenant_id, region)
    except RuntimeError as e:
        err(str(e))
        return 1
    ok(
        f"tenant {tenant_id!r} exists "
        f"(is_internal_testenv={existing.get('is_internal_testenv', False)})"
    )

    approved_github: tuple[str, int] | None = None
    if skip_patch:
        if github_org or github_installation_id:
            warn("--skip-patch: ignoring GitHub setup arguments")
    else:
        try:
            approved_github = validate_github_setup(
                github_org,
                github_installation_id,
            )
        except RuntimeError as e:
            err(str(e))
            return 1

    # ----- Step 2: operator-owned test marker -----
    step("Marking tenant as an internal test environment")
    try:
        mark_internal_testenv(tenant_id, region)
    except Exception:  # noqa: BLE001 -- boto exceptions vary by dependency version
        err("could not set config.is_internal_testenv in DynamoDB")
        return 1
    ok("config.is_internal_testenv=true")

    # ----- Step 3: separate seeder token -----
    step("Verifying the disposable Slack seeder token")
    try:
        bot_token = load_seeder_bot_token()
    except RuntimeError as e:
        err(str(e))
        return 1
    ok("seeder bot token loaded")

    # ----- Step 4: Slack client + channels -----
    step("Discovering + joining test channels")
    client = make_slack_client(bot_token)
    state = SeederState(tenant_id)
    channel_map, missing = discover_and_join(client, state)
    if missing:
        err(f"missing {len(missing)} channels in the workspace:")
        for name in missing:
            grey(f"  #{name}")
        warn(
            "create these channels manually in Slack (see scripts/testenv/README.md) "
            "and re-run bootstrap"
        )
        return 1
    ok(
        f"{len(channel_map)} channels ready: {', '.join('#' + n for n in TESTENV_CHANNELS)}"
    )

    # ----- Step 5: approve GitHub + build + PATCH config -----
    if skip_patch:
        warn("--skip-patch: leaving tenant config unchanged")
    else:
        if approved_github is not None:
            account_login, installation_id = approved_github
            step("Approving GitHub App installation through the ops endpoint")
            try:
                admin_secret = load_admin_secret(region)
            except Exception:  # noqa: BLE001 -- never expose secret-store details
                err("could not load ADMIN_SECRET for GitHub approval")
                return 1
            try:
                approve_github_installation(
                    tenant_id,
                    installation_id,
                    account_login,
                    bridge_url=bridge_url,
                    admin_secret=admin_secret,
                )
            except RuntimeError as e:
                err(str(e))
                return 1
            ok(f"GitHub installation approved for {account_login}")

        step("Building rich Acme Data Co config + PATCHing bridge")
        config_dict = build_testenv_config(
            channel_map=channel_map,
            github_org=(approved_github[0] if approved_github else None),
        )
        grey(
            f"sections: catalog({len(config_dict['catalog']['allowed_tools'])} tools), "
            f"skills({len(config_dict['skills'])}), "
            f"escalation({len(config_dict['escalation']['routes'])} routes), "
            f"channels({len(config_dict['channels'])}), "
            f"codebases({'on' if config_dict['codebases']['enabled'] else 'off'})"
        )
        try:
            state_secret = load_state_secret(region)
        except RuntimeError as e:
            err(str(e))
            return 1
        try:
            patch_tenant_config(
                tenant_id,
                config_dict,
                bridge_url=bridge_url,
                state_secret=state_secret,
            )
        except RuntimeError as e:
            err(str(e))
            return 1
        ok("PATCH succeeded — tenant config is now Acme Data Co flavored")

    # ----- Step 6: seed Slack history -----
    if skip_seed:
        warn("--skip-seed: leaving Slack channels unchanged")
    else:
        step("Seeding Slack history (this takes ~5–8 min)")
        try:
            counts = seed_all(tenant_id, region=region)
        except RuntimeError as e:
            err(str(e))
            return 1
        ok(
            f"seed done: {counts['posted']} posted, "
            f"{counts['skipped']} skipped, "
            f"{counts['failed']} failed"
        )
        if counts["failed"]:
            warn("some messages failed — check the log and re-run to retry")

    # ----- Step 7: external integrations (optional) -----
    integration_results: dict[str, int] = {}
    if integrations:
        step(
            f"Running {len(integrations)} integration seeder(s): {', '.join(integrations)}"
        )
        integration_results = _run_integration_seeders(
            tenant_id,
            region=region,
            bridge_url=bridge_url,
            integrations=integrations,
        )

    # ----- Step 8: summary -----
    step("Test env is ready")
    print()
    print(f"  {C.BOLD}Tenant:{C.RESET}   {tenant_id}")
    print(f"  {C.BOLD}Bridge:{C.RESET}   {bridge_url}")
    print(
        f"  {C.BOLD}Channels:{C.RESET} {', '.join('#' + n for n in TESTENV_CHANNELS)}"
    )
    print(
        f"  {C.BOLD}Metrics:{C.RESET}  {bridge_url.replace('/api', '')}/workspace/{tenant_id}/metrics"
    )
    if integration_results:
        print(f"  {C.BOLD}Integrations:{C.RESET}")
        for name, code in integration_results.items():
            status = f"{C.GREEN}✓{C.RESET}" if code == 0 else f"{C.RED}✗{C.RESET}"
            print(f"    {status} {name}")
    print()
    print(f"  {C.BOLD}Try this first:{C.RESET}")
    grey("   1. Open your test Slack workspace")
    grey(
        "   2. Go to #ask-data and ask: 'what does the team think about dbt vs airflow?'"
    )
    grey("   3. Go to #alerts-sre, find the unacked P2 alert, @mention the bot")
    grey(
        "   4. Go to #incidents, find the Feb checkout-api thread, ask the bot to 'catch me up'"
    )
    grey("   5. Try /runbook rds-password-rotation in #ask-platform")
    print()
    print(f"  {C.BOLD}On-demand alert injection:{C.RESET}")
    grey(
        f"   uv run python -m scripts.testenv.inject_alert --tenant {tenant_id} --type pagerduty"
    )
    print()
    return 0


def _main() -> int:
    parser = argparse.ArgumentParser(
        description="Bootstrap the Agent manual-test environment.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--tenant",
        required=True,
        help="Tenant id (from the OAuth callback URL, e.g. slack-t0xxxxxxx).",
    )
    parser.add_argument(
        "--region",
        default=os.getenv("AWS_REGION", DEFAULT_REGION),
        help="AWS region (default: us-west-2 or AWS_REGION).",
    )
    parser.add_argument(
        "--bridge-url",
        default=os.getenv("BRIDGE_BASE_URL", DEFAULT_BRIDGE_URL),
        help=f"Bridge public URL (default: {DEFAULT_BRIDGE_URL}).",
    )
    parser.add_argument(
        "--github-org",
        default=os.getenv("TESTENV_GITHUB_ORG"),
        help="GitHub org where acme-data-api / acme-infra / acme-runbooks live.",
    )
    parser.add_argument(
        "--github-installation-id",
        default=os.getenv("TESTENV_GITHUB_INSTALLATION_ID"),
        help="Numeric GitHub App installation id (from the install callback URL).",
    )
    parser.add_argument(
        "--skip-seed",
        action="store_true",
        help="Skip the Slack history seeding step (config-only run).",
    )
    parser.add_argument(
        "--skip-patch",
        action="store_true",
        help="Skip the config PATCH step (seed-only run).",
    )
    parser.add_argument(
        "--integrations",
        default="",
        help=(
            "Comma-separated list of external integrations to seed. "
            "Valid: pagerduty, jira, linear, sentry. "
            "Use 'all' to seed every supported integration. "
            "Datadog content seeding is a separate, explicit command. "
            "Requires credentials in Secrets Manager at "
            "agentcore/testenv/<integration> — see "
            "scripts/testenv/integrations/README.md for setup."
        ),
    )
    args = parser.parse_args()

    # Parse integrations list
    try:
        integrations = parse_integrations(args.integrations)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    return run_bootstrap(
        tenant_id=args.tenant,
        region=args.region,
        bridge_url=args.bridge_url,
        github_org=args.github_org,
        github_installation_id=args.github_installation_id,
        skip_seed=args.skip_seed,
        skip_patch=args.skip_patch,
        integrations=integrations,
    )


if __name__ == "__main__":
    sys.exit(_main())
