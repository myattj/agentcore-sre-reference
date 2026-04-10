#!/usr/bin/env python3
"""Provision the shared AgentCore Gateway after the GatewayStack is deployed.

CDK can't create the Gateway directly (no CFN resource type for
`bedrock-agentcore-control::Gateway` as of 2026-04). This script picks up
the Lambda ARN + role ARN from the deployed stack outputs and calls
`bedrock-agentcore-control:CreateGateway` directly.

What it creates:
  1. A single Gateway with protocolType=MCP, authorizerType=CUSTOM_JWT,
     pointing the JWT discoveryUrl at the bridge's
     /.well-known/openid-configuration route.
  2. The interceptor Lambda is wired in as a REQUEST interceptor at
     `BEFORE_TARGET` with `passRequestHeaders=true` (the interceptor
     needs the Authorization header to verify the JWT).
  3. The resulting Gateway ID + URL + ARN are written to SSM Parameter
     Store under /agentcore/gateway/{id,url,arn} so the bridge's
     per-tenant target provisioner (chunk D) can find them.

Idempotent: re-running the script when a Gateway already exists at the
same name updates the SSM params and re-reads the existing Gateway
state, rather than creating a duplicate. To force a clean re-create,
run delete_gateway.py first.

Usage:
  cd infra/data
  uv run --with boto3 python scripts/provision_gateway.py \\
    --gateway-name agentcore-shared-gateway \\
    --region us-west-2

  # The script reads BRIDGE_PUBLIC_URL from the deployed stack output,
  # so you don't pass it on the CLI here — set it as a CDK context
  # variable when you deployed the stack.

After this runs, smoke-test the Gateway with:
  aws bedrock-agentcore-control get-gateway --gateway-identifier <id>
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

import boto3
from botocore.exceptions import ClientError

DEFAULT_GATEWAY_NAME = "agentcore-shared-gateway"
DEFAULT_REGION = "us-west-2"
GATEWAY_STACK_NAME_TEMPLATE = "AgentCore-coreAgent-gateway-{region}"

SSM_GATEWAY_ID = "/agentcore/gateway/id"
SSM_GATEWAY_URL = "/agentcore/gateway/url"
SSM_GATEWAY_ARN = "/agentcore/gateway/arn"


# ----------------------------------------------------------------------------
# Output helpers
# ----------------------------------------------------------------------------

class Color:
    GREEN = "\033[92m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    GREY = "\033[90m"
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


# ----------------------------------------------------------------------------
# CFN stack outputs
# ----------------------------------------------------------------------------

def fetch_stack_outputs(stack_name: str, region: str) -> dict[str, str]:
    """Read CloudFormation outputs from the deployed GatewayStack."""
    cfn = boto3.client("cloudformation", region_name=region)
    try:
        resp = cfn.describe_stacks(StackName=stack_name)
    except ClientError as e:
        fail(f"could not read stack {stack_name}: {e}")
        raise SystemExit(1) from e

    stacks = resp.get("Stacks", [])
    if not stacks:
        fail(f"stack {stack_name} returned no Stacks entries")
        raise SystemExit(1)

    outputs = {
        o["OutputKey"]: o["OutputValue"]
        for o in (stacks[0].get("Outputs") or [])
    }
    expected = {
        "InterceptorLambdaArn",
        "GatewayRoleArn",
        "BridgeJwksUrl",
        "BridgeOidcDiscoveryUrl",
    }
    missing = expected - outputs.keys()
    if missing:
        fail(f"stack {stack_name} is missing required outputs: {sorted(missing)}")
        raise SystemExit(1)
    return outputs


# ----------------------------------------------------------------------------
# Gateway lookup / create
# ----------------------------------------------------------------------------

def find_existing_gateway(client: Any, gateway_name: str) -> dict[str, Any] | None:
    """Return the gateway dict if one with this name already exists."""
    paginator = client.get_paginator("list_gateways")
    for page in paginator.paginate():
        for gw in page.get("items", []):
            if gw.get("name") == gateway_name:
                return gw
    return None


def create_gateway(
    client: Any,
    *,
    name: str,
    role_arn: str,
    interceptor_lambda_arn: str,
    bridge_oidc_discovery_url: str,
) -> dict[str, Any]:
    """Call CreateGateway with our standard configuration.

    The interceptor is wired in as a REQUEST interceptor at the
    BEFORE_TARGET interception point with passRequestHeaders=true so
    the handler can read the Authorization header. The CUSTOM_JWT
    authorizer audience matches what bridge/bridge/gateway_jwt.py mints
    (`agentcore-gateway`).
    """
    request: dict[str, Any] = {
        "name": name,
        "description": (
            "Shared multi-tenant Gateway for the AgentCore platform. "
            "Per-tenant routing enforced by the gateway_interceptor Lambda."
        ),
        "roleArn": role_arn,
        "protocolType": "MCP",
        "authorizerType": "CUSTOM_JWT",
        "authorizerConfiguration": {
            "customJWTAuthorizer": {
                "discoveryUrl": bridge_oidc_discovery_url,
                "allowedAudience": ["agentcore-gateway"],
            },
        },
        "interceptorConfigurations": [
            {
                # Field name is `arn` (not `lambdaArn`) per the
                # bedrock-agentcore-control service model — verified via
                # the boto3 input shape introspection during chunk C dev.
                "interceptor": {"lambda": {"arn": interceptor_lambda_arn}},
                # Enum is REQUEST | RESPONSE (not BEFORE_TARGET / AFTER_TARGET).
                # REQUEST runs before the target invocation; that's where
                # we enforce tenant isolation.
                "interceptionPoints": ["REQUEST"],
                "inputConfiguration": {
                    # The handler bails if headers are missing — this MUST
                    # be true for the JWT to reach the interceptor.
                    "passRequestHeaders": True,
                },
            },
        ],
        "exceptionLevel": "DEBUG",  # verbose error reporting until chunk G smoke test passes
    }
    return client.create_gateway(**request)


def wait_for_gateway_ready(client: Any, gateway_id: str, timeout: int = 120) -> dict[str, Any]:
    """Poll the gateway until it's no longer in CREATING state.

    AgentCore Gateway provisioning is documented as fast (sub-minute) but
    we give it 2 min for headroom. Returns the final gateway dict.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        gw = client.get_gateway(gatewayIdentifier=gateway_id)
        status = gw.get("status", "")
        if status in ("READY", "ACTIVE"):
            return gw
        if status in ("FAILED", "DELETING", "DELETED"):
            fail(f"gateway entered terminal state {status}: {gw.get('statusReasons')}")
            raise SystemExit(1)
        time.sleep(3)
    fail(f"gateway did not become ready within {timeout}s; current status={gw.get('status')}")
    raise SystemExit(1)


