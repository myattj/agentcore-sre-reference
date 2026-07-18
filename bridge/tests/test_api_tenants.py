"""Tests for `/api/tenants/*` routes.

Uses `fastapi.testclient.TestClient` to exercise the full FastAPI
stack (dependency resolution, Pydantic validation, auth dependency,
response serialization). The tenant store is stubbed out via
monkeypatching `bridge.api.get_tenant_row` and
`bridge.api.update_tenant_row` to use an in-process dict so tests
don't touch `examples/tenants/*.json`.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
from fastapi.testclient import TestClient

from bridge.api_models import MemoryConfigPatch
from bridge.main import app
from bridge.slack_oauth import make_session_token
from bridge.tenant_write import TenantConfigConflictError


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

    def fake_update(
        tenant_id: str,
        _region: str,
        full: dict[str, Any],
        expected_config: dict[str, Any] | None = None,
    ) -> None:
        if tenant_id not in store:
            raise KeyError(tenant_id)
        if expected_config is not None and store[tenant_id] != expected_config:
            raise TenantConfigConflictError(tenant_id)
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


def test_get_tenant_malformed_auth_header_401(
    client: TestClient, stub_store: dict
) -> None:
    r = client.get(
        "/api/tenants/slack-alpha",
        headers={"Authorization": "NotBearer xyz"},
    )
    assert r.status_code == 401


def test_get_tenant_wrong_tenant_in_token_403(
    client: TestClient, stub_store: dict
) -> None:
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
    assert body["memory"]["shared_across_channels"] is False
    assert body["admin_user_ids"] == []


def test_get_tenant_redacts_operator_trust_configuration(
    client: TestClient, stub_store: dict
) -> None:
    stub_store["slack-alpha"]["admin_user_ids"] = ["U_ADMIN_SENTINEL"]
    stub_store["slack-alpha"]["is_internal_testenv"] = True
    stub_store["slack-alpha"]["memory"]["namespace"] = (
        "tenants/private-namespace-sentinel"
    )
    stub_store["slack-alpha"]["byo"]["gateway_endpoint"] = (
        "https://private-gateway-sentinel.example/mcp"
    )
    stub_store["slack-alpha"]["byo"]["gateway_auth"] = {
        "headers": {"Authorization": "raw-secret-sentinel"}
    }
    stub_store["slack-alpha"]["codebases"] = {
        "github_installation_id": "123456789-sentinel"
    }
    token = make_session_token("slack-alpha")

    response = client.get("/api/tenants/slack-alpha", headers=_auth(token))

    assert response.status_code == 200
    body = response.json()
    assert body["admin_user_ids"] == []
    assert body["is_internal_testenv"] is False
    assert body["memory"]["namespace"] == ""
    assert body["byo"]["gateway_endpoint"] is None
    assert body["byo"]["gateway_auth"] is None
    assert body["codebases"]["github_installation_id"] is None
    assert "raw-secret-sentinel" not in response.text
    assert "private-gateway-sentinel" not in response.text
    assert "private-namespace-sentinel" not in response.text
    assert "U_ADMIN_SENTINEL" not in response.text
    assert "123456789-sentinel" not in response.text


def test_get_tenant_not_found_404(client: TestClient, stub_store: dict) -> None:
    token = make_session_token("slack-gamma")
    r = client.get("/api/tenants/slack-gamma", headers=_auth(token))
    assert r.status_code == 404


# ----------------------------------------------------------------------------
# PATCH
# ----------------------------------------------------------------------------


def test_patch_system_prompt_merges(client: TestClient, stub_store: dict) -> None:
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
    assert stub_store["slack-alpha"]["catalog"]["tool_config"] == {
        "echo": {"prefix": "[alpha]"}
    }


def test_patch_response_redacts_but_preserves_operator_fields(
    client: TestClient,
    stub_store: dict,
) -> None:
    row = stub_store["slack-alpha"]
    row["admin_user_ids"] = ["U_ADMIN_SENTINEL"]
    row["memory"]["namespace"] = "tenants/private-sentinel"
    row["byo"]["gateway_endpoint"] = "https://gateway-sentinel.example/mcp"
    row["byo"]["gateway_auth"] = {"headers": {"X-Secret": "secret-sentinel"}}
    row["codebases"] = {"github_installation_id": "12345"}
    token = make_session_token("slack-alpha")

    response = client.patch(
        "/api/tenants/slack-alpha",
        headers=_auth(token),
        json={"system_prompt": "Updated safely."},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["admin_user_ids"] == []
    assert body["memory"]["namespace"] == ""
    assert body["byo"]["gateway_endpoint"] is None
    assert body["byo"]["gateway_auth"] is None
    assert body["codebases"]["github_installation_id"] is None
    assert stub_store["slack-alpha"]["admin_user_ids"] == ["U_ADMIN_SENTINEL"]
    assert stub_store["slack-alpha"]["memory"]["namespace"] == (
        "tenants/private-sentinel"
    )
    assert stub_store["slack-alpha"]["byo"]["gateway_auth"] == {
        "headers": {"X-Secret": "secret-sentinel"}
    }


def test_patch_retries_without_reverting_concurrent_operator_update(
    client: TestClient,
    stub_store: dict[str, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def conflicting_update(
        tenant_id: str,
        _region: str,
        full: dict[str, Any],
        expected_config: dict[str, Any] | None = None,
    ) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            stub_store[tenant_id]["admin_user_ids"] = ["U_OPERATOR"]
        if expected_config is not None and stub_store[tenant_id] != expected_config:
            raise TenantConfigConflictError(tenant_id)
        stub_store[tenant_id] = full

    monkeypatch.setattr("bridge.api.update_tenant_row", conflicting_update)
    token = make_session_token("slack-alpha")

    response = client.patch(
        "/api/tenants/slack-alpha",
        headers=_auth(token),
        json={"system_prompt": "Updated without clobbering operators."},
    )

    assert response.status_code == 200, response.text
    assert calls == 2
    assert stub_store["slack-alpha"]["admin_user_ids"] == ["U_OPERATOR"]
    assert (
        stub_store["slack-alpha"]["system_prompt"]
        == "Updated without clobbering operators."
    )


def test_patch_returns_conflict_after_bounded_retries(
    client: TestClient,
    stub_store: dict[str, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def always_conflicts(
        _tenant_id: str,
        _region: str,
        _full: dict[str, Any],
        expected_config: dict[str, Any] | None = None,
    ) -> None:
        nonlocal calls
        calls += 1
        assert expected_config is not None
        raise TenantConfigConflictError("changed")

    monkeypatch.setattr("bridge.api.update_tenant_row", always_conflicts)
    token = make_session_token("slack-alpha")

    response = client.patch(
        "/api/tenants/slack-alpha",
        headers=_auth(token),
        json={"system_prompt": "Never persisted."},
    )

    assert response.status_code == 409
    assert response.json() == {
        "detail": "tenant configuration changed; retry request"
    }
    assert calls == 3
    assert stub_store["slack-alpha"]["system_prompt"] == "You are Alpha's helper."


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
    assert stub_store["slack-alpha"]["catalog"]["tool_config"] == {
        "echo": {"prefix": "[alpha]"}
    }


def test_patch_empty_system_prompt_422(client: TestClient, stub_store: dict) -> None:
    """Empty system_prompt is blocked by Pydantic min_length=1 before it
    can hit Bedrock."""
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


@pytest.mark.parametrize(
    "trigger",
    [
        r"(a+)+$",
        "a" * 513,
    ],
)
def test_patch_rejects_unsafe_or_oversized_skill_trigger(
    client: TestClient,
    stub_store: dict,
    trigger: str,
) -> None:
    token = make_session_token("slack-alpha")
    before = copy.deepcopy(stub_store["slack-alpha"])

    response = client.patch(
        "/api/tenants/slack-alpha",
        headers=_auth(token),
        json={
            "skills": [
                {
                    "trigger": trigger,
                    "name": "unsafe",
                    "prompt_template": "Do a thing",
                }
            ]
        },
    )

    assert response.status_code == 422
    assert stub_store["slack-alpha"] == before


def test_patch_explicit_null_returns_422_without_writing(
    client: TestClient, stub_store: dict
) -> None:
    token = make_session_token("slack-alpha")
    before = copy.deepcopy(stub_store["slack-alpha"])

    response = client.patch(
        "/api/tenants/slack-alpha",
        headers=_auth(token),
        json={"memory": {"shared_across_channels": None}},
    )

    assert response.status_code == 422
    assert response.json() == {"detail": "invalid tenant configuration"}
    assert stub_store["slack-alpha"] == before


def test_memory_patch_schema_does_not_advertise_explicit_null() -> None:
    field_schema = MemoryConfigPatch.model_json_schema()["properties"][
        "shared_across_channels"
    ]

    assert field_schema["type"] == "boolean"
    assert "anyOf" not in field_schema


def test_patch_preserves_intentionally_clearable_fields(
    client: TestClient, stub_store: dict
) -> None:
    stub_store["slack-alpha"]["codebases"] = {
        "enabled": True,
        "default_repo": "acme/widget",
        "bindings": [],
        "allow_learning": True,
    }
    token = make_session_token("slack-alpha")

    response = client.patch(
        "/api/tenants/slack-alpha",
        headers=_auth(token),
        json={"codebases": {"default_repo": None}},
    )

    assert response.status_code == 200, response.text
    assert response.json()["codebases"]["default_repo"] is None
    assert stub_store["slack-alpha"]["codebases"]["default_repo"] is None


def test_patch_merged_validation_error_is_sanitized_and_does_not_write(
    client: TestClient, stub_store: dict
) -> None:
    secret = "stored-secret-that-must-not-be-reflected"
    stub_store["slack-alpha"]["catalog"]["allowed_tools"] = [
        {"api_token": secret}
    ]
    before = copy.deepcopy(stub_store["slack-alpha"])
    token = make_session_token("slack-alpha")

    response = client.patch(
        "/api/tenants/slack-alpha",
        headers=_auth(token),
        json={"system_prompt": "Still valid."},
    )

    assert response.status_code == 422
    assert response.json() == {"detail": "invalid tenant configuration"}
    assert secret not in response.text
    assert stub_store["slack-alpha"] == before


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


def test_patch_unknown_top_level_field_422(
    client: TestClient, stub_store: dict
) -> None:
    """TenantConfigPatch has extra='forbid' — unknown keys fail fast."""
    token = make_session_token("slack-alpha")
    r = client.patch(
        "/api/tenants/slack-alpha",
        headers=_auth(token),
        json={"nonexistent_field": "whatever"},
    )
    assert r.status_code == 422


def test_patch_cannot_claim_github_app_installation(
    client: TestClient, stub_store: dict
) -> None:
    """Installation-to-tenant binding is operator-only, never self-service."""
    token = make_session_token("slack-alpha")
    r = client.patch(
        "/api/tenants/slack-alpha",
        headers=_auth(token),
        json={"codebases": {"github_installation_id": "99999"}},
    )
    assert r.status_code == 422


def test_patch_cannot_grant_runtime_admin(client: TestClient, stub_store: dict) -> None:
    """Runtime admin IDs are operator-managed, never tenant-session writable."""
    token = make_session_token("slack-alpha")
    r = client.patch(
        "/api/tenants/slack-alpha",
        headers=_auth(token),
        json={"admin_user_ids": ["U_ATTACKER"]},
    )
    assert r.status_code == 422
    assert "admin_user_ids" not in stub_store["slack-alpha"]


@pytest.mark.parametrize(
    "patch",
    [
        {"memory": {"namespace": "tenants/slack-beta"}},
        {"cost_cap": {"enabled": False}},
        {"cost_cap": {"monthly_limit_dollars": 999999}},
        {"is_internal_testenv": True},
    ],
)
def test_patch_cannot_mutate_platform_enforcement_fields(
    client: TestClient,
    stub_store: dict,
    patch: dict[str, Any],
) -> None:
    token = make_session_token("slack-alpha")
    before = copy.deepcopy(stub_store["slack-alpha"])

    response = client.patch(
        "/api/tenants/slack-alpha",
        headers=_auth(token),
        json=patch,
    )

    assert response.status_code == 422
    assert stub_store["slack-alpha"] == before


@pytest.mark.parametrize(
    "byo_patch",
    [
        {"gateway_endpoint": "https://public.example/mcp"},
        {"gateway_endpoint": "http://127.0.0.1:8000/mcp"},
        {"gateway_endpoint": "http://169.254.169.254/latest/meta-data"},
        {"gateway_auth": {"headers": {"Authorization": "Bearer attacker"}}},
        {"enabled": True},
        {"connected_integrations": ["attacker"]},
    ],
)
def test_patch_cannot_mutate_operator_byo_config(
    client: TestClient,
    stub_store: dict,
    byo_patch: dict[str, Any],
) -> None:
    token = make_session_token("slack-alpha")
    before = dict(stub_store["slack-alpha"]["byo"])

    response = client.patch(
        "/api/tenants/slack-alpha",
        headers=_auth(token),
        json={"byo": byo_patch},
    )

    assert response.status_code == 422
    assert stub_store["slack-alpha"]["byo"] == before
