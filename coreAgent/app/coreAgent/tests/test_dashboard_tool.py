"""Focused tests for the ephemeral dashboard catalog tool."""
from __future__ import annotations

import logging
import stat
from decimal import Decimal
from types import SimpleNamespace

import pytest

import tools as catalog_tools
from audit import InMemoryAuditStore
from tenant import DEFAULT_CATALOG_TOOLS


class _FakeTable:
    def __init__(self) -> None:
        self.item: dict | None = None

    def put_item(self, *, Item: dict) -> None:
        self.item = Item


def _chart_panel(*, value: float = 2.3) -> dict:
    return {
        "type": "chart",
        "title": "Latency",
        "chart_type": "line",
        "labels": ["10:00"],
        "datasets": [{"label": "p99", "data": [value]}],
    }


def _pie_panel(values: list[int | float]) -> dict:
    return {
        "type": "chart",
        "title": "Traffic share",
        "chart_type": "pie",
        "labels": [f"slice-{index}" for index in range(len(values))],
        "datasets": [{"label": "requests", "data": values}],
    }


def test_render_dashboard_validates_and_writes_dynamodb_safe_numbers(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=catalog_tools.__name__)
    table = _FakeTable()
    monkeypatch.setenv("AGENT_LOCAL_STORES", "0")
    monkeypatch.setenv("DASHBOARD_BASE_URL", "https://dashboards.example.com")
    monkeypatch.setattr(catalog_tools, "_dashboards_table", lambda: table)
    monkeypatch.setattr(
        catalog_tools,
        "get_context",
        lambda: {"tenant_id": "tenant-a", "user_id": "U123"},
    )
    monkeypatch.setattr(
        catalog_tools.uuid,
        "uuid4",
        lambda: SimpleNamespace(hex="a" * 32),
    )

    url = catalog_tools._render_dashboard("Service health", [_chart_panel()])

    assert url == f"https://dashboards.example.com/d/{'a' * 32}"
    assert table.item is not None
    assert table.item["panels"][0]["datasets"][0]["data"] == [Decimal("2.3")]
    assert table.item["tenant_id"] == "tenant-a"
    assert table.item["created_by"] == "U123"
    assert "a" * 32 not in caplog.text
    assert "dashboards.example.com" not in caplog.text


def test_dashboard_audit_omits_panel_content_and_bearer_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    table = _FakeTable()
    store = InMemoryAuditStore()
    token = "c" * 32
    sentinel = "PRIVATE_INCIDENT_DETAIL"
    monkeypatch.setenv("AGENT_LOCAL_STORES", "0")
    monkeypatch.setenv("DASHBOARD_BASE_URL", "https://dashboards.example.com")
    monkeypatch.setattr(catalog_tools, "_dashboards_table", lambda: table)
    monkeypatch.setattr(catalog_tools, "_audit", store)
    monkeypatch.setattr(
        catalog_tools,
        "get_context",
        lambda: {
            "tenant_id": "tenant-a",
            "user_id": "U123",
            "invocation_id": "inv-1",
        },
    )
    monkeypatch.setattr(
        catalog_tools.uuid,
        "uuid4",
        lambda: SimpleNamespace(hex=token),
    )

    result = catalog_tools.render_dashboard._tool_func(
        "Incident review",
        [{"type": "text", "title": "Summary", "content": sentinel}],
    )

    assert result.endswith(f"/d/{token}")
    row = store.rows_for("tenant-a")[0]
    assert sentinel not in str(row)
    assert token not in str(row)
    assert '"panel_count": 1' in row["tool_args_summary"]
    assert row["tool_result_summary"] == "<dashboard bearer URL redacted>"


def test_invocation_audit_redacts_dashboard_bearer_tokens() -> None:
    store = InMemoryAuditStore()
    token = "d" * 32
    store.write(
        {
            "tenant_id": "tenant-a",
            "output_summary": f"Open https://agent.example.com/d/{token}",
        }
    )

    row = store.rows_for("tenant-a")[0]
    assert token not in row["output_summary"]
    assert row["output_summary"].endswith("/d/[REDACTED]")


