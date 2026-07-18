"""Focused storage and HTTP tests for ephemeral dashboards."""
from __future__ import annotations

import json
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from bridge import dashboard_store, tenant_write
from bridge import main as bridge_main
from bridge.main import app
from bridge.rate_limit import TokenBucketRateLimiter


TOKEN = "a" * 32


def _stored_item(*, ttl: int = 2_000_000_000) -> dict:
    return {
        "token": TOKEN,
        "tenant_id": "tenant-private",
        "created_by": "U_PRIVATE",
        "created_at": "2026-07-17T12:00:00.000Z",
        "ttl": Decimal(ttl),
        "title": "Service health",
        "panels": [
            {
                "type": "chart",
                "chart_type": "line",
                "labels": ["now"],
                "datasets": [{"label": "latency", "data": [Decimal("2.3")]}],
            }
        ],
    }


class _FakeTable:
    def __init__(self, item: dict | None = None, error: Exception | None = None) -> None:
        self.item = item
        self.error = error
        self.calls: list[dict] = []

    def get_item(self, **kwargs: object) -> dict:
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return {"Item": self.item} if self.item is not None else {}


def test_store_uses_consistent_read_enforces_ttl_and_hides_private_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    table = _FakeTable(_stored_item())
    monkeypatch.setenv("LOCAL_DEV", "0")
    monkeypatch.setattr(dashboard_store, "_table_singleton", table)
    monkeypatch.setattr(dashboard_store.time, "time", lambda: 1_900_000_000)

    result = dashboard_store.get_dashboard_spec(TOKEN)

    assert table.calls == [{"Key": {"token": TOKEN}, "ConsistentRead": True}]
    assert result == {
        "created_at": "2026-07-17T12:00:00.000Z",
        "ttl": 2_000_000_000,
        "title": "Service health",
        "panels": [
            {
                "type": "chart",
                "chart_type": "line",
                "labels": ["now"],
                "datasets": [{"label": "latency", "data": [2.3]}],
            }
        ],
    }
    assert "tenant_id" not in result
    assert "created_by" not in result
    assert "token" not in result


def test_store_treats_expired_record_as_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOCAL_DEV", "0")
    monkeypatch.setattr(dashboard_store, "_table_singleton", _FakeTable(_stored_item(ttl=99)))
    monkeypatch.setattr(dashboard_store.time, "time", lambda: 100)
    assert dashboard_store.get_dashboard_spec(TOKEN) is None


def test_store_distinguishes_backend_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOCAL_DEV", "0")
    monkeypatch.setattr(
        dashboard_store,
        "_table_singleton",
        _FakeTable(error=RuntimeError("ddb unavailable")),
    )
    with pytest.raises(dashboard_store.DashboardStoreError, match="unavailable"):
        dashboard_store.get_dashboard_spec(TOKEN)


def test_local_store_reads_shared_json_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    monkeypatch.setenv("LOCAL_DEV", "1")
    monkeypatch.setenv("DASHBOARD_LOCAL_DIR", str(tmp_path))
    item = _stored_item()
    item["ttl"] = 2_000_000_000
    item["panels"][0]["datasets"][0]["data"] = [2.3]
    (tmp_path / f"{TOKEN}.json").write_text(json.dumps(item))
    monkeypatch.setattr(dashboard_store.time, "time", lambda: 1_900_000_000)

    assert dashboard_store.get_dashboard_spec(TOKEN)["title"] == "Service health"


def test_local_store_does_not_follow_dashboard_symlinks(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    monkeypatch.setenv("LOCAL_DEV", "1")
    monkeypatch.setenv("DASHBOARD_LOCAL_DIR", str(tmp_path / "dashboards"))
    (tmp_path / "dashboards").mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text("private local data")
    (tmp_path / "dashboards" / f"{TOKEN}.json").symlink_to(outside)

    assert dashboard_store.get_dashboard_spec(TOKEN) is None


def test_dashboard_route_validates_token_and_sets_private_cache_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_get(token: str) -> dict:
        calls.append(token)
        return {
            "created_at": "2026-07-17T12:00:00.000Z",
            "ttl": 2_000_000_000,
            "title": "Service health",
            "panels": [],
        }

    monkeypatch.setattr(dashboard_store, "get_dashboard_spec", fake_get)
    with TestClient(app) as client:
        invalid = client.get(
            "/internal/dashboard",
            headers={"X-Dashboard-Token": "not-a-token"},
        )
        response = client.get(
            "/internal/dashboard",
            headers={"X-Dashboard-Token": TOKEN},
        )

    assert invalid.status_code == 404
    assert calls == [TOKEN]
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-robots-tag"] == "noindex"
    assert response.headers["referrer-policy"] == "no-referrer"


def test_dashboard_route_maps_store_failure_to_503(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(_: str) -> None:
        raise dashboard_store.DashboardStoreError("down")

    monkeypatch.setattr(dashboard_store, "get_dashboard_spec", fail)
    with TestClient(app) as client:
        response = client.get(
            "/internal/dashboard",
            headers={"X-Dashboard-Token": TOKEN},
        )

    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["retry-after"] == "5"


def test_dashboard_route_rate_limits_well_shaped_token_guesses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setenv("DASHBOARD_TRUST_X_FORWARDED_FOR", "1")
    monkeypatch.setattr(
        bridge_main,
        "_DASHBOARD_RATE_LIMIT",
        TokenBucketRateLimiter(capacity=2, clock=lambda: 100.0),
    )
    monkeypatch.setattr(
        dashboard_store,
        "get_dashboard_spec",
        lambda token: calls.append(token) or None,
    )

    with TestClient(app) as client:
        responses = [
            client.get(
                "/internal/dashboard",
                headers={
                    "X-Dashboard-Token": TOKEN,
                    "X-Forwarded-For": "spoofed, 203.0.113.9",
                },
            )
            for _ in range(3)
        ]

    assert [response.status_code for response in responses] == [404, 404, 429]
    assert calls == [TOKEN, TOKEN]
    assert responses[-1].headers["cache-control"] == "no-store"
    assert int(responses[-1].headers["retry-after"]) >= 1


def test_dashboard_route_rejects_when_read_capacity_is_saturated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class NoCapacity:
        def acquire(self, *, blocking: bool) -> bool:
            assert blocking is False
            return False

        def release(self) -> None:
            raise AssertionError("an unacquired slot must not be released")

    monkeypatch.setattr(bridge_main, "_DASHBOARD_READ_SLOTS", NoCapacity())
    monkeypatch.setattr(
        dashboard_store,
        "get_dashboard_spec",
        lambda _token: pytest.fail("saturated requests must not read DynamoDB"),
    )

    with TestClient(app) as client:
        response = client.get(
            "/internal/dashboard",
            headers={"X-Dashboard-Token": TOKEN},
        )

    assert response.status_code == 429
    assert response.headers["retry-after"] == "1"


def test_render_dashboard_is_in_bridge_default_catalog() -> None:
    assert "render_dashboard" in tenant_write.DEFAULT_CATALOG_TOOLS
    assert "propose_pr" not in tenant_write.DEFAULT_CATALOG_TOOLS


def test_external_document_search_is_not_a_bridge_catalog_default() -> None:
    assert "search_docs" not in tenant_write.DEFAULT_CATALOG_TOOLS
