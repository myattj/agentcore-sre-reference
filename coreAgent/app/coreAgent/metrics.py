"""Per-tenant CloudWatch metrics via Embedded Metric Format (EMF).

Every invocation and every catalog tool call produces a structured JSON line on
stdout. CloudWatch Logs auto-parses those lines and publishes them as metrics
under the ``AgentCore Reference/Agent`` namespace (configurable via ``METRICS_NAMESPACE``).

**Why EMF instead of ``cloudwatch:PutMetricData``?**

The agent already writes to CloudWatch Logs — stdout is captured by the
AgentCore Runtime log group. EMF lets us piggyback on that pipeline so metric
emission is:
  - free (no per-call API charge)
  - synchronous-but-nonblocking (a print() call)
  - resilient (logging buffers; no throttling to design around)
  - permission-free (no new IAM grant needed on the agent role)

Trade-off: the metric shape lives in this file rather than in a config file.
EMF spec: https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch_Embedded_Metric_Format_Specification.html

## Metric catalogue

Invocation-level (emitted from ``main.py:invoke`` finally block):
  - ``Invocations``             Count, dims: [tenant_id]
  - ``InvocationErrors``        Count, dims: [tenant_id]  (only when success=False)
  - ``InvocationDurationMs``    Milliseconds, dims: [tenant_id]
  - ``InputTokens``             Count, dims: [tenant_id, model_id]
  - ``OutputTokens``            Count, dims: [tenant_id, model_id]
  - ``EstimatedCostCents``      None (integer cents), dims: [tenant_id, model_id]

Tool-level (emitted from ``tools.py:audited_tool`` finally block):
  - ``ToolCalls``               Count, dims: [tenant_id, tool_name]
  - ``ToolCallErrors``          Count, dims: [tenant_id, tool_name]  (only when success=False)
  - ``ToolCallDurationMs``      Milliseconds, dims: [tenant_id, tool_name]

Non-dimension properties (queryable via CloudWatch Logs Insights but not
promoted to metric dimensions, so they don't inflate CloudWatch's
metric-count billing): ``invocation_id``, ``channel_id``, ``workspace_id``.

## Cost metric

``EstimatedCostCents`` uses ``pricing.compute_cost_cents(model_id, in, out)``
— the same function the spend tracker uses for cost caps. Single source of
truth for token-to-dollar math.

## Dev-loop wiring (mirrors audit.py / spend_tracker.py)

  - ``LOCAL_AUDIT=memory``      → InMemoryMetricsEmitter (smoke tests assert)
  - ``AGENT_LOCAL_STORES=1``    → NullMetricsEmitter (silent)
  - else                        → EMFMetricsEmitter (production stdout)

**Emit methods must never raise.** Callers assume ``emit_*`` is safe inside
an existing ``finally`` block. Exceptions are caught and logged.
"""
from __future__ import annotations

import json
import logging
import os
from collections import defaultdict
from typing import Any, Protocol

from pricing import compute_cost_cents

log = logging.getLogger(__name__)

DEFAULT_NAMESPACE = "AgentCore Reference/Agent"


class MetricsEmitter(Protocol):
    """Emission contract. Implementations must swallow all exceptions so
    metric failures never break the caller."""

    def emit_invocation(
        self,
        *,
        tenant_id: str,
        model_id: str,
        input_tokens: int,
        output_tokens: int,
        duration_ms: int,
        success: bool,
        invocation_id: str = "",
        channel_id: str = "",
        workspace_id: str = "",
    ) -> None: ...

    def emit_tool_call(
        self,
        *,
        tenant_id: str,
        tool_name: str,
        duration_ms: int,
        success: bool,
        invocation_id: str = "",
    ) -> None: ...


class NullMetricsEmitter:
    """Drops everything. Used for ``AGENT_LOCAL_STORES=1``."""

    def emit_invocation(self, **kwargs: Any) -> None:
        return None

    def emit_tool_call(self, **kwargs: Any) -> None:
        return None


