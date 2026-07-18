#!/usr/bin/env python3
"""Verify and bind a GitHub App installation to one Agent tenant.

ADMIN_SECRET is read from the environment and never accepted as a command-line
argument, keeping it out of the process argument list. Prompt for it before
exporting so the value is not recorded in shell history, and unset it afterward.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


def _bridge_origin(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise argparse.ArgumentTypeError("bridge URL must be an HTTP(S) origin")
    if parsed.scheme == "http":
        hostname = parsed.hostname.lower()
        is_loopback = hostname == "localhost"
        if not is_loopback:
            try:
                is_loopback = ipaddress.ip_address(hostname).is_loopback
            except ValueError:
                is_loopback = False
        if not is_loopback:
            raise argparse.ArgumentTypeError(
                "plain HTTP is allowed only for a loopback bridge"
            )
    return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")


def _positive_installation_id(value: str) -> int:
    try:
        number = int(value, 10)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("installation ID must be an integer") from exc
    if number <= 0 or number > 2**63 - 1:
        raise argparse.ArgumentTypeError(
            "installation ID must be a positive 64-bit integer"
        )
    return number


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Approve an exclusive GitHub App installation binding",
    )
    parser.add_argument("tenant_id")
    parser.add_argument("installation_id", type=_positive_installation_id)
    parser.add_argument("expected_account_login")
    parser.add_argument(
        "--bridge-url",
        type=_bridge_origin,
        default=_bridge_origin(os.getenv("BRIDGE_PUBLIC_URL", "http://localhost:8000")),
    )
    args = parser.parse_args(argv)

    admin_secret = os.getenv("ADMIN_SECRET", "")
    if not admin_secret:
        parser.error("ADMIN_SECRET must be set in the environment")

    tenant = urllib.parse.quote(args.tenant_id, safe="")
    url = f"{args.bridge_url}/api/ops/tenants/{tenant}/codebases/github/approve"
    payload = json.dumps(
        {
            "installation_id": args.installation_id,
            "expected_account_login": args.expected_account_login,
        }
    ).encode()
    request = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Admin-Token": admin_secret,
            "User-Agent": "Agent-GitHub-Approval/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            result = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        print(f"Approval failed (HTTP {exc.code}): {detail}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"Approval failed: could not reach the bridge ({exc})", file=sys.stderr)
        return 1

    print(
        "Approved GitHub installation "
        f"{result['installation_id']} ({result['account_login']}) "
        f"for tenant {result['tenant_id']}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
