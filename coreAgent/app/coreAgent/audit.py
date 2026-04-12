"""Audit log: per-invocation, per-tool-call, and per-propose-PR rows.

Three row types, one table:
  - row_type="invocation":  one per @app.entrypoint call, with model_id,
    token counts, input/output summaries, total duration.
  - row_type="tool_call":   one per catalog tool invocation, linked to its
    parent invocation by `invocation_id`.
  - row_type="propose_pr":  TWO per Phase B `propose_pr` tool call. The
    first ("launched") is written when the agent successfully fires the
    Fargate sandbox task; the second ("completed") is written when the
    DDB poller observes a terminal status (success / error / orphaned).
    These rows track the asynchronous lifecycle of a PR-writing job
    that the synchronous `tool_call` row can't see (the tool_call row
    only captures the immediate "I'm working on it" return).

**Audit writes MUST NEVER fail the caller.** The DynamoDB client is wrapped
in a try/except that swallows everything; if DDB is down, the agent still
responds. Callers can assume `write()` is safe to call without a `try`.

Local dev (`AGENT_LOCAL_STORES=1`) uses `NullAuditStore` (drops rows
silently). Smoke tests (`LOCAL_AUDIT=memory`) use `InMemoryAuditStore` so
assertions can check that rows were actually produced.

The env var is deliberately NOT named `LOCAL_DEV`: the AgentCore CLI
hardcodes `LOCAL_DEV=1` into every `agentcore dev` subprocess, so we'd
never escape NullAuditStore in production-mode-locally smoke tests.

Row shape:

invocation:
    {
      tenant_id:       (PK, isolation key)
      sk:              "INV#{iso_ts}#{invocation_id}"
      row_type:        "invocation"
      invocation_id:   uuid4 hex
      timestamp:       ISO8601 UTC
      created_at:      ISO8601 UTC  (GDPR/audit hedge)
      user_id, channel_id, thread_id, workspace_id
      model_id:        from TenantConfig.model_id
      input_summary:   truncated prompt
      output_summary:  truncated response
      input_tokens, output_tokens, duration_ms
      success:         bool
    }

tool_call:
    {
      tenant_id:       (PK)
      sk:              "TOOL#{iso_ts}#{invocation_id}#{uuid8}"
      row_type:        "tool_call"
      invocation_id:   links to parent invocation row
      timestamp, created_at
      user_id, channel_id
      tool_name
      tool_args_summary, tool_result_summary  (both truncated)
      duration_ms, success
    }

propose_pr:
    {
      tenant_id:       (PK)
      sk:              "PR#{iso_ts}#{task_id}#{event}"
      row_type:        "propose_pr"
      task_id:         the sandbox task id (e.g. "pr-abc12345")
      event:           "launched" | "completed"
      invocation_id:   links to the parent invocation row
      timestamp, created_at
      user_id, channel_id, thread_id
      repo:            "owner/name" target repo
      status:          "launched" | "success" | "error" | "orphaned"
      pr_url:          present only on status="success"
      error:           present only on status in {"error","orphaned"}
    }
"""
from __future__ import annotations

import logging
import os
from collections import defaultdict
from typing import Any, Protocol

log = logging.getLogger(__name__)

# Truncation cap for string fields (prompts, responses, tool args/results).
# DynamoDB item size limit is 400 KB; a single row can easily fit many 1 KB
# fields, but we truncate to keep scans cheap and prevent accidental PII
# bloat.
_MAX_FIELD_BYTES = 1024


def _truncate(value: Any, max_bytes: int = _MAX_FIELD_BYTES) -> str:
    """Coerce to str and truncate to `max_bytes`. Appends an ellipsis marker
    when truncation happens so downstream readers can tell."""
    if value is None:
        return ""
    s = value if isinstance(value, str) else str(value)
    encoded = s.encode("utf-8")
    if len(encoded) <= max_bytes:
        return s
    # Truncate on byte boundary, then decode ignoring any split multibyte char.
    return encoded[: max_bytes - 3].decode("utf-8", errors="ignore") + "..."


