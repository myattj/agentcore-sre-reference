from __future__ import annotations

import json
import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/resolve_agent_runtime.sh"
ACCOUNT = "123456789012"
REGION = "eu-west-1"
RUNTIME_NAME = "coreAgent_coreAgent"
RUNTIME_ID_ONE = "coreAgent-ABCDEFGHIJ"
RUNTIME_ID_TWO = "coreAgent-KLMNOPQRST"
RUNTIME_ONE = (
    f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:"
    "agent/00000000-0000-0000-0000-000000000001:1"
)
RUNTIME_TWO = (
    f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:"
    "agent/00000000-0000-0000-0000-000000000002:2"
)
LEGACY_RUNTIME = (
    f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/{RUNTIME_ID_ONE}"
)


class ResolveAgentRuntimeTests(unittest.TestCase):
    def run_resolver(
        self,
        runtimes: list[dict[str, str]],
        *,
        override: str = "",
        status: str = "READY",
        identity_arn: str | None = None,
        region: str = REGION,
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as temporary:
            fake_bin = Path(temporary)
            fake_aws = fake_bin / "aws"
            fake_aws.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env bash
                    set -eu
                    case "$*" in
                      *"sts get-caller-identity"*) printf '%s\\n' "$FAKE_IDENTITY" ;;
                      *"list-agent-runtimes"*) printf '%s\\n' "$FAKE_RUNTIMES" ;;
                      *"get-agent-runtime"*)
                        [[ "$*" == *"--agent-runtime-id $FAKE_RUNTIME_ID"* ]]
                        [[ "$*" == *"--agent-runtime-version $FAKE_RUNTIME_VERSION"* ]]
                        printf '%s\\n' "$FAKE_RUNTIME"
                        ;;
                      *) echo "unexpected aws call: $*" >&2; exit 9 ;;
                    esac
                    """
                ),
                encoding="utf-8",
            )
            fake_aws.chmod(0o755)
            selected_runtime = next(
                (
                    runtime
                    for runtime in runtimes
                    if runtime["agentRuntimeArn"] == override
                ),
                runtimes[0] if runtimes else {},
            )
            env = os.environ.copy()
            env.pop("RUNTIME_NAME", None)
            env.update(
                {
                    "PATH": f"{fake_bin}:{env['PATH']}",
                    "AWS_REGION": region,
                    "RUNTIME_ARN_OVERRIDE": override,
                    "FAKE_IDENTITY": json.dumps(
                        {
                            "Account": ACCOUNT,
                            "Arn": identity_arn
                            or f"arn:aws:sts::{ACCOUNT}:assumed-role/Deploy/session",
                        }
                    ),
                    "FAKE_RUNTIMES": json.dumps({"agentRuntimes": runtimes}),
                    "FAKE_RUNTIME": json.dumps(
                        {
                            "agentRuntimeArn": selected_runtime.get(
                                "agentRuntimeArn", ""
                            ),
                            "agentRuntimeId": selected_runtime.get(
                                "agentRuntimeId", ""
                            ),
                            "agentRuntimeVersion": selected_runtime.get(
                                "agentRuntimeVersion", ""
                            ),
                            "agentRuntimeName": RUNTIME_NAME,
                            "status": status,
                        }
                    ),
                    "FAKE_RUNTIME_ID": selected_runtime.get("agentRuntimeId", ""),
                    "FAKE_RUNTIME_VERSION": selected_runtime.get(
                        "agentRuntimeVersion", ""
                    ),
                }
            )
            return subprocess.run(
                ["bash", str(SCRIPT)],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

    @staticmethod
    def runtime(
        arn: str,
        runtime_id: str = RUNTIME_ID_ONE,
        version: str = "1",
    ) -> dict[str, str]:
        return {
            "agentRuntimeName": RUNTIME_NAME,
            "agentRuntimeArn": arn,
            "agentRuntimeId": runtime_id,
            "agentRuntimeVersion": version,
        }

    def test_one_ready_runtime_is_selected(self) -> None:
        result = self.run_resolver([self.runtime(RUNTIME_ONE)])
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stdout.strip(), RUNTIME_ONE)
        self.assertIn(f"Resolved {RUNTIME_NAME} runtime", result.stderr)

    def test_zero_or_multiple_runtimes_require_an_exact_selection(self) -> None:
        for runtimes, count in (
            ([], 0),
            (
                [
                    self.runtime(RUNTIME_ONE),
                    self.runtime(RUNTIME_TWO, RUNTIME_ID_TWO, "2"),
                ],
                2,
            ),
        ):
            with self.subTest(count=count):
                result = self.run_resolver(runtimes)
                self.assertEqual(result.returncode, 1)
                self.assertIn(f"found {count}", result.stderr)

    def test_valid_override_selects_one_duplicate(self) -> None:
        result = self.run_resolver(
            [
                self.runtime(RUNTIME_ONE),
                self.runtime(RUNTIME_TWO, RUNTIME_ID_TWO, "2"),
            ],
            override=RUNTIME_TWO,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stdout.strip(), RUNTIME_TWO)

    def test_override_must_be_present_and_match_account_region_partition(self) -> None:
        invalid = (
            "arn:aws:bedrock-agentcore:us-east-1:123456789012:"
            "agent/00000000-0000-0000-0000-000000000001:1"
        )
        for override, expected in ((RUNTIME_TWO, "does not identify"), (invalid, "not a Runtime ARN")):
            with self.subTest(override=override):
                result = self.run_resolver([self.runtime(RUNTIME_ONE)], override=override)
                self.assertEqual(result.returncode, 1)
                self.assertIn(expected, result.stderr)

    def test_non_ready_runtime_is_rejected(self) -> None:
        result = self.run_resolver([self.runtime(RUNTIME_ONE)], status="UPDATING")
        self.assertEqual(result.returncode, 1)
        self.assertIn("readiness validation", result.stderr)

    def test_documented_legacy_runtime_arn_is_supported(self) -> None:
        result = self.run_resolver([self.runtime(LEGACY_RUNTIME)])
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stdout.strip(), LEGACY_RUNTIME)

    def test_versioned_arn_must_match_runtime_metadata(self) -> None:
        result = self.run_resolver([self.runtime(RUNTIME_ONE, version="2")])
        self.assertEqual(result.returncode, 1)
        self.assertIn("does not match agentRuntimeVersion", result.stderr)

    def test_identity_partition_must_match_region(self) -> None:
        result = self.run_resolver(
            [self.runtime(RUNTIME_ONE)],
            identity_arn=(
                f"arn:aws-us-gov:sts::{ACCOUNT}:assumed-role/Deploy/session"
            ),
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("partition does not match", result.stderr)

    def test_commercial_identity_rejects_sovereign_region(self) -> None:
        result = self.run_resolver(
            [self.runtime(RUNTIME_ONE)],
            region="us-iso-east-1",
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("pinned AgentCore CLI", result.stderr)


if __name__ == "__main__":
    unittest.main()
