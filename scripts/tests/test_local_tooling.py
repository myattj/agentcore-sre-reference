from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.testenv import _common as testenv_common

ROOT = Path(__file__).resolve().parents[2]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bootstrap_env = load_module(
    "bootstrap_local_env", ROOT / "scripts/bootstrap_local_env.py"
)
demo_dashboard = load_module(
    "create_demo_dashboard", ROOT / "scripts/create_demo_dashboard.py"
)


class LocalEnvTests(unittest.TestCase):
    def make_root(self, path: Path) -> None:
        for component in ("bridge", "onboarding"):
            directory = path / component
            directory.mkdir(parents=True)
            (directory / ".env.example").write_text(
                f"COMPONENT={component}\nBRIDGE_OAUTH_STATE_SECRET=\n",
                encoding="utf-8",
            )

    def test_bootstrap_creates_matching_private_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.make_root(root)

            messages = bootstrap_env.bootstrap(root)

            self.assertEqual(len(messages), 2)
            values = []
            for component in ("bridge", "onboarding"):
                target = root / component / ".env.local"
                values.append(bootstrap_env._read_secret(target))
                self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            self.assertEqual(values[0], values[1])
            self.assertGreaterEqual(len(values[0]), 32)

    def test_bootstrap_preserves_existing_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.make_root(root)
            shared = "a" * 64
            original = f"KEEP=this-byte-for-byte\nBRIDGE_OAUTH_STATE_SECRET={shared}\n"
            bridge = root / "bridge/.env.local"
            bridge.write_text(original, encoding="utf-8")
            os.chmod(bridge, 0o644)

            bootstrap_env.bootstrap(root)

            self.assertEqual(bridge.read_text(encoding="utf-8"), original)
            self.assertEqual(stat.S_IMODE(bridge.stat().st_mode), 0o600)
            self.assertEqual(
                bootstrap_env._read_secret(root / "onboarding/.env.local"), shared
            )

    def test_bootstrap_rejects_mismatch_without_rewriting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.make_root(root)
            paths = [root / "bridge/.env.local", root / "onboarding/.env.local"]
            originals = []
            for index, path in enumerate(paths):
                content = (
                    f"BRIDGE_OAUTH_STATE_SECRET={('a' if index == 0 else 'b') * 64}\n"
                )
                path.write_text(content, encoding="utf-8")
                originals.append(content)

            with self.assertRaisesRegex(RuntimeError, "disagree"):
                bootstrap_env.bootstrap(root)

            self.assertEqual(
                [path.read_text(encoding="utf-8") for path in paths], originals
            )

    def test_bootstrap_rejects_env_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.make_root(root)
            outside = root / "outside"
            outside.write_text(
                f"BRIDGE_OAUTH_STATE_SECRET={'a' * 64}\n", encoding="utf-8"
            )
            original = outside.read_bytes()
            original_mode = stat.S_IMODE(outside.stat().st_mode)
            (root / "bridge/.env.local").symlink_to(outside)

            with self.assertRaisesRegex(RuntimeError, "not a regular file"):
                bootstrap_env.bootstrap(root)

            self.assertEqual(outside.read_bytes(), original)
            self.assertEqual(stat.S_IMODE(outside.stat().st_mode), original_mode)

    def test_bootstrap_rejects_template_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.make_root(root)
            example = root / "bridge/.env.example"
            content = example.read_text(encoding="utf-8")
            example.unlink()
            outside = root / "template"
            outside.write_text(content, encoding="utf-8")
            example.symlink_to(outside)

            with self.assertRaisesRegex(RuntimeError, "missing or unsafe"):
                bootstrap_env.bootstrap(root)

            self.assertFalse((root / "bridge/.env.local").exists())
            self.assertFalse((root / "onboarding/.env.local").exists())


class DemoDashboardTests(unittest.TestCase):
    def test_fixture_gets_fresh_ttl_and_private_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "dashboards"
            destination = demo_dashboard.create_dashboard(
                ROOT / "examples/dashboard.sample.json", output
            )
            payload = json.loads(destination.read_text(encoding="utf-8"))

            self.assertGreater(payload["ttl"], int(__import__("time").time()))
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
            self.assertGreaterEqual(len(payload["panels"]), 5)


