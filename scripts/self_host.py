#!/usr/bin/env python3
"""Guided, fail-closed deployment for a self-hosted Agent installation.

This command deliberately orchestrates the repository's existing deployment
interfaces instead of reimplementing them. It never accepts AWS access keys;
developers authenticate through the normal AWS CLI/SDK credential chain.

The default path deploys the supported core product:

    data + alarms -> AgentCore runtime -> bridge/onboarding -> shared Gateway

The experimental PR sandbox remains disabled. AgentCore Memory is optional and
is not required for the first Slack response.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import secrets
import shutil
import ssl
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Mapping, Sequence

try:
    from scripts import configure_aws
except ImportError:  # Executed directly from scripts/.
    import configure_aws  # type: ignore[no-redef]


ROOT = Path(__file__).resolve().parent.parent
AGENTCORE_VERSION = "0.24.1"
SLACK_SECRET_NAME = "agentcore/services/slack"
BRIDGE_SECRET_NAME = "agentcore/services/bridge"
STATE_DIR = ROOT / ".self-host"
GENERATED_MANIFEST = STATE_DIR / "slack_manifest.json"

_DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)
_CERTIFICATE_ARN_RE = re.compile(
    r"^arn:(?P<partition>aws(?:-us-gov)?):acm:(?P<region>[a-z0-9-]+):"
    r"(?P<account>[0-9]{12}):certificate/[0-9a-fA-F-]+$"
)
_SLACK_APP_ID_RE = re.compile(r"^A[A-Z0-9]{8,}$")
_SLACK_SIGNING_SECRET_RE = re.compile(r"^[a-f0-9]{32}$")
_SLACK_CLIENT_ID_RE = re.compile(r"^[0-9]+\.[0-9]+$")


class SelfHostError(RuntimeError):
    """A configuration or deployment failure with an actionable message."""


@dataclass(frozen=True)
class DeploymentConfig:
    profile: str | None
    region: str
    account: str
    partition: str
    domain: str
    certificate_arn: str

    @property
    def public_url(self) -> str:
        return f"https://{self.domain}"

    @property
    def aws_env(self) -> dict[str, str]:
        values = {
            "AWS_REGION": self.region,
            "AWS_DEFAULT_REGION": self.region,
        }
        if self.profile:
            values["AWS_PROFILE"] = self.profile
            values["AWS_DEFAULT_PROFILE"] = self.profile
        return values


@dataclass(frozen=True)
class SecretDocument:
    arn: str
    payload: dict[str, str]
    existed: bool


class CommandRunner:
    """Run commands with one selected AWS identity and visible progress."""

    def __init__(self, base_env: Mapping[str, str] | None = None) -> None:
        self.base_env = dict(base_env or {})

    def run(
        self,
        command: Sequence[str],
        *,
        cwd: Path = ROOT,
        capture: bool = False,
        allow_failure: bool = False,
        quiet: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        if not quiet:
            print(f"\n  $ {_display_command(command)}", flush=True)
        env = os.environ.copy()
        env.update(self.base_env)
        result = subprocess.run(
            list(command),
            cwd=cwd,
            env=env,
            check=False,
            text=True,
            capture_output=capture,
        )
        if result.returncode != 0 and not allow_failure:
            detail = (result.stderr or result.stdout or "").strip().splitlines()
            suffix = f": {detail[-1]}" if detail else ""
            raise SelfHostError(
                f"command failed ({result.returncode}): {_display_command(command)}{suffix}"
            )
        return result


def _display_command(command: Sequence[str]) -> str:
    """Render a command for humans without ever including secret values."""
    return " ".join(str(part) for part in command)


def normalize_domain(raw: str) -> str:
    domain = raw.strip().lower().rstrip(".")
    if "://" in domain or "/" in domain or not _DOMAIN_RE.fullmatch(domain):
        raise SelfHostError(
            "domain must be a DNS hostname such as agent.example.com, without a URL path"
        )
    return domain


def validate_certificate_arn(
    arn: str, *, account: str, region: str, partition: str
) -> str:
    value = arn.strip()
    match = _CERTIFICATE_ARN_RE.fullmatch(value)
    if not match:
        raise SelfHostError("certificate ARN is not a valid ACM certificate ARN")
    expected = (partition, region, account)
    actual = (
        match.group("partition"),
        match.group("region"),
        match.group("account"),
    )
    if actual != expected:
        raise SelfHostError(
            "certificate ARN must use the selected AWS account, partition, and region"
        )
    return value


def validate_slack_payload(payload: Mapping[str, str]) -> dict[str, str]:
    values = {
        key: str(payload.get(key, "")).strip()
        for key in (
            "SLACK_CLIENT_ID",
            "SLACK_CLIENT_SECRET",
            "SLACK_SIGNING_SECRET",
            "SLACK_APP_ID",
        )
    }
    if not _SLACK_CLIENT_ID_RE.fullmatch(values["SLACK_CLIENT_ID"]):
        raise SelfHostError("Slack client ID must look like 123456789.123456789")
    if len(values["SLACK_CLIENT_SECRET"]) < 16 or any(
        character.isspace() for character in values["SLACK_CLIENT_SECRET"]
    ):
        raise SelfHostError("Slack client secret is missing or malformed")
    if not _SLACK_SIGNING_SECRET_RE.fullmatch(values["SLACK_SIGNING_SECRET"]):
        raise SelfHostError("Slack signing secret must be 32 lowercase hex characters")
    if not _SLACK_APP_ID_RE.fullmatch(values["SLACK_APP_ID"]):
        raise SelfHostError("Slack app ID must begin with A and contain only A-Z/0-9")
    return values


def validate_bridge_payload(payload: Mapping[str, object]) -> dict[str, str]:
    required = (
        "BRIDGE_OAUTH_STATE_SECRET",
        "BRIDGE_GATEWAY_JWT_PRIVATE_KEY_PEM",
        "ADMIN_SECRET",
    )
    values = {key: payload.get(key) for key in required}
    if not all(isinstance(value, str) for value in values.values()):
        raise SelfHostError("existing bridge secret is missing required string fields")
    result = {key: str(value) for key, value in values.items()}
    if len(result["BRIDGE_OAUTH_STATE_SECRET"]) < 32:
        raise SelfHostError("existing bridge OAuth state secret is too short")
    if len(result["ADMIN_SECRET"]) < 32:
        raise SelfHostError("existing admin secret is too short")
    private_key = result["BRIDGE_GATEWAY_JWT_PRIVATE_KEY_PEM"]
    if (
        "BEGIN PRIVATE KEY" not in private_key
        and "BEGIN RSA PRIVATE KEY" not in private_key
    ):
        raise SelfHostError("existing Gateway JWT key is not a PEM private key")
    return result


def generated_slack_manifest(domain: str) -> dict[str, object]:
    template = json.loads(
        (ROOT / "bridge/slack_manifest.json").read_text(encoding="utf-8")
    )
    public_url = f"https://{domain}"
    template["_comment"] = (
        "Generated by make self-host. Import this manifest at api.slack.com/apps, "
        "then provide the resulting app credentials to the installer."
    )
    template["oauth_config"]["redirect_urls"] = [f"{public_url}/slack/oauth/callback"]
    settings = template["settings"]
    settings["event_subscriptions"]["request_url"] = f"{public_url}/slack/events"
    settings["interactivity"]["request_url"] = f"{public_url}/slack/interactions"
    return template


def write_generated_manifest(
    domain: str, destination: Path = GENERATED_MANIFEST
) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink():
        raise SelfHostError(f"refusing to replace symlink: {destination}")
    content = json.dumps(generated_slack_manifest(domain), indent=2) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        os.chmod(destination, stat.S_IRUSR | stat.S_IWUSR)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return destination


def _aws_command(config: DeploymentConfig, *parts: str) -> list[str]:
    command = ["aws"]
    if config.profile:
        command.extend(("--profile", config.profile))
    command.extend(("--region", config.region, *parts))
    return command


def _decode_secret(
    result: subprocess.CompletedProcess[str], name: str
) -> SecretDocument:
    try:
        document = json.loads(result.stdout)
        payload = json.loads(document["SecretString"])
        arn = document["ARN"]
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise SelfHostError(
            f"Secrets Manager returned invalid JSON for {name}"
        ) from exc
    if not isinstance(payload, dict) or not isinstance(arn, str):
        raise SelfHostError(f"Secrets Manager returned an invalid value for {name}")
    return SecretDocument(
        arn=arn,
        payload={str(key): str(value) for key, value in payload.items()},
        existed=True,
    )


def read_secret(
    runner: CommandRunner, config: DeploymentConfig, name: str
) -> SecretDocument | None:
    result = runner.run(
        _aws_command(
            config,
            "secretsmanager",
            "get-secret-value",
            "--secret-id",
            name,
            "--output",
            "json",
            "--no-cli-pager",
        ),
        capture=True,
        allow_failure=True,
        quiet=True,
    )
    if result.returncode == 0:
        return _decode_secret(result, name)
    detail = f"{result.stderr}\n{result.stdout}"
    if "ResourceNotFoundException" in detail:
        return None
    raise SelfHostError(
        f"could not read Secrets Manager secret {name}; verify secretsmanager permissions"
    )


def put_secret(
    runner: CommandRunner,
    config: DeploymentConfig,
    name: str,
    payload: Mapping[str, str],
    *,
    existed: bool,
) -> SecretDocument:
    descriptor, temporary_name = tempfile.mkstemp(prefix="agent-self-host-secret-")
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(dict(payload), handle)
            handle.flush()
            os.fsync(handle.fileno())
        action = "put-secret-value" if existed else "create-secret"
        identifier = "--secret-id" if existed else "--name"
        runner.run(
            _aws_command(
                config,
                "secretsmanager",
                action,
                identifier,
                name,
                "--secret-string",
                f"file://{temporary}",
                "--output",
                "json",
                "--no-cli-pager",
            ),
            capture=True,
            quiet=True,
        )
    finally:
        temporary.unlink(missing_ok=True)

    stored = read_secret(runner, config, name)
    if stored is None:
        raise SelfHostError(f"secret {name} was not readable after it was written")
    return SecretDocument(arn=stored.arn, payload=dict(payload), existed=existed)


def generate_gateway_private_key(runner: CommandRunner) -> str:
    if shutil.which("openssl") is None:
        raise SelfHostError(
            "OpenSSL is required to generate the Gateway JWT signing key"
        )
    descriptor, temporary_name = tempfile.mkstemp(
        prefix="agent-gateway-key-", suffix=".pem"
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
        runner.run(
            (
                "openssl",
                "genpkey",
                "-algorithm",
                "RSA",
                "-pkeyopt",
                "rsa_keygen_bits:2048",
                "-out",
                str(temporary),
            ),
            capture=True,
            quiet=True,
        )
        value = temporary.read_text(encoding="utf-8")
    finally:
        temporary.unlink(missing_ok=True)
    if "BEGIN PRIVATE KEY" not in value:
        raise SelfHostError("OpenSSL did not produce a PEM private key")
    return value


def _prompt(label: str, *, default: str | None = None, secret: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    reader = getpass.getpass if secret else input
    value = reader(f"{label}{suffix}: ").strip()
    return value or (default or "")


def collect_slack_payload(existing: SecretDocument | None) -> dict[str, str]:
    current = dict(existing.payload) if existing else {}
    environment = {
        key: os.environ.get(key, "")
        for key in (
            "SLACK_CLIENT_ID",
            "SLACK_CLIENT_SECRET",
            "SLACK_SIGNING_SECRET",
            "SLACK_APP_ID",
        )
    }
    provided = {key: value for key, value in environment.items() if value}
    if existing and not provided:
        print(f"  Reusing existing {SLACK_SECRET_NAME} secret.")
        return validate_slack_payload(current)

    values = current | provided
    prompts = (
        ("SLACK_CLIENT_ID", "Slack client ID", False),
        ("SLACK_CLIENT_SECRET", "Slack client secret", True),
        ("SLACK_SIGNING_SECRET", "Slack signing secret", True),
        ("SLACK_APP_ID", "Slack app ID", False),
    )
    for key, label, is_secret in prompts:
        if not values.get(key):
            values[key] = _prompt(label, secret=is_secret)
    return validate_slack_payload(values)


def collect_bridge_payload(
    runner: CommandRunner, existing: SecretDocument | None
) -> dict[str, str]:
    if existing:
        print(f"  Reusing existing {BRIDGE_SECRET_NAME} secret.")
        return validate_bridge_payload(existing.payload)
    return {
        "BRIDGE_OAUTH_STATE_SECRET": secrets.token_hex(32),
        "BRIDGE_GATEWAY_JWT_PRIVATE_KEY_PEM": generate_gateway_private_key(runner),
        "ADMIN_SECRET": secrets.token_urlsafe(48),
    }


def _require_tools() -> None:
    missing = [
        name
        for name in ("aws", "docker", "jq", "node", "npm", "npx", "uv")
        if shutil.which(name) is None
    ]
    if missing:
        raise SelfHostError(
            f"missing deployment tools: {', '.join(missing)}; run make doctor"
        )
    docker = subprocess.run(
        ["docker", "info"], check=False, capture_output=True, text=True
    )
    if docker.returncode != 0:
        raise SelfHostError("Docker is installed but its daemon is not running")


def _confirm(config: DeploymentConfig, *, assume_yes: bool) -> None:
    if assume_yes:
        return
    print(
        "\nThis deployment creates billable AWS resources, including an ALB, ECS/Fargate,"
    )
    print("AgentCore Runtime, DynamoDB, CloudWatch, ECR, and Secrets Manager.")
    expected = f"deploy {config.account}/{config.region}"
    actual = input(f"Type {expected!r} to continue: ").strip()
    if actual != expected:
        raise SelfHostError("deployment cancelled; no billable resources were changed")


@contextmanager
def _pinned_agentcore_cli() -> Iterator[Path]:
    """Expose the pinned npx package as `agentcore` for deploy_agent.sh."""
    with tempfile.TemporaryDirectory(prefix="agent-self-host-cli-") as temporary:
        directory = Path(temporary)
        executable = directory / "agentcore"
        executable.write_text(
            f'#!/bin/sh\nexec npx --yes @aws/agentcore@{AGENTCORE_VERSION} "$@"\n',
            encoding="utf-8",
        )
        executable.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        yield directory


def _cdk_deploy(
    runner: CommandRunner,
    stack_names: Sequence[str],
    config: DeploymentConfig,
    *contexts: tuple[str, str],
) -> None:
    command = ["npx", "cdk", "deploy", *stack_names, "--require-approval", "never"]
    command.extend(("--context", f"region={config.region}"))
    for key, value in contexts:
        command.extend(("--context", f"{key}={value}"))
    runner.run(command, cwd=ROOT / "infra/data")


def _stack_outputs(
    runner: CommandRunner, config: DeploymentConfig, stack_name: str
) -> dict[str, str]:
    result = runner.run(
        _aws_command(
            config,
            "cloudformation",
            "describe-stacks",
            "--stack-name",
            stack_name,
            "--query",
            "Stacks[0].Outputs",
            "--output",
            "json",
            "--no-cli-pager",
        ),
        capture=True,
        quiet=True,
    )
    try:
        rows = json.loads(result.stdout)
        return {
            row["OutputKey"]: row["OutputValue"]
            for row in rows
            if isinstance(row, dict) and "OutputKey" in row and "OutputValue" in row
        }
    except (TypeError, json.JSONDecodeError) as exc:
        raise SelfHostError(
            f"CloudFormation returned invalid outputs for {stack_name}"
        ) from exc


def _verify_health(public_url: str, *, attempts: int = 20, delay: float = 6.0) -> bool:
    context = ssl.create_default_context()
    url = f"{public_url}/healthz"
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(url, timeout=10, context=context) as response:
                if response.status == 200:
                    return True
        except (urllib.error.URLError, TimeoutError, ssl.SSLError):
            pass
        if attempt < attempts:
            time.sleep(delay)
    return False


def _write_state(config: DeploymentConfig, outputs: Mapping[str, str]) -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    destination = STATE_DIR / "deployment.json"
    if destination.is_symlink():
        raise SelfHostError(f"refusing to replace symlink: {destination}")
    payload = {
        "account": config.account,
        "partition": config.partition,
        "region": config.region,
        "domain": config.domain,
        "public_url": config.public_url,
        "services_stack": f"AgentCore-coreAgent-services-{config.region}",
        "alb_dns_name": outputs.get("AlbDnsName", ""),
    }
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        os.chmod(destination, stat.S_IRUSR | stat.S_IWUSR)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return destination


def deploy(args: argparse.Namespace) -> int:
    _require_tools()
    if not args.skip_setup:
        CommandRunner().run((str(ROOT / "scripts/setup.sh"),))

    target, bootstrap_version = configure_aws.inspect_target(
        profile=args.profile,
        explicit_region=args.region,
        environ=os.environ,
        verify_agentcore=True,
    )
    if target.partition != "aws":
        raise SelfHostError(
            "the guided path currently supports commercial AWS only because the default "
            "Bedrock model is a global commercial inference profile"
        )
    destination, outcome = configure_aws.write_target(
        ROOT, target, force=args.force_target
    )
    print(f"\nAWS target {outcome}: {destination}")

    domain = normalize_domain(args.domain or _prompt("Public domain"))
    certificate = validate_certificate_arn(
        args.certificate_arn or _prompt("Validated ACM certificate ARN"),
        account=target.account,
        region=target.region,
        partition=target.partition,
    )
    config = DeploymentConfig(
        profile=target.profile,
        region=target.region,
        account=target.account,
        partition=target.partition,
        domain=domain,
        certificate_arn=certificate,
    )
    runner = CommandRunner(config.aws_env)

    manifest = write_generated_manifest(domain)
    print(f"\nSlack manifest ready: {manifest}")
    print("Import it at https://api.slack.com/apps before entering app credentials.")

    existing_slack = read_secret(runner, config, SLACK_SECRET_NAME)
    existing_bridge = read_secret(runner, config, BRIDGE_SECRET_NAME)
    slack_payload = collect_slack_payload(existing_slack)
    bridge_payload = collect_bridge_payload(runner, existing_bridge)

    _confirm(config, assume_yes=args.yes)

    if not bootstrap_version:
        runner.run(
            (
                "npx",
                "cdk",
                "bootstrap",
                f"aws://{config.account}/{config.region}",
            ),
            cwd=ROOT / "infra/data",
        )

    slack_secret = put_secret(
        runner,
        config,
        SLACK_SECRET_NAME,
        slack_payload,
        existed=existing_slack is not None,
    )
    bridge_secret = put_secret(
        runner,
        config,
        BRIDGE_SECRET_NAME,
        bridge_payload,
        existed=existing_bridge is not None,
    )

    data_stack = f"AgentCore-coreAgent-data-{config.region}"
    observability_stack = f"AgentCore-coreAgent-observability-{config.region}"
    services_stack = f"AgentCore-coreAgent-services-{config.region}"
    gateway_stack = f"AgentCore-coreAgent-gateway-{config.region}"

    runner.run(("npm", "run", "build"), cwd=ROOT / "infra/data")
    _cdk_deploy(runner, (data_stack, observability_stack), config)

    with _pinned_agentcore_cli() as cli_directory:
        runtime_runner = CommandRunner(
            config.aws_env
            | {
                "PATH": f"{cli_directory}{os.pathsep}{os.environ.get('PATH', '')}",
                "DOMAIN_NAME": config.domain,
                "GITHUB_APP_ID": "",
                "AGENTCORE_MEMORY_ID": "",
                "AGENTCORE_SEMANTIC_STRATEGY_ID": "",
                "AGENTCORE_USER_PREF_STRATEGY_ID": "",
                "DEPLOY_EXPERIMENTAL_SANDBOX": "false",
            }
        )
        runtime_runner.run(("bash", "scripts/deploy_agent.sh"))

    runtime_result = runner.run(
        ("bash", "scripts/resolve_agent_runtime.sh"), capture=True
    )
    runtime_arn = runtime_result.stdout.strip()
    if not runtime_arn.startswith("arn:"):
        raise SelfHostError("AgentCore runtime resolver did not return an ARN")
    runner.run(("bash", "infra/data/scripts/attach_agent_policy.sh"))

    _cdk_deploy(
        runner,
        (services_stack,),
        config,
        ("agentRuntimeArn", runtime_arn),
        ("certificateArn", config.certificate_arn),
        ("domainName", config.domain),
        ("slackSecretsArn", slack_secret.arn),
        ("bridgeSecretsArn", bridge_secret.arn),
    )

    runner.run(("bash", "scripts/build_interceptor_zip.sh"), cwd=ROOT / "infra/data")
    _cdk_deploy(
        runner,
        (gateway_stack,),
        config,
        ("bridgePublicUrl", config.public_url),
    )
    runner.run(
        (
            str(ROOT / "bridge/.venv/bin/python"),
            "scripts/provision_gateway.py",
            "--region",
            config.region,
        ),
        cwd=ROOT / "infra/data",
    )

    outputs = _stack_outputs(runner, config, services_stack)
    alb_dns = outputs.get("AlbDnsName")
    if not alb_dns:
        raise SelfHostError("services stack did not return AlbDnsName")
    state_path = _write_state(config, outputs)

    print("\nAWS deployment completed.")
    print(f"  DNS:      create an ALIAS/CNAME from {config.domain} to {alb_dns}")
    print(f"  Manifest: {manifest}")
    print(f"  State:    {state_path}")
    print(f"  Install:  {config.public_url}/slack/install")

    if args.skip_health_check:
        print("\nHealth verification skipped. Run:")
        print(f"  curl --fail {config.public_url}/healthz")
        return 0

    if not args.yes:
        input(
            "\nCreate the DNS record above, wait for it to resolve, then press Enter: "
        )
    print(f"Verifying {config.public_url}/healthz ...", flush=True)
    if not _verify_health(config.public_url):
        raise SelfHostError(
            "deployment finished but HTTPS health verification failed; check DNS, ACM, "
            "the ALB target groups, and ECS service events"
        )
    print("\nAgent is healthy. Open the install URL and add it to a Slack workspace.")
    return 0


def _dry_run(args: argparse.Namespace) -> int:
    domain = normalize_domain(args.domain or "agent.example.com")
    print("SELF-HOST DEPLOYMENT PLAN")
    print("=========================")
    print(f"Profile: {args.profile or 'default AWS credential chain'}")
    print(f"Region:  {args.region or 'AWS profile/environment'}")
    print(f"Domain:  {domain}")
    print(
        "\n1. Verify tools, Docker, AWS identity, region, AgentCore, and CDK bootstrap"
    )
    print("2. Generate a domain-specific Slack manifest")
    print("3. Create or reuse Slack and bridge secrets in AWS Secrets Manager")
    print("4. Deploy data tables and CloudWatch alarms")
    print("5. Validate and deploy the AgentCore runtime, then attach data IAM")
    print("6. Build and deploy the bridge/onboarding ECS services behind HTTPS")
    print("7. Deploy and provision the shared tenant-scoped AgentCore Gateway")
    print("8. Print the DNS target, verify /healthz, and open Slack installation")
    print("\nExperimental PR sandbox: disabled")
    print("AgentCore Memory: optional, not part of first-run deployment")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Deploy Agent into your own AWS account and Slack app."
    )
    parser.add_argument("--profile", help="AWS shared-config profile")
    parser.add_argument("--region", help="supported AgentCore AWS region")
    parser.add_argument(
        "--domain", help="public hostname, for example agent.example.com"
    )
    parser.add_argument("--certificate-arn", help="validated ACM certificate ARN")
    parser.add_argument(
        "--dry-run", action="store_true", help="print the deployment phases only"
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="skip the typed billable-resource confirmation (for automation)",
    )
    parser.add_argument(
        "--skip-setup", action="store_true", help="reuse already-installed dependencies"
    )
    parser.add_argument(
        "--skip-health-check",
        action="store_true",
        help="finish after deployment without waiting for DNS/HTTPS",
    )
    parser.add_argument(
        "--force-target",
        action="store_true",
        help="replace an existing aws-targets.json selecting another account/region",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.dry_run:
            return _dry_run(args)
        return deploy(args)
    except (SelfHostError, configure_aws.AwsConfigurationError, OSError) as exc:
        print(f"self-host deployment failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
