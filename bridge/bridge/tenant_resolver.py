"""Workspace → tenant resolution.

v0: reads `examples/workspace_to_tenant.json` from the repo root. Walks up
from this file to find it. Replace with DynamoDB or Postgres lookup when
the workspace count grows past a handful.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def _load_mapping() -> dict[str, str]:
    """Load and cache the workspace→tenant mapping. Cache is process-lifetime;
    bounce the bridge to pick up edits in v0."""
    current = Path(__file__).resolve()
    for parent in current.parents:
        candidate = parent / "examples" / "workspace_to_tenant.json"
        if candidate.exists():
            return json.loads(candidate.read_text())
    raise FileNotFoundError(
        f"workspace_to_tenant.json not found above {current}"
    )


def resolve_tenant_id(workspace_id: str) -> str:
    """Map a client workspace_id to a tenant_id. Falls back to 'demo' if
    the workspace is unknown — useful for local testing, but production
    should reject unknown workspaces explicitly."""
    return _load_mapping().get(workspace_id, "demo")
