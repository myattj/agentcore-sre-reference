from __future__ import annotations

import ast
import io
import json
import os
import sys
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock
from urllib.parse import unquote

from scripts.testenv import bootstrap
from scripts.testenv.config import TESTENV_CHANNELS, build_testenv_config
from scripts.testenv.integrations import seed_datadog


ROOT = Path(__file__).resolve().parents[2]


def _pydantic_field_names(class_name: str) -> set[str]:
    """Read declared model fields without importing bridge dependencies."""
    source = (ROOT / "bridge/bridge/api_models.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return {
                statement.target.id
                for statement in node.body
                if isinstance(statement, ast.AnnAssign)
                and isinstance(statement.target, ast.Name)
            }
    raise AssertionError(f"missing bridge model {class_name}")


def _channel_map() -> dict[str, str]:
    return {name: f"C{index:03d}" for index, name in enumerate(TESTENV_CHANNELS)}


class TestEnvPatchPayloadTests(unittest.TestCase):
    def test_payload_is_tenant_safe_and_matches_current_patch_models(self) -> None:
        payload = build_testenv_config(
            _channel_map(),
            github_org="acme-labs",
        )

        self.assertNotIn("byo", payload)
        self.assertNotIn("cost_cap", payload)
        self.assertNotIn("is_internal_testenv", payload)
        self.assertNotIn("namespace", payload["memory"])
        self.assertTrue(payload["memory"]["shared_across_channels"])
        self.assertNotIn("github_installation_id", json.dumps(payload))
        self.assertTrue(payload["codebases"]["enabled"])

        self.assertLessEqual(
            set(payload),
            _pydantic_field_names("TenantConfigPatch"),
        )
        self.assertLessEqual(
            set(payload["memory"]),
            _pydantic_field_names("MemoryConfigPatch"),
        )
        self.assertLessEqual(
            set(payload["codebases"]),
            _pydantic_field_names("CodebasesConfigPatch"),
        )

    def test_codebases_stay_disabled_without_operator_approved_org(self) -> None:
        payload = build_testenv_config(_channel_map())

        self.assertFalse(payload["codebases"]["enabled"])
        self.assertEqual(payload["codebases"]["bindings"], [])
        self.assertNotIn("github_installation_id", payload["codebases"])


class IntegrationSelectionTests(unittest.TestCase):
    def test_all_excludes_content_only_datadog(self) -> None:
        selected = bootstrap.parse_integrations("all")

        self.assertEqual(selected, ["pagerduty", "jira", "linear", "sentry"])
        self.assertNotIn("datadog", selected)

    def test_datadog_cannot_be_selected_through_bootstrap(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            r"content-seed-only.*--skip-connect",
        ):
            bootstrap.parse_integrations("pagerduty,datadog")

    def test_datadog_seeder_fails_closed_without_acknowledgement(self) -> None:
        with (
            mock.patch.object(seed_datadog, "load_integration_secret") as load_secret,
            redirect_stdout(io.StringIO()),
        ):
            result = seed_datadog.run_seed(
                "slack-test",
                skip_connect=False,
            )

        self.assertEqual(result, 2)
        load_secret.assert_not_called()


