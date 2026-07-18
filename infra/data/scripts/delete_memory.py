#!/usr/bin/env python3
"""Tear down the shared AgentCore Memory resource provisioned by provision_memory.py.

Deletes:
  1. The memory resource (DeleteMemory)
  2. The SSM parameters under /agentcore/memory/*

The IAM permissions in data-stack.ts are NOT removed — those live in
the CDK stack and are cleaned up by `cdk destroy`.

Usage:
  cd infra/data
  uv run --with boto3 python scripts/delete_memory.py \\
    --memory-name agentcore_shared_memory \\
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

DEFAULT_MEMORY_NAME = "agentcore_shared_memory"
DEFAULT_REGION = resolve_default_region()

SSM_PARAMS = (
    "/agentcore/memory/id",
    "/agentcore/memory/semantic_strategy_id",
    "/agentcore/memory/user_preference_strategy_id",
)


class Color:
    GREEN = "\033[92m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    BOLD = "\033[1m"
    RESET = "\033[0m"


def step(msg: str) -> None:
    print(f"\n{Color.BOLD}{Color.BLUE}\u25b6 {msg}{Color.RESET}")


def ok(msg: str) -> None:
    print(f"  {Color.GREEN}\u2713{Color.RESET} {msg}")


def warn(msg: str) -> None:
    print(f"  {Color.YELLOW}!{Color.RESET} {msg}")


def fail(msg: str) -> None:
    print(f"  {Color.RED}\u2717 {msg}{Color.RESET}")


def find_memory(client: Any, name: str) -> dict[str, Any] | None:
    try:
        resp = client.list_memories(maxResults=100)
        for mem in resp.get("memories", []):
            if mem.get("name") == name:
                return mem
    except ClientError as e:
        fail(f"list_memories failed: {e}")
        raise SystemExit(1) from e
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--memory-name", default=DEFAULT_MEMORY_NAME)
    parser.add_argument("--region", default=DEFAULT_REGION)
    parser.add_argument("--yes", action="store_true", help="Skip confirmation prompt")
    args = parser.parse_args(argv)

    client = boto3.client("bedrock-agentcore-control", region_name=args.region)

    step(f"looking for memory {args.memory_name!r}")
    mem = find_memory(client, args.memory_name)
    if not mem:
        warn("no memory found; nothing to delete")
        # Still purge SSM in case stale params linger.
        _purge_ssm(args.region)
        return 0

    memory_id = mem.get("id") or mem.get("memoryId", "")
    ok(f"found: id={memory_id} status={mem.get('status')}")

    if not args.yes:
        confirm = input(
            f"\n{Color.YELLOW}!{Color.RESET} About to delete memory {memory_id} "
            f"and all its data. This is irreversible. Type 'yes' to proceed: "
        )
        if confirm.strip() != "yes":
            print("aborted")
            return 1

    step("deleting memory")
    try:
        client.delete_memory(memoryId=memory_id)
    except ClientError as e:
        fail(f"DeleteMemory failed: {e}")
        return 1
    ok("DeleteMemory accepted; waiting for completion")

    # Poll until ResourceNotFound confirms deletion.
    deadline = time.time() + 300
    while time.time() < deadline:
        try:
            resp = client.get_memory(memoryId=memory_id)
            status = resp.get("memory", {}).get("status", "")
            if status in ("FAILED",):
                warn(f"memory entered FAILED state during deletion")
                break
            elapsed = int(300 - (deadline - time.time()))
            print(f"  ... status={status} ({elapsed}s elapsed)", flush=True)
        except ClientError as e:
            if "ResourceNotFound" in str(e) or "NotFound" in str(e):
                ok("memory deleted")
                break
            fail(f"unexpected error polling: {e}")
            return 1
        time.sleep(10)
    else:
        warn("memory did not finish deleting within 300s — check console")

    _purge_ssm(args.region)

    step("done")
    return 0


def _purge_ssm(region: str) -> None:
    step("purging SSM parameters")
    ssm = boto3.client("ssm", region_name=region)
    for name in SSM_PARAMS:
        try:
            ssm.delete_parameter(Name=name)
            ok(f"deleted SSM {name}")
        except ssm.exceptions.ParameterNotFound:
            warn(f"SSM {name} not found (already gone)")


if __name__ == "__main__":
    sys.exit(main())