class AuditStore(Protocol):
    """Write-only storage contract for audit rows. Implementations must
    swallow all exceptions — audit failures must never take down the caller."""

    def write(self, row: dict[str, Any]) -> None: ...


class NullAuditStore:
    """Drops rows silently. Used when `AGENT_LOCAL_STORES=1` and no
    smoke-test assertions are needed."""

    def write(self, row: dict[str, Any]) -> None:
        return None


class InMemoryAuditStore:
    """Keeps rows in a dict keyed by tenant_id. Used by smoke tests
    (`LOCAL_AUDIT=memory`) to assert on produced rows.

    Rows are stored in insertion order per tenant. Not thread-safe for
    concurrent writes across tenants — fine for local dev / single-request
    smoke tests."""

    def __init__(self) -> None:
        self._rows: dict[str, list[dict[str, Any]]] = defaultdict(list)

    def write(self, row: dict[str, Any]) -> None:
        tenant_id = row.get("tenant_id", "unknown")
        self._rows[tenant_id].append(dict(row))  # defensive copy

    def rows_for(self, tenant_id: str) -> list[dict[str, Any]]:
        return list(self._rows.get(tenant_id, []))

    def all_rows(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for rows in self._rows.values():
            out.extend(rows)
        return out

    def clear(self) -> None:
        self._rows.clear()


class DynamoAuditStore:
    """Writes rows to a DynamoDB table via boto3's resource API.

    The boto3 client is lazily constructed on first write so import-time
    cost is zero in local dev. All exceptions are caught and logged; they
    never propagate to the caller. If the table does not exist or the IAM
    role lacks permission, audit rows are silently dropped — surface this
    via CloudWatch logs, not via broken invocations.
    """

    def __init__(self, table_name: str, region: str | None = None) -> None:
        self.table_name = table_name
        self.region = region or os.getenv("AWS_REGION", "us-west-2")
        self._table: Any | None = None

    def _get_table(self) -> Any:
        if self._table is None:
            import boto3

            resource = boto3.resource("dynamodb", region_name=self.region)
            self._table = resource.Table(self.table_name)
        return self._table

    def write(self, row: dict[str, Any]) -> None:
        try:
            # Truncate known-unbounded string fields. Numeric/bool fields
            # pass through untouched.
            cleaned = dict(row)
            for field in (
                "input_summary",
                "output_summary",
                "tool_args_summary",
                "tool_result_summary",
            ):
                if field in cleaned:
                    cleaned[field] = _truncate(cleaned[field])

            self._get_table().put_item(Item=cleaned)
        except Exception as e:  # pragma: no cover - safety net
            # Audit failures must never break the caller. Log and drop.
            log.warning(
                "DynamoAuditStore.write dropped a row for tenant=%s sk=%s: %s",
                row.get("tenant_id"),
                row.get("sk"),
                e,
            )


# Module-level singleton so that main.py and tools.py share ONE store even
# when they each call build_audit_store() at import time. In production
# (DynamoAuditStore) both clients would talk to the same DDB table so the
# distinction is invisible, but in-process smoke tests need a single shared
# store to see all rows from one place.
_cached_store: AuditStore | None = None


def build_audit_store() -> AuditStore:
    """Factory that respects env-var wiring. Returns a module-level singleton.

    - LOCAL_AUDIT=memory      → InMemoryAuditStore (smoke tests)
    - AGENT_LOCAL_STORES=1    → NullAuditStore (local dev loop)
    - else                    → DynamoAuditStore (production)
    """
    global _cached_store
    if _cached_store is not None:
        return _cached_store

    if os.getenv("LOCAL_AUDIT") == "memory":
        _cached_store = InMemoryAuditStore()
    elif os.getenv("AGENT_LOCAL_STORES") == "1":
        _cached_store = NullAuditStore()
    else:
        _cached_store = DynamoAuditStore(
            table_name=os.getenv("AUDIT_LOG_TABLE", "audit_log"),
        )
    return _cached_store


def reset_store_for_tests() -> None:
    """Test helper: clear the singleton so the next `build_audit_store()`
    re-reads env vars. Not used by production code."""
    global _cached_store
    _cached_store = None
