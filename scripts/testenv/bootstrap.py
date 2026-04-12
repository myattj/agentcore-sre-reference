#!/usr/bin/env python3
"""One-shot orchestrator for the AgentCore Reference test-env rig.

Idempotent. Safe to re-run. Expects the manual steps from
``scripts/testenv/README.md`` to be done first (Slack workspace
created, AgentCore Reference app installed, channels created, GitHub App
installed optionally).

What it does, in order:

  1. Verify the tenant row exists in the prod ``tenants`` DynamoDB
     table. If not, print the install URL and exit.
  2. Verify the bot token is in Secrets Manager.
  3. Load the shared ``BRIDGE_OAUTH_STATE_SECRET`` from Secrets Manager
     (same secret the bridge Fargate task uses) and mint a session
     token locally.
  4. Discover + join the expected test channels via Slack API.
  5. PATCH the rich ``build_testenv_config()`` dict via the bridge's
     ``/api/tenants/{id}`` — this exercises the real validation path.
  6. Run all the seed packs (``seed_slack_history.seed_all``).
  7. Print a ready-to-test summary.

Run via ``scripts/testenv-bootstrap.sh`` (bridge venv launcher).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

# Ensure the bridge package is importable (for make_session_token).
# Mirrors scripts/smoke.py's sys.path hack.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_BRIDGE_DIR = _REPO_ROOT / "bridge"
if str(_BRIDGE_DIR) not in sys.path:
    sys.path.insert(0, str(_BRIDGE_DIR))

from ._channels import discover_and_join  # noqa: E402
from ._common import (  # noqa: E402
    configure_logging,
    load_bot_token,
    make_slack_client,
)
from ._state import SeederState  # noqa: E402
from .config import TESTENV_CHANNELS, build_testenv_config  # noqa: E402
from .seed_slack_history import seed_all  # noqa: E402

log = logging.getLogger(__name__)


DEFAULT_BRIDGE_URL = "https://agent.example.com"
DEFAULT_REGION = "us-west-2"


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
# Prod secrets + state
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
        f"account or you lack secretsmanager:ListSecrets. Set "
        f"BRIDGE_OAUTH_STATE_SECRET in the environment to skip this lookup."
    )


def load_state_secret(region: str) -> str:
    """Fetch BRIDGE_OAUTH_STATE_SECRET from Secrets Manager.

    The secret at ``agentcore/services/bridge-<suffix>`` has a JSON
    blob; we extract the ``BRIDGE_OAUTH_STATE_SECRET`` key. Falls back
    to the env var if set, which is useful for local dev.
    """
    env_override = os.getenv("BRIDGE_OAUTH_STATE_SECRET")
    if env_override:
        log.info("using BRIDGE_OAUTH_STATE_SECRET from env")
        return env_override

    import boto3

    name = _find_bridge_secret_name(region)
    log.info("fetching state secret from %s", name)
    client = boto3.client("secretsmanager", region_name=region)
    resp = client.get_secret_value(SecretId=name)
    blob = resp.get("SecretString") or "{}"
    try:
        data = json.loads(blob)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"Secret {name} is not JSON: {e}. Expected a blob with key "
            f"BRIDGE_OAUTH_STATE_SECRET."
        ) from e
    secret = data.get("BRIDGE_OAUTH_STATE_SECRET")
    if not secret:
        raise RuntimeError(
            f"Secret {name} JSON has no BRIDGE_OAUTH_STATE_SECRET key. "
            f"Got keys: {sorted(data)}"
        )
    return secret


# ----------------------------------------------------------------------------
# Tenant verification + PATCH
# ----------------------------------------------------------------------------

def verify_tenant_exists(tenant_id: str, region: str) -> dict[str, Any]:
    """GetItem against prod ``tenants`` table. Returns the config dict.
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
            f"Install the AgentCore Reference Slack app to your test workspace first: "
            f"{os.getenv('BRIDGE_BASE_URL', DEFAULT_BRIDGE_URL)}/slack/install"
        )
    config = item.get("config") or {}
    return dict(config)  # type: ignore[return-value]


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
    url = f"{bridge_url.rstrip('/')}/api/tenants/{tenant_id}"

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

