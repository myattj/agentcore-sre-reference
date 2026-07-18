from __future__ import annotations

import importlib.util
import io
import json
import os
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "approve_github_installation",
    ROOT / "scripts/approve_github_installation.py",
)
assert SPEC and SPEC.loader
approval_cli = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(approval_cli)


class GitHubApprovalCliTests(unittest.TestCase):
    def test_plain_http_requires_loopback(self) -> None:
        with self.assertRaisesRegex(Exception, "loopback"):
            approval_cli._bridge_origin("http://bridge.example.test")
        self.assertEqual(
            approval_cli._bridge_origin("http://127.0.0.1:8000/"),
            "http://127.0.0.1:8000",
        )

    def test_approval_uses_env_secret_and_canonical_payload(self) -> None:
        captured = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self):
                return json.dumps(
                    {
                        "approved": True,
                        "tenant_id": "slack-acme",
                        "installation_id": "12345",
                        "account_login": "acme-org",
                    }
                ).encode()

        def fake_open(request, timeout):
            captured["url"] = request.full_url
            captured["secret"] = request.headers["X-admin-token"]
            captured["body"] = json.loads(request.data)
            captured["timeout"] = timeout
            return Response()

        with (
            mock.patch.dict(os.environ, {"ADMIN_SECRET": "operator-secret"}),
            mock.patch.object(
                approval_cli.urllib.request,
                "urlopen",
                side_effect=fake_open,
            ),
            mock.patch("sys.stdout", new=io.StringIO()),
        ):
            result = approval_cli.main(
                [
                    "slack-acme",
                    "12345",
                    "acme-org",
                    "--bridge-url",
                    "https://bridge.example.test",
                ]
            )

        self.assertEqual(result, 0)
        self.assertEqual(
            captured,
            {
                "url": (
                    "https://bridge.example.test/api/ops/tenants/slack-acme/"
                    "codebases/github/approve"
                ),
                "secret": "operator-secret",
                "body": {
                    "installation_id": 12345,
                    "expected_account_login": "acme-org",
                },
                "timeout": 20,
            },
        )


if __name__ == "__main__":
    unittest.main()
