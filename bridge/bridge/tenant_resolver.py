"""Workspace → tenant resolution.

Storage:
  - LOCAL_DEV=1: reads `examples/workspace_to_tenant.json` from the repo root.
                 Falls back to "demo" on unknown workspaces (convenient for
                 `curl /debug/message`).
  - else:        DynamoDB table (name via WORKSPACE_TO_TENANT_TABLE, default
                 "workspace_to_tenant"). Raises on unknown workspaces — the
                 production path should never silently route strangers to
                 the demo tenant.

The module-level `resolve_tenant_id()` function is the only public API; the
bridge imports it directly. Implementation is a lazy singleton so the DDB
client isn't constructed at import time in local dev.
"""
from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol


class WorkspaceResolver(Protocol):
    """Maps a client workspace_id (Slack team_id, Discord guild_id, ...)
    to the tenant_id that owns that workspace."""

    def resolve(self, workspace_id: str) -> str: ...


class JsonFileWorkspaceResolver:
    """Reads `examples/workspace_to_tenant.json` from the repo root.

    Walks up from this file to find it. Falls back to `"demo"` on unknown
    workspaces so that local `curl -d '{"workspace_id":"demo-ws"}'` calls
    work without manual config editing. This fallback is LOCAL-DEV-ONLY —
    the Dynamo resolver raises instead.
    """

    def __init__(self) -> None:
        self._mapping: dict[str, str] | None = None

    def _load(self) -> dict[str, str]:
        if self._mapping is not None:
            return self._mapping
        current = Path(__file__).resolve()
        for parent in current.parents:
            candidate = parent / "examples" / "workspace_to_tenant.json"
            if candidate.exists():
                self._mapping = json.loads(candidate.read_text())
                return self._mapping
        raise FileNotFoundError(
            f"workspace_to_tenant.json not found above {current}"
        )

    def resolve(self, workspace_id: str) -> str:
        return self._load().get(workspace_id, "demo")


class DynamoWorkspaceResolver:
    """Reads from a DynamoDB table keyed by workspace_id.

    Item shape:
        {workspace_id: str, tenant_id: str, created_at, updated_at}

    Unknown workspaces raise `KeyError`. The bridge is expected to map
    unknown-workspace errors to a 404 or an "app not installed" reply
    rather than silently routing traffic to the demo tenant.

    Caches successful lookups in-process with an LRU of 1024 entries.
    If an existing workspace mapping changes out of band, restart the
    bridge or call cache_clear().
    """

    def __init__(self, table_name: str, region: str | None = None) -> None:
        self.table_name = table_name
        self.region = region or os.getenv("AWS_REGION", "us-west-2")
        self._table: Any | None = None
        # Wrap the actual lookup in an lru_cache attached to the instance.
        # A bound method can't be @lru_cache'd directly (unbound self
        # reference), so we build the cached function here.
        self._cached_lookup = lru_cache(maxsize=1024)(self._lookup_uncached)

    def _get_table(self) -> Any:
        if self._table is None:
            import boto3

            resource = boto3.resource("dynamodb", region_name=self.region)
            self._table = resource.Table(self.table_name)
        return self._table

    def _lookup_uncached(self, workspace_id: str) -> str:
        response = self._get_table().get_item(Key={"workspace_id": workspace_id})
        item = response.get("Item")
        if not item:
            raise KeyError(
                f"No tenant mapping for workspace_id={workspace_id!r} "
                f"in table={self.table_name!r}"
            )
        tenant_id = item.get("tenant_id")
        if not tenant_id:
            raise KeyError(
                f"Mapping row for workspace_id={workspace_id!r} has no tenant_id"
            )
        return str(tenant_id)

    def resolve(self, workspace_id: str) -> str:
        return self._cached_lookup(workspace_id)

    def cache_clear(self) -> None:
        self._cached_lookup.cache_clear()


# ----------------------------------------------------------------------------
# Lazy singleton
# ----------------------------------------------------------------------------

_default_resolver: WorkspaceResolver | None = None


def _resolver() -> WorkspaceResolver:
    global _default_resolver
    if _default_resolver is None:
        if os.getenv("LOCAL_DEV") == "1":
            _default_resolver = JsonFileWorkspaceResolver()
        else:
            _default_resolver = DynamoWorkspaceResolver(
                table_name=os.getenv("WORKSPACE_TO_TENANT_TABLE", "workspace_to_tenant"),
            )
    return _default_resolver


def resolve_tenant_id(workspace_id: str) -> str:
    """Map a client workspace_id to a tenant_id.

    LOCAL_DEV=1 falls back to "demo" on unknown workspaces. The Dynamo path
    raises KeyError — callers (`bridge/main.py`) should translate that into
    an appropriate HTTP response.
    """
    return _resolver().resolve(workspace_id)


def reset_resolver_for_tests() -> None:
    """Test helper: clear the cached resolver so the next call re-reads env vars."""
    global _default_resolver
    _default_resolver = None
