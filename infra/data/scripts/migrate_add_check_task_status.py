#!/usr/bin/env python3
"""One-shot migration: add 'check_task_status' to every tenant's allowed_tools.

Scans the tenants DDB table. For each tenant whose
config.catalog.allowed_tools does NOT already include
'check_task_status', appends the tool and writes the row back.

Idempotent — safe to re-run.

Usage:
    # Dry-run (no writes):
    uv run --with boto3 python infra/data/scripts/migrate_add_check_task_status.py --dry-run

    # Actual migration:
    uv run --with boto3 python infra/data/scripts/migrate_add_check_task_status.py

    # Custom table / region:
    uv run --with boto3 python infra/data/scripts/migrate_add_check_task_status.py \
        --table tenants --region us-west-2
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

import boto3

from aws_region import resolve_default_region

TOOL_NAME = "check_task_status"
DEFAULT_REGION = resolve_default_region()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=f"Add '{TOOL_NAME}' to every tenant's allowed_tools.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print changes without writing")
    parser.add_argument("--table", default="tenants", help="DDB table name (default: tenants)")
    parser.add_argument("--region", default=DEFAULT_REGION)
    args = parser.parse_args()

    table = boto3.resource("dynamodb", region_name=args.region).Table(args.table)

    # Paginated scan — handles tables with >1 MB of items.
    items: list[dict] = []
    scan_kwargs: dict = {}
    while True:
        resp = table.scan(**scan_kwargs)
        items.extend(resp.get("Items", []))
        last_key = resp.get("LastEvaluatedKey")
        if not last_key:
            break
        scan_kwargs["ExclusiveStartKey"] = last_key

    print(f"Found {len(items)} tenant(s) in {args.table}\n")

    updated = 0
    skipped = 0

    for item in items:
        tenant_id = item.get("tenant_id", "?")
        config = item.get("config") or {}
        catalog = config.get("catalog") or {}
        tools: list[str] = catalog.get("allowed_tools") or []

        if TOOL_NAME in tools:
            print(f"  skip  {tenant_id} — already has {TOOL_NAME}")
            skipped += 1
            continue

        tools.append(TOOL_NAME)

        if args.dry_run:
            print(f"  [dry-run] would add {TOOL_NAME} to {tenant_id}")
            updated += 1
            continue

        # Minimal update: only touch the config blob + updated_at.
        config.setdefault("catalog", {})["allowed_tools"] = tools
        now = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        table.update_item(
            Key={"tenant_id": tenant_id},
            UpdateExpression="SET #config = :config, updated_at = :now",
            ExpressionAttributeNames={"#config": "config"},
            ExpressionAttributeValues={":config": config, ":now": now},
        )
        print(f"  updated {tenant_id}")
        updated += 1

    action = "Would update" if args.dry_run else "Updated"
    print(f"\nDone. {action}: {updated}, Skipped: {skipped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