class InMemoryMetricsEmitter:
    """Keeps emitted records in a list per tenant. Used for
    ``LOCAL_AUDIT=memory`` smoke tests to assert that metrics were produced.

    Not thread-safe for concurrent writes across tenants — fine for
    single-request smoke tests."""

    def __init__(self) -> None:
        self._records: dict[str, list[dict[str, Any]]] = defaultdict(list)

    def emit_invocation(
        self,
        *,
        tenant_id: str,
        model_id: str,
        input_tokens: int,
        output_tokens: int,
        duration_ms: int,
        success: bool,
        invocation_id: str = "",
        channel_id: str = "",
        workspace_id: str = "",
    ) -> None:
        cost_cents = compute_cost_cents(model_id, input_tokens, output_tokens)
        self._records[tenant_id].append({
            "kind": "invocation",
            "tenant_id": tenant_id,
            "model_id": model_id,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "estimated_cost_cents": cost_cents,
            "duration_ms": duration_ms,
            "success": success,
            "invocation_id": invocation_id,
            "channel_id": channel_id,
            "workspace_id": workspace_id,
        })

    def emit_tool_call(
        self,
        *,
        tenant_id: str,
        tool_name: str,
        duration_ms: int,
        success: bool,
        invocation_id: str = "",
    ) -> None:
        self._records[tenant_id].append({
            "kind": "tool_call",
            "tenant_id": tenant_id,
            "tool_name": tool_name,
            "duration_ms": duration_ms,
            "success": success,
            "invocation_id": invocation_id,
        })

    def records_for(self, tenant_id: str) -> list[dict[str, Any]]:
        return list(self._records.get(tenant_id, []))

    def all_records(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for rows in self._records.values():
            out.extend(rows)
        return out

    def clear(self) -> None:
        self._records.clear()


class EMFMetricsEmitter:
    """Writes CloudWatch Embedded Metric Format JSON lines to stdout.

    The EMF line is a normal JSON object with a special ``_aws`` block that
    tells CloudWatch Logs which fields are metrics, their units, and which
    fields are dimensions. CloudWatch Logs parses matching log lines and
    publishes the metrics automatically — no boto3 client, no API call, no
    extra IAM.

    Implementation notes:
      - Uses ``print()`` to stdout rather than ``log.info()`` so the line
        lands in the log stream without a logger prefix or timestamp prefix
        that would break EMF parsing. The AgentCore Runtime captures stdout
        into CloudWatch Logs already.
      - One metric record per call — no batching. Batching EMF records
        requires wrapping them in an array under ``_aws.CloudWatchMetrics``
        which is fiddly, and at our scale the overhead is negligible.
      - ``flush=True`` on print so the line is pushed immediately, not
        buffered until process exit (the runtime may checkpoint).
    """

    def __init__(self, namespace: str | None = None) -> None:
        self.namespace = namespace or os.getenv("METRICS_NAMESPACE", DEFAULT_NAMESPACE)

    def _emit_emf(
        self,
        *,
        dimensions: list[list[str]],
        metrics: list[dict[str, str]],
        values: dict[str, Any],
        properties: dict[str, Any],
    ) -> None:
        """Assemble and print one EMF record.

        ``dimensions`` is a list of dimension-sets. Each set is a list of
        field names whose values (looked up in ``values``) form one unique
        dimension combination. EMF allows multiple sets per record so one
        line can produce per-tenant AND per-(tenant, model) metrics without
        two separate emissions, but we keep it to a single set per record
        for simplicity and to match how the CDK dashboard queries them.

        ``metrics`` is ``[{"Name": "Invocations", "Unit": "Count"}, ...]`` —
        the set of metric values to publish from this record. Names must
        appear as keys in ``values``.

        ``properties`` are extra fields (not promoted to metric dimensions)
        attached to the log line for Insights queries.
        """
        record: dict[str, Any] = {
            "_aws": {
                "Timestamp": int(_now_ms()),
                "CloudWatchMetrics": [
                    {
                        "Namespace": self.namespace,
                        "Dimensions": dimensions,
                        "Metrics": metrics,
                    }
                ],
            },
        }
        record.update(values)
        record.update(properties)
        try:
            # print(flush=True) → CloudWatch Logs via captured stdout.
            print(json.dumps(record, default=str), flush=True)
        except Exception as e:  # pragma: no cover - safety net
            log.warning("EMFMetricsEmitter: failed to write record: %s", e)

    def emit_invocation(
        self,
        *,
        tenant_id: str,
        model_id: str,
        input_tokens: int,
        output_tokens: int,
        duration_ms: int,
        success: bool,
        invocation_id: str = "",
        channel_id: str = "",
        workspace_id: str = "",
    ) -> None:
        try:
            cost_cents = compute_cost_cents(model_id, input_tokens, output_tokens)

            # Record 1 — per-tenant only (Invocations, Errors, Duration).
            # Kept as its own record so tenant-level dashboards don't fan
            # out into N_tenants x N_models metric series.
            errors_value = 0 if success else 1
            self._emit_emf(
                dimensions=[["tenant_id"]],
                metrics=[
                    {"Name": "Invocations", "Unit": "Count"},
                    {"Name": "InvocationErrors", "Unit": "Count"},
                    {"Name": "InvocationDurationMs", "Unit": "Milliseconds"},
                ],
                values={
                    "tenant_id": tenant_id,
                    "Invocations": 1,
                    "InvocationErrors": errors_value,
                    "InvocationDurationMs": duration_ms,
                },
                properties={
                    "invocation_id": invocation_id,
                    "channel_id": channel_id,
                    "workspace_id": workspace_id,
                    "model_id": model_id,
                    "success": success,
                },
            )

            # Record 2 — per (tenant, model) for tokens + cost. Model is
            # a dimension so the dashboard can split cost by model when
            # a tenant mixes Sonnet and Haiku. Emitted only when we have
            # token data — skipping avoids planting a zero-token series
            # for cost-capped rejections.
            if input_tokens or output_tokens:
                self._emit_emf(
                    dimensions=[["tenant_id", "model_id"]],
                    metrics=[
                        {"Name": "InputTokens", "Unit": "Count"},
                        {"Name": "OutputTokens", "Unit": "Count"},
                        {"Name": "EstimatedCostCents", "Unit": "None"},
                    ],
                    values={
                        "tenant_id": tenant_id,
                        "model_id": model_id,
                        "InputTokens": input_tokens,
                        "OutputTokens": output_tokens,
                        "EstimatedCostCents": cost_cents,
                    },
                    properties={
                        "invocation_id": invocation_id,
                    },
                )
        except Exception as e:  # pragma: no cover - safety net
            log.warning("EMFMetricsEmitter.emit_invocation failed: %s", e)

    def emit_tool_call(
        self,
        *,
        tenant_id: str,
        tool_name: str,
        duration_ms: int,
        success: bool,
        invocation_id: str = "",
    ) -> None:
        try:
            errors_value = 0 if success else 1
            self._emit_emf(
                dimensions=[["tenant_id", "tool_name"]],
                metrics=[
                    {"Name": "ToolCalls", "Unit": "Count"},
                    {"Name": "ToolCallErrors", "Unit": "Count"},
                    {"Name": "ToolCallDurationMs", "Unit": "Milliseconds"},
                ],
                values={
                    "tenant_id": tenant_id,
                    "tool_name": tool_name,
                    "ToolCalls": 1,
                    "ToolCallErrors": errors_value,
                    "ToolCallDurationMs": duration_ms,
                },
                properties={
                    "invocation_id": invocation_id,
                    "success": success,
                },
            )
        except Exception as e:  # pragma: no cover - safety net
            log.warning("EMFMetricsEmitter.emit_tool_call failed: %s", e)


def _now_ms() -> float:
    """Current time in epoch milliseconds. Broken out so tests can patch it."""
    import time
    return time.time() * 1000


# ---------------------------------------------------------------------------
# Factory + singleton (mirrors audit.py pattern)
# ---------------------------------------------------------------------------

_cached_emitter: MetricsEmitter | None = None


def build_metrics_emitter() -> MetricsEmitter:
    """Factory that respects env-var wiring. Returns a module-level singleton.

    - ``LOCAL_AUDIT=memory``      → InMemoryMetricsEmitter (smoke tests)
    - ``AGENT_LOCAL_STORES=1``    → NullMetricsEmitter (local dev)
    - else                        → EMFMetricsEmitter (production)
    """
    global _cached_emitter
    if _cached_emitter is not None:
        return _cached_emitter

    if os.getenv("LOCAL_AUDIT") == "memory":
        _cached_emitter = InMemoryMetricsEmitter()
    elif os.getenv("AGENT_LOCAL_STORES") == "1":
        _cached_emitter = NullMetricsEmitter()
    else:
        _cached_emitter = EMFMetricsEmitter()
    return _cached_emitter


def reset_emitter_for_tests() -> None:
    """Test helper: clear the singleton so the next call re-reads env vars."""
    global _cached_emitter
    _cached_emitter = None
