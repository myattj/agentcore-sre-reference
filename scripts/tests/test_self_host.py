from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import tempfile
import unittest
from contextlib import nullcontext, redirect_stdout
from pathlib import Path
from unittest import mock

from scripts import self_host


def completed(
    command: list[str] | tuple[str, ...],
    stdout: str = "",
    *,
    returncode: int = 0,
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, returncode, stdout, stderr)


def config() -> self_host.DeploymentConfig:
    return self_host.DeploymentConfig(
        profile="sandbox",
        region="us-west-2",
        account="123456789012",
        partition="aws",
        domain="agent.example.com",
        certificate_arn=(
            "arn:aws:acm:us-west-2:123456789012:"
            "certificate/00000000-0000-0000-0000-000000000000"
        ),
    )


class InputValidationTests(unittest.TestCase):
    def test_domain_normalizes_case_and_trailing_dot(self) -> None:
        self.assertEqual(
            self_host.normalize_domain(" Agent.Example.COM. "), "agent.example.com"
        )

    def test_domain_rejects_urls_paths_and_single_labels(self) -> None:
        for value in (
            "https://agent.example.com",
            "agent.example.com/path",
            "localhost",
            "-agent.example.com",
            "agent..example.com",
        ):
            with self.subTest(value=value), self.assertRaises(self_host.SelfHostError):
                self_host.normalize_domain(value)

    def test_certificate_must_match_selected_identity(self) -> None:
        value = config().certificate_arn
        self.assertEqual(
            self_host.validate_certificate_arn(
                value,
                account="123456789012",
                region="us-west-2",
                partition="aws",
            ),
            value,
        )
        for change in (
            {"account": "999999999999"},
            {"region": "eu-west-1"},
            {"partition": "aws-us-gov"},
        ):
            kwargs = {
                "account": "123456789012",
                "region": "us-west-2",
                "partition": "aws",
            } | change
            with (
                self.subTest(change=change),
                self.assertRaisesRegex(self_host.SelfHostError, "selected AWS account"),
            ):
                self_host.validate_certificate_arn(value, **kwargs)

    def test_slack_payload_requires_all_verified_shapes(self) -> None:
        valid = {
            "SLACK_CLIENT_ID": "123456789.987654321",
            "SLACK_CLIENT_SECRET": "a" * 32,
            "SLACK_SIGNING_SECRET": "b" * 32,
            "SLACK_APP_ID": "A123456789",
        }
        self.assertEqual(self_host.validate_slack_payload(valid), valid)

        invalid = {
            "SLACK_CLIENT_ID": "not-an-id",
            "SLACK_CLIENT_SECRET": "short",
            "SLACK_SIGNING_SECRET": "Z" * 32,
            "SLACK_APP_ID": "B123",
        }
        for key, bad_value in invalid.items():
            payload = valid | {key: bad_value}
            with self.subTest(key=key), self.assertRaises(self_host.SelfHostError):
                self_host.validate_slack_payload(payload)

    def test_bridge_payload_rejects_missing_or_weak_secrets(self) -> None:
        valid = {
            "BRIDGE_OAUTH_STATE_SECRET": "a" * 64,
            "BRIDGE_GATEWAY_JWT_PRIVATE_KEY_PEM": (
                "-----BEGIN PRIVATE KEY-----\nkey\n-----END PRIVATE KEY-----\n"
            ),
            "ADMIN_SECRET": "b" * 48,
        }
        self.assertEqual(self_host.validate_bridge_payload(valid), valid)
        for payload in (
            valid | {"ADMIN_SECRET": "short"},
            valid | {"BRIDGE_OAUTH_STATE_SECRET": "short"},
            valid | {"BRIDGE_GATEWAY_JWT_PRIVATE_KEY_PEM": "not a key"},
            {"ADMIN_SECRET": "b" * 48},
        ):
            with (
                self.subTest(payload=payload),
                self.assertRaises(self_host.SelfHostError),
            ):
                self_host.validate_bridge_payload(payload)


class ManifestTests(unittest.TestCase):
    def test_generated_manifest_rewrites_every_public_slack_url(self) -> None:
        manifest = self_host.generated_slack_manifest("ops.example.com")
        self.assertEqual(
            manifest["oauth_config"]["redirect_urls"],
            ["https://ops.example.com/slack/oauth/callback"],
        )
        settings = manifest["settings"]
        self.assertEqual(
            settings["event_subscriptions"]["request_url"],
            "https://ops.example.com/slack/events",
        )
        self.assertEqual(
            settings["interactivity"]["request_url"],
            "https://ops.example.com/slack/interactions",
        )
        serialized = json.dumps(manifest)
        self.assertNotIn("agent.example.com", serialized)
        self.assertIn("app_mentions:read", serialized)

    def test_manifest_write_is_private_and_rejects_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "manifest.json"
            self_host.write_generated_manifest("ops.example.com", destination)
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
            self.assertEqual(
                json.loads(destination.read_text())["oauth_config"]["redirect_urls"],
                ["https://ops.example.com/slack/oauth/callback"],
            )

            outside = Path(temporary) / "outside"
            outside.write_text("do not replace", encoding="utf-8")
            destination.unlink()
            destination.symlink_to(outside)
            with self.assertRaisesRegex(self_host.SelfHostError, "symlink"):
                self_host.write_generated_manifest("other.example.com", destination)
            self.assertEqual(outside.read_text(encoding="utf-8"), "do not replace")