class GitHubApprovalTests(unittest.TestCase):
    def _fake_httpx(
        self,
        *,
        status_code: int,
        response_text: str = "",
    ) -> tuple[types.SimpleNamespace, dict[str, object]]:
        request: dict[str, object] = {}

        class FakeClient:
            def __init__(self, *, timeout: float) -> None:
                request["timeout"] = timeout

            def __enter__(self) -> "FakeClient":
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def post(self, url: str, **kwargs: object) -> types.SimpleNamespace:
                request["url"] = url
                request.update(kwargs)
                body = kwargs["json"]
                tenant_id = unquote(url.split("/tenants/", 1)[1].split("/", 1)[0])
                return types.SimpleNamespace(
                    status_code=status_code,
                    text=response_text,
                    json=lambda: {
                        "approved": True,
                        "tenant_id": tenant_id,
                        "installation_id": str(body["installation_id"]),  # type: ignore[index]
                        "account_login": body["expected_account_login"],  # type: ignore[index]
                    },
                )

        return types.SimpleNamespace(Client=FakeClient), request

    def test_approval_uses_narrow_endpoint_numeric_body_and_header(self) -> None:
        admin_secret = "admin-secret-that-must-not-leak"
        fake_httpx, request = self._fake_httpx(status_code=200)
        output = io.StringIO()

        with (
            mock.patch.dict(sys.modules, {"httpx": fake_httpx}),
            redirect_stdout(output),
            redirect_stderr(output),
        ):
            bootstrap.approve_github_installation(
                "slack/T TEST",
                12345,
                "acme-labs",
                bridge_url="https://bridge.example",
                admin_secret=admin_secret,
            )

        self.assertEqual(
            request["url"],
            "https://bridge.example/api/ops/tenants/slack%2FT%20TEST/"
            "codebases/github/approve",
        )
        self.assertEqual(request["headers"], {"X-Admin-Token": admin_secret})
        self.assertEqual(
            request["json"],
            {
                "installation_id": 12345,
                "expected_account_login": "acme-labs",
            },
        )
        self.assertIsInstance(request["json"]["installation_id"], int)  # type: ignore[index]
        self.assertNotIn(admin_secret, str(request["url"]))
        self.assertNotIn(admin_secret, json.dumps(request["json"]))
        self.assertNotIn(admin_secret, output.getvalue())

    def test_approval_error_does_not_reflect_response_or_secret(self) -> None:
        admin_secret = "do-not-reflect-this-admin-secret"
        fake_httpx, _ = self._fake_httpx(
            status_code=409,
            response_text=f"proxy reflected {admin_secret}",
        )
        output = io.StringIO()

        with (
            mock.patch.dict(sys.modules, {"httpx": fake_httpx}),
            redirect_stdout(output),
            redirect_stderr(output),
            self.assertRaisesRegex(
                RuntimeError, r"approval failed \(HTTP 409\)"
            ) as raised,
        ):
            bootstrap.approve_github_installation(
                "slack-TTEST",
                12345,
                "acme-labs",
                bridge_url="https://bridge.example",
                admin_secret=admin_secret,
            )

        self.assertNotIn(admin_secret, str(raised.exception))
        self.assertNotIn(admin_secret, output.getvalue())

    def test_approval_rejects_plain_http_except_on_loopback(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "loopback"):
            bootstrap.approve_github_installation(
                "slack-TTEST",
                12345,
                "acme-labs",
                bridge_url="http://bridge.example",
                admin_secret="secret",
            )

        fake_httpx, request = self._fake_httpx(status_code=200)
        with mock.patch.dict(sys.modules, {"httpx": fake_httpx}):
            bootstrap.approve_github_installation(
                "slack-TTEST",
                12345,
                "acme-labs",
                bridge_url="http://127.0.0.1:8000",
                admin_secret="secret",
            )
        self.assertEqual(
            request["url"],
            "http://127.0.0.1:8000/api/ops/tenants/slack-TTEST/"
            "codebases/github/approve",
        )

    def test_admin_secret_prefers_environment_without_secret_store_lookup(self) -> None:
        with (
            mock.patch.dict(os.environ, {"ADMIN_SECRET": "from-env"}, clear=True),
            mock.patch.object(bootstrap, "_load_bridge_secret_value") as fallback,
        ):
            self.assertEqual(bootstrap.load_admin_secret("us-west-2"), "from-env")

        fallback.assert_not_called()

    def test_admin_secret_falls_back_to_existing_bridge_secret_json(self) -> None:
        admin_secret = "from-existing-bridge-secret"
        secrets_client = mock.Mock()
        secrets_client.get_secret_value.return_value = {
            "SecretString": json.dumps(
                {
                    "ADMIN_SECRET": admin_secret,
                    "BRIDGE_OAUTH_STATE_SECRET": "state-secret",
                }
            )
        }
        fake_boto3 = types.SimpleNamespace(
            client=mock.Mock(return_value=secrets_client),
        )
        output = io.StringIO()

        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.dict(sys.modules, {"boto3": fake_boto3}),
            mock.patch.object(
                bootstrap,
                "_find_bridge_secret_name",
                return_value="agentcore/services/bridge-abc123",
            ),
            redirect_stdout(output),
            redirect_stderr(output),
        ):
            loaded = bootstrap.load_admin_secret("us-west-2")

        self.assertEqual(loaded, admin_secret)
        fake_boto3.client.assert_called_once_with(  # type: ignore[attr-defined]
            "secretsmanager",
            region_name="us-west-2",
        )
        secrets_client.get_secret_value.assert_called_once_with(
            SecretId="agentcore/services/bridge-abc123"
        )
        self.assertNotIn(admin_secret, output.getvalue())

    def test_approval_failure_aborts_before_binding_patch_is_built(self) -> None:
        channel_map = _channel_map()
        output = io.StringIO()
        with (
            mock.patch.object(bootstrap, "configure_logging"),
            mock.patch.object(bootstrap, "verify_tenant_exists", return_value={}),
            mock.patch.object(bootstrap, "mark_internal_testenv"),
            mock.patch.object(
                bootstrap,
                "load_seeder_bot_token",
                return_value="xoxb-test-token",
            ),
            mock.patch.object(bootstrap, "make_slack_client", return_value=object()),
            mock.patch.object(bootstrap, "SeederState", return_value=object()),
            mock.patch.object(
                bootstrap,
                "discover_and_join",
                return_value=(channel_map, []),
            ),
            mock.patch.object(bootstrap, "load_admin_secret", return_value="secret"),
            mock.patch.object(
                bootstrap,
                "approve_github_installation",
                side_effect=RuntimeError(
                    "GitHub installation approval failed (HTTP 409)"
                ),
            ),
            mock.patch.object(bootstrap, "build_testenv_config") as build_config,
            mock.patch.object(bootstrap, "patch_tenant_config") as patch_config,
            redirect_stdout(output),
            redirect_stderr(output),
        ):
            result = bootstrap.run_bootstrap(
                "slack-TTEST",
                region="us-west-2",
                bridge_url="https://bridge.example",
                github_org="acme-labs",
                github_installation_id="12345",
                skip_seed=True,
                skip_patch=False,
                integrations=[],
            )

        self.assertEqual(result, 1)
        build_config.assert_not_called()
        patch_config.assert_not_called()
        self.assertIn("approval failed", output.getvalue())

    def test_github_arguments_are_all_or_nothing_and_numeric(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "must be provided together"):
            bootstrap.validate_github_setup("acme-labs", None)
        with self.assertRaisesRegex(RuntimeError, "must be numeric"):
            bootstrap.validate_github_setup("acme-labs", "not-a-number")
        self.assertEqual(
            bootstrap.validate_github_setup("acme-labs", "12345"),
            ("acme-labs", 12345),
        )


class InternalTestMarkerTests(unittest.TestCase):
    def test_dynamodb_update_is_narrowly_scoped_to_internal_marker(self) -> None:
        table = mock.Mock()
        resource = mock.Mock()
        resource.Table.return_value = table
        fake_boto3 = types.SimpleNamespace(
            resource=mock.Mock(return_value=resource),
        )

        with (
            mock.patch.dict(sys.modules, {"boto3": fake_boto3}),
            mock.patch.dict(os.environ, {"TENANTS_TABLE": "tenant-table"}, clear=True),
        ):
            bootstrap.mark_internal_testenv("slack-TTEST", "us-west-2")

        fake_boto3.resource.assert_called_once_with(  # type: ignore[attr-defined]
            "dynamodb",
            region_name="us-west-2",
        )
        resource.Table.assert_called_once_with("tenant-table")
        table.update_item.assert_called_once_with(
            Key={"tenant_id": "slack-TTEST"},
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


if __name__ == "__main__":
    unittest.main()
