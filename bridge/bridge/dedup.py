"""Slack event_id dedup.

Slack retries any non-200 webhook 3x with backoff. Without dedup, every
retry would re-invoke the agent (double-spending Bedrock and writing
duplicate audit rows). The bridge marks events as seen BEFORE dispatching
to `dispatch_async`; subsequent retries with the same `event_id` are
ack'd 200 and dropped.

Storage:
  - LOCAL_DEV=1: `InMemoryDedup` — process-local dict with manual TTL.
  - else:        `DynamoDedup` — `processed_events` table, conditional
                 PutItem with TTL.

The contract is one method: `seen(event_id) -> bool`. The semantics are:

  - Returns False on the first call for a given event_id and atomically
    marks it as seen.
  - Returns True on every subsequent call (within the TTL window).

Implementations MUST be safe to call from concurrent request handlers.
The Dynamo impl uses `ConditionExpression="attribute_not_exists(event_id)"`
which is naturally atomic. The in-memory impl uses a lock.

Failure mode: if the dedup backend is unreachable, we LOG and FAIL OPEN —
i.e. treat the event as unseen and dispatch. The cost of a duplicate
invocation is much smaller than the cost of dropping a real event.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Protocol

log = logging.getLogger(__name__)

# How long an event_id stays in the dedup set. Slack's retry window is
# ~15 minutes; 1h is a comfortable buffer with no real downside.
_TTL_SECONDS = 3600


class Dedup(Protocol):
    """Atomic seen-check. Returns True iff the event_id has been observed
    before within the TTL window."""

    def seen(self, event_id: str) -> bool: ...


class InMemoryDedup:
    """Process-local dedup with a dict + a lock + manual TTL eviction.

    Used by the LOCAL_DEV path and unit tests. Single-process only — if
    you scale the bridge horizontally without DynamoDedup, retries can
    slip through to a different replica.
    """

    def __init__(self, ttl_seconds: int = _TTL_SECONDS) -> None:
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()
        self._ttl_seconds = ttl_seconds

    def seen(self, event_id: str) -> bool:
        now = time.time()
        with self._lock:
            self._evict_expired(now)
            if event_id in self._seen:
                return True
            self._seen[event_id] = now + self._ttl_seconds
            return False

    def _evict_expired(self, now: float) -> None:
        # Cheap O(n) eviction. Acceptable for a small in-memory dedup —
        # at Slack's retry rate (3x per event over 15 minutes) this set
        # stays bounded by `requests/hour` even without proactive eviction.
        expired = [eid for eid, exp in self._seen.items() if exp <= now]
        for eid in expired:
            del self._seen[eid]


class DynamoDedup:
    """DynamoDB-backed dedup using a conditional PutItem.

    Schema (see `infra/data/lib/data-stack.ts`):
        {event_id: str (PK), ttl: number (epoch seconds)}

    The PutItem uses `ConditionExpression="attribute_not_exists(event_id)"`
    so the first writer wins atomically. ConditionalCheckFailedException
    means another writer (or a Slack retry) already marked it.

    Reads from the env var `PROCESSED_EVENTS_TABLE` for the table name
    (default `processed_events`).
    """

    def __init__(
        self,
        table_name: str,
        region: str | None = None,
        ttl_seconds: int = _TTL_SECONDS,
    ) -> None:
        self.table_name = table_name
        self.region = region or os.getenv("AWS_REGION", "us-west-2")
        self._ttl_seconds = ttl_seconds
        self._table: Any | None = None

    def _get_table(self) -> Any:
        if self._table is None:
            import boto3

            resource = boto3.resource("dynamodb", region_name=self.region)
            self._table = resource.Table(self.table_name)
        return self._table

    def seen(self, event_id: str) -> bool:
        # Lazy import to avoid hard boto3 dependency at module load time
        # in pure-LOCAL_DEV scenarios.
        try:
            from botocore.exceptions import ClientError
        except ImportError:  # pragma: no cover - dev environment without botocore
            log.warning("DynamoDedup.seen: botocore not installed; failing open for event_id=%s", event_id)
            return False

        ttl_value = int(time.time()) + self._ttl_seconds
        try:
            self._get_table().put_item(
                Item={"event_id": event_id, "ttl": ttl_value},
                ConditionExpression="attribute_not_exists(event_id)",
            )
            return False
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code")
            if code == "ConditionalCheckFailedException":
                return True
            # Any other error: fail OPEN. Better to risk a duplicate
            # invocation than to drop a real Slack event silently.
            log.warning(
                "DynamoDedup.seen: PutItem failed for event_id=%s (%s); failing open",
                event_id,
                code,
            )
            return False
        except Exception as e:  # pragma: no cover - safety net
            log.warning(
                "DynamoDedup.seen: unexpected error for event_id=%s: %s; failing open",
                event_id,
                e,
            )
            return False


# ----------------------------------------------------------------------------
# Lazy singleton
# ----------------------------------------------------------------------------

_default_dedup: Dedup | None = None


def _dedup() -> Dedup:
    global _default_dedup
    if _default_dedup is None:
        if os.getenv("LOCAL_DEV") == "1":
            _default_dedup = InMemoryDedup()
        else:
            _default_dedup = DynamoDedup(
                table_name=os.getenv("PROCESSED_EVENTS_TABLE", "processed_events"),
            )
    return _default_dedup


def is_duplicate(event_id: str) -> bool:
    """Return True if this event_id has already been processed (within
    the TTL window). Atomically marks unseen event_ids as seen.

    `event_id` of None or empty string is treated as not-a-duplicate to
    avoid swallowing events from clients that don't supply an id.
    """
    if not event_id:
        return False
    return _dedup().seen(event_id)


def reset_dedup_for_tests() -> None:
    """Test helper: clear the cached singleton so the next call re-reads env vars."""
    global _default_dedup
    _default_dedup = None
