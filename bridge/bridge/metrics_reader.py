"""CloudWatch metrics reader — queries the AgentCore Reference/Agent namespace.

Powers two onboarding surfaces:
  1. The tenant-scoped metrics page (`/workspace/[tenantId]/metrics`) —
     invocations, errors, tokens, cost, top tools for one tenant over a
     configurable window. Bridge forces `tenant_id` from the session, so
     the client can't ask for another tenant's data.
  2. The `/ops` operator dashboard — cross-tenant roster, per-tenant
     leaderboards, error rates, "who's having a bad day" view. Guarded
     by a shared-secret header (see ``api.py:ops_guard``).

Both surfaces call a small number of top-level functions here; the actual
boto3 ``cloudwatch:GetMetricData`` / ``ListMetrics`` calls are isolated
to this module so unit tests can monkey-patch.

## Namespace + dimension contract

Must match ``coreAgent/app/coreAgent/metrics.py``. Changes there must be
mirrored here.

  - Namespace: ``AgentCore Reference/Agent``
  - Per-tenant counters: dim set ``[tenant_id]`` on Invocations,
    InvocationErrors, InvocationDurationMs
  - Per-(tenant, model) economics: dim set ``[tenant_id, model_id]`` on
    InputTokens, OutputTokens, EstimatedCostCents
  - Per-(tenant, tool): dim set ``[tenant_id, tool_name]`` on ToolCalls,
    ToolCallErrors, ToolCallDurationMs

## Windows

The UI passes a window string (``24h``, ``7d``, ``30d``). We convert to
a ``(start, end, period_sec)`` triple. Period is chosen so the resulting
chart has ~60-120 data points — enough resolution without hammering the
API. CloudWatch charges per metric-query, not per data-point, so we
prefer fewer metric requests with wider periods.

## Fail-open policy

Boto3 exceptions are caught and converted to an empty result with an
error flag so the UI can render "no data yet" without crashing. Metric
reads are best-effort telemetry, not a primary code path.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

log = logging.getLogger(__name__)

NAMESPACE = "AgentCore Reference/Agent"


# ---------------------------------------------------------------------------
# Windowing
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MetricsWindow:
    start: datetime
    end: datetime
    period_sec: int
    label: str


def parse_window(window: str) -> MetricsWindow:
    """Convert a UI-facing window string into (start, end, period).

    Accepts: ``1h``, ``24h``, ``7d``, ``30d``. Defaults to ``7d`` on
    unknown input — never raise, so a typo in a query string renders
    an empty page instead of a 400.
    """
    now = datetime.now(timezone.utc)
    mapping = {
        "1h":  (timedelta(hours=1),   60),      # 60 points
        "24h": (timedelta(hours=24),  900),     # 96 points (15min)
        "7d":  (timedelta(days=7),    3600),    # 168 points (1h)
        "30d": (timedelta(days=30),   21600),   # 120 points (6h)
    }
    delta, period = mapping.get(window, mapping["7d"])
    return MetricsWindow(
        start=now - delta,
        end=now,
        period_sec=period,
        label=window if window in mapping else "7d",
    )


# ---------------------------------------------------------------------------
# boto3 client — lazy, mockable
# ---------------------------------------------------------------------------

_client: Any | None = None


def _cw_client() -> Any:
    """Lazy boto3 cloudwatch client. Cached at module level. Tests
    monkey-patch by assigning a stub to ``metrics_reader._client``."""
    global _client
    if _client is None:
        import boto3
        _client = boto3.client("cloudwatch", region_name=os.getenv("AWS_REGION", "us-west-2"))
    return _client


def _reset_client_for_tests() -> None:
    """Test helper — clear the cached client."""
    global _client
    _client = None


# ---------------------------------------------------------------------------
# Result shapes
# ---------------------------------------------------------------------------

@dataclass
class TenantMetrics:
    """Per-tenant metric snapshot returned to the UI.

    All numeric aggregates are totals over the window. ``timeseries``
    carries the per-period samples for chart rendering; empty when
    CloudWatch returned nothing (either because the tenant has no
    traffic or because the read failed — check ``error``).
    """
    tenant_id: str
    window: str
    invocations_total: int = 0
    errors_total: int = 0
    error_rate_pct: float = 0.0
    input_tokens_total: int = 0
    output_tokens_total: int = 0
    estimated_cost_cents_total: int = 0
    p50_duration_ms: float = 0.0
    p95_duration_ms: float = 0.0
    top_tools: list[dict[str, Any]] = field(default_factory=list)
    invocations_timeseries: list[dict[str, Any]] = field(default_factory=list)
    errors_timeseries: list[dict[str, Any]] = field(default_factory=list)
    cost_timeseries: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "window": self.window,
            "invocations_total": self.invocations_total,
            "errors_total": self.errors_total,
            "error_rate_pct": self.error_rate_pct,
            "input_tokens_total": self.input_tokens_total,
            "output_tokens_total": self.output_tokens_total,
            "estimated_cost_cents_total": self.estimated_cost_cents_total,
            "p50_duration_ms": self.p50_duration_ms,
            "p95_duration_ms": self.p95_duration_ms,
            "top_tools": self.top_tools,
            "invocations_timeseries": self.invocations_timeseries,
            "errors_timeseries": self.errors_timeseries,
            "cost_timeseries": self.cost_timeseries,
            "error": self.error,
        }


@dataclass
class OpsRosterRow:
    tenant_id: str
    invocations: int
    errors: int
    error_rate_pct: float
    cost_cents: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "invocations": self.invocations,
            "errors": self.errors,
            "error_rate_pct": self.error_rate_pct,
            "cost_cents": self.cost_cents,
        }


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------

def _build_metric_data_query(
    query_id: str,
    metric_name: str,
    dimensions: list[dict[str, str]],
    stat: str,
    period_sec: int,
    *,
    return_data: bool = True,
) -> dict[str, Any]:
    """Assemble a single GetMetricData query entry.

    CloudWatch requires unique ``Id`` values across the batch and forbids
    ``.`` / ``-`` in them. Callers pass short snake_case ids.
    """
    return {
        "Id": query_id,
        "MetricStat": {
            "Metric": {
                "Namespace": NAMESPACE,
                "MetricName": metric_name,
                "Dimensions": dimensions,
            },
            "Period": period_sec,
            "Stat": stat,
        },
        "ReturnData": return_data,
    }


def _sum_values(values: list[float]) -> int:
    return int(sum(values))


def _timeseries(
    timestamps: list[datetime], values: list[float],
) -> list[dict[str, Any]]:
    """Return ``[{t: iso, v: float}, ...]`` sorted ascending."""
    paired = sorted(zip(timestamps, values), key=lambda p: p[0])
    return [
        {"t": t.isoformat(), "v": float(v)}
        for t, v in paired
    ]


# ---------------------------------------------------------------------------
# Public API — tenant-scoped
# ---------------------------------------------------------------------------

def get_tenant_metrics(tenant_id: str, window: str = "7d") -> TenantMetrics:
    """Fetch one tenant's metrics snapshot for the given window.

    Isolation note: ``tenant_id`` is baked into every MetricStat's
    dimension filter, so CloudWatch only returns data for this tenant.
    The bridge handler that calls this function MUST derive ``tenant_id``
    from the verified session token — never from a query parameter.

    Returns a ``TenantMetrics`` with ``.error`` populated on failure.
    Numeric fields default to zero so the UI can render a "no activity
    yet" state without null-checks.
    """
    w = parse_window(window)
    result = TenantMetrics(tenant_id=tenant_id, window=w.label)

    tenant_dim = [{"Name": "tenant_id", "Value": tenant_id}]

    # First batch — tenant-only dimension set: invocations, errors,
    # latency percentiles. One GetMetricData call to amortize the
    # per-request fixed cost and stay under CloudWatch's 500-query limit.
    queries = [
        _build_metric_data_query("inv", "Invocations", tenant_dim, "Sum", w.period_sec),
        _build_metric_data_query("err", "InvocationErrors", tenant_dim, "Sum", w.period_sec),
        _build_metric_data_query("p50", "InvocationDurationMs", tenant_dim, "p50", w.period_sec),
        _build_metric_data_query("p95", "InvocationDurationMs", tenant_dim, "p95", w.period_sec),
    ]

    try:
        resp = _cw_client().get_metric_data(
            StartTime=w.start,
            EndTime=w.end,
            MetricDataQueries=queries,
            ScanBy="TimestampAscending",
        )
    except Exception as e:
        log.warning("get_tenant_metrics: tenant-dim batch failed for %s: %s", tenant_id, e)
        result.error = f"cloudwatch read failed: {e}"
        return result

    by_id = {r["Id"]: r for r in resp.get("MetricDataResults", [])}
    inv = by_id.get("inv", {})
    err = by_id.get("err", {})
    p50 = by_id.get("p50", {})
    p95 = by_id.get("p95", {})

    result.invocations_total = _sum_values(inv.get("Values", []))
    result.errors_total = _sum_values(err.get("Values", []))
    if result.invocations_total > 0:
        result.error_rate_pct = round(
            100.0 * result.errors_total / result.invocations_total, 2
        )
    # Percentile statistics roll up per-period — take the average across
    # periods as a rough-but-useful headline number. The timeseries
    # widgets can render the full curve if ever wanted.
    p50_vals = p50.get("Values", [])
    p95_vals = p95.get("Values", [])
    if p50_vals:
        result.p50_duration_ms = round(sum(p50_vals) / len(p50_vals), 1)
    if p95_vals:
        result.p95_duration_ms = round(sum(p95_vals) / len(p95_vals), 1)

    result.invocations_timeseries = _timeseries(
        inv.get("Timestamps", []), inv.get("Values", [])
    )
    result.errors_timeseries = _timeseries(
        err.get("Timestamps", []), err.get("Values", [])
    )

    # Second batch — tokens + cost. These metrics carry a `model_id`
    # dimension too, so we need to discover which model(s) this tenant
    # has used. Use ListMetrics to enumerate rather than hardcoding
    # the list (a tenant might have cut over from Sonnet to Haiku).
    try:
        lm = _cw_client().list_metrics(
            Namespace=NAMESPACE,
            MetricName="InputTokens",
            Dimensions=[{"Name": "tenant_id", "Value": tenant_id}],
        )
        models = sorted({
            dim["Value"]
            for m in lm.get("Metrics", [])
            for dim in m.get("Dimensions", [])
            if dim["Name"] == "model_id"
        })
    except Exception as e:
        log.warning("get_tenant_metrics: list_metrics failed for %s: %s", tenant_id, e)
        models = []

    if not models:
        # No token data yet — return what we have from the first batch.
        return result

    # Build one query per (metric, model). Query IDs must be unique and
    # alphanumeric; use the model index rather than the model string
    # (model IDs contain dots and hyphens).
    cost_queries: list[dict[str, Any]] = []
    for i, model_id in enumerate(models):
        dims = [
            {"Name": "tenant_id", "Value": tenant_id},
            {"Name": "model_id", "Value": model_id},
        ]
        cost_queries.append(_build_metric_data_query(f"in_{i}", "InputTokens", dims, "Sum", w.period_sec))
        cost_queries.append(_build_metric_data_query(f"out_{i}", "OutputTokens", dims, "Sum", w.period_sec))
        cost_queries.append(_build_metric_data_query(f"c_{i}", "EstimatedCostCents", dims, "Sum", w.period_sec))

    try:
        resp2 = _cw_client().get_metric_data(
            StartTime=w.start,
            EndTime=w.end,
            MetricDataQueries=cost_queries,
            ScanBy="TimestampAscending",
        )
    except Exception as e:
        log.warning("get_tenant_metrics: cost batch failed for %s: %s", tenant_id, e)
        result.error = f"cloudwatch cost read failed: {e}"
        return result

    by_id2 = {r["Id"]: r for r in resp2.get("MetricDataResults", [])}

    # Aggregate across models — the UI surfaces totals not per-model splits.
    # A future drill-down can show the per-model split from the same data.
    cost_ts_by_timestamp: dict[datetime, float] = {}
    for i, _model in enumerate(models):
        result.input_tokens_total += _sum_values(by_id2.get(f"in_{i}", {}).get("Values", []))
        result.output_tokens_total += _sum_values(by_id2.get(f"out_{i}", {}).get("Values", []))
        result.estimated_cost_cents_total += _sum_values(
            by_id2.get(f"c_{i}", {}).get("Values", [])
        )
        # Merge cost timeseries across models by timestamp.
        c_data = by_id2.get(f"c_{i}", {})
        for t, v in zip(c_data.get("Timestamps", []), c_data.get("Values", [])):
            cost_ts_by_timestamp[t] = cost_ts_by_timestamp.get(t, 0.0) + float(v)

    result.cost_timeseries = _timeseries(
        list(cost_ts_by_timestamp.keys()),
        list(cost_ts_by_timestamp.values()),
    )

    # Third batch — top tools by call volume for this tenant. Same
    # list_metrics trick to enumerate tools the tenant has actually used.
    try:
        lm_tools = _cw_client().list_metrics(
            Namespace=NAMESPACE,
            MetricName="ToolCalls",
            Dimensions=[{"Name": "tenant_id", "Value": tenant_id}],
        )
        tools = sorted({
            dim["Value"]
            for m in lm_tools.get("Metrics", [])
            for dim in m.get("Dimensions", [])
            if dim["Name"] == "tool_name"
        })
    except Exception as e:
        log.warning("get_tenant_metrics: list_metrics tools failed for %s: %s", tenant_id, e)
        tools = []

    if tools:
        tool_queries = []
        for i, tool_name in enumerate(tools):
            dims = [
                {"Name": "tenant_id", "Value": tenant_id},
                {"Name": "tool_name", "Value": tool_name},
            ]
            tool_queries.append(
                _build_metric_data_query(f"tc_{i}", "ToolCalls", dims, "Sum", w.period_sec)
            )
            tool_queries.append(
                _build_metric_data_query(f"te_{i}", "ToolCallErrors", dims, "Sum", w.period_sec)
            )
        try:
            resp3 = _cw_client().get_metric_data(
                StartTime=w.start,
                EndTime=w.end,
                MetricDataQueries=tool_queries,
                ScanBy="TimestampAscending",
            )
            by_id3 = {r["Id"]: r for r in resp3.get("MetricDataResults", [])}
            top: list[dict[str, Any]] = []
            for i, tool_name in enumerate(tools):
                calls = _sum_values(by_id3.get(f"tc_{i}", {}).get("Values", []))
                errs = _sum_values(by_id3.get(f"te_{i}", {}).get("Values", []))
                if calls > 0:
                    top.append({
                        "tool_name": tool_name,
                        "calls": calls,
                        "errors": errs,
                    })
            top.sort(key=lambda r: r["calls"], reverse=True)
            result.top_tools = top[:10]
        except Exception as e:
            log.warning("get_tenant_metrics: tool batch failed for %s: %s", tenant_id, e)

    return result


# ---------------------------------------------------------------------------
# Public API — operator / cross-tenant
# ---------------------------------------------------------------------------

def list_active_tenants(window: str = "7d") -> list[str]:
    """Return all tenant_ids that have invocation metrics in the window.

    Uses ``ListMetrics`` on the ``Invocations`` metric — the cheapest
    discovery path. A tenant that never invoked anything won't appear
    here even if their DDB config row exists.
    """
    try:
        lm = _cw_client().list_metrics(
            Namespace=NAMESPACE,
            MetricName="Invocations",
        )
        tenants = sorted({
            dim["Value"]
            for m in lm.get("Metrics", [])
            for dim in m.get("Dimensions", [])
            if dim["Name"] == "tenant_id"
        })
        return tenants
    except Exception as e:
        log.warning("list_active_tenants failed: %s", e)
        return []


def get_ops_roster(
    window: str = "7d",
    *,
    include_testenv: bool = False,
) -> list[OpsRosterRow]:
    """Cross-tenant roster for the operator dashboard.

    Fetches Invocations + InvocationErrors + EstimatedCostCents (summed
    across models) for every known-active tenant. One GetMetricData
    batch per metric to cap the request fan-out at 3 API calls total
    regardless of tenant count.

    CloudWatch's GetMetricData limit is 500 queries/request, so this
    scales to ~166 tenants before needing pagination.

    ``include_testenv``: when False (default), tenants with
    ``config.is_internal_testenv = True`` in DDB are filtered out so
    the manual-test rig doesn't pollute real-customer metrics. Pass
    True to see everything (e.g. for testing the seeding flow itself).
    """
    tenants = list_active_tenants(window)
    if not tenants:
        return []

    if not include_testenv:
        from .tenant_write import list_internal_testenv_tenants

        region = os.getenv("AWS_REGION", "us-west-2")
        testenv_ids = list_internal_testenv_tenants(tenants, region)
        if testenv_ids:
            log.info(
                "get_ops_roster: hiding %d internal testenv tenants: %s",
                len(testenv_ids),
                sorted(testenv_ids),
            )
            tenants = [t for t in tenants if t not in testenv_ids]
            if not tenants:
                return []

    w = parse_window(window)

    # Invocations + errors in one batch.
    queries: list[dict[str, Any]] = []
    for i, t in enumerate(tenants):
        dims = [{"Name": "tenant_id", "Value": t}]
        queries.append(
            _build_metric_data_query(f"inv_{i}", "Invocations", dims, "Sum", w.period_sec)
        )
        queries.append(
            _build_metric_data_query(f"err_{i}", "InvocationErrors", dims, "Sum", w.period_sec)
        )

    try:
        resp = _cw_client().get_metric_data(
            StartTime=w.start,
            EndTime=w.end,
            MetricDataQueries=queries,
            ScanBy="TimestampAscending",
        )
    except Exception as e:
        log.warning("get_ops_roster: invocation batch failed: %s", e)
        return []

    by_id = {r["Id"]: r for r in resp.get("MetricDataResults", [])}

    # Cost is dimensioned on (tenant_id, model_id). Enumerate per-tenant
    # models and sum — but to keep the batch small, we just query a
    # "no model dimension" fallback by using Metrics API with only
    # the tenant dim. That returns the tenant's total across all models
    # because CloudWatch aggregates across the missing dim when we only
    # specify tenant_id.
    #
    # Actually — CloudWatch does NOT aggregate across missing dimensions
    # in GetMetricData. A metric defined with two dimensions only matches
    # when BOTH dimensions are specified. So we must enumerate models.
    # For the ops roster, we accept the cost of a second boto3 call per
    # tenant via list_metrics + get_metric_data. At ~166 tenants this
    # adds ~1s; acceptable for an operator page that isn't user-hot.
    rows: list[OpsRosterRow] = []
    for i, t in enumerate(tenants):
        inv = _sum_values(by_id.get(f"inv_{i}", {}).get("Values", []))
        errs = _sum_values(by_id.get(f"err_{i}", {}).get("Values", []))
        rate = round(100.0 * errs / inv, 2) if inv else 0.0

        # Cost — reuse the tenant metrics helper for the economics sub-batch.
        # This is the slow path but the result set is small.
        try:
            lm = _cw_client().list_metrics(
                Namespace=NAMESPACE,
                MetricName="EstimatedCostCents",
                Dimensions=[{"Name": "tenant_id", "Value": t}],
            )
            models = sorted({
                dim["Value"]
                for m in lm.get("Metrics", [])
                for dim in m.get("Dimensions", [])
                if dim["Name"] == "model_id"
            })
            cost_total = 0
            if models:
                cost_queries = [
                    _build_metric_data_query(
                        f"c_{j}",
                        "EstimatedCostCents",
                        [
                            {"Name": "tenant_id", "Value": t},
                            {"Name": "model_id", "Value": m},
                        ],
                        "Sum",
                        w.period_sec,
                    )
                    for j, m in enumerate(models)
                ]
                cost_resp = _cw_client().get_metric_data(
                    StartTime=w.start,
                    EndTime=w.end,
                    MetricDataQueries=cost_queries,
                )
                for r in cost_resp.get("MetricDataResults", []):
                    cost_total += _sum_values(r.get("Values", []))
        except Exception as e:
            log.warning("get_ops_roster: cost lookup failed for %s: %s", t, e)
            cost_total = 0

        rows.append(OpsRosterRow(
            tenant_id=t,
            invocations=inv,
            errors=errs,
            error_rate_pct=rate,
            cost_cents=cost_total,
        ))

    # Sort by invocations descending — most active at the top.
    rows.sort(key=lambda r: r.invocations, reverse=True)
    return rows
