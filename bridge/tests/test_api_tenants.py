"""Tests for `/api/tenants/*` routes.

Uses `fastapi.testclient.TestClient` to exercise the full FastAPI
stack (dependency resolution, Pydantic validation, auth dependency,
response serialization). The tenant store is stubbed out via
monkeypatching `bridge.api.get_tenant_row` and
`bridge.api.update_tenant_row` to use an in-process dict so tests
don't touch `examples/tenants/*.json`.
"""
from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from bridge.main import app
from bridge.slack_oauth import make_session_token


@pytest.fixture
def stub_store(monkeypatch: pytest.MonkeyPatch) -> dict[str, dict[str, Any]]:
    """Replace bridge.api's tenant_write helpers with an in-memory dict."""
    store: dict[str, dict[str, Any]] = {
        "slack-alpha": {
            "tenant_id": "slack-alpha",
            "model_id": "global.anthropic.claude-sonnet-4-6",
            "system_prompt": "You are Alpha's helper.",
            "catalog": {
                "allowed_tools": ["echo"],
                "tool_config": {"echo": {"prefix": "[alpha]"}},
            },
            "byo": {"enabled": False, "gateway_endpoint": None, "gateway_auth": None},
            "memory": {
                "triggers": {
                    "message_count": 6,
                    "token_count": 1000,
                    "idle_timeout_seconds": 1800,
                },
                "namespace": "tenants/slack-alpha",
                "extraction": {"enabled": True, "rules": ["facts"]},
            },
            "heartbeat": {"busy_threshold": 1, "max_background_seconds": 3600},
        },
        "slack-beta": {
            "tenant_id": "slack-beta",
            "model_id": "global.anthropic.claude-sonnet-4-6",
            "system_prompt": "You are Beta's helper.",
            "catalog": {
                "allowed_tools": ["echo"],
                "tool_config": {},
            },
            "byo": {"enabled": False, "gateway_endpoint": None, "gateway_auth": None},
            "memory": {
                "triggers": {
                    "message_count": 6,
                    "token_count": 1000,
                    "idle_timeout_seconds": 1800,
                },
                "namespace": "tenants/slack-beta",
                "extraction": {"enabled": True, "rules": ["facts"]},
            },
            "heartbeat": {"busy_threshold": 1, "max_background_seconds": 3600},
        },
    }

    def fake_get(tenant_id: str, _region: str) -> dict[str, Any]:
        if tenant_id not in store:
            raise KeyError(tenant_id)
        # Return a copy so routes can't accidentally mutate the backing store.
        import copy
        return copy.deepcopy(store[tenant_id])

    def fake_update(tenant_id: str, _region: str, full: dict[str, Any]) -> None:
        if tenant_id not in store:
            raise KeyError(tenant_id)
        store[tenant_id] = full

    monkeypatch.setattr("bridge.api.get_tenant_row", fake_get)
    monkeypatch.setattr("bridge.api.update_tenant_row", fake_update)
    return store


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ----------------------------------------------------------------------------
# Auth
# ----------------------------------------------------------------------------

def test_get_tenant_no_auth_header_401(client: TestClient, stub_store: dict) -> None:
    r = client.get("/api/tenants/slack-alpha")
    assert r.status_code == 401


def test_get_tenant_malformed_auth_header_401(client: TestClient, stub_store: dict) -> None:
    r = client.get(
        "/api/tenants/slack-alpha",
        headers={"Authorization": "NotBearer xyz"},
    )
    assert r.status_code == 401


def test_get_tenant_wrong_tenant_in_token_403(client: TestClient, stub_store: dict) -> None:
    token = make_session_token("slack-alpha")
    r = client.get("/api/tenants/slack-beta", headers=_auth(token))
    assert r.status_code == 403


def test_get_tenant_invalid_token_401(client: TestClient, stub_store: dict) -> None:
    r = client.get(
        "/api/tenants/slack-alpha",
        headers=_auth("totally.not.a.valid.token"),
    )
    assert r.status_code == 401


# ----------------------------------------------------------------------------
# GET
# ----------------------------------------------------------------------------

def test_get_tenant_success(client: TestClient, stub_store: dict) -> None:
    token = make_session_token("slack-alpha")
    r = client.get("/api/tenants/slack-alpha", headers=_auth(token))
    assert r.status_code == 200
    body = r.json()
    assert body["tenant_id"] == "slack-alpha"
    assert body["system_prompt"] == "You are Alpha's helper."
    assert body["catalog"]["allowed_tools"] == ["echo"]
    assert body["catalog"]["tool_config"] == {"echo": {"prefix": "[alpha]"}}


