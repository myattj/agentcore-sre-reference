from __future__ import annotations

import importlib.util
import json
import os
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

        fake_bin = root / "fake-bin"
        fake_bin.mkdir()
        call_log = root / "calls.log"
        for name in ("python3.13", "uv", "npm", "gitleaks"):
            tool = fake_bin / name
            tool.write_text(
                "#!/usr/bin/env bash\n"
                'if [ "$(basename "$0")" = "gitleaks" ]; then\n'
                '  printf "%s\\n" "$*" >> "$CHECK_CALL_LOG"\n'
                "fi\n"
                "exit 0\n",
                encoding="utf-8",
            )
            tool.chmod(0o755)
        return fake_bin, call_log

    def run_check(
        self, root: Path, fake_bin: Path, call_log: Path
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["PATH"] = f"{fake_bin}:{env['PATH']}"
        env["CHECK_CALL_LOG"] = str(call_log)
        return subprocess.run(
            ["bash", str(root / "scripts/check.sh"), "--quick"],
            cwd=root,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_source_archive_scans_tree_and_skips_only_git_metadata_check(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            fake_bin, call_log = self.make_check_root(root)

            result = self.run_check(root, fake_bin, call_log)

            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertIn("source tree", result.stdout)
            self.assertIn("source archive has no .git metadata", result.stdout)
            self.assertEqual(
                call_log.read_text(encoding="utf-8").strip(),
                f"dir --redact --no-banner {root}",
            )

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
                "git --redact --no-banner",
            )


class ReleaseWorkflowContractTests(unittest.TestCase):
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
        self.assertIn("list-agent-runtimes", workflow)
        self.assertIn("get-agent-runtime", workflow)
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
