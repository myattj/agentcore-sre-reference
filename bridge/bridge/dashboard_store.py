"""Read-only access to the dashboards DynamoDB table.

The agent writes dashboard specs via its `render_dashboard` tool. The bridge
only reads them — the onboarding service calls ``GET /internal/dashboard``
with the bearer token in a request header, which delegates here.

Uses the same lazy-singleton DDB resource pattern as ``tenant_write.py``.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_DASHBOARDS_TABLE_NAME = os.getenv("DASHBOARDS_TABLE", "dashboards")
_TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")
_PUBLIC_FIELDS = ("created_at", "ttl", "title", "panels")


class DashboardStoreError(RuntimeError):
    """Dashboard storage is unavailable or returned a malformed record."""


# Reuse the lazy DDB resource from tenant_write to avoid creating a
# second boto3 resource per process. Import is deferred so the module
# loads even if tenant_write hasn't been imported yet.
_table_singleton: Any | None = None


def _table() -> Any:
    global _table_singleton
    if _table_singleton is None:
        from .tenant_write import _get_table

        region = os.getenv("AWS_REGION", os.getenv("AWS_DEFAULT_REGION", "us-west-2"))
        _table_singleton = _get_table(region, _DASHBOARDS_TABLE_NAME)
    return _table_singleton


def is_valid_dashboard_token(token: str) -> bool:
    return bool(_TOKEN_RE.fullmatch(token))


def _local_dashboards_dir() -> Path:
    configured = os.getenv("DASHBOARD_LOCAL_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    current = Path(__file__).resolve()
    for parent in current.parents:
        examples = parent / "examples"
        if examples.is_dir():
            return examples / "dashboards"
    raise DashboardStoreError("local dashboard directory could not be resolved")


def _read_item(token: str) -> dict[str, Any] | None:
    if os.getenv("LOCAL_DEV") == "1":
        path = _local_dashboards_dir() / f"{token}.json"
        if path.is_symlink() or not path.is_file():
            return None
        try:
            item = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise DashboardStoreError("local dashboard record could not be read") from exc
        if not isinstance(item, dict):
            raise DashboardStoreError("local dashboard record is malformed")
        return item

    try:
        response = _table().get_item(Key={"token": token}, ConsistentRead=True)
    except Exception as exc:
        log.exception("dashboard_store: DynamoDB read failed")
        raise DashboardStoreError("dashboard storage is unavailable") from exc
    item = response.get("Item")
    if item is not None and not isinstance(item, dict):
        raise DashboardStoreError("dashboard record is malformed")
    return item


def get_dashboard_spec(token: str) -> dict[str, Any] | None:
    """Fetch an unexpired public dashboard spec without private metadata."""
    if not is_valid_dashboard_token(token):
        return None

    item = _read_item(token)
    if item is None:
        return None

    converted = _convert_decimals(item)
    ttl = converted.get("ttl")
    if not isinstance(ttl, int):
        raise DashboardStoreError("dashboard record has an invalid ttl")
    # DynamoDB TTL deletion is asynchronous and can lag by days. Enforce the
    # access deadline here so an expired bearer URL stops working on time.
    if ttl <= int(time.time()):
        return None
    if not isinstance(converted.get("created_at"), str):
        raise DashboardStoreError("dashboard record has an invalid created_at")
    if not isinstance(converted.get("title"), str):
        raise DashboardStoreError("dashboard record has an invalid title")
    if not isinstance(converted.get("panels"), list):
        raise DashboardStoreError("dashboard record has invalid panels")

    # tenant_id, created_by, and token are audit/storage metadata. A bearer
    # link grants access to the dashboard, not to internal tenant/user IDs.
    return {field: converted[field] for field in _PUBLIC_FIELDS}


def _convert_decimals(obj: Any) -> Any:
    """Recursively convert Decimal values to int or float."""
    if isinstance(obj, Decimal):
        if not obj.is_finite():
            raise DashboardStoreError("dashboard record contains a non-finite number")
        return int(obj) if obj == int(obj) else float(obj)
    if isinstance(obj, dict):
        return {k: _convert_decimals(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_convert_decimals(v) for v in obj]
    return obj