_ALL_INTEGRATIONS = ("datadog", "pagerduty", "jira", "linear", "sentry")


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
            if name == "datadog":
                from .integrations.seed_datadog import run_seed as run_dd
                results[name] = run_dd(
                    tenant_id, region=region, bridge_url=bridge_url
                )
            elif name == "pagerduty":
                from .integrations.seed_pagerduty import run_seed as run_pd
                results[name] = run_pd(
                    tenant_id, region=region, bridge_url=bridge_url
                )
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

    # ----- Step 2: bot token -----
    step("Verifying Slack bot token in Secrets Manager")
    try:
        bot_token = load_bot_token(tenant_id, region=region)
    except RuntimeError as e:
        err(str(e))
        return 1
    ok("bot token loaded")

    # ----- Step 3: Slack client + channels -----
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
    ok(f"{len(channel_map)} channels ready: {', '.join('#' + n for n in TESTENV_CHANNELS)}")

    # ----- Step 4: build + PATCH config -----
    if skip_patch:
        warn("--skip-patch: leaving tenant config unchanged")
    else:
        step("Building rich Acme Data Co config + PATCHing bridge")
        config_dict = build_testenv_config(
            tenant_id=tenant_id,
            channel_map=channel_map,
            github_org=github_org,
            github_installation_id=github_installation_id,
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

    # ----- Step 5: seed Slack history -----
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

    # ----- Step 6: external integrations (optional) -----
    integration_results: dict[str, int] = {}
    if integrations:
        step(f"Running {len(integrations)} integration seeder(s): {', '.join(integrations)}")
        integration_results = _run_integration_seeders(
            tenant_id,
            region=region,
            bridge_url=bridge_url,
            integrations=integrations,
        )

    # ----- Step 7: summary -----
    step("Test env is ready")
    print()
    print(f"  {C.BOLD}Tenant:{C.RESET}   {tenant_id}")
    print(f"  {C.BOLD}Bridge:{C.RESET}   {bridge_url}")
    print(f"  {C.BOLD}Channels:{C.RESET} {', '.join('#' + n for n in TESTENV_CHANNELS)}")
    print(f"  {C.BOLD}Metrics:{C.RESET}  {bridge_url.replace('/api', '')}/workspace/{tenant_id}/metrics")
    if integration_results:
        print(f"  {C.BOLD}Integrations:{C.RESET}")
        for name, code in integration_results.items():
            status = f"{C.GREEN}✓{C.RESET}" if code == 0 else f"{C.RED}✗{C.RESET}"
            print(f"    {status} {name}")
    print()
    print(f"  {C.BOLD}Try this first:{C.RESET}")
    grey("   1. Open your test Slack workspace")
    grey("   2. Go to #ask-data and ask: 'what does the team think about dbt vs airflow?'")
    grey("   3. Go to #alerts-sre, find the unacked P2 alert, @mention the bot")
    grey("   4. Go to #incidents, find the Feb checkout-api thread, ask the bot to 'catch me up'")
    grey("   5. Try /runbook rds-password-rotation in #ask-platform")
    print()
    print(f"  {C.BOLD}On-demand alert injection:{C.RESET}")
    grey(f"   uv run python -m scripts.testenv.inject_alert --tenant {tenant_id} --type pagerduty")
    print()
    return 0


def _main() -> int:
    parser = argparse.ArgumentParser(
        description="Bootstrap the AgentCore Reference manual-test environment.",
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
            "Valid: datadog, pagerduty, jira, linear, sentry. "
            "Use 'all' to seed every supported integration. "
            "Requires credentials in Secrets Manager at "
            "agentcore/testenv/<integration> — see "
            "scripts/testenv/integrations/README.md for setup."
        ),
    )
    args = parser.parse_args()

    # Parse integrations list
    integrations: list[str] = []
    if args.integrations:
        if args.integrations.strip().lower() == "all":
            integrations = list(_ALL_INTEGRATIONS)
        else:
            raw = [i.strip().lower() for i in args.integrations.split(",") if i.strip()]
            bad = [i for i in raw if i not in _ALL_INTEGRATIONS]
            if bad:
                print(
                    f"error: unknown integration(s): {bad}. "
                    f"Valid: {list(_ALL_INTEGRATIONS)}",
                    file=sys.stderr,
                )
                return 1
            integrations = raw

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