def test_get_tenant_not_found_404(client: TestClient, stub_store: dict) -> None:
    token = make_session_token("slack-gamma")
    r = client.get("/api/tenants/slack-gamma", headers=_auth(token))
    assert r.status_code == 404


# ----------------------------------------------------------------------------
# PATCH
# ----------------------------------------------------------------------------

def test_patch_system_prompt_merges(
    client: TestClient, stub_store: dict
) -> None:
    token = make_session_token("slack-alpha")
    r = client.patch(
        "/api/tenants/slack-alpha",
        headers=_auth(token),
        json={"system_prompt": "You are Alpha's new helper."},
    )
    assert r.status_code == 200, r.text
    assert r.json()["system_prompt"] == "You are Alpha's new helper."
    # catalog must survive untouched, including the nested tool_config
    assert stub_store["slack-alpha"]["catalog"]["allowed_tools"] == ["echo"]
    assert (
        stub_store["slack-alpha"]["catalog"]["tool_config"]
        == {"echo": {"prefix": "[alpha]"}}
    )


def test_patch_allowed_tools_preserves_tool_config(
    client: TestClient, stub_store: dict
) -> None:
    """The critical deep-merge test: sending catalog.allowed_tools must
    NOT wipe catalog.tool_config (which is the bug Pydantic model_copy
    would cause if we used shallow merge)."""
    token = make_session_token("slack-alpha")
    r = client.patch(
        "/api/tenants/slack-alpha",
        headers=_auth(token),
        json={"catalog": {"allowed_tools": ["echo", "start_background_task"]}},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["catalog"]["allowed_tools"] == ["echo", "start_background_task"]
    assert body["catalog"]["tool_config"] == {"echo": {"prefix": "[alpha]"}}
    # And confirm it's actually persisted, not just in the response
    assert (
        stub_store["slack-alpha"]["catalog"]["tool_config"]
        == {"echo": {"prefix": "[alpha]"}}
    )


def test_patch_empty_system_prompt_422(client: TestClient, stub_store: dict) -> None:
    """Empty system_prompt is blocked by Pydantic min_length=1 before it
    can hit Bedrock. Matches the week-2 smoke-test bug fix."""
    token = make_session_token("slack-alpha")
    r = client.patch(
        "/api/tenants/slack-alpha",
        headers=_auth(token),
        json={"system_prompt": ""},
    )
    assert r.status_code == 422


def test_patch_invalid_type_422(client: TestClient, stub_store: dict) -> None:
    """allowed_tools must be a list."""
    token = make_session_token("slack-alpha")
    r = client.patch(
        "/api/tenants/slack-alpha",
        headers=_auth(token),
        json={"catalog": {"allowed_tools": "echo"}},
    )
    assert r.status_code == 422


def test_patch_cross_tenant_isolation(client: TestClient, stub_store: dict) -> None:
    """A token for tenant A must not be able to modify tenant B, and
    neither tenant's data should be altered by the attempt."""
    alpha_before = dict(stub_store["slack-alpha"])
    beta_before = dict(stub_store["slack-beta"])

    alpha_token = make_session_token("slack-alpha")
    r = client.patch(
        "/api/tenants/slack-beta",
        headers=_auth(alpha_token),
        json={"system_prompt": "pwned"},
    )
    assert r.status_code == 403
    assert stub_store["slack-alpha"] == alpha_before
    assert stub_store["slack-beta"] == beta_before


def test_patch_nonexistent_tenant_404(client: TestClient, stub_store: dict) -> None:
    """PATCH refuses to create; only OAuth can bring a tenant into existence."""
    token = make_session_token("slack-gamma")
    r = client.patch(
        "/api/tenants/slack-gamma",
        headers=_auth(token),
        json={"system_prompt": "hello"},
    )
    assert r.status_code == 404


def test_patch_unknown_top_level_field_422(client: TestClient, stub_store: dict) -> None:
    """TenantConfigPatch has extra='forbid' — unknown keys fail fast."""
    token = make_session_token("slack-alpha")
    r = client.patch(
        "/api/tenants/slack-alpha",
        headers=_auth(token),
        json={"nonexistent_field": "whatever"},
    )
    assert r.status_code == 422
