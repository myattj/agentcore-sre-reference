#!/usr/bin/env python3
"""Verify an AWS identity and write the ignored AgentCore deployment target."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence


_ACCOUNT_RE = re.compile(r"^[0-9]{12}$")
_REGION_RE = re.compile(r"^[a-z]{2}(?:-[a-z0-9]+)+-[0-9]+$")
_SUPPORTED_AGENTCORE_REGIONS = frozenset(
    Path(__file__)
    .with_name("agentcore_cli_regions.txt")
    .read_text(encoding="utf-8")
    .splitlines()
)
_SUPPORTED_PARTITIONS = {"aws", "aws-us-gov"}
_TARGET_RELATIVE_PATH = Path("coreAgent/agentcore/aws-targets.json")
_DEFAULT_AWS_CLI_TIMEOUT_SECONDS = 30.0


class AwsConfigurationError(RuntimeError):
    """The selected AWS profile cannot safely configure this deployment."""


def _aws_cli_timeout(environ: Mapping[str, str] = os.environ) -> float:
    raw_value = environ.get(
        "AGENTCORE_AWS_CLI_TIMEOUT_SECONDS",
        str(_DEFAULT_AWS_CLI_TIMEOUT_SECONDS),
    )
    try:
        timeout = float(raw_value)
    except ValueError as exc:
        raise AwsConfigurationError(
            "AGENTCORE_AWS_CLI_TIMEOUT_SECONDS must be a positive number"
        ) from exc
    if not math.isfinite(timeout) or timeout <= 0:
        raise AwsConfigurationError(
            "AGENTCORE_AWS_CLI_TIMEOUT_SECONDS must be a positive number"
        )
    return timeout


@dataclass(frozen=True)
class AwsTarget:
    account: str
    region: str
    partition: str
    caller_arn: str
    profile: str | None


def _run_command(
    command: Sequence[str], *, allow_failure: bool = False
) -> subprocess.CompletedProcess[str]:
    timeout = _aws_cli_timeout()
    try:
        result = subprocess.run(
            list(command),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise AwsConfigurationError(
            f"AWS CLI command timed out after {timeout:g} seconds"
        ) from exc
    if result.returncode != 0 and not allow_failure:
        detail = (result.stderr or result.stdout).strip().splitlines()
        message = detail[-1] if detail else f"exit status {result.returncode}"
        raise AwsConfigurationError(f"AWS CLI command failed: {message}")
    return result


def _aws_prefix(profile: str | None, region: str | None = None) -> list[str]:
    command = ["aws"]
    if profile:
        command.extend(("--profile", profile))
    if region:
        command.extend(("--region", region))
    return command


def _configured_region(profile: str | None) -> str | None:
    command = ["aws", "configure", "get", "region"]
    if profile:
        command.extend(("--profile", profile))
    result = _run_command(command, allow_failure=True)
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _resolve_profile(
    explicit_profile: str | None,
    environ: Mapping[str, str],
) -> str | None:
    return (
        explicit_profile
        or environ.get("AWS_PROFILE")
        or environ.get("AWS_DEFAULT_PROFILE")
        or None
    )


def _resolve_region(
    explicit_region: str | None,
    profile: str | None,
    environ: Mapping[str, str],
) -> str:
    region = (
        explicit_region
        or environ.get("AWS_REGION")
        or environ.get("AWS_DEFAULT_REGION")
        or _configured_region(profile)
    )
    if not region:
        raise AwsConfigurationError(
            "no AWS region is configured; pass --region or set AWS_REGION"
        )
    if not _REGION_RE.fullmatch(region):
        raise AwsConfigurationError(f"invalid AWS region: {region!r}")
    if region not in _SUPPORTED_AGENTCORE_REGIONS:
        raise AwsConfigurationError(
            f"AWS region {region!r} is not supported by the pinned AgentCore CLI; "
            "choose a region listed in scripts/agentcore_cli_regions.txt"
        )
    return region


def _load_identity(profile: str | None, region: str) -> AwsTarget:
    command = _aws_prefix(profile, region)
    command.extend(
        ("sts", "get-caller-identity", "--output", "json", "--no-cli-pager")
    )
    result = _run_command(command)
    try:
        identity = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise AwsConfigurationError("AWS STS returned invalid JSON") from exc

    account = identity.get("Account")
    caller_arn = identity.get("Arn")
    if not isinstance(account, str) or not _ACCOUNT_RE.fullmatch(account):
        raise AwsConfigurationError("AWS STS returned an invalid account ID")
    if not isinstance(caller_arn, str) or not caller_arn.startswith("arn:"):
        raise AwsConfigurationError("AWS STS returned an invalid caller ARN")

    arn_parts = caller_arn.split(":", 5)
    if len(arn_parts) != 6 or arn_parts[4] != account:
        raise AwsConfigurationError("AWS STS account and caller ARN disagree")
    partition = arn_parts[1]
    if partition not in _SUPPORTED_PARTITIONS:
        raise AwsConfigurationError(
            f"AWS partition {partition!r} is not supported by this reference stack; "
            "use a commercial AWS or GovCloud profile"
        )
    if region not in _SUPPORTED_AGENTCORE_REGIONS:
        raise AwsConfigurationError(
            f"AWS region {region!r} is not supported by the pinned AgentCore CLI"
        )
    if partition == "aws-us-gov" and region != "us-gov-west-1":
        raise AwsConfigurationError(
            f"GovCloud identity cannot target commercial region {region!r}"
        )
    if partition == "aws" and region == "us-gov-west-1":
        raise AwsConfigurationError(
            f"commercial AWS identity cannot target region {region!r}"
        )
    return AwsTarget(
        account=account,
        region=region,
        partition=partition,
        caller_arn=caller_arn,
        profile=profile,
    )


def _verify_agentcore(target: AwsTarget) -> None:
    command = _aws_prefix(target.profile, target.region)
    command.extend(
        (
            "bedrock-agentcore-control",
            "list-agent-runtimes",
            "--max-results",
            "1",
            "--no-paginate",
            "--output",
            "json",
            "--no-cli-pager",
        )
    )
    _run_command(command)


def _cdk_bootstrap_version(target: AwsTarget) -> str | None:
    command = _aws_prefix(target.profile, target.region)
    command.extend(
        (
            "ssm",
            "get-parameter",
            "--name",
            "/cdk-bootstrap/hnb659fds/version",
            "--query",
            "Parameter.Value",
            "--output",
            "text",
            "--no-cli-pager",
        )
    )
    result = _run_command(command, allow_failure=True)
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def inspect_target(
    *,
    profile: str | None,
    explicit_region: str | None,
    environ: Mapping[str, str],
    verify_agentcore: bool,
) -> tuple[AwsTarget, str | None]:
    if shutil.which("aws") is None:
        raise AwsConfigurationError("AWS CLI v2 is required; run make doctor")
    region = _resolve_region(explicit_region, profile, environ)
    target = _load_identity(profile, region)
    if verify_agentcore:
        _verify_agentcore(target)
    return target, _cdk_bootstrap_version(target)


def _target_document(target: AwsTarget) -> str:
    payload = [
        {
            "name": "default",
            "description": "AgentCore deployment target configured from AWS STS",
            "account": target.account,
            "region": target.region,
        }
    ]
    return json.dumps(payload, indent=2) + "\n"


def _target_selection(document: str) -> tuple[str, str, str] | None:
    try:
        payload = json.loads(document)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, list) or len(payload) != 1:
        return None
    entry = payload[0]
    if not isinstance(entry, dict):
        return None
    selection = (entry.get("name"), entry.get("account"), entry.get("region"))
    if not all(isinstance(value, str) for value in selection):
        return None
    return selection


def write_target(root: Path, target: AwsTarget, *, force: bool) -> tuple[Path, str]:
    destination = root.resolve() / _TARGET_RELATIVE_PATH
    destination.parent.mkdir(parents=True, exist_ok=True)
    content = _target_document(target)

    if destination.is_symlink():
        raise AwsConfigurationError(f"refusing to replace symlink: {destination}")
    if destination.exists():
        if not destination.is_file():
            raise AwsConfigurationError(f"deployment target is not a file: {destination}")
        existing = destination.read_text(encoding="utf-8")
        expected_selection = ("default", target.account, target.region)
        if existing == content or _target_selection(existing) == expected_selection:
            os.chmod(destination, stat.S_IRUSR | stat.S_IWUSR)
            return destination, "unchanged"
        if not force:
            raise AwsConfigurationError(
                f"{destination} already selects a different AWS target; "
                "rerun with --force to replace it"
            )

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
    return destination, "written"


def _print_report(
    target: AwsTarget,
    bootstrap_version: str | None,
    *,
    agentcore_verified: bool,
) -> None:
    print("AWS identity verified")
    print(f"  Account:   {target.account}")
    print(f"  Partition: {target.partition}")
    print(f"  Region:    {target.region}")
    print(f"  Profile:   {target.profile or 'default credential chain'}")
    if agentcore_verified:
        print("  AgentCore: control plane reachable")
    else:
        print("  AgentCore: check skipped")
    if bootstrap_version:
        print(f"  CDK:       bootstrapped (version {bootstrap_version})")
    else:
        print(
            "  CDK:       bootstrap not detected; run `cd infra/data && npx cdk bootstrap` "
            "or verify SSM read access"
        )
    if target.partition == "aws-us-gov":
        print(
            "  Warning: GovCloud feature availability differs; the default global "
            "Bedrock model and optional Memory/Gateway configuration must be reviewed."
        )


def _print_shell_exports(target: AwsTarget) -> None:
    print("\nUse the same identity for subsequent commands:")
    if target.profile:
        print(f"  export AWS_PROFILE={shlex.quote(target.profile)}")
    print(f"  export AWS_REGION={shlex.quote(target.region)}")
    print("  (cd infra/data && npx cdk bootstrap)")
    print("  make agent-deploy")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Verify the selected AWS identity/region and create the ignored "
            "AgentCore aws-targets.json file."
        )
    )
    parser.add_argument("--profile", help="AWS shared-config profile (default: SDK chain)")
    parser.add_argument("--region", help="AWS region (default: AWS env/profile)")
    parser.add_argument(
        "--check-only", action="store_true", help="verify access without writing a file"
    )
    parser.add_argument(
        "--force", action="store_true", help="replace an existing different target"
    )
    parser.add_argument(
        "--skip-agentcore-check",
        action="store_true",
        help="skip the read-only AgentCore endpoint/permission check",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="repository root (default: inferred from this script)",
    )
    args = parser.parse_args(argv)

    try:
        profile = _resolve_profile(args.profile, os.environ)
        target, bootstrap_version = inspect_target(
            profile=profile,
            explicit_region=args.region,
            environ=os.environ,
            verify_agentcore=not args.skip_agentcore_check,
        )
        _print_report(
            target,
            bootstrap_version,
            agentcore_verified=not args.skip_agentcore_check,
        )
        if not args.check_only:
            destination, outcome = write_target(args.root, target, force=args.force)
            print(f"\nDeployment target {outcome}: {destination}")
        _print_shell_exports(target)
    except (AwsConfigurationError, OSError) as exc:
        print(f"aws configuration failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
