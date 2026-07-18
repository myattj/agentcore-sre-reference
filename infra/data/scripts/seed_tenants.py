#!/usr/bin/env python3
"""Seed DynamoDB tables from the local JSON fixtures.

Reads `examples/tenants/*.json` and `examples/workspace_to_tenant.json`
from the repo root and writes them into the two DynamoDB tables created
by `infra/data/lib/data-stack.ts`. Idempotent: re-running refreshes
`updated_at` but preserves the original `created_at` via a conditional
update.

The tenant-row write path goes through the authoritative
`coreAgent.tenant.DynamoTenantStore.upsert`. Slack OAuth and the onboarding
UI mirror that schema in `bridge/bridge/tenant_write.py`; keep both shapes
in sync. Workspace mappings use the same DynamoDB row shape as OAuth.

Usage:
    # Dry-run (no writes, just prints what would change):
    uv run --with boto3 python infra/data/scripts/seed_tenants.py --dry-run

    # Actual seed (uses default table names from the data stack):
    uv run --with boto3 python infra/data/scripts/seed_tenants.py

    # Custom table names / region:
    uv run --with boto3 python infra/data/scripts/seed_tenants.py \\
        --tenants-table tenants \\
        --workspace-table workspace_to_tenant \\
        --region us-west-2

Credentials: picks up the user's default AWS profile via boto3. Run
`aws sts get-caller-identity` first to confirm you're pointed at the
right account.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def find_repo_root() -> Path:
    """Walk up from this script until we find a directory containing
    `examples/tenants/`."""
    current = Path(__file__).resolve()
    for parent in current.parents:
        if (parent / "examples" / "tenants").is_dir():
            return parent
    raise FileNotFoundError(
        f"Could not find examples/tenants/ above {current}. "
        "Run this script from within the agent repo."
    )


def load_tenant_files(root: Path) -> list[tuple[str, dict[str, Any]]]:
    """Return a list of (tenant_id, config_dict) for every JSON file in
    examples/tenants/."""
    tenants_dir = root / "examples" / "tenants"
    out: list[tuple[str, dict[str, Any]]] = []
    for json_path in sorted(tenants_dir.glob("*.json")):
        tenant_id = json_path.stem
        data = json.loads(json_path.read_text())
        # Sanity check: the file's tenant_id should match its filename.
        file_tid = data.get("tenant_id")
        if file_tid and file_tid != tenant_id:
            raise ValueError(
                f"{json_path}: tenant_id in file ({file_tid!r}) does not "
                f"match filename ({tenant_id!r})"
            )
        out.append((tenant_id, data))
    return out


def load_workspace_mapping(root: Path) -> dict[str, str]:
    """Return the workspace_id → tenant_id dict."""
    path = root / "examples" / "workspace_to_tenant.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def put_tenant(store: Any, tenant_id: str, config_data: dict[str, Any], dry_run: bool) -> None:
    """Write one tenant row via the shared `DynamoTenantStore.upsert`
    primitive in `coreAgent/app/coreAgent/tenant.py`.

    The seed file is validated as a `TenantConfig` first so a malformed
    fixture is caught at seed time, not at first request.
    """
    if dry_run:
        print(f"  [dry-run] would upsert tenant_id={tenant_id!r}")
        return
    # Imported lazily so --dry-run doesn't need coreAgent on sys.path.
    # The path setup happens in main() when not dry-run.
    from tenant import TenantConfig  # type: ignore[import-not-found]

    config = TenantConfig.model_validate(config_data)
    store.upsert(config)
    print(f"  upserted tenant_id={tenant_id!r}")


def put_workspace(table: Any, workspace_id: str, tenant_id: str, dry_run: bool) -> None:
    """Write one workspace → tenant mapping row."""
    now = iso_now()
    if dry_run:
        print(f"  [dry-run] would upsert workspace_id={workspace_id!r} -> {tenant_id!r}")
        return
    table.update_item(
        Key={"workspace_id": workspace_id},
        UpdateExpression=(
            "SET tenant_id = :tid, "
            "updated_at = :now, "
            "created_at = if_not_exists(created_at, :now)"
        ),
        ExpressionAttributeValues={":tid": tenant_id, ":now": now},
    )
    print(f"  upserted workspace_id={workspace_id!r} -> tenant_id={tenant_id!r}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenants-table", default="tenants")
    parser.add_argument("--workspace-table", default="workspace_to_tenant")
    parser.add_argument("--region", default="us-west-2")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be written; don't actually call DynamoDB.")
    args = parser.parse_args()

    root = find_repo_root()
    print(f"Repo root: {root}")

    tenants = load_tenant_files(root)
    mappings = load_workspace_mapping(root)
    print(f"Found {len(tenants)} tenant file(s) and {len(mappings)} workspace mapping(s).")

    if not args.dry_run:
        import boto3

        # The agent's modules import each other as top-level names
        # (e.g. `from tenant import ...`), matching the AgentCore CLI's
        # `codeLocation: app/coreAgent/` runtime layout. Mirror that
        # here so we can construct DynamoTenantStore from the same source.
        agent_src = find_repo_root() / "coreAgent" / "app" / "coreAgent"
        if str(agent_src) not in sys.path:
            sys.path.insert(0, str(agent_src))

        from tenant import DynamoTenantStore  # type: ignore[import-not-found]

        tenants_store = DynamoTenantStore(
            table_name=args.tenants_table, region=args.region
        )
        dynamodb = boto3.resource("dynamodb", region_name=args.region)
        workspace_table = dynamodb.Table(args.workspace_table)
    else:
        tenants_store = None
        workspace_table = None

    print(f"\nSeeding {args.tenants_table!r}:")
    for tenant_id, config in tenants:
        put_tenant(tenants_store, tenant_id, config, args.dry_run)

    print(f"\nSeeding {args.workspace_table!r}:")
    for workspace_id, tenant_id in mappings.items():
        put_workspace(workspace_table, workspace_id, tenant_id, args.dry_run)

    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
