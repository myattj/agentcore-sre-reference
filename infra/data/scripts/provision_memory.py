#!/usr/bin/env python3
"""Provision the shared AgentCore Memory resource.

Creates a single memory resource with SEMANTIC + USER_PREFERENCE built-in
strategies. All tenants share this resource; isolation is enforced via
namespace (actorId = {tenant_id}_{channel_id} or {tenant_id}_{user_id}).

The resulting memory ID + strategy IDs are published to SSM Parameter
Store under /agentcore/memory/* so the agent runtime can find them.

Idempotent: re-running when the memory resource already exists updates
SSM params from the existing resource rather than creating a duplicate.
To force a clean re-create, run delete_memory.py first.

Usage:
  cd infra/data
  uv run --with boto3 python scripts/provision_memory.py \\
    --memory-name agentcore_shared_memory \\
    --region us-west-2

After this runs, set the env var on the agent:
  AGENTCORE_MEMORY_ID=<memory_id from SSM>
"""
from __future__ import annotations

import argparse
import sys
import time
from typing import Any

import boto3
from botocore.exceptions import ClientError

DEFAULT_MEMORY_NAME = "agentcore_shared_memory"
DEFAULT_REGION = "us-west-2"
DEFAULT_EVENT_EXPIRY_DAYS = 90

SSM_MEMORY_ID = "/agentcore/memory/id"
SSM_SEMANTIC_STRATEGY_ID = "/agentcore/memory/semantic_strategy_id"
SSM_USER_PREF_STRATEGY_ID = "/agentcore/memory/user_preference_strategy_id"

SSM_PARAMS = (SSM_MEMORY_ID, SSM_SEMANTIC_STRATEGY_ID, SSM_USER_PREF_STRATEGY_ID)


# ----------------------------------------------------------------------------
# Output helpers (same style as provision_gateway.py)
# ----------------------------------------------------------------------------

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


# ----------------------------------------------------------------------------
# Memory lookup / create
# ----------------------------------------------------------------------------

def find_existing_memory(client: Any, memory_name: str) -> dict[str, Any] | None:
    """Return the memory dict if one with this name already exists."""
    try:
        resp = client.list_memories(maxResults=100)
        for mem in resp.get("memories", []):
            if mem.get("name") == memory_name:
                return mem
    except ClientError as e:
        fail(f"list_memories failed: {e}")
        raise SystemExit(1) from e
    return None


def create_memory(client: Any, *, name: str, event_expiry_days: int) -> dict[str, Any]:
    """Call CreateMemory with SEMANTIC + USER_PREFERENCE built-in strategies.

    Both strategies use the default namespace template which scopes
    records to /strategies/{memoryStrategyId}/actors/{actorId}/. The
    agent sets actorId = {tenant_id}_{channel_id} (or {tenant_id}_{user_id}
    for DMs) at invocation time, giving us per-channel workspace-level
    memory with per-tenant isolation.
    """
    import uuid

    strategies = [
        {
            "semanticMemoryStrategy": {
                "name": "semantic",
                "description": "Extracts factual memories from conversations",
                "namespaces": ["/strategies/{memoryStrategyId}/actors/{actorId}/"],
            }
        },
        {
            "userPreferenceMemoryStrategy": {
                "name": "user_preferences",
                "description": "Extracts user preferences from conversations",
                "namespaces": ["/strategies/{memoryStrategyId}/actors/{actorId}/"],
            }
        },
    ]

    resp = client.create_memory(
        name=name,
        description=(
            "Shared multi-tenant memory for the AgentCore platform. "
            "Per-tenant isolation via namespace (actorId prefix)."
        ),
        eventExpiryDuration=event_expiry_days,
        memoryStrategies=strategies,
        clientToken=str(uuid.uuid4()),
    )
    return resp["memory"]


