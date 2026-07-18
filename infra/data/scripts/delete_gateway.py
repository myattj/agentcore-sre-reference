#!/usr/bin/env python3
"""Tear down the shared AgentCore Gateway provisioned by provision_gateway.py.

This is the inverse of provision_gateway.py — it deletes:
  1. Every gateway target attached to the gateway (DeleteGatewayTarget)
  2. The gateway itself (DeleteGateway)
  3. The SSM parameters under /agentcore/gateway/*

The interceptor Lambda + IAM roles are NOT deleted by this script —
those live in the GatewayStack and are removed by `cdk destroy`.

Use this before reprovisioning after changing the JWT issuer URL or other
Gateway configuration that cannot be updated in place.

Usage:
  cd infra/data
  uv run --with boto3 python scripts/delete_gateway.py \\
    --gateway-name agentcore-shared-gateway \\
    --region us-west-2

Pass --yes to skip the confirmation prompt.
"""
from __future__ import annotations

import argparse
import sys
import time
from typing import Any

import boto3
from botocore.exceptions import ClientError

from aws_region import resolve_default_region

DEFAULT_GATEWAY_NAME = "agentcore-shared-gateway"
DEFAULT_REGION = resolve_default_region()

SSM_PARAMS = (
    "/agentcore/gateway/id",
    "/agentcore/gateway/url",
    "/agentcore/gateway/arn",
)


class Color:
    GREEN = "\033[92m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    BOLD = "\033[1m"
    RESET = "\033[0m"


def step(msg: str) -> None:
    print(f"\n{Color.BOLD}{Color.BLUE}▶ {msg}{Color.RESET}")


def ok(msg: str) -> None:
    print(f"  {Color.GREEN}✓{Color.RESET} {msg}")


def warn(msg: str) -> None:
    print(f"  {Color.YELLOW}!{Color.RESET} {msg}")


def fail(msg: str) -> None:
    print(f"  {Color.RED}✗ {msg}{Color.RESET}")


def find_gateway(client: Any, name: str) -> dict[str, Any] | None:
    paginator = client.get_paginator("list_gateways")
    for page in paginator.paginate():
        for gw in page.get("items", []):
            if gw.get("name") == name:
                return gw
    return None


def delete_all_targets(client: Any, gateway_id: str) -> int:
    """Delete every target attached to the gateway. Returns count deleted."""
    count = 0
    paginator = client.get_paginator("list_gateway_targets")
    target_ids: list[str] = []
    for page in paginator.paginate(gatewayIdentifier=gateway_id):
        for target in page.get("items", []):
            target_ids.append(target.get("targetId") or target.get("id"))
    for target_id in target_ids:
        if not target_id:
            continue
        try:
            client.delete_gateway_target(
                gatewayIdentifier=gateway_id, targetId=target_id
            )
            ok(f"deleted target {target_id}")
            count += 1
        except ClientError as e:
            warn(f"failed to delete target {target_id}: {e}")
    return count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway-name", default=DEFAULT_GATEWAY_NAME)
    parser.add_argument("--region", default=DEFAULT_REGION)
    parser.add_argument("--yes", action="store_true", help="Skip confirmation prompt")
    args = parser.parse_args(argv)

    client = boto3.client("bedrock-agentcore-control", region_name=args.region)

    step(f"looking for gateway {args.gateway_name!r}")
    gw = find_gateway(client, args.gateway_name)
    if not gw:
        warn("no gateway found; nothing to delete")
        # Still purge SSM in case a stale param lingers.
        ssm = boto3.client("ssm", region_name=args.region)
        for name in SSM_PARAMS:
            try:
                ssm.delete_parameter(Name=name)
                ok(f"deleted SSM {name}")
            except ssm.exceptions.ParameterNotFound:
                pass
        return 0

    gateway_id = gw["gatewayId"]
    ok(f"found: id={gateway_id} status={gw.get('status')}")

    if not args.yes:
        confirm = input(
            f"\n{Color.YELLOW}!{Color.RESET} About to delete gateway {gateway_id} "
            f"and all its targets. Type 'yes' to proceed: "
        )
        if confirm.strip() != "yes":
            print("aborted")
            return 1

    step("deleting all gateway targets")
    n = delete_all_targets(client, gateway_id)
    ok(f"deleted {n} target(s)")

    step("deleting gateway")
    try:
        client.delete_gateway(gatewayIdentifier=gateway_id)
    except ClientError as e:
        fail(f"DeleteGateway failed: {e}")
        return 1
    ok("DeleteGateway accepted; waiting for completion")

    # Poll for deletion. Once GetGateway raises ResourceNotFound, it's gone.
    deadline = time.time() + 120
    while time.time() < deadline:
        try:
            client.get_gateway(gatewayIdentifier=gateway_id)
        except ClientError as e:
            if "ResourceNotFound" in str(e) or "NotFound" in str(e):
                ok("gateway deleted")
                break
        time.sleep(3)
    else:
        warn("gateway did not finish deleting within 120s — check console")

    step("purging SSM parameters")
    ssm = boto3.client("ssm", region_name=args.region)
    for name in SSM_PARAMS:
        try:
            ssm.delete_parameter(Name=name)
            ok(f"deleted SSM {name}")
        except ssm.exceptions.ParameterNotFound:
            warn(f"SSM {name} not found (already gone)")

    step("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