class SecretTests(unittest.TestCase):
    def test_read_secret_handles_found_missing_and_permission_failure(self) -> None:
        document = {
            "ARN": "arn:aws:secretsmanager:us-west-2:123456789012:secret:example-abc",
            "SecretString": json.dumps({"KEY": "value"}),
        }
        runner = mock.Mock()
        runner.run.return_value = completed([], json.dumps(document))
        found = self_host.read_secret(runner, config(), "example")
        assert found
        self.assertEqual(found.payload, {"KEY": "value"})
        self.assertTrue(found.existed)

        runner.run.return_value = completed(
            [], returncode=254, stderr="ResourceNotFoundException"
        )
        self.assertIsNone(self_host.read_secret(runner, config(), "missing"))

        runner.run.return_value = completed(
            [], returncode=254, stderr="AccessDeniedException"
        )
        with self.assertRaisesRegex(self_host.SelfHostError, "permissions"):
            self_host.read_secret(runner, config(), "forbidden")

    def test_put_secret_uses_private_temp_file_and_removes_it(self) -> None:
        runner = mock.Mock()
        runner.run.return_value = completed([], "{}")
        stored = self_host.SecretDocument(
            arn="arn:aws:secretsmanager:us-west-2:123456789012:secret:example-abc",
            payload={"TOKEN": "secret-value"},
            existed=True,
        )
        with mock.patch.object(self_host, "read_secret", return_value=stored):
            result = self_host.put_secret(
                runner,
                config(),
                "example",
                {"TOKEN": "secret-value"},
                existed=False,
            )

        self.assertEqual(result.arn, stored.arn)
        command = runner.run.call_args_list[0].args[0]
        self.assertIn("create-secret", command)
        self.assertNotIn("secret-value", command)
        file_arg = next(part for part in command if str(part).startswith("file://"))
        self.assertFalse(Path(str(file_arg).removeprefix("file://")).exists())

    def test_existing_slack_secret_is_reused_without_prompt(self) -> None:
        payload = {
            "SLACK_CLIENT_ID": "123456789.987654321",
            "SLACK_CLIENT_SECRET": "a" * 32,
            "SLACK_SIGNING_SECRET": "b" * 32,
            "SLACK_APP_ID": "A123456789",
        }
        existing = self_host.SecretDocument("arn:example", payload, True)
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch("builtins.input", side_effect=AssertionError("prompted")),
            mock.patch("getpass.getpass", side_effect=AssertionError("prompted")),
        ):
            self.assertEqual(self_host.collect_slack_payload(existing), payload)


