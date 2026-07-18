#!/usr/bin/env python3
"""Operator CLI for querying the `audit_log` DynamoDB table.

Use it to answer questions such as "how much is tenant X costing me?"
or "what tools did tenant X call yesterday?" directly from audit data.

The audit table is keyed by (tenant_id, sk). Two row types share the
sort key namespace:
  - INV#{iso_ts}#{invocation_id}        — one per @app.entrypoint call
  - TOOL#{iso_ts}#{invocation_id}#{u8}  — one per catalog tool call

All queries are scoped by tenant_id, so this script never does a Scan.

Usage:
    # Recent invocations for a tenant (default 20):
    uv run --with boto3 python infra/data/scripts/audit_query.py recent \\
        --tenant slack-t12345 --limit 50

    # Token cost estimate for the last 7 days:
    uv run --with boto3 python infra/data/scripts/audit_query.py cost \\
        --tenant slack-t12345 --days 7

    # Recent tool calls:
    uv run --with boto3 python infra/data/scripts/audit_query.py tools \\
        --tenant slack-t12345 --limit 50

The cost estimate uses hardcoded Sonnet 4.6 per-token prices (see
SONNET_4_6_PRICING). Update those if Bedrock pricing changes or if you
add multi-model support.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from typing import Any


# Per-million-token prices for Claude Sonnet 4.6 via Bedrock (us-west-2).
# Update if pricing changes or if multiple models need attribution.
SONNET_4_6_PRICING = {
    "input_per_million": 3.00,
    "output_per_million": 15.00,
}


def _connect_table(table_name: str, region: str) -> Any:
    import boto3

    return boto3.resource("dynamodb", region_name=region).Table(table_name)


def _query_rows(
    table: Any,
    tenant_id: str,
    sk_prefix: str,
    limit: int,
    since_iso: str | None = None,
) -> list[dict[str, Any]]:
    """Query rows for one tenant whose `sk` starts with `sk_prefix`,
    optionally filtered to `sk >= "{sk_prefix}{since_iso}"` so we get
    rows since a given timestamp. Newest first.

    Note: DDB Query with `begins_with` returns items in sort-key order;
    pass `ScanIndexForward=False` to get newest-first. The `since_iso`
    filter narrows further by composing the lower bound into the SK
    range condition.
    """
    from boto3.dynamodb.conditions import Key

    if since_iso:
        # SK format is "{prefix}{iso_ts}#{...}". `between` is exclusive
        # of nothing it can express here, so use `gte` + `begins_with`
        # combined via a Filter expression. Simpler: do `begins_with`
        # and post-filter on the iso_ts substring; the prefix scan is
        # still cheap because we cap with Limit.
        condition = Key("tenant_id").eq(tenant_id) & Key("sk").begins_with(sk_prefix)
    else:
        condition = Key("tenant_id").eq(tenant_id) & Key("sk").begins_with(sk_prefix)

    response = table.query(
        KeyConditionExpression=condition,
        ScanIndexForward=False,  # newest first
        Limit=limit,
    )
    items = response.get("Items", [])

    if since_iso:
        # SK format: "{prefix}{iso_ts}#..."  →  ts is between prefix and first '#'
        prefix_len = len(sk_prefix)
        items = [
            row for row in items
            if row.get("sk", "")[prefix_len:prefix_len + len(since_iso)] >= since_iso
        ]
    return items


# ----------------------------------------------------------------------------
# Subcommands
# ----------------------------------------------------------------------------

def cmd_recent(args: argparse.Namespace) -> int:
    table = _connect_table(args.table, args.region)
    rows = _query_rows(table, args.tenant, "INV#", args.limit)
    if not rows:
        print(f"No invocation rows for tenant_id={args.tenant!r}")
        return 0

    print(f"{'timestamp':<26} {'model':<40} {'in':>6} {'out':>6} {'ms':>6}  ok  user")
    print("-" * 110)
    for row in rows:
        ts = str(row.get("timestamp", ""))[:25]
        model = str(row.get("model_id", ""))[:40]
        in_t = int(row.get("input_tokens", 0))
        out_t = int(row.get("output_tokens", 0))
        ms = int(row.get("duration_ms", 0))
        ok = "y" if row.get("success") else "n"
        user = str(row.get("user_id", ""))[:20]
        print(f"{ts:<26} {model:<40} {in_t:>6} {out_t:>6} {ms:>6}  {ok}   {user}")
    return 0


def cmd_cost(args: argparse.Namespace) -> int:
    table = _connect_table(args.table, args.region)
    since = datetime.now(timezone.utc) - timedelta(days=args.days)
    since_iso = since.isoformat(timespec="milliseconds").replace("+00:00", "Z")

    # We query a generous Limit then post-filter; for production-scale
    # tenants this should switch to paginated Query.
    rows = _query_rows(table, args.tenant, "INV#", limit=10_000, since_iso=since_iso)

    if not rows:
        print(f"No invocations in the last {args.days}d for tenant_id={args.tenant!r}")
        return 0

    total_in = sum(int(row.get("input_tokens", 0)) for row in rows)
    total_out = sum(int(row.get("output_tokens", 0)) for row in rows)
    in_cost = total_in / 1_000_000 * SONNET_4_6_PRICING["input_per_million"]
    out_cost = total_out / 1_000_000 * SONNET_4_6_PRICING["output_per_million"]
    total_cost = in_cost + out_cost

    print(f"Tenant:        {args.tenant}")
    print(f"Window:        last {args.days} days (since {since_iso})")
    print(f"Invocations:   {len(rows):,}")
    print(f"Input tokens:  {total_in:,}")
    print(f"Output tokens: {total_out:,}")
    print(f"Input cost:    ${in_cost:,.4f}")
    print(f"Output cost:   ${out_cost:,.4f}")
    print(f"TOTAL:         ${total_cost:,.4f}")
    print()
    print("(Pricing: Claude Sonnet 4.6 via Bedrock; "
          f"${SONNET_4_6_PRICING['input_per_million']}/M in, "
          f"${SONNET_4_6_PRICING['output_per_million']}/M out)")
    return 0


def cmd_tools(args: argparse.Namespace) -> int:
    table = _connect_table(args.table, args.region)
    rows = _query_rows(table, args.tenant, "TOOL#", args.limit)
    if not rows:
        print(f"No tool_call rows for tenant_id={args.tenant!r}")
        return 0

    print(f"{'timestamp':<26} {'tool_name':<32} {'ms':>6}  ok")
    print("-" * 80)
    for row in rows:
        ts = str(row.get("timestamp", ""))[:25]
        name = str(row.get("tool_name", ""))[:32]
        ms = int(row.get("duration_ms", 0))
        ok = "y" if row.get("success") else "n"
        print(f"{ts:<26} {name:<32} {ms:>6}  {ok}")
    return 0


# ----------------------------------------------------------------------------
# argparse
# ----------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--table", default="audit_log",
                        help="DynamoDB table name (default: audit_log)")
    parser.add_argument("--region", default="us-west-2")

    sub = parser.add_subparsers(dest="cmd", required=True)

    p_recent = sub.add_parser("recent", help="Recent invocation rows for a tenant")
    p_recent.add_argument("--tenant", required=True, help="tenant_id to query")
    p_recent.add_argument("--limit", type=int, default=20)
    p_recent.set_defaults(func=cmd_recent)

    p_cost = sub.add_parser("cost", help="Token cost estimate over the last N days")
    p_cost.add_argument("--tenant", required=True)
    p_cost.add_argument("--days", type=int, default=7)
    p_cost.set_defaults(func=cmd_cost)

    p_tools = sub.add_parser("tools", help="Recent tool_call rows for a tenant")
    p_tools.add_argument("--tenant", required=True)
    p_tools.add_argument("--limit", type=int, default=20)
    p_tools.set_defaults(func=cmd_tools)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
