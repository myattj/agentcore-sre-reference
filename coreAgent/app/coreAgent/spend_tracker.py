"""Per-tenant monthly spend tracking.

Maintains a running counter on the DynamoDB tenant row as top-level
attributes (NOT inside the config blob):
  - ``monthly_spend_cents``: integer cents accumulated this month
  - ``spend_month``: calendar month string e.g. "2026-04"

The counter is managed atomically via DynamoDB conditional updates so
concurrent invocations for the same tenant cannot race past the cap.

Three implementations (same factory pattern as audit.py):
  - ``DynamoSpendTracker``: production, uses the tenants table
  - ``InMemorySpendTracker``: smoke tests (``LOCAL_AUDIT=memory``)
  - ``NullSpendTracker``: local dev (``AGENT_LOCAL_STORES=1``), always allows
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timezone
from typing import Any, Protocol

log = logging.getLogger(__name__)


class SpendTracker(Protocol):
    """Contract for spend tracking implementations."""

    def check_budget(self, tenant_id: str, cap_cents: int) -> tuple[bool, int]:
        """Check if the tenant is within budget.

        Returns (allowed, current_spend_cents). ``allowed`` is False
        when current_spend_cents >= cap_cents.
        """
        ...

    def record_spend(self, tenant_id: str, cost_cents: int) -> None:
        """Record spend after a successful invocation. Handles month
        rollover automatically."""
        ...


def _current_month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


class NullSpendTracker:
    """Always allows. Used for AGENT_LOCAL_STORES=1."""

    def check_budget(self, tenant_id: str, cap_cents: int) -> tuple[bool, int]:
        return True, 0

    def record_spend(self, tenant_id: str, cost_cents: int) -> None:
        return


class InMemorySpendTracker:
    """Thread-safe in-memory tracker for smoke tests."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # {tenant_id: (spend_cents, month)}
        self._data: dict[str, tuple[int, str]] = {}

    def check_budget(self, tenant_id: str, cap_cents: int) -> tuple[bool, int]:
        month = _current_month()
        with self._lock:
            spend, stored_month = self._data.get(tenant_id, (0, month))
            if stored_month != month:
                spend = 0
            return spend < cap_cents, spend

    def record_spend(self, tenant_id: str, cost_cents: int) -> None:
        month = _current_month()
        with self._lock:
            spend, stored_month = self._data.get(tenant_id, (0, month))
            if stored_month != month:
                spend = 0
            self._data[tenant_id] = (spend + cost_cents, month)

    def get_spend(self, tenant_id: str) -> tuple[int, str]:
        """Test helper: return (spend_cents, month)."""
        with self._lock:
            return self._data.get(tenant_id, (0, _current_month()))

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


class DynamoSpendTracker:
    """Atomic spend tracking on the tenants DynamoDB table.

    Uses ``monthly_spend_cents`` and ``spend_month`` as top-level
    attributes on the tenant row (outside the ``config`` blob). This
    avoids conflicting with the config PATCH path and keeps operational
    state separate from customer-editable configuration.

    check_budget: single GetItem to read current spend.
    record_spend: conditional UpdateItem that atomically increments
    the counter, with automatic month rollover.
    """

    def __init__(self, table_name: str, region: str | None = None) -> None:
        self._table_name = table_name
        self._region = region or os.getenv("AWS_REGION", "us-west-2")
        self._table: Any | None = None

    def _get_table(self) -> Any:
        if self._table is None:
            import boto3
            resource = boto3.resource("dynamodb", region_name=self._region)
            self._table = resource.Table(self._table_name)
        return self._table

    def check_budget(self, tenant_id: str, cap_cents: int) -> tuple[bool, int]:
        """Read the current spend for this month. Returns (allowed, spend)."""
        try:
            table = self._get_table()
            response = table.get_item(
                Key={"tenant_id": tenant_id},
                ProjectionExpression="monthly_spend_cents, spend_month",
            )
            item = response.get("Item", {})
            stored_month = item.get("spend_month", "")
            month = _current_month()
            if stored_month != month:
                # New month — counter is effectively zero.
                return True, 0
            spend = int(item.get("monthly_spend_cents", 0))
            return spend < cap_cents, spend
        except Exception as e:
            # Spend check failures must not block invocations — fail open.
            log.warning("SpendTracker.check_budget failed for %s, allowing: %s", tenant_id, e)
            return True, 0

    def record_spend(self, tenant_id: str, cost_cents: int) -> None:
        """Atomically increment spend for the current month.

        If the stored month is stale (different from current), resets
        the counter to this invocation's cost.
        """
        if cost_cents <= 0:
            return

        month = _current_month()
        table = self._get_table()

        try:
            # Try to increment within the current month.
            table.update_item(
                Key={"tenant_id": tenant_id},
                UpdateExpression=(
                    "SET monthly_spend_cents = "
                    "if_not_exists(monthly_spend_cents, :zero) + :cost, "
                    "spend_month = :month"
                ),
                ConditionExpression=(
                    "attribute_not_exists(spend_month) OR spend_month = :month"
                ),
                ExpressionAttributeValues={
                    ":cost": cost_cents,
                    ":zero": 0,
                    ":month": month,
                },
            )
        except Exception as e:
            error_code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
            if error_code == "ConditionalCheckFailedException":
                # Month rolled over — reset counter to this cost.
                try:
                    table.update_item(
                        Key={"tenant_id": tenant_id},
                        UpdateExpression=(
                            "SET monthly_spend_cents = :cost, "
                            "spend_month = :month"
                        ),
                        ExpressionAttributeValues={
                            ":cost": cost_cents,
                            ":month": month,
                        },
                    )
                except Exception as reset_exc:
                    log.warning(
                        "SpendTracker.record_spend month-reset failed for %s: %s",
                        tenant_id, reset_exc,
                    )
            else:
                # Non-conditional failure — log and drop (never fail the caller).
                log.warning(
                    "SpendTracker.record_spend failed for %s: %s",
                    tenant_id, e,
                )


# ---------------------------------------------------------------------------
# Factory + singleton (mirrors audit.py pattern)
# ---------------------------------------------------------------------------

_cached_tracker: SpendTracker | None = None


def build_spend_tracker() -> SpendTracker:
    """Factory that respects env-var wiring. Returns a module-level singleton.

    - LOCAL_AUDIT=memory       -> InMemorySpendTracker (smoke tests)
    - AGENT_LOCAL_STORES=1     -> NullSpendTracker (local dev)
    - else                     -> DynamoSpendTracker (production)
    """
    global _cached_tracker
    if _cached_tracker is not None:
        return _cached_tracker

    if os.getenv("LOCAL_AUDIT") == "memory":
        _cached_tracker = InMemorySpendTracker()
    elif os.getenv("AGENT_LOCAL_STORES") == "1":
        _cached_tracker = NullSpendTracker()
    else:
        _cached_tracker = DynamoSpendTracker(
            table_name=os.getenv("TENANTS_TABLE", "tenants"),
        )
    return _cached_tracker


def reset_tracker_for_tests() -> None:
    """Test helper: clear the singleton so the next call re-reads env vars."""
    global _cached_tracker
    _cached_tracker = None
