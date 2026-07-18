from __future__ import annotations

import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "configure_aws", ROOT / "scripts/configure_aws.py"
)
assert SPEC and SPEC.loader
configure_aws = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = configure_aws
SPEC.loader.exec_module(configure_aws)


def completed(
    command: list[str], stdout: str = "", *, returncode: int = 0, stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, returncode, stdout, stderr)


class RegionResolutionTests(unittest.TestCase):
    def test_aws_cli_timeout_is_bounded_and_configurable(self) -> None:
        self.assertEqual(configure_aws._aws_cli_timeout({}), 30.0)
        self.assertEqual(
            configure_aws._aws_cli_timeout(
                {"AGENTCORE_AWS_CLI_TIMEOUT_SECONDS": "7.5"}
            ),
            7.5,
        )
        for invalid in ("0", "-1", "nan", "inf", "-inf", "not-a-number"):
            with (
                self.subTest(value=invalid),
                self.assertRaisesRegex(
                    configure_aws.AwsConfigurationError, "positive number"
                ),
            ):
                configure_aws._aws_cli_timeout(
                    {"AGENTCORE_AWS_CLI_TIMEOUT_SECONDS": invalid}
                )

    def test_aws_cli_timeout_is_reported_as_configuration_error(self) -> None:
        with (
            mock.patch.object(configure_aws, "_aws_cli_timeout", return_value=2.0),
            mock.patch.object(
                configure_aws.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired(["aws", "sts"], 2.0),
            ) as run,
            self.assertRaisesRegex(
                configure_aws.AwsConfigurationError, "timed out after 2 seconds"
            ),
        ):
            configure_aws._run_command(["aws", "sts"])

        self.assertEqual(run.call_args.kwargs["timeout"], 2.0)

    def test_explicit_profile_wins_and_aws_profile_is_supported(self) -> None:
        self.assertEqual(
            configure_aws._resolve_profile(
                "explicit", {"AWS_PROFILE": "environment"}
            ),
            "explicit",
        )
        self.assertEqual(
            configure_aws._resolve_profile(None, {"AWS_PROFILE": "environment"}),
            "environment",
        )
        self.assertEqual(
            configure_aws._resolve_profile(
                None, {"AWS_DEFAULT_PROFILE": "default-environment"}
            ),
            "default-environment",
        )

    def test_explicit_region_wins_over_environment_and_profile(self) -> None:
        with mock.patch.object(
            configure_aws, "_configured_region", return_value="ap-southeast-2"
        ):
            region = configure_aws._resolve_region(
                "eu-west-1",
                "sandbox",
                {"AWS_REGION": "us-east-1", "AWS_DEFAULT_REGION": "us-east-2"},
            )
        self.assertEqual(region, "eu-west-1")

    def test_profile_region_is_used_when_environment_is_empty(self) -> None:
        with mock.patch.object(
            configure_aws, "_configured_region", return_value="ap-southeast-2"
        ):
            region = configure_aws._resolve_region(None, "sandbox", {})
        self.assertEqual(region, "ap-southeast-2")

    def test_missing_or_malformed_region_fails_closed(self) -> None:
        with (
            mock.patch.object(configure_aws, "_configured_region", return_value=None),
            self.assertRaisesRegex(configure_aws.AwsConfigurationError, "no AWS region"),
        ):
            configure_aws._resolve_region(None, None, {})
        with self.assertRaisesRegex(
            configure_aws.AwsConfigurationError, "invalid AWS region"
        ):
            configure_aws._resolve_region("https://example.com", None, {})
        with self.assertRaisesRegex(
            configure_aws.AwsConfigurationError, "pinned AgentCore CLI"
        ):
            configure_aws._resolve_region("eu-west-2", None, {})


class IdentityTests(unittest.TestCase):
    def test_commercial_identity_is_loaded_from_sts(self) -> None:
        response = completed(
            [],
            json.dumps(
                {
                    "Account": "123456789012",
                    "Arn": "arn:aws:sts::123456789012:assumed-role/Deploy/session",
                    "UserId": "synthetic",
                }
            ),
        )
        with mock.patch.object(configure_aws, "_run_command", return_value=response):
            target = configure_aws._load_identity("sandbox", "eu-west-1")

        self.assertEqual(target.account, "123456789012")
        self.assertEqual(target.partition, "aws")
        self.assertEqual(target.region, "eu-west-1")
        self.assertEqual(target.profile, "sandbox")

    def test_partition_and_region_mismatch_is_rejected(self) -> None:
        response = completed(
            [],
            json.dumps(
                {
                    "Account": "123456789012",
                    "Arn": "arn:aws-us-gov:sts::123456789012:assumed-role/Deploy/session",
                }
            ),
        )
        with (
            mock.patch.object(configure_aws, "_run_command", return_value=response),
            self.assertRaisesRegex(
                configure_aws.AwsConfigurationError, "GovCloud identity"
            ),
        ):
            configure_aws._load_identity(None, "us-west-2")

    def test_commercial_identity_rejects_sovereign_region(self) -> None:
        response = completed(
            [],
            json.dumps(
                {
                    "Account": "123456789012",
                    "Arn": "arn:aws:sts::123456789012:assumed-role/Deploy/session",
                }
            ),
        )
        with (
            mock.patch.object(configure_aws, "_run_command", return_value=response),
            self.assertRaisesRegex(
                configure_aws.AwsConfigurationError, "pinned AgentCore CLI"
            ),
        ):
            configure_aws._load_identity(None, "us-iso-east-1")

    def test_unsupported_partition_is_rejected(self) -> None:
        response = completed(
            [],
            json.dumps(
                {
                    "Account": "123456789012",
                    "Arn": "arn:aws-cn:sts::123456789012:assumed-role/Deploy/session",
                }
            ),
        )
        with (
            mock.patch.object(configure_aws, "_run_command", return_value=response),
            self.assertRaisesRegex(
                configure_aws.AwsConfigurationError, "not supported"
            ),
        ):
            configure_aws._load_identity(None, "cn-north-1")


class TargetFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.target = configure_aws.AwsTarget(
            account="123456789012",
            region="eu-west-1",
            partition="aws",
            caller_arn="arn:aws:sts::123456789012:assumed-role/Deploy/session",
            profile="sandbox",
        )

    def test_target_is_written_atomically_with_private_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination, outcome = configure_aws.write_target(
                root, self.target, force=False
            )

            self.assertEqual(outcome, "written")
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
            payload = json.loads(destination.read_text(encoding="utf-8"))
            self.assertEqual(payload[0]["account"], "123456789012")
            self.assertEqual(payload[0]["region"], "eu-west-1")

    def test_different_existing_target_requires_force(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination, _ = configure_aws.write_target(root, self.target, force=False)
            changed = configure_aws.AwsTarget(
                **{**self.target.__dict__, "region": "us-east-1"}
            )

            with self.assertRaisesRegex(
                configure_aws.AwsConfigurationError, "--force"
            ):
                configure_aws.write_target(root, changed, force=False)
            self.assertEqual(
                json.loads(destination.read_text(encoding="utf-8"))[0]["region"],
                "eu-west-1",
            )

            destination, outcome = configure_aws.write_target(
                root, changed, force=True
            )
            self.assertEqual(outcome, "written")
            self.assertEqual(
                json.loads(destination.read_text(encoding="utf-8"))[0]["region"],
                "us-east-1",
            )

    def test_equivalent_existing_target_does_not_require_force(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / "coreAgent/agentcore/aws-targets.json"
            destination.parent.mkdir(parents=True)
            payload = json.loads(
                (ROOT / "coreAgent/agentcore/aws-targets.example.json").read_text(
                    encoding="utf-8"
                )
            )
            payload[0]["account"] = self.target.account
            payload[0]["region"] = self.target.region
            existing = json.dumps(payload, separators=(",", ":"))
            destination.write_text(existing, encoding="utf-8")
            os.chmod(destination, 0o644)

            returned, outcome = configure_aws.write_target(
                root, self.target, force=False
            )

            self.assertEqual(returned, destination.resolve())
            self.assertEqual(outcome, "unchanged")
            self.assertEqual(destination.read_text(encoding="utf-8"), existing)
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)

    def test_symlink_target_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / "coreAgent/agentcore/aws-targets.json"
            destination.parent.mkdir(parents=True)
            outside = root / "outside.json"
            outside.write_text("[]\n", encoding="utf-8")
            destination.symlink_to(outside)

            with self.assertRaisesRegex(
                configure_aws.AwsConfigurationError, "symlink"
            ):
                configure_aws.write_target(root, self.target, force=True)
            self.assertEqual(outside.read_text(encoding="utf-8"), "[]\n")


class CliTests(unittest.TestCase):
    def _make_fake_aws(self, root: Path) -> tuple[Path, Path]:
        fake_bin = root / "fake-bin"
        fake_bin.mkdir()
        call_log = root / "aws-calls.log"
        fake_aws = fake_bin / "aws"
        fake_aws.write_text(
            "#!/usr/bin/env bash\n"
            "set -eu\n"
            'printf "%s\\n" "$*" >> "$FAKE_AWS_CALL_LOG"\n'
            'if [ -n "${FAKE_AWS_FAIL_MATCH:-}" ]; then\n'
            '  case "$*" in\n'
            '    *"$FAKE_AWS_FAIL_MATCH"*)\n'
            '      printf "synthetic AWS failure for %s\\n" "$FAKE_AWS_FAIL_MATCH" >&2\n'
            "      exit 42\n"
            "      ;;\n"
            "  esac\n"
            "fi\n"
            'case " $* " in\n'
            '  *" sts get-caller-identity "*)\n'
            "    printf '%s\\n' "
            "'{\"Account\":\"123456789012\",\"Arn\":\"arn:aws:sts::123456789012:assumed-role/Deploy/session\"}'\n"
            "    ;;\n"
            '  *" bedrock-agentcore-control list-agent-runtimes "*)\n'
            "    printf '%s\\n' '{\"agentRuntimes\":[]}'\n"
            "    ;;\n"
            '  *" ssm get-parameter "*)\n'
            "    printf '%s\\n' '23'\n"
            "    ;;\n"
            "  *)\n"
            '    printf "unexpected fake aws command: %s\\n" "$*" >&2\n'
            "    exit 64\n"
            "    ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        fake_aws.chmod(0o755)
        return fake_bin, call_log

    def _run_cli(
        self,
        root: Path,
        fake_bin: Path,
        call_log: Path,
        *arguments: str,
        fail_match: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["PATH"] = f"{fake_bin}:{env['PATH']}"
        env["FAKE_AWS_CALL_LOG"] = str(call_log)
        for variable in (
            "AWS_PROFILE",
            "AWS_DEFAULT_PROFILE",
            "AWS_REGION",
            "AWS_DEFAULT_REGION",
        ):
            env.pop(variable, None)
        if fail_match is not None:
            env["FAKE_AWS_FAIL_MATCH"] = fail_match
        else:
            env.pop("FAKE_AWS_FAIL_MATCH", None)
        return subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/configure_aws.py"),
                "--root",
                str(root),
                *arguments,
            ],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_help_does_not_contact_aws(self) -> None:
        result = subprocess.run(
            ["python3.13", str(ROOT / "scripts/configure_aws.py"), "--help"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("usage:", result.stdout.lower())
        self.assertIn("--check-only", result.stdout)

    def test_check_only_forwards_profile_and_region_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake_bin, call_log = self._make_fake_aws(root)

            result = self._run_cli(
                root,
                fake_bin,
                call_log,
                "--check-only",
                "--profile",
                "sandbox",
                "--region",
                "eu-west-1",
            )

            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertFalse(
                (root / "coreAgent/agentcore/aws-targets.json").exists()
            )
            calls = call_log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(calls), 3)
            for call in calls:
                self.assertTrue(
                    call.startswith("--profile sandbox --region eu-west-1 "),
                    msg=call,
                )
            self.assertIn(" sts get-caller-identity ", f" {calls[0]} ")
            self.assertIn(
                " bedrock-agentcore-control list-agent-runtimes ",
                f" {calls[1]} ",
            )
            self.assertIn(" --no-paginate ", f" {calls[1]} ")
            self.assertIn(" ssm get-parameter ", f" {calls[2]} ")

    def test_normal_mode_writes_verified_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake_bin, call_log = self._make_fake_aws(root)

            result = self._run_cli(
                root,
                fake_bin,
                call_log,
                "--profile",
                "sandbox",
                "--region",
                "eu-west-1",
            )

            self.assertEqual(result.returncode, 0, msg=result.stderr)
            destination = root / "coreAgent/agentcore/aws-targets.json"
            payload = json.loads(destination.read_text(encoding="utf-8"))
            self.assertEqual(payload[0]["account"], "123456789012")
            self.assertEqual(payload[0]["region"], "eu-west-1")
            self.assertIn("Deployment target written", result.stdout)

    def test_skip_agentcore_check_omits_control_plane_call(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake_bin, call_log = self._make_fake_aws(root)

            result = self._run_cli(
                root,
                fake_bin,
                call_log,
                "--check-only",
                "--skip-agentcore-check",
                "--region",
                "eu-west-1",
            )

            self.assertEqual(result.returncode, 0, msg=result.stderr)
            calls = call_log.read_text(encoding="utf-8")
            self.assertIn("sts get-caller-identity", calls)
            self.assertIn("ssm get-parameter", calls)
            self.assertNotIn("bedrock-agentcore-control", calls)
            self.assertIn("AgentCore: check skipped", result.stdout)

    def test_required_aws_failure_returns_one_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake_bin, call_log = self._make_fake_aws(root)

            result = self._run_cli(
                root,
                fake_bin,
                call_log,
                "--region",
                "eu-west-1",
                fail_match="bedrock-agentcore-control list-agent-runtimes",
            )

            self.assertEqual(result.returncode, 1)
            self.assertIn("synthetic AWS failure", result.stderr)
            self.assertFalse(
                (root / "coreAgent/agentcore/aws-targets.json").exists()
            )
            calls = call_log.read_text(encoding="utf-8")
            self.assertIn("sts get-caller-identity", calls)
            self.assertIn("bedrock-agentcore-control list-agent-runtimes", calls)
            self.assertNotIn("ssm get-parameter", calls)

    def test_skipped_agentcore_check_is_reported_truthfully(self) -> None:
        target = configure_aws.AwsTarget(
            account="123456789012",
            region="eu-west-1",
            partition="aws",
            caller_arn="arn:aws:sts::123456789012:assumed-role/Deploy/session",
            profile=None,
        )
        output = io.StringIO()
        with redirect_stdout(output):
            configure_aws._print_report(
                target,
                bootstrap_version=None,
                agentcore_verified=False,
            )

        self.assertIn("AgentCore: check skipped", output.getvalue())
        self.assertNotIn("control plane reachable", output.getvalue())


if __name__ == "__main__":
    unittest.main()
