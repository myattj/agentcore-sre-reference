"""Tests for bridge/metrics_reader.py and the /api/*/metrics* routes.

``metrics_reader`` talks to CloudWatch via boto3; we substitute a fake
client at the module level so tests can prescribe the exact
GetMetricData / ListMetrics responses without hitting AWS.

Covered here:
  - ``parse_window`` canonicalization + fallback on bogus input
  - ``get_tenant_metrics`` happy path: aggregates invocations, errors,
    cost, p50/p95, top tools from stubbed CloudWatch responses
  - ``get_tenant_metrics`` fail-open: boto3 errors return an empty
    snapshot with ``.error`` populated, not an exception
  - ``list_active_tenants`` roster enumeration
  - ``get_ops_roster`` cross-tenant aggregation + sort order

Route coverage:
  - ``GET /api/tenants/{id}/metrics`` — session-gated, tenant_id forced
    from the token (even if the URL path says otherwise)
  - ``GET /api/ops/metrics/roster`` — 503 when ADMIN_SECRET unset,
    401 on wrong header, 200 on match
  - ``GET /api/ops/metrics/tenants/{id}`` — admin-gated drill-down
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from bridge import metrics_reader
from bridge.main import app
from bridge.slack_oauth import make_session_token


# ---------------------------------------------------------------------------
# Fake CloudWatch client
# ---------------------------------------------------------------------------

class FakeCloudWatch:
    """Programmable stand-in for ``boto3.client('cloudwatch')``.

    Pass a sequence of responses per API method. Each call pops the
    next response from the queue. Missing responses return ``{}``.
    Raising can be tested by passing an ``Exception`` instance — the
    stub raises it instead of returning.
    """

    def __init__(
        self,
        get_metric_data_responses: list[Any] | None = None,
        list_metrics_responses: list[Any] | None = None,
    ) -> None:
        self.gmd_queue = list(get_metric_data_responses or [])
        self.lm_queue = list(list_metrics_responses or [])
        self.gmd_calls: list[dict[str, Any]] = []
        self.lm_calls: list[dict[str, Any]] = []

    def get_metric_data(self, **kwargs: Any) -> dict[str, Any]:
        self.gmd_calls.append(kwargs)
        if not self.gmd_queue:
            return {"MetricDataResults": []}
        nxt = self.gmd_queue.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    def list_metrics(self, **kwargs: Any) -> dict[str, Any]:
        self.lm_calls.append(kwargs)
        if not self.lm_queue:
            return {"Metrics": []}
        nxt = self.lm_queue.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


@pytest.fixture(autouse=True)
def _reset_client() -> Any:
    """Ensure a clean module-level client cache for every test."""
    metrics_reader._reset_client_for_tests()
    yield
    metrics_reader._reset_client_for_tests()


def install_fake(monkeypatch: pytest.MonkeyPatch, fake: FakeCloudWatch) -> None:
    """Inject the fake client into the metrics_reader singleton slot."""
    monkeypatch.setattr(metrics_reader, "_client", fake)


# ---------------------------------------------------------------------------
# parse_window
# ---------------------------------------------------------------------------

class TestParseWindow:
    def test_known_windows(self) -> None:
        for win, expected_period in [("1h", 60), ("24h", 900), ("7d", 3600), ("30d", 21600)]:
            w = metrics_reader.parse_window(win)
            assert w.period_sec == expected_period
            assert w.label == win

    def test_bogus_falls_back_to_7d(self) -> None:
        w = metrics_reader.parse_window("fortnight")
        assert w.label == "7d"
        assert w.period_sec == 3600

    def test_window_covers_requested_range(self) -> None:
        w = metrics_reader.parse_window("24h")
        delta = w.end - w.start
        # Allow a second of slack from time-of-call drift.
        assert abs(delta - timedelta(hours=24)) < timedelta(seconds=5)


# ---------------------------------------------------------------------------
# get_tenant_metrics
# ---------------------------------------------------------------------------

def _make_points(count: int, value: float = 1.0) -> tuple[list[datetime], list[float]]:
    now = datetime.now(timezone.utc)
    return (
        [now - timedelta(minutes=i) for i in range(count)],
        [value] * count,
    )


class TestGetTenantMetrics:
    def test_happy_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # First GMD batch: invocations/errors/p50/p95
        inv_ts, inv_vs = _make_points(5, 10.0)   # 50 invocations
        err_ts, err_vs = _make_points(5, 1.0)    # 5 errors (10% rate)
        p50_ts, p50_vs = _make_points(5, 200.0)  # p50 average 200ms
        p95_ts, p95_vs = _make_points(5, 800.0)

        batch1 = {
            "MetricDataResults": [
                {"Id": "inv", "Timestamps": inv_ts, "Values": inv_vs},
                {"Id": "err", "Timestamps": err_ts, "Values": err_vs},
                {"Id": "p50", "Timestamps": p50_ts, "Values": p50_vs},
                {"Id": "p95", "Timestamps": p95_ts, "Values": p95_vs},
            ]
        }

        # ListMetrics discovers one model
        lm_models = {
            "Metrics": [
                {
                    "Namespace": metrics_reader.NAMESPACE,
                    "MetricName": "InputTokens",
                    "Dimensions": [
                        {"Name": "tenant_id", "Value": "acme"},
                        {"Name": "model_id", "Value": "global.anthropic.claude-sonnet-4-6"},
                    ],
                }
            ]
        }

        # Second GMD batch: tokens + cost (one model = 3 queries)
        in_ts, in_vs = _make_points(5, 1000.0)   # 5000 input tokens
        out_ts, out_vs = _make_points(5, 500.0)  # 2500 output tokens
        c_ts, c_vs = _make_points(5, 4.0)        # 20 cents
        batch2 = {
            "MetricDataResults": [
                {"Id": "in_0", "Timestamps": in_ts, "Values": in_vs},
                {"Id": "out_0", "Timestamps": out_ts, "Values": out_vs},
                {"Id": "c_0", "Timestamps": c_ts, "Values": c_vs},
            ]
        }

        # ListMetrics for tools — one tool
        lm_tools = {
            "Metrics": [
                {
                    "Namespace": metrics_reader.NAMESPACE,
                    "MetricName": "ToolCalls",
                    "Dimensions": [
                        {"Name": "tenant_id", "Value": "acme"},
                        {"Name": "tool_name", "Value": "echo"},
                    ],
                }
            ]
        }

        # Third GMD batch: tool call volume + errors
        batch3 = {
            "MetricDataResults": [
                {"Id": "tc_0", "Timestamps": inv_ts, "Values": [7.0] * 5},  # 35 calls
                {"Id": "te_0", "Timestamps": inv_ts, "Values": [0.0] * 5},
            ]
        }

        fake = FakeCloudWatch(
            get_metric_data_responses=[batch1, batch2, batch3],
            list_metrics_responses=[lm_models, lm_tools],
        )
        install_fake(monkeypatch, fake)

        result = metrics_reader.get_tenant_metrics("acme", "7d")

        assert result.tenant_id == "acme"
        assert result.window == "7d"
        assert result.invocations_total == 50
        assert result.errors_total == 5
        assert result.error_rate_pct == 10.0
        assert result.p50_duration_ms == 200.0
        assert result.p95_duration_ms == 800.0
        assert result.input_tokens_total == 5000
        assert result.output_tokens_total == 2500
        assert result.estimated_cost_cents_total == 20
        assert result.top_tools == [{"tool_name": "echo", "calls": 35, "errors": 0}]
        assert result.error is None
        assert len(result.invocations_timeseries) == 5

    def test_no_data_returns_empty_snapshot(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = FakeCloudWatch(
            get_metric_data_responses=[{"MetricDataResults": []}],
            list_metrics_responses=[{"Metrics": []}],
        )
        install_fake(monkeypatch, fake)

        result = metrics_reader.get_tenant_metrics("empty-tenant", "7d")

        assert result.invocations_total == 0
        assert result.errors_total == 0
        assert result.error_rate_pct == 0.0
        assert result.error is None
        assert result.top_tools == []

    def test_first_batch_failure_returns_error(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fake = FakeCloudWatch(
            get_metric_data_responses=[RuntimeError("throttled")],
        )
        install_fake(monkeypatch, fake)

        result = metrics_reader.get_tenant_metrics("acme", "24h")
        assert result.error is not None
        assert "throttled" in result.error
        assert result.invocations_total == 0

    def test_tenant_id_is_baked_into_every_query(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Isolation check: the boto3 call must filter on tenant_id — a
        bug that dropped the dimension filter would leak other tenants'
        data into the response."""
        fake = FakeCloudWatch(
            get_metric_data_responses=[{"MetricDataResults": []}],
            list_metrics_responses=[{"Metrics": []}],
        )
        install_fake(monkeypatch, fake)

        metrics_reader.get_tenant_metrics("tenant-xyz", "7d")

        # Every MetricStat.Metric must carry the tenant_id dimension.
        for call in fake.gmd_calls:
            for q in call["MetricDataQueries"]:
                dims = q["MetricStat"]["Metric"]["Dimensions"]
                assert any(
                    d["Name"] == "tenant_id" and d["Value"] == "tenant-xyz"
                    for d in dims
                ), f"query {q['Id']} missing tenant_id filter"


