from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
AWS_SCRIPTS = ROOT / "infra/data/scripts"

REGION_CLI_PATHS = (
    AWS_SCRIPTS / "provision_gateway.py",
    AWS_SCRIPTS / "provision_memory.py",
    AWS_SCRIPTS / "delete_gateway.py",
    AWS_SCRIPTS / "delete_memory.py",
    AWS_SCRIPTS / "migrate_add_check_task_status.py",
    AWS_SCRIPTS / "audit_query.py",
    AWS_SCRIPTS / "seed_tenants.py",
)


def load_module(name: str, path: Path) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


aws_region = load_module("aws_region", AWS_SCRIPTS / "aws_region.py")


class ParserCaptured(Exception):
    """Stop a CLI after argparse is configured and before application work starts."""


class AwsRegionResolutionTests(unittest.TestCase):
    def test_region_environment_precedence(self) -> None:
        cases = (
            (
                {"AWS_REGION": "eu-west-1", "AWS_DEFAULT_REGION": "ap-southeast-2"},
                "me-central-1",
                "eu-west-1",
            ),
            (
                {"AWS_DEFAULT_REGION": "ap-southeast-2"},
                "me-central-1",
                "ap-southeast-2",
            ),
            ({}, "me-central-1", "me-central-1"),
            ({}, None, "us-west-2"),
        )

        for environ, profile_region, expected in cases:
            with self.subTest(environ=environ):
                with mock.patch.object(
                    aws_region,
                    "_configured_profile_region",
                    return_value=profile_region,
                ):
                    self.assertEqual(
                        aws_region.resolve_default_region(environ),
                        expected,
                    )

    def test_every_cli_parser_uses_the_shared_region_default_without_aws(self) -> None:
        fake_boto3 = types.ModuleType("boto3")
        fake_boto3.client = mock.Mock(name="boto3.client")  # type: ignore[attr-defined]
        fake_boto3.resource = mock.Mock(name="boto3.resource")  # type: ignore[attr-defined]

        fake_botocore = types.ModuleType("botocore")
        fake_exceptions = types.ModuleType("botocore.exceptions")

        class FakeClientError(Exception):
            pass

        fake_exceptions.ClientError = FakeClientError  # type: ignore[attr-defined]
        fake_botocore.exceptions = fake_exceptions  # type: ignore[attr-defined]

        environments = (
            (
                {"AWS_REGION": "eu-west-1", "AWS_DEFAULT_REGION": "ap-southeast-2"},
                "me-central-1",
                "eu-west-1",
            ),
            (
                {"AWS_DEFAULT_REGION": "ap-southeast-2"},
                "me-central-1",
                "ap-southeast-2",
            ),
            ({"AWS_PROFILE": "sandbox"}, "me-central-1", "me-central-1"),
            ({}, None, "us-west-2"),
        )

        observed: list[str] = []

        def capture_region_default(
            parser: argparse.ArgumentParser,
            *_args: object,
            **_kwargs: object,
        ) -> argparse.Namespace:
            observed.append(parser.get_default("region"))
            raise ParserCaptured

        fake_modules = {
            "boto3": fake_boto3,
            "botocore": fake_botocore,
            "botocore.exceptions": fake_exceptions,
        }
        with mock.patch.dict(sys.modules, fake_modules):
            for case_index, (environ, profile_region, expected) in enumerate(
                environments
            ):
                with mock.patch.dict(os.environ, environ, clear=True):
                    with mock.patch.object(
                        aws_region,
                        "_configured_profile_region",
                        return_value=profile_region,
                    ):
                        for cli_path in REGION_CLI_PATHS:
                            with self.subTest(cli=cli_path.name, environ=environ):
                                module = load_module(
                                    f"aws_region_cli_{case_index}_{cli_path.stem}",
                                    cli_path,
                                )
                                with (
                                    mock.patch.object(
                                        argparse.ArgumentParser,
                                        "parse_args",
                                        new=capture_region_default,
                                    ),
                                    self.assertRaises(ParserCaptured),
                                ):
                                    module.main()
                                self.assertEqual(observed[-1], expected)

        fake_boto3.client.assert_not_called()  # type: ignore[attr-defined]
        fake_boto3.resource.assert_not_called()  # type: ignore[attr-defined]

    def test_shell_region_helper_matches_the_same_precedence(self) -> None:
        helper = AWS_SCRIPTS / "aws_region.sh"
        cases = (
            (
                {
                    "REGION": "ca-central-1",
                    "AWS_REGION": "eu-west-1",
                    "AWS_DEFAULT_REGION": "ap-southeast-2",
                },
                "ca-central-1",
            ),
            (
                {"AWS_REGION": "eu-west-1", "AWS_DEFAULT_REGION": "ap-southeast-2"},
                "eu-west-1",
            ),
            ({"AWS_DEFAULT_REGION": "ap-southeast-2"}, "ap-southeast-2"),
            (
                {
                    "AWS_PROFILE": "sandbox",
                    "FAKE_PROFILE_REGION": "me-central-1",
                },
                "me-central-1",
            ),
            ({}, "us-west-2"),
        )
        with tempfile.TemporaryDirectory() as temporary:
            fake_aws = Path(temporary) / "aws"
            fake_aws.write_text(
                "#!/usr/bin/env bash\n"
                "set -eu\n"
                '[[ " $* " == *" configure get region "* ]]\n'
                '[[ -n "${FAKE_PROFILE_REGION:-}" ]] || exit 1\n'
                'printf "%s\\n" "$FAKE_PROFILE_REGION"\n',
                encoding="utf-8",
            )
            fake_aws.chmod(0o755)
            for selected, expected in cases:
                with self.subTest(selected=selected):
                    env = os.environ.copy()
                    for name in (
                        "REGION",
                        "AWS_REGION",
                        "AWS_DEFAULT_REGION",
                        "AWS_PROFILE",
                        "AWS_DEFAULT_PROFILE",
                        "FAKE_PROFILE_REGION",
                    ):
                        env.pop(name, None)
                    env["PATH"] = f"{temporary}:{env['PATH']}"
                    env.update(selected)
                    result = subprocess.run(
                        [
                            "bash",
                            "-c",
                            'source "$1"; resolve_aws_region',
                            "bash",
                            str(helper),
                        ],
                        env=env,
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    self.assertEqual(result.returncode, 0, msg=result.stderr)
                    self.assertEqual(result.stdout.strip(), expected)


if __name__ == "__main__":
    unittest.main()