class DeploymentPlanTests(unittest.TestCase):
    def test_pinned_agentcore_cli_is_private_and_temporary(self) -> None:
        with self_host._pinned_agentcore_cli() as directory:
            executable = directory / "agentcore"
            self.assertTrue(executable.exists())
            self.assertEqual(stat.S_IMODE(executable.stat().st_mode), 0o700)
            self.assertIn("@aws/agentcore@0.24.1", executable.read_text())
        self.assertFalse(directory.exists())

    def test_guided_deploy_reuses_existing_interfaces_in_dependency_order(self) -> None:
        target = self_host.configure_aws.AwsTarget(
            account="123456789012",
            region="us-west-2",
            partition="aws",
            caller_arn=("arn:aws:sts::123456789012:assumed-role/Deploy/test-session"),
            profile="sandbox",
        )
        slack_payload = {
            "SLACK_CLIENT_ID": "123456789.987654321",
            "SLACK_CLIENT_SECRET": "a" * 32,
            "SLACK_SIGNING_SECRET": "b" * 32,
            "SLACK_APP_ID": "A123456789",
        }
        bridge_payload = {
            "BRIDGE_OAUTH_STATE_SECRET": "c" * 64,
            "BRIDGE_GATEWAY_JWT_PRIVATE_KEY_PEM": (
                "-----BEGIN PRIVATE KEY-----\nkey\n-----END PRIVATE KEY-----\n"
            ),
            "ADMIN_SECRET": "d" * 48,
        }
        existing_slack = self_host.SecretDocument("arn:slack", slack_payload, True)
        existing_bridge = self_host.SecretDocument("arn:bridge", bridge_payload, True)
        runner = mock.Mock()

        def run(command, **kwargs):
            if list(command) == ["bash", "scripts/resolve_agent_runtime.sh"]:
                return completed(
                    list(command),
                    "arn:aws:bedrock-agentcore:us-west-2:123456789012:"
                    "agent/00000000-0000-0000-0000-000000000001:1\n",
                )
            return completed(list(command))

        runner.run.side_effect = run
        args = self_host.build_parser().parse_args(
            [
                "--profile",
                "sandbox",
                "--region",
                "us-west-2",
                "--domain",
                "agent.example.com",
                "--certificate-arn",
                config().certificate_arn,
                "--skip-setup",
                "--yes",
                "--skip-health-check",
            ]
        )

        with (
            mock.patch.object(self_host, "_require_tools"),
            mock.patch.object(
                self_host.configure_aws,
                "inspect_target",
                return_value=(target, "20"),
            ),
            mock.patch.object(
                self_host.configure_aws,
                "write_target",
                return_value=(Path("aws-targets.json"), "written"),
            ),
            mock.patch.object(
                self_host, "CommandRunner", return_value=runner
            ) as runner_factory,
            mock.patch.object(
                self_host,
                "write_generated_manifest",
                return_value=Path("manifest.json"),
            ),
            mock.patch.object(
                self_host,
                "read_secret",
                side_effect=[existing_slack, existing_bridge],
            ),
            mock.patch.object(
                self_host,
                "put_secret",
                side_effect=[existing_slack, existing_bridge],
            ),
            mock.patch.object(self_host, "_confirm"),
            mock.patch.object(
                self_host,
                "_pinned_agentcore_cli",
                return_value=nullcontext(Path("/tmp/pinned-agentcore")),
            ),
            mock.patch.object(self_host, "_cdk_deploy") as cdk_deploy,
            mock.patch.object(
                self_host,
                "_stack_outputs",
                return_value={"AlbDnsName": "alb.example.amazonaws.com"},
            ),
            mock.patch.object(
                self_host, "_write_state", return_value=Path("deployment.json")
            ),
        ):
            result = self_host.deploy(args)

        self.assertEqual(result, 0)
        deployed_stacks = [call.args[1] for call in cdk_deploy.call_args_list]
        self.assertEqual(
            deployed_stacks,
            [
                (
                    "AgentCore-coreAgent-data-us-west-2",
                    "AgentCore-coreAgent-observability-us-west-2",
                ),
                ("AgentCore-coreAgent-services-us-west-2",),
                ("AgentCore-coreAgent-gateway-us-west-2",),
            ],
        )
        all_commands = [list(call.args[0]) for call in runner.run.call_args_list]
        self.assertIn(["bash", "scripts/deploy_agent.sh"], all_commands)
        runtime_environment = runner_factory.call_args_list[1].args[0]
        self.assertEqual(runtime_environment["DOMAIN_NAME"], "agent.example.com")
        self.assertEqual(runtime_environment["DEPLOY_EXPERIMENTAL_SANDBOX"], "false")
        self.assertTrue(runtime_environment["PATH"].startswith("/tmp/pinned-agentcore"))
        self.assertTrue(
            any("provision_gateway.py" in " ".join(command) for command in all_commands)
        )
        self.assertFalse(
            any("sandbox" in " ".join(command).lower() for command in all_commands)
        )

    def test_cdk_deploy_is_explicit_and_never_includes_sandbox(self) -> None:
        runner = mock.Mock()
        self_host._cdk_deploy(
            runner,
            ("AgentCore-coreAgent-services-us-west-2",),
            config(),
            ("agentRuntimeArn", "arn:runtime"),
            ("domainName", "agent.example.com"),
        )
        command = runner.run.call_args.args[0]
        self.assertEqual(command[:3], ["npx", "cdk", "deploy"])
        self.assertIn("--require-approval", command)
        self.assertIn("never", command)
        self.assertIn("agentRuntimeArn=arn:runtime", command)
        self.assertNotIn("sandbox", " ".join(command).lower())
        self.assertEqual(
            runner.run.call_args.kwargs["cwd"], self_host.ROOT / "infra/data"
        )

    def test_typed_confirmation_must_match_account_and_region(self) -> None:
        with (
            mock.patch("builtins.input", return_value="yes"),
            self.assertRaisesRegex(self_host.SelfHostError, "cancelled"),
        ):
            self_host._confirm(config(), assume_yes=False)
        with mock.patch("builtins.input", return_value="deploy 123456789012/us-west-2"):
            self_host._confirm(config(), assume_yes=False)

    def test_dry_run_is_non_mutating_and_names_every_phase(self) -> None:
        output = io.StringIO()
        args = self_host.build_parser().parse_args(
            ["--dry-run", "--domain", "agent.example.com", "--region", "us-west-2"]
        )
        with redirect_stdout(output):
            result = self_host._dry_run(args)
        self.assertEqual(result, 0)
        text = output.getvalue()
        for phrase in (
            "Slack manifest",
            "Secrets Manager",
            "AgentCore runtime",
            "bridge/onboarding",
            "tenant-scoped AgentCore Gateway",
            "Experimental PR sandbox: disabled",
        ):
            self.assertIn(phrase, text)

    def test_state_file_contains_no_secret_material(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.object(self_host, "STATE_DIR", Path(temporary)):
                destination = self_host._write_state(
                    config(), {"AlbDnsName": "alb.example.amazonaws.com"}
                )
            payload = json.loads(destination.read_text(encoding="utf-8"))
            self.assertEqual(payload["alb_dns_name"], "alb.example.amazonaws.com")
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
            serialized = json.dumps(payload).lower()
            self.assertNotIn("secret", serialized)
            self.assertNotIn("token", serialized)


if __name__ == "__main__":
    unittest.main()