# ---------------------------------------------------------------------------
# list_active_tenants + ops roster
# ---------------------------------------------------------------------------

class TestOpsRoster:
    def test_list_active_tenants(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = FakeCloudWatch(
            list_metrics_responses=[{
                "Metrics": [
                    {
                        "MetricName": "Invocations",
                        "Dimensions": [{"Name": "tenant_id", "Value": "acme"}],
                    },
                    {
                        "MetricName": "Invocations",
                        "Dimensions": [{"Name": "tenant_id", "Value": "globex"}],
                    },
                    {
                        "MetricName": "Invocations",
                        "Dimensions": [{"Name": "tenant_id", "Value": "acme"}],  # dup
                    },
                ]
            }]
        )
        install_fake(monkeypatch, fake)

        tenants = metrics_reader.list_active_tenants()
        assert tenants == ["acme", "globex"]

    def test_get_ops_roster_sorted_by_invocations(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # list_active_tenants
        roster_lm = {
            "Metrics": [
                {"Dimensions": [{"Name": "tenant_id", "Value": "a"}]},
                {"Dimensions": [{"Name": "tenant_id", "Value": "b"}]},
            ]
        }

        # First GMD: invocations + errors for both tenants
        inv_data = {
            "MetricDataResults": [
                {"Id": "inv_0", "Values": [100.0]},  # a
                {"Id": "err_0", "Values": [5.0]},
                {"Id": "inv_1", "Values": [300.0]},  # b
                {"Id": "err_1", "Values": [0.0]},
            ]
        }

        # Per-tenant cost lookups — one list_metrics + one get_metric_data each.
        cost_lm_a = {
            "Metrics": [
                {"Dimensions": [
                    {"Name": "tenant_id", "Value": "a"},
                    {"Name": "model_id", "Value": "global.anthropic.claude-sonnet-4-6"},
                ]}
            ]
        }
        cost_gmd_a = {"MetricDataResults": [{"Id": "c_0", "Values": [12.0]}]}
        cost_lm_b = {
            "Metrics": [
                {"Dimensions": [
                    {"Name": "tenant_id", "Value": "b"},
                    {"Name": "model_id", "Value": "global.anthropic.claude-sonnet-4-6"},
                ]}
            ]
        }
        cost_gmd_b = {"MetricDataResults": [{"Id": "c_0", "Values": [34.0]}]}

        fake = FakeCloudWatch(
            get_metric_data_responses=[inv_data, cost_gmd_a, cost_gmd_b],
            list_metrics_responses=[roster_lm, cost_lm_a, cost_lm_b],
        )
        install_fake(monkeypatch, fake)

        rows = metrics_reader.get_ops_roster("7d")
        # b has more invocations and should sort first
        assert len(rows) == 2
        assert rows[0].tenant_id == "b"
        assert rows[0].invocations == 300
        assert rows[0].errors == 0
        assert rows[0].error_rate_pct == 0.0
        assert rows[0].cost_cents == 34
        assert rows[1].tenant_id == "a"
        assert rows[1].invocations == 100
        assert rows[1].errors == 5
        assert rows[1].error_rate_pct == 5.0
        assert rows[1].cost_cents == 12


# ---------------------------------------------------------------------------
# Route integration — exercise the FastAPI auth + handler wiring
# ---------------------------------------------------------------------------

@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


class TestMetricsRoutes:
    def test_tenant_metrics_requires_session(self, client: TestClient) -> None:
        resp = client.get("/api/tenants/acme/metrics")
        assert resp.status_code == 401

    def test_tenant_metrics_wrong_tenant_token(self, client: TestClient) -> None:
        token = make_session_token("other-tenant")
        resp = client.get(
            "/api/tenants/acme/metrics",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 403

    def test_tenant_metrics_happy_path(
        self, monkeypatch: pytest.MonkeyPatch, client: TestClient,
    ) -> None:
        fake = FakeCloudWatch(
            get_metric_data_responses=[{"MetricDataResults": []}],
            list_metrics_responses=[{"Metrics": []}],
        )
        install_fake(monkeypatch, fake)

        token = make_session_token("acme")
        resp = client.get(
            "/api/tenants/acme/metrics?window=24h",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["tenant_id"] == "acme"
        assert body["window"] == "24h"
        assert body["invocations_total"] == 0

    def test_ops_roster_without_admin_secret_returns_503(
        self, monkeypatch: pytest.MonkeyPatch, client: TestClient,
    ) -> None:
        monkeypatch.delenv("ADMIN_SECRET", raising=False)
        resp = client.get(
            "/api/ops/metrics/roster",
            headers={"X-Admin-Token": "anything"},
        )
        assert resp.status_code == 503

    def test_ops_roster_wrong_token(
        self, monkeypatch: pytest.MonkeyPatch, client: TestClient,
    ) -> None:
        monkeypatch.setenv("ADMIN_SECRET", "real-secret")
        resp = client.get(
            "/api/ops/metrics/roster",
            headers={"X-Admin-Token": "wrong-secret"},
        )
        assert resp.status_code == 401

    def test_ops_roster_happy_path(
        self, monkeypatch: pytest.MonkeyPatch, client: TestClient,
    ) -> None:
        monkeypatch.setenv("ADMIN_SECRET", "real-secret")

        fake = FakeCloudWatch(
            list_metrics_responses=[{"Metrics": []}],  # no active tenants
        )
        install_fake(monkeypatch, fake)

        resp = client.get(
            "/api/ops/metrics/roster",
            headers={"X-Admin-Token": "real-secret"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["window"] == "7d"
        assert body["tenants"] == []

    def test_ops_tenant_drilldown_admin_gated(
        self, monkeypatch: pytest.MonkeyPatch, client: TestClient,
    ) -> None:
        monkeypatch.setenv("ADMIN_SECRET", "real-secret")
        fake = FakeCloudWatch(
            get_metric_data_responses=[{"MetricDataResults": []}],
            list_metrics_responses=[{"Metrics": []}],
        )
        install_fake(monkeypatch, fake)

        # No token → 503 (when guard checks secret and finds no X-Admin-Token).
        resp = client.get("/api/ops/metrics/tenants/acme")
        assert resp.status_code == 401

        # With the correct token → 200 and a well-shaped response.
        resp = client.get(
            "/api/ops/metrics/tenants/acme",
            headers={"X-Admin-Token": "real-secret"},
        )
        assert resp.status_code == 200
        assert resp.json()["tenant_id"] == "acme"