@pytest.mark.parametrize(
    "panels, message",
    [
        ([{"type": "chart", "chart_type": "line", "labels": ["x"]}], "datasets"),
        ([_chart_panel(value=float("nan"))], "finite"),
        (
            [{"type": "table", "columns": ["a", "b"], "rows": [[1]]}],
            "one cell per column",
        ),
        ([{"type": "mystery", "content": "nope"}], "does not match"),
    ],
)
def test_dashboard_schema_rejects_malformed_panels(panels: list[dict], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        catalog_tools._validate_dashboard("Test", panels)


@pytest.mark.parametrize(
    "values, message",
    [
        ([3, -1, 2], "non-negative"),
        ([0, 0, 0], "at least one positive"),
    ],
)
def test_pie_charts_reject_invalid_values(
    values: list[int | float], message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        catalog_tools._validate_dashboard("Traffic", [_pie_panel(values)])


def test_pie_charts_accept_zero_when_another_slice_is_positive() -> None:
    spec = catalog_tools._validate_dashboard("Traffic", [_pie_panel([0, 4, 2.5])])
    assert spec["panels"][0]["datasets"][0]["data"] == [0, 4, 2.5]


def test_dashboard_rejects_oversized_item(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_LOCAL_STORES", "0")
    monkeypatch.setenv("DASHBOARD_BASE_URL", "https://dashboards.example.com")
    panels = [
        {"type": "text", "title": f"Panel {index}", "content": "x" * 20_000}
        for index in range(14)
    ]

    with pytest.raises(ValueError, match="serialized spec exceeds"):
        catalog_tools._render_dashboard("Too large", panels)


def test_dashboard_rejects_excessive_browser_render_complexity() -> None:
    columns = [f"column-{index}" for index in range(24)]
    rows = [[index] * len(columns) for index in range(500)]

    with pytest.raises(ValueError, match="render complexity"):
        catalog_tools._validate_dashboard(
            "Too many cells",
            [{"type": "table", "columns": columns, "rows": rows}],
        )


def test_non_finite_numbers_are_rejected_before_dynamodb() -> None:
    with pytest.raises(ValueError, match="finite"):
        catalog_tools._floats_to_decimal(float("inf"))


def test_dashboard_base_url_is_portable_and_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DASHBOARD_BASE_URL", raising=False)
    monkeypatch.setenv("AGENT_LOCAL_STORES", "1")
    assert catalog_tools._dashboard_base_url() == "http://localhost:3000"

    monkeypatch.setenv("AGENT_LOCAL_STORES", "0")
    monkeypatch.delenv("LOCAL_DEV", raising=False)
    with pytest.raises(RuntimeError, match="required"):
        catalog_tools._dashboard_base_url()

    monkeypatch.setenv("DASHBOARD_BASE_URL", "http://dashboards.example.com")
    with pytest.raises(ValueError, match="HTTPS"):
        catalog_tools._dashboard_base_url()


def test_local_dashboard_store_writes_shared_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    monkeypatch.setenv("AGENT_LOCAL_STORES", "1")
    monkeypatch.setenv("DASHBOARD_LOCAL_DIR", str(tmp_path))
    monkeypatch.delenv("DASHBOARD_BASE_URL", raising=False)
    monkeypatch.setattr(catalog_tools, "get_context", lambda: {"tenant_id": "demo"})
    monkeypatch.setattr(
        catalog_tools.uuid,
        "uuid4",
        lambda: SimpleNamespace(hex="b" * 32),
    )

    url = catalog_tools._render_dashboard(
        "Local dashboard",
        [{"type": "stat", "value": "99.9%"}],
    )

    assert url == f"http://localhost:3000/d/{'b' * 32}"
    payload = (tmp_path / f"{'b' * 32}.json").read_text()
    assert '"title": "Local dashboard"' in payload
    assert stat.S_IMODE((tmp_path / f"{'b' * 32}.json").stat().st_mode) == 0o600


def test_render_dashboard_is_in_default_catalog() -> None:
    assert "render_dashboard" in DEFAULT_CATALOG_TOOLS
    assert "propose_pr" not in DEFAULT_CATALOG_TOOLS
    assert "render_dashboard" in catalog_tools.CATALOG


def test_runtime_feature_gates_default_to_safe_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "DASHBOARD_BASE_URL",
        "AGENT_LOCAL_STORES",
        "LOCAL_DEV",
        "ENABLE_EXPERIMENTAL_PR_SANDBOX",
    ):
        monkeypatch.delenv(name, raising=False)

    requested = ["echo", "render_dashboard", "propose_pr"]
    assert catalog_tools.filter_runtime_available_tools(requested) == ["echo"]

    monkeypatch.setenv("DASHBOARD_BASE_URL", "https://agent.example.com")
    assert catalog_tools.filter_runtime_available_tools(requested) == [
        "echo",
        "render_dashboard",
    ]

    monkeypatch.setenv("ENABLE_EXPERIMENTAL_PR_SANDBOX", "1")
    assert catalog_tools.filter_runtime_available_tools(requested) == requested


def test_external_document_search_is_not_a_catalog_stub() -> None:
    assert "search_docs" not in DEFAULT_CATALOG_TOOLS
    assert "search_docs" not in catalog_tools.CATALOG
