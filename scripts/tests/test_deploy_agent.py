from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/deploy_agent.sh"
REGIONS = ROOT / "scripts/agentcore_cli_regions.txt"
CONTROLLED_ENV = (
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
    "GITHUB_APP_ID",
    "DOMAIN_NAME",
    "AGENTCORE_MEMORY_ID",
    "AGENTCORE_SEMANTIC_STRATEGY_ID",
    "AGENTCORE_USER_PREF_STRATEGY_ID",
    "DEPLOY_EXPERIMENTAL_SANDBOX",
)


class DeployAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        if shutil.which("jq") is None:
            self.skipTest("jq is required for deploy wrapper tests")

    def make_repo(self, root: Path, *, target_region: str = "us-west-2") -> dict[str, Path]:
        scripts = root / "scripts"
        manifest_dir = root / "coreAgent/agentcore"
        fake_bin = root / "fake-bin"
        captures = root / "captures"
        for directory in (scripts, manifest_dir, fake_bin, captures):
            directory.mkdir(parents=True, exist_ok=True)

        shutil.copy2(SCRIPT, scripts / "deploy_agent.sh")
        shutil.copy2(REGIONS, scripts / "agentcore_cli_regions.txt")
        (scripts / "deploy_agent.sh").chmod(0o755)

        manifest = manifest_dir / "agentcore.json"
        manifest_payload = {
            "name": "coreAgent",
            "runtimes": [
                {
                    "name": "coreAgent",
                    "envVars": [
                        {"name": "KEEP_ME", "value": "untouched"},
                        {"name": "AWS_REGION", "value": "stale-one"},
                        {"name": "AWS_REGION", "value": "stale-two"},
                        {"name": "GITHUB_APP_ID", "value": "old"},
                        {"name": "DASHBOARD_BASE_URL", "value": "old"},
                        {"name": "AGENTCORE_MEMORY_ID", "value": "old"},
                        {
                            "name": "AGENTCORE_SEMANTIC_STRATEGY_ID",
                            "value": "old",
                        },
                        {
                            "name": "AGENTCORE_USER_PREF_STRATEGY_ID",
                            "value": "old",
                        },
                        {
                            "name": "ENABLE_EXPERIMENTAL_PR_SANDBOX",
                            "value": "old",
                        },
                    ],
                }
            ],
        }
        manifest.write_text(
            json.dumps(manifest_payload, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        manifest.chmod(0o640)
        (manifest_dir / "aws-targets.json").write_text(
            json.dumps(
                [
                    {
                        "name": "default",
                        "account": "123456789012",
                        "region": target_region,
                    }
                ]
            ),
            encoding="utf-8",
        )

        agentcore = fake_bin / "agentcore"
        agentcore.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env bash
                set -eu
                command_name="${1:-missing}"
                printf '%s\n' "$*" >> "$FAKE_CALL_LOG"
                cp "$FAKE_MANIFEST" "$FAKE_CAPTURE_DIR/${command_name}.json"
                case "$command_name" in
                  validate) exit "${FAKE_VALIDATE_EXIT:-0}" ;;
                  deploy) exit "${FAKE_DEPLOY_EXIT:-0}" ;;
                  *) exit 90 ;;
                esac
                """
            ),
            encoding="utf-8",
        )
        agentcore.chmod(0o755)

        return {
            "script": scripts / "deploy_agent.sh",
            "manifest": manifest,
            "manifest_dir": manifest_dir,
            "fake_bin": fake_bin,
            "captures": captures,
            "call_log": root / "calls.log",
        }

    def run_wrapper(
        self,
        paths: dict[str, Path],
        *,
        region: str = "us-west-2",
        extra_env: dict[str, str] | None = None,
        args: tuple[str, ...] = ("--yes",),
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        for name in CONTROLLED_ENV:
            env.pop(name, None)
        env.update(
            {
                "PATH": f"{paths['fake_bin']}:{env['PATH']}",
                "AWS_REGION": region,
                "FAKE_MANIFEST": str(paths["manifest"]),
                "FAKE_CAPTURE_DIR": str(paths["captures"]),
                "FAKE_CALL_LOG": str(paths["call_log"]),
            }
        )
        env.update(extra_env or {})
        return subprocess.run(
            [str(paths["script"]), *args],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def assert_original_restored(
        self,
        paths: dict[str, Path],
        original: bytes,
        original_mode: int,
    ) -> None:
        self.assertEqual(paths["manifest"].read_bytes(), original)
        self.assertEqual(
            stat.S_IMODE(paths["manifest"].stat().st_mode),
            original_mode,
        )
        self.assertFalse(
            (paths["manifest_dir"] / ".agentcore-deploy.lock").exists()
        )

    def test_success_injects_environment_once_and_restores_exact_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self.make_repo(Path(temporary))
            original = paths["manifest"].read_bytes()
            original_mode = stat.S_IMODE(paths["manifest"].stat().st_mode)

            result = self.run_wrapper(
                paths,
                extra_env={
                    "GITHUB_APP_ID": "123456",
                    "DOMAIN_NAME": "agents.example.test",
                    "AGENTCORE_MEMORY_ID": "memory-1",
                    "AGENTCORE_SEMANTIC_STRATEGY_ID": "semantic-1",
                    "AGENTCORE_USER_PREF_STRATEGY_ID": "preferences-1",
                    "DEPLOY_EXPERIMENTAL_SANDBOX": "true",
                },
            )

            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertEqual(
                paths["call_log"].read_text(encoding="utf-8").splitlines(),
                ["validate", "deploy --yes"],
            )
            validate_payload = json.loads(
                (paths["captures"] / "validate.json").read_text(encoding="utf-8")
            )
            deploy_payload = json.loads(
                (paths["captures"] / "deploy.json").read_text(encoding="utf-8")
            )
            self.assertEqual(validate_payload, deploy_payload)
            env_vars = validate_payload["runtimes"][0]["envVars"]
            self.assertEqual(
                {item["name"]: item["value"] for item in env_vars},
                {
                    "KEEP_ME": "untouched",
                    "AWS_REGION": "us-west-2",
                    "GITHUB_APP_ID": "123456",
                    "DASHBOARD_BASE_URL": "https://agents.example.test",
                    "AGENTCORE_MEMORY_ID": "memory-1",
                    "AGENTCORE_SEMANTIC_STRATEGY_ID": "semantic-1",
                    "AGENTCORE_USER_PREF_STRATEGY_ID": "preferences-1",
                    "ENABLE_EXPERIMENTAL_PR_SANDBOX": "1",
                },
            )
            self.assertEqual(
                len([item for item in env_vars if item["name"] == "AWS_REGION"]),
                1,
            )
            self.assert_original_restored(paths, original, original_mode)

    def test_validate_failure_is_propagated_without_deploy_and_restores(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self.make_repo(Path(temporary))
            original = paths["manifest"].read_bytes()
            original_mode = stat.S_IMODE(paths["manifest"].stat().st_mode)

            result = self.run_wrapper(
                paths,
                extra_env={"FAKE_VALIDATE_EXIT": "42"},
            )

            self.assertEqual(result.returncode, 42, msg=result.stderr)
            self.assertEqual(
                paths["call_log"].read_text(encoding="utf-8").splitlines(),
                ["validate"],
            )
            self.assertFalse((paths["captures"] / "deploy.json").exists())
            self.assert_original_restored(paths, original, original_mode)

    def test_empty_optional_values_are_omitted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self.make_repo(Path(temporary))

            result = self.run_wrapper(paths)

            self.assertEqual(result.returncode, 0, msg=result.stderr)
            payload = json.loads(
                (paths["captures"] / "validate.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                payload["runtimes"][0]["envVars"],
                [
                    {"name": "KEEP_ME", "value": "untouched"},
                    {"name": "AWS_REGION", "value": "us-west-2"},
                ],
            )

    def test_unsupported_or_mismatched_region_fails_before_cli(self) -> None:
        cases = (
            ("eu-west-2", "eu-west-2", "pinned AgentCore CLI"),
            ("us-east-1", "us-west-2", "does not match"),
        )
        for region, target_region, expected in cases:
            with self.subTest(region=region, target_region=target_region):
                with tempfile.TemporaryDirectory() as temporary:
                    paths = self.make_repo(
                        Path(temporary), target_region=target_region
                    )
                    original = paths["manifest"].read_bytes()
                    original_mode = stat.S_IMODE(paths["manifest"].stat().st_mode)

                    result = self.run_wrapper(paths, region=region)

                    self.assertEqual(result.returncode, 2)
                    self.assertIn(expected, result.stderr)
                    self.assertFalse(paths["call_log"].exists())
                    self.assert_original_restored(paths, original, original_mode)


if __name__ == "__main__":
    unittest.main()