# ----------------------------------------------------------------------------
# SSM publishing
# ----------------------------------------------------------------------------

def publish_to_ssm(region: str, *, gateway_id: str, gateway_url: str, gateway_arn: str) -> None:
    """Write the gateway coordinates to SSM Parameter Store.

    The bridge's chunk-D target provisioner reads these instead of
    repeatedly calling list_gateways or hardcoding the gateway ID.
    """
    ssm = boto3.client("ssm", region_name=region)
    for name, value in (
        (SSM_GATEWAY_ID, gateway_id),
        (SSM_GATEWAY_URL, gateway_url),
        (SSM_GATEWAY_ARN, gateway_arn),
    ):
        ssm.put_parameter(
            Name=name,
            Value=value,
            Type="String",
            Overwrite=True,
            Description=(
                "AgentCore shared Gateway coordinate. Set by "
                "infra/data/scripts/provision_gateway.py — do not edit by hand."
            ),
        )
        ok(f"SSM {name} = {value[:60]}...")


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--gateway-name",
        default=DEFAULT_GATEWAY_NAME,
        help=f"Name for the Gateway resource (default: {DEFAULT_GATEWAY_NAME})",
    )
    parser.add_argument(
        "--region",
        default=DEFAULT_REGION,
        help=f"AWS region (default: {DEFAULT_REGION})",
    )
    parser.add_argument(
        "--stack-name",
        default=None,
        help="Override the GatewayStack CFN stack name (default: derived from --region)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the CreateGateway request body without calling the API.",
    )
    args = parser.parse_args(argv)

    stack_name = args.stack_name or GATEWAY_STACK_NAME_TEMPLATE.format(region=args.region)

    step(f"reading CFN outputs from {stack_name}")
    outputs = fetch_stack_outputs(stack_name, args.region)
    for k in ("InterceptorLambdaArn", "GatewayRoleArn", "BridgeJwksUrl", "BridgeOidcDiscoveryUrl"):
        ok(f"{k} = {outputs[k]}")

    client = boto3.client("bedrock-agentcore-control", region_name=args.region)

    step(f"checking for existing gateway named {args.gateway_name!r}")
    existing = find_existing_gateway(client, args.gateway_name)
    if existing:
        gateway_id = existing["gatewayId"]
        warn(f"gateway already exists: id={gateway_id} status={existing.get('status')}")
        # list_gateways may not include gatewayUrl/gatewayArn — fetch full details.
        full = client.get_gateway(gatewayIdentifier=gateway_id)
        warn("re-publishing existing coordinates to SSM (idempotent path)")
        publish_to_ssm(
            args.region,
            gateway_id=gateway_id,
            gateway_url=full.get("gatewayUrl") or "",
            gateway_arn=full.get("gatewayArn") or "",
        )
        ok("done — to recreate, run delete_gateway.py first")
        return 0
    ok("no existing gateway; will create")

    if args.dry_run:
        step("dry-run: would call CreateGateway with this body")
        request_body = {
            "name": args.gateway_name,
            "roleArn": outputs["GatewayRoleArn"],
            "protocolType": "MCP",
            "authorizerType": "CUSTOM_JWT",
            "authorizerConfiguration": {
                "customJWTAuthorizer": {
                    "discoveryUrl": outputs["BridgeOidcDiscoveryUrl"],
                    "allowedAudience": ["agentcore-gateway"],
                },
            },
            "interceptorConfigurations": [
                {
                    "interceptor": {"lambda": {"arn": outputs["InterceptorLambdaArn"]}},
                    "interceptionPoints": ["REQUEST"],
                    "inputConfiguration": {"passRequestHeaders": True},
                },
            ],
        }
        print(json.dumps(request_body, indent=2))
        return 0

    step("calling CreateGateway")
    try:
        resp = create_gateway(
            client,
            name=args.gateway_name,
            role_arn=outputs["GatewayRoleArn"],
            interceptor_lambda_arn=outputs["InterceptorLambdaArn"],
            bridge_oidc_discovery_url=outputs["BridgeOidcDiscoveryUrl"],
        )
    except ClientError as e:
        fail(f"CreateGateway failed: {e}")
        return 1
    gateway_id = resp["gatewayId"]
    ok(f"created: gatewayId={gateway_id}")

    step("waiting for gateway to be ready")
    final = wait_for_gateway_ready(client, gateway_id)
    ok(f"status={final['status']}")
    ok(f"gatewayUrl={final.get('gatewayUrl')}")

    step("publishing coordinates to SSM Parameter Store")
    publish_to_ssm(
        args.region,
        gateway_id=gateway_id,
        gateway_url=final.get("gatewayUrl") or "",
        gateway_arn=final.get("gatewayArn") or "",
    )

    step("done")
    print(
        f"  Verify the JWKS URL is reachable from AWS:\n"
        f"    curl {outputs['BridgeJwksUrl']}\n"
        f"  Then chunk D can provision per-tenant targets against:\n"
        f"    {final.get('gatewayUrl')}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