class TestEnvSeederTokenTests(unittest.TestCase):
    def test_missing_token_fails_closed(self) -> None:
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            self.assertRaisesRegex(RuntimeError, "SLACK_SEEDER_BOT_TOKEN is required"),
        ):
            testenv_common.load_seeder_bot_token()

    def test_invalid_token_is_rejected(self) -> None:
        for token in ("xoxp-user-token", "Bearer xoxb-token", "xoxb-token\n"):
            with (
                self.subTest(token=token),
                mock.patch.dict(
                    os.environ,
                    {"SLACK_SEEDER_BOT_TOKEN": token},
                    clear=True,
                ),
                self.assertRaisesRegex(RuntimeError, "beginning with xoxb-"),
            ):
                testenv_common.load_seeder_bot_token()

    def test_valid_token_is_returned(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"SLACK_SEEDER_BOT_TOKEN": "xoxb-synthetic-test-token"},
            clear=True,
        ):
            self.assertEqual(
                testenv_common.load_seeder_bot_token(),
                "xoxb-synthetic-test-token",
            )


class RepositoryCheckTests(unittest.TestCase):
    def make_check_root(self, root: Path) -> tuple[Path, Path]:
        scripts = root / "scripts"
        scripts.mkdir(parents=True)
        shutil.copy2(ROOT / "scripts/check.sh", scripts / "check.sh")
        shutil.copy2(
            ROOT / "scripts/offline_aws_env.sh", scripts / "offline_aws_env.sh"
        )
        shutil.copy2(ROOT / ".source-scan-excludes", root / ".source-scan-excludes")
        for name in ("setup.sh", "doctor.sh", "demo.sh"):
            target = scripts / name
            target.write_text("#!/usr/bin/env bash\n", encoding="utf-8")

        pytest = root / "bridge/.venv/bin/pytest"
        pytest.parent.mkdir(parents=True)
        pytest.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        pytest.chmod(0o755)
        (root / "onboarding/node_modules").mkdir(parents=True)
        for directory in (
            "coreAgent/app/coreAgent",
            "coreAgent/agentcore/cdk",
            "workers/gateway_interceptor",
            "infra/sandbox",
            "infra/data",
            "seed/tests",
        ):
            (root / directory).mkdir(parents=True)
        for relative_path in (
            "scripts/deploy_agent.sh",
            "scripts/resolve_agent_runtime.sh",
            "infra/data/scripts/aws_region.sh",
            "infra/data/scripts/attach_agent_policy.sh",
            "infra/data/scripts/check_portability.sh",
            "infra/data/scripts/deploy_sandbox.sh",
        ):
            target = root / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("#!/usr/bin/env bash\n", encoding="utf-8")

        fake_bin = root / "fake-bin"
        fake_bin.mkdir()
        call_log = root / "calls.log"
        for name in ("python3.13", "uv", "npm", "gitleaks"):
            tool = fake_bin / name
            tool.write_text(
                "#!/usr/bin/env bash\n"
                'if [ "$(basename "$0")" = "gitleaks" ]; then\n'
                '  printf "ARGS %s\\n" "$*" >> "$CHECK_CALL_LOG"\n'
                '  if [ "${1:-}" = "dir" ]; then\n'
                '    scan_path=""\n'
                '    for argument in "$@"; do scan_path=$argument; done\n'
                '    (cd "$scan_path" && find . -type f -print | LC_ALL=C sort) '\
                '| sed "s#^\\./#FILE #" >> "$CHECK_CALL_LOG"\n'
                "  fi\n"
                'if [ "${FAKE_GITLEAKS_EXIT:-0}" -ne 0 ]; then\n'
                '  exit "$FAKE_GITLEAKS_EXIT"\n'
                "fi\n"
                "fi\n"
                "exit 0\n",
                encoding="utf-8",
            )
            tool.chmod(0o755)
        return fake_bin, call_log

    def run_check(
        self,
        root: Path,
        fake_bin: Path,
        call_log: Path,
        *,
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["PATH"] = f"{fake_bin}:{env['PATH']}"
        env["CHECK_CALL_LOG"] = str(call_log)
        env.update(extra_env or {})
        return subprocess.run(
            ["bash", str(root / "scripts/check.sh"), "--quick"],
            cwd=root,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_source_archive_scans_only_source_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            fake_bin, call_log = self.make_check_root(root)
            (root / "README.md").write_text("source marker\n", encoding="utf-8")
            (root / "bridge/.env.example").write_text(
                "EXAMPLE_VALUE=\n", encoding="utf-8"
            )
            generated_files = (
                "bridge/.env.local",
                "bridge/.venv/generated-secret.txt",
                "onboarding/node_modules/generated-secret.txt",
                "onboarding/.next/cache/.previewinfo",
                "infra/data/build/generated-secret.txt",
                "coreAgent/agentcore/aws-targets.json",
                "coreAgent/agentcore/.cli/deployed-state.json",
            )
            for relative_path in generated_files:
                path = root / relative_path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("generated secret marker\n", encoding="utf-8")

            result = self.run_check(root, fake_bin, call_log)

            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertIn("source tree", result.stdout)
            self.assertIn("source archive has no .git metadata", result.stdout)
            calls = call_log.read_text(encoding="utf-8").splitlines()
            self.assertTrue(calls[0].startswith("ARGS dir --redact --no-banner "))
            self.assertNotEqual(calls[0].rsplit(" ", 1)[-1], str(root))
            scanned_files = {
                line.removeprefix("FILE ")
                for line in calls[1:]
                if line.startswith("FILE ")
            }
            self.assertIn("README.md", scanned_files)
            self.assertIn("bridge/.env.example", scanned_files)
            for relative_path in generated_files:
                self.assertNotIn(relative_path, scanned_files)

    def test_source_archive_secret_detection_fails_and_cleans_up(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            fake_bin, call_log = self.make_check_root(root)
            (root / "README.md").write_text("source marker\n", encoding="utf-8")

            result = self.run_check(
                root,
                fake_bin,
                call_log,
                extra_env={"FAKE_GITLEAKS_EXIT": "17"},
            )

            self.assertEqual(result.returncode, 17)
            self.assertNotIn("All requested local validation gates passed", result.stdout)
            first_call = call_log.read_text(encoding="utf-8").splitlines()[0]
            scan_path = Path(first_call.rsplit(" ", 1)[-1])
            self.assertFalse(scan_path.exists())

    def test_git_clone_scans_history_and_runs_whitespace_check(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            fake_bin, call_log = self.make_check_root(root)
            subprocess.run(
                ["git", "init", "--quiet", str(root)],
                check=True,
                text=True,
                capture_output=True,
            )

            result = self.run_check(root, fake_bin, call_log)

            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertIn("full history", result.stdout)
            self.assertIn("Git whitespace check", result.stdout)
            self.assertEqual(
                call_log.read_text(encoding="utf-8").strip(),
                "ARGS git --redact --no-banner",
            )


class ReleaseWorkflowContractTests(unittest.TestCase):
    def test_pinned_agentcore_region_allowlist_is_shared(self) -> None:
        canonical = [
            line
            for line in (ROOT / "scripts/agentcore_cli_regions.txt")
            .read_text(encoding="utf-8")
            .splitlines()
            if line
        ]
        self.assertEqual(canonical, sorted(set(canonical)))
        self.assertEqual(
            canonical,
            [
                "ap-northeast-1",
                "ap-south-1",
                "ap-southeast-1",
                "ap-southeast-2",
                "eu-central-1",
                "eu-west-1",
                "us-east-1",
                "us-east-2",
                "us-west-2",
            ],
        )

        schema_source = (
            ROOT / "coreAgent/agentcore/.llm-context/aws-targets.ts"
        ).read_text(
            encoding="utf-8"
        )
        region_block = schema_source.split("type AgentCoreRegion =", 1)[1].split(
            ";", 1
        )[0]
        self.assertEqual(re.findall(r"\| '([^']+)'", region_block), canonical)

        configure = (ROOT / "scripts/configure_aws.py").read_text(encoding="utf-8")
        resolver = (ROOT / "scripts/resolve_agent_runtime.sh").read_text(
            encoding="utf-8"
        )
        workflow = (ROOT / ".github/workflows/ci-cd.yml").read_text(
            encoding="utf-8"
        )
        wrapper = (ROOT / "scripts/deploy_agent.sh").read_text(encoding="utf-8")
        self.assertIn('with_name("agentcore_cli_regions.txt")', configure)
        self.assertIn('print("  make agent-deploy")', configure)
        self.assertNotIn("agentcore_cli_regions.txt", resolver)
        self.assertIn('SUPPORTED_REGIONS="$SCRIPT_DIR/agentcore_cli_regions.txt"', wrapper)
        self.assertIn(
            'grep -Fxq -- "$DEPLOY_REGION" scripts/agentcore_cli_regions.txt',
            workflow,
        )

    def test_cdk_checks_ignore_the_active_aws_profile(self) -> None:
        check = (ROOT / "scripts/check.sh").read_text(encoding="utf-8")
        portability = (ROOT / "infra/data/scripts/check_portability.sh").read_text(
            encoding="utf-8"
        )
        offline_aws = (ROOT / "scripts/offline_aws_env.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn(
            'SYNTH_APP="env CDK_DEFAULT_ACCOUNT=000000000000 '
            'CDK_DEFAULT_REGION=us-west-2 node dist/bin/data.js"',
            check,
        )
        self.assertEqual(check.count('--app "$SYNTH_APP"'), 5)
        self.assertGreaterEqual(check.count("--context region=us-west-2"), 5)
        self.assertIn('. "$ROOT_DIR/scripts/offline_aws_env.sh"', check)
        self.assertIn("run_offline_aws", check)
        self.assertIn("bash scripts/check_portability.sh", check)
        self.assertIn('. "$ROOT_DIR/scripts/offline_aws_env.sh"', portability)
        self.assertIn("run_offline_aws", portability)
        self.assertIn("AWS_CONFIG_FILE=/dev/null", offline_aws)
        self.assertIn("AWS_SHARED_CREDENTIALS_FILE=/dev/null", offline_aws)
        self.assertIn("-u AWS_WEB_IDENTITY_TOKEN_FILE", offline_aws)
        self.assertNotIn("AWS_CONFIG_FILE=/dev/null", check)
        self.assertNotIn("AWS_CONFIG_FILE=/dev/null", portability)

    def test_offline_aws_helper_scrubs_credentials_and_target_selection(self) -> None:
        helper = ROOT / "scripts/offline_aws_env.sh"
        command = f'. "{helper}"; run_offline_aws env'
        env = {
            **os.environ,
            "AWS_ACCESS_KEY_ID": "synthetic-access-key",
            "AWS_SECRET_ACCESS_KEY": "synthetic-secret-key",
            "AWS_SESSION_TOKEN": "synthetic-session-token",
            "AWS_PROFILE": "personal-profile",
            "AWS_REGION": "eu-central-1",
            "CDK_DEFAULT_ACCOUNT": "123456789012",
        }

        result = subprocess.run(
            ["bash", "-c", command],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, msg=result.stderr)
        for variable in (
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_SESSION_TOKEN",
            "AWS_PROFILE",
            "AWS_REGION",
            "CDK_DEFAULT_ACCOUNT",
        ):
            self.assertNotIn(f"{variable}=", result.stdout)
        self.assertIn("AWS_CONFIG_FILE=/dev/null", result.stdout)
        self.assertIn("AWS_SHARED_CREDENTIALS_FILE=/dev/null", result.stdout)
        self.assertIn("AWS_EC2_METADATA_DISABLED=true", result.stdout)

    def test_agent_deploy_installs_codezip_packaging_runtime(self) -> None:
        workflow = (ROOT / ".github/workflows/ci-cd.yml").read_text(encoding="utf-8")
        deploy_agent = workflow.split("  deploy-agent:", 1)[1].split(
            "  deploy-services:", 1
        )[0]

        self.assertIn("actions/setup-python@", deploy_agent)
        self.assertIn('python-version: "3.13"', deploy_agent)
        self.assertIn("astral-sh/setup-uv@", deploy_agent)
        self.assertLess(
            deploy_agent.index("astral-sh/setup-uv@"),
            deploy_agent.index("run: bash scripts/deploy_agent.sh --yes"),
        )

    def test_ci_synthesizes_govcloud_and_rejects_commercial_arns(self) -> None:
        workflow = (ROOT / ".github/workflows/ci-cd.yml").read_text(encoding="utf-8")
        synth_job = workflow.split("  cdk-synth:", 1)[1].split(
            "  # ─── Deploy", 1
        )[0]

        self.assertIn("bash scripts/check_portability.sh", synth_job)
        portability = (ROOT / "infra/data/scripts/check_portability.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("AgentCore-coreAgent-data-us-gov-west-1", portability)
        self.assertIn("AgentCore-coreAgent-services-us-gov-west-1", portability)
        self.assertIn("arn:aws-us-gov:bedrock-agentcore", portability)
        self.assertIn("commercial ARN leaked", portability)

    def test_oidc_trust_is_scoped_to_the_protected_environment(self) -> None:
        workflow = (ROOT / ".github/workflows/ci-cd.yml").read_text(encoding="utf-8")

        self.assertIn(
            '"repo:<OWNER>/<REPOSITORY>:environment:production"',
            workflow,
        )
        self.assertNotIn('"repo:<OWNER>/<REPOSITORY>:*"', workflow)

    def test_production_deploy_requires_https_configuration_up_front(self) -> None:
        workflow = (ROOT / ".github/workflows/ci-cd.yml").read_text(encoding="utf-8")
        data_job = workflow.split("  deploy-data:", 1)[1].split(
            "  deploy-agent:", 1
        )[0]
        services_job = workflow.split("  deploy-services:", 1)[1].split(
            "  deploy-sandbox:", 1
        )[0]

        for variable in ("CERTIFICATE_ARN", "DOMAIN_NAME"):
            self.assertIn(variable, data_job)
            self.assertIn(variable, services_job)
        self.assertNotIn("both omitted for HTTP", services_job)

    def test_agent_runtime_is_discovered_and_forwarded(self) -> None:
        workflow = (ROOT / ".github/workflows/ci-cd.yml").read_text(encoding="utf-8")
        data_job = workflow.split("  deploy-data:", 1)[1].split("  deploy-agent:", 1)[0]

        self.assertNotIn("AGENT_RUNTIME_ARN", data_job)
        self.assertIn("bash scripts/resolve_agent_runtime.sh", workflow)
        self.assertNotIn("RUNTIME_NAME: coreAgent", workflow)
        resolver = (ROOT / "scripts/resolve_agent_runtime.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn('RUNTIME_NAME="${project_name}_${runtime_component}"', resolver)
        self.assertIn(
            "AGENT_RUNTIME_ARN: ${{ needs.deploy-agent.outputs.agent_runtime_arn }}",
            workflow,
        )

    def test_runtime_memory_environment_names_match_application(self) -> None:
        workflow = (ROOT / ".github/workflows/ci-cd.yml").read_text(encoding="utf-8")

        self.assertIn("AGENTCORE_SEMANTIC_STRATEGY_ID", workflow)
        self.assertIn("AGENTCORE_USER_PREF_STRATEGY_ID", workflow)
        self.assertNotIn("SEMANTIC_MEMORY_STRATEGY_ID", workflow)
        self.assertNotIn("USER_PREFERENCE_STRATEGY_ID", workflow)

    def test_manual_deploy_uses_one_configurable_region(self) -> None:
        workflow = (ROOT / ".github/workflows/ci-cd.yml").read_text(encoding="utf-8")
        deploy_jobs = workflow.split("  # ─── Deploy", 1)[1]

        self.assertIn("DEPLOY_REGION: ${{ vars.AWS_REGION || 'us-west-2' }}", workflow)
        self.assertIn('.region = $region', deploy_jobs)
        self.assertEqual(deploy_jobs.count("bash scripts/deploy_agent.sh --yes"), 1)
        self.assertNotIn("add_env", deploy_jobs)
        self.assertNotIn("agentcore.json.tmp", deploy_jobs)
        self.assertIn('--context "region=$DEPLOY_REGION"', deploy_jobs)
        self.assertIn("scripts/agentcore_cli_regions.txt", deploy_jobs)
        self.assertIn("not supported by the pinned AgentCore CLI", deploy_jobs)
        for variable in (
            "AWS_REGION",
            "GITHUB_APP_ID",
            "DOMAIN_NAME",
            "AGENTCORE_MEMORY_ID",
            "AGENTCORE_SEMANTIC_STRATEGY_ID",
            "AGENTCORE_USER_PREF_STRATEGY_ID",
            "DEPLOY_EXPERIMENTAL_SANDBOX",
        ):
            self.assertIn(variable, deploy_jobs)
        deploy_agent = deploy_jobs.split("  deploy-agent:", 1)[1].split(
            "  deploy-services:", 1
        )[0]
        self.assertNotIn("agentcore validate", deploy_agent)
        self.assertNotIn("run: agentcore deploy", deploy_agent)
        self.assertLess(
            deploy_agent.index("Write portable AgentCore target"),
            deploy_agent.index("Validate and deploy AgentCore runtime"),
        )
        deploy_data = deploy_jobs.split("  deploy-data:", 1)[1].split(
            "  deploy-agent:", 1
        )[0]
        self.assertLess(
            deploy_data.index("actions/checkout@"),
            deploy_data.index("Validate data deployment configuration"),
        )
        self.assertNotIn("aws-region: us-west-2", deploy_jobs)
        self.assertNotIn("AgentCore-coreAgent-services-us-west-2", deploy_jobs)
        self.assertNotIn("AgentCore-coreAgent-sandbox-us-west-2", deploy_jobs)

    def test_sandbox_wrapper_threads_its_region_into_cdk_context(self) -> None:
        wrapper = (ROOT / "infra/data/scripts/deploy_sandbox.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn('"--context" "region=$REGION"', wrapper)
        self.assertIn('REGION="$REGION" ATTACH_SANDBOX_POLICY=1', wrapper)

    def test_shell_aws_helpers_share_region_resolution(self) -> None:
        attach = (ROOT / "infra/data/scripts/attach_agent_policy.sh").read_text(
            encoding="utf-8"
        )
        sandbox = (ROOT / "infra/data/scripts/deploy_sandbox.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn('source "$SCRIPT_DIR/aws_region.sh"', attach)
        self.assertIn('source "$SCRIPT_DIR/aws_region.sh"', sandbox)
        self.assertGreaterEqual(attach.count('--region "$REGION"'), 5)

    def test_certificate_free_services_do_not_bake_localhost(self) -> None:
        services_stack = (ROOT / "infra/data/lib/services-stack.ts").read_text(
            encoding="utf-8"
        )

        self.assertNotIn("http://localhost:8000/slack/install", services_stack)
        self.assertIn(
            "const slackInstallUrl = `${publicUrl}/slack/install`", services_stack
        )
        self.assertIn("ONBOARDING_PUBLIC_URL: publicUrl", services_stack)


class SmokeCliContractTests(unittest.TestCase):
    def test_keep_alive_is_success_only_and_documents_fixture_cleanup(self) -> None:
        smoke = (ROOT / "scripts/smoke.py").read_text(encoding="utf-8")

        self.assertIn(
            "keep_services = args.keep_alive and run_completed and not results.failed",
            smoke,
        )
        self.assertIn("synthetic tenant files are still restored", smoke)
        self.assertIn("kill -KILL -- {process_groups}", smoke)

    def test_session_token_never_uses_the_deleted_welcome_query_flow(self) -> None:
        smoke = (ROOT / "scripts/smoke.py").read_text(encoding="utf-8")

        self.assertNotIn('params={"t": token}', smoke)
        self.assertNotIn("/welcome", smoke)
        self.assertIn("_assert_token_not_in_request_url", smoke)


class CliContractTests(unittest.TestCase):
    def test_shell_entrypoints_have_help(self) -> None:
        for script in ("setup.sh", "doctor.sh", "demo.sh", "check.sh"):
            result = subprocess.run(
                ["bash", str(ROOT / "scripts" / script), "--help"],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, msg=f"{script}: {result.stderr}")
            self.assertIn("Usage:", result.stdout)


if __name__ == "__main__":
    unittest.main()