def wait_for_memory_active(
    client: Any, memory_id: str, timeout: int = 300
) -> dict[str, Any]:
    """Poll until the memory resource reaches ACTIVE status.

    Memory provisioning can take a few minutes as AgentCore sets up
    the underlying infrastructure for each strategy.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = client.get_memory(memoryId=memory_id)
        memory = resp["memory"]
        status = memory.get("status", "")
        if status == "ACTIVE":
            return memory
        if status in ("FAILED", "DELETING"):
            fail(f"memory entered terminal state {status}: {memory.get('failureReason', 'unknown')}")
            raise SystemExit(1)
        elapsed = int(timeout - (deadline - time.time()))
        print(f"  ... status={status} ({elapsed}s elapsed)", flush=True)
        time.sleep(10)
    fail(f"memory did not become ACTIVE within {timeout}s")
    raise SystemExit(1)


def extract_strategy_ids(memory: dict[str, Any]) -> dict[str, str]:
    """Extract strategy IDs from the memory response, keyed by strategy name."""
    result: dict[str, str] = {}
    for strategy in memory.get("strategies", memory.get("memoryStrategies", [])):
        # Built-in strategies nest under their type key; flatten to get name + id
        for key in (
            "semanticMemoryStrategy",
            "userPreferenceMemoryStrategy",
            "summaryMemoryStrategy",
            "episodicMemoryStrategy",
        ):
            if key in strategy:
                inner = strategy[key]
                name = inner.get("name", key)
                sid = inner.get("memoryStrategyId") or inner.get("strategyId") or inner.get("id", "")
                if sid:
                    result[name] = sid
                break
        # Also check flattened response format (newer API versions)
        if "name" in strategy and ("strategyId" in strategy or "memoryStrategyId" in strategy):
            name = strategy["name"]
            sid = strategy.get("strategyId") or strategy.get("memoryStrategyId", "")
            if sid:
                result[name] = sid
    return result


# ----------------------------------------------------------------------------
# SSM publishing
# ----------------------------------------------------------------------------

def publish_to_ssm(
    region: str,
    *,
    memory_id: str,
    semantic_strategy_id: str,
    user_pref_strategy_id: str,
) -> None:
    """Write memory coordinates to SSM Parameter Store."""
    ssm = boto3.client("ssm", region_name=region)
    for name, value in (
        (SSM_MEMORY_ID, memory_id),
        (SSM_SEMANTIC_STRATEGY_ID, semantic_strategy_id),
        (SSM_USER_PREF_STRATEGY_ID, user_pref_strategy_id),
    ):
        ssm.put_parameter(
            Name=name,
            Value=value,
            Type="String",
            Overwrite=True,
            Description=(
                "AgentCore shared Memory coordinate. Set by "
                "infra/data/scripts/provision_memory.py — do not edit by hand."
            ),
        )
        ok(f"SSM {name} = {value}")


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--memory-name",
        default=DEFAULT_MEMORY_NAME,
        help=f"Name for the Memory resource (default: {DEFAULT_MEMORY_NAME})",
    )
    parser.add_argument(
        "--region",
        default=DEFAULT_REGION,
        help=f"AWS region (default: {DEFAULT_REGION})",
    )
    parser.add_argument(
        "--event-expiry-days",
        type=int,
        default=DEFAULT_EVENT_EXPIRY_DAYS,
        help=f"Event retention in days (default: {DEFAULT_EVENT_EXPIRY_DAYS})",
    )
    args = parser.parse_args(argv)

    client = boto3.client("bedrock-agentcore-control", region_name=args.region)

    step(f"checking for existing memory named {args.memory_name!r}")
    existing = find_existing_memory(client, args.memory_name)

    if existing:
        memory_id = existing.get("id") or existing.get("memoryId", "")
        warn(f"memory already exists: id={memory_id} status={existing.get('status')}")

        # Fetch full details for strategy IDs
        full = client.get_memory(memoryId=memory_id)["memory"]
        strategy_ids = extract_strategy_ids(full)
        ok(f"strategies: {strategy_ids}")

        step("re-publishing existing coordinates to SSM (idempotent path)")
        publish_to_ssm(
            args.region,
            memory_id=memory_id,
            semantic_strategy_id=strategy_ids.get("semantic", ""),
            user_pref_strategy_id=strategy_ids.get("user_preferences", ""),
        )
        ok("done — to recreate, run delete_memory.py first")
        return 0
    ok("no existing memory; will create")

    step("calling CreateMemory")
    try:
        memory = create_memory(
            client,
            name=args.memory_name,
            event_expiry_days=args.event_expiry_days,
        )
    except ClientError as e:
        fail(f"CreateMemory failed: {e}")
        return 1
    memory_id = memory.get("id") or memory.get("memoryId", "")
    ok(f"created: memoryId={memory_id}")

    step("waiting for memory to become ACTIVE (may take a few minutes)")
    final = wait_for_memory_active(client, memory_id)
    ok(f"status={final['status']}")

    strategy_ids = extract_strategy_ids(final)
    ok(f"strategies: {strategy_ids}")

    step("publishing coordinates to SSM Parameter Store")
    publish_to_ssm(
        args.region,
        memory_id=memory_id,
        semantic_strategy_id=strategy_ids.get("semantic", ""),
        user_pref_strategy_id=strategy_ids.get("user_preferences", ""),
    )

    step("done")
    print(
        f"\n  Set this env var on the agent runtime:\n"
        f"    AGENTCORE_MEMORY_ID={memory_id}\n"
        f"\n  Verify with:\n"
        f"    aws bedrock-agentcore-control get-memory --memory-id {memory_id} --region {args.region}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
