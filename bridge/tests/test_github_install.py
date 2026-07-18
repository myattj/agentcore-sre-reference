"""Tests for the GitHub App install-time warm-start.

Covers:
  - ``rank_repos`` / ``repos_to_bindings`` pure-function edge cases
  - ``run_install_warm_start`` orchestrator happy path + error paths
    (tenant-not-found, token-mint-failed, list-repos-failed)
  - ``POST /api/tenants/{id}/codebases/github/install`` endpoint via
    TestClient with monkeypatched HTTP + tenant store

No network — ``list_installation_repos`` and ``get_installation_token``
are monkeypatched. The tenant store uses the same in-memory dict
pattern as ``test_api_tenants.py``.
"""
from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from bridge.github_install import (
    rank_repos,
    repos_to_bindings,
    run_install_warm_start,
)
from bridge.main import app
from bridge.slack_oauth import make_session_token
from bridge.tenant_write import build_default_config_dict


# ----------------------------------------------------------------------------
# Pure function tests
# ----------------------------------------------------------------------------

def test_rank_repos_empty_list() -> None:
    assert rank_repos([]) == []


def test_rank_repos_pushed_at_primary_key() -> None:
    """Most recent push wins regardless of stars."""
    repos = [
        {"full_name": "a/old-popular", "pushed_at": "2024-01-01T00:00:00Z", "stargazers_count": 5000},
        {"full_name": "a/new-small", "pushed_at": "2026-04-11T00:00:00Z", "stargazers_count": 3},
    ]
    ranked = rank_repos(repos)
    assert ranked[0]["full_name"] == "a/new-small"
    assert ranked[1]["full_name"] == "a/old-popular"


def test_rank_repos_stars_tiebreaker() -> None:
    """When pushed_at ties, stars decide."""
    repos = [
        {"full_name": "a/low-stars", "pushed_at": "2026-04-10T12:00:00Z", "stargazers_count": 1},
        {"full_name": "a/high-stars", "pushed_at": "2026-04-10T12:00:00Z", "stargazers_count": 500},
    ]
    ranked = rank_repos(repos)
    assert ranked[0]["full_name"] == "a/high-stars"


def test_rank_repos_missing_pushed_at_sorts_last() -> None:
    """A repo with no push timestamp must not beat one that does, even with huge stars."""
    repos = [
        {"full_name": "a/no-ts", "pushed_at": None, "stargazers_count": 99999},
        {"full_name": "a/real", "pushed_at": "2024-01-01T00:00:00Z", "stargazers_count": 0},
    ]
    ranked = rank_repos(repos)
    assert ranked[0]["full_name"] == "a/real"
    assert ranked[1]["full_name"] == "a/no-ts"


def test_repos_to_bindings_drops_missing_full_name() -> None:
    """Malformed rows without full_name should be filtered out."""
    ranked = [
        {"pushed_at": "2026-04-10T00:00:00Z"},  # no full_name
        {"full_name": "a/good", "default_branch": "main", "pushed_at": "2026-04-09T00:00:00Z"},
    ]
    bindings = repos_to_bindings(ranked, limit=5)
    assert len(bindings) == 1
    assert bindings[0]["repo"] == "a/good"


def test_repos_to_bindings_respects_limit() -> None:
    ranked = [
        {"full_name": f"a/repo{i}", "default_branch": "main"}
        for i in range(10)
    ]
    assert len(repos_to_bindings(ranked, limit=3)) == 3


def test_repos_to_bindings_default_branch_fallback() -> None:
    ranked = [{"full_name": "a/no-branch"}]
    bindings = repos_to_bindings(ranked)
    assert bindings[0]["default_branch"] == "main"


# ----------------------------------------------------------------------------
# Orchestrator tests (unit)
# ----------------------------------------------------------------------------

@pytest.fixture
def stub_tenant_store(monkeypatch: pytest.MonkeyPatch) -> dict[str, dict[str, Any]]:
    """In-memory tenant store, monkeypatched into github_install's import site.

    Because github_install.py imports via ``from .tenant_write import ...``,
    the names are captured at import time — we patch them on the
    ``bridge.github_install`` module, not on ``bridge.tenant_write``.
    """
    store: dict[str, dict[str, Any]] = {
        "acme": build_default_config_dict("acme"),
    }
    # GitHub installation IDs are operator-approved trust bindings, not
    # tenant-editable settings. Most tests exercise the approved happy path.
    store["acme"]["codebases"]["github_installation_id"] = "12345"

    def fake_get(tenant_id: str, _region: str) -> dict[str, Any]:
        if tenant_id not in store:
            raise KeyError(tenant_id)
        import copy
        return copy.deepcopy(store[tenant_id])

    def fake_update(tenant_id: str, _region: str, full: dict[str, Any]) -> None:
        if tenant_id not in store:
            raise KeyError(tenant_id)
        store[tenant_id] = full

    monkeypatch.setattr("bridge.github_install.get_tenant_row", fake_get)
    monkeypatch.setattr("bridge.github_install.update_tenant_row", fake_update)
    monkeypatch.setattr(
        "bridge.github_install.find_tenant_by_github_installation",
        lambda _installation_id, _region: "acme",
    )
    return store


def _stub_repos() -> list[dict[str, Any]]:
    return [
        {
            "full_name": "acme/platform",
            "default_branch": "main",
            "pushed_at": "2026-04-10T00:00:00Z",
            "stargazers_count": 42,
        },
        {
            "full_name": "acme/billing",
            "default_branch": "main",
            "pushed_at": "2026-04-09T00:00:00Z",
            "stargazers_count": 10,
        },
        {
            "full_name": "acme/legacy",
            "default_branch": "master",
            "pushed_at": "2023-01-01T00:00:00Z",
            "stargazers_count": 100,
        },
    ]


def test_run_install_warm_start_happy_path(
    monkeypatch: pytest.MonkeyPatch,
    stub_tenant_store: dict[str, dict[str, Any]],
) -> None:
    monkeypatch.setattr(
        "bridge.github_install.get_installation_token",
        lambda _id: "ghs_fake",
    )
    monkeypatch.setattr(
        "bridge.github_install.list_installation_repos",
        lambda _token: _stub_repos(),
    )

    result = run_install_warm_start("acme", "12345", "us-west-2")

    assert result.ok is True
    assert result.default_repo == "acme/platform"
    assert result.total_repos_available == 3
    assert [b["repo"] for b in result.bindings] == ["acme/platform", "acme/billing", "acme/legacy"]

    # The tenant row should have the codebases block updated.
    updated = stub_tenant_store["acme"]
    assert updated["codebases"]["enabled"] is True
    assert updated["codebases"]["github_installation_id"] == "12345"
    assert updated["codebases"]["default_repo"] == "acme/platform"
    assert len(updated["codebases"]["bindings"]) == 3


def test_run_install_warm_start_preserves_other_config(
    monkeypatch: pytest.MonkeyPatch,
    stub_tenant_store: dict[str, dict[str, Any]],
) -> None:
    """Deep-merge must not clobber unrelated fields."""
    # Set a distinctive value elsewhere in the config
    stub_tenant_store["acme"]["system_prompt"] = "custom prompt for acme"
    stub_tenant_store["acme"]["catalog"]["tool_config"] = {"echo": {"prefix": "[acme]"}}

    monkeypatch.setattr(
        "bridge.github_install.get_installation_token", lambda _id: "ghs_fake"
    )
    monkeypatch.setattr(
        "bridge.github_install.list_installation_repos", lambda _token: _stub_repos()
    )

    run_install_warm_start("acme", "12345", "us-west-2")

    updated = stub_tenant_store["acme"]
    assert updated["system_prompt"] == "custom prompt for acme"
    assert updated["catalog"]["tool_config"] == {"echo": {"prefix": "[acme]"}}
    assert updated["codebases"]["default_repo"] == "acme/platform"


def test_run_install_warm_start_tenant_not_found(
    monkeypatch: pytest.MonkeyPatch,
    stub_tenant_store: dict[str, dict[str, Any]],
) -> None:
    result = run_install_warm_start("ghost", "12345", "us-west-2")
    assert result.ok is False
    assert result.error is not None
    assert "ghost" in result.error


def test_run_install_warm_start_rejects_unapproved_installation_before_mint(
    monkeypatch: pytest.MonkeyPatch,
    stub_tenant_store: dict[str, dict[str, Any]],
) -> None:
    stub_tenant_store["acme"]["codebases"]["github_installation_id"] = None
    minted: list[str] = []
    monkeypatch.setattr(
        "bridge.github_install.get_installation_token",
        lambda installation_id: minted.append(installation_id),
    )

    result = run_install_warm_start("acme", "12345", "us-west-2")

    assert result.ok is False
    assert result.pending_approval is True
    assert "operator-approved" in (result.error or "")
    assert minted == []


def test_run_install_warm_start_rejects_other_tenants_installation_before_mint(
    monkeypatch: pytest.MonkeyPatch,
    stub_tenant_store: dict[str, dict[str, Any]],
) -> None:
    minted: list[str] = []
    monkeypatch.setattr(
        "bridge.github_install.get_installation_token",
        lambda installation_id: minted.append(installation_id),
    )

    result = run_install_warm_start("acme", "99999", "us-west-2")

    assert result.ok is False
    assert result.pending_approval is False
    assert "not approved" in (result.error or "")
    assert minted == []


def test_run_install_warm_start_token_mint_fails(
    monkeypatch: pytest.MonkeyPatch,
    stub_tenant_store: dict[str, dict[str, Any]],
) -> None:
    def raise_mint(_id: str) -> str:
        raise RuntimeError("GITHUB_APP_ID env var is not set")

    monkeypatch.setattr("bridge.github_install.get_installation_token", raise_mint)

    result = run_install_warm_start("acme", "12345", "us-west-2")
    assert result.ok is False
    assert result.error is not None
    assert "GITHUB_APP_ID" in result.error
    # Tenant row must be untouched on failure
    assert stub_tenant_store["acme"]["codebases"]["enabled"] is False


def test_run_install_warm_start_list_repos_fails(
    monkeypatch: pytest.MonkeyPatch,
    stub_tenant_store: dict[str, dict[str, Any]],
) -> None:
    monkeypatch.setattr(
        "bridge.github_install.get_installation_token", lambda _id: "ghs_fake"
    )

    def raise_list(_token: str) -> list[dict[str, Any]]:
        raise RuntimeError("HTTP 500: server error")

    monkeypatch.setattr("bridge.github_install.list_installation_repos", raise_list)

    result = run_install_warm_start("acme", "12345", "us-west-2")
    assert result.ok is False
    assert result.error is not None
    assert "HTTP 500" in result.error
    # Tenant row untouched
    assert stub_tenant_store["acme"]["codebases"]["enabled"] is False


def test_run_install_warm_start_empty_repo_list(
    monkeypatch: pytest.MonkeyPatch,
    stub_tenant_store: dict[str, dict[str, Any]],
) -> None:
    """An installation with zero repos should still succeed — just empty bindings."""
    monkeypatch.setattr(
        "bridge.github_install.get_installation_token", lambda _id: "ghs_fake"
    )
    monkeypatch.setattr(
        "bridge.github_install.list_installation_repos", lambda _token: []
    )

    result = run_install_warm_start("acme", "12345", "us-west-2")
    assert result.ok is True
    assert result.default_repo is None
    assert result.bindings == []
    assert result.total_repos_available == 0
    # Tenant row should still have codebases.enabled=True so the UI
    # knows the install happened, just with no bindings.
    updated = stub_tenant_store["acme"]
    assert updated["codebases"]["enabled"] is True
    assert updated["codebases"]["github_installation_id"] == "12345"


# ----------------------------------------------------------------------------
# Endpoint test — POST /api/tenants/{id}/codebases/github/install
# ----------------------------------------------------------------------------

@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _auth(tenant_id: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_session_token(tenant_id)}"}


def test_install_endpoint_happy_path(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    stub_tenant_store: dict[str, dict[str, Any]],
) -> None:
    monkeypatch.setattr(
        "bridge.github_install.get_installation_token", lambda _id: "ghs_fake"
    )
    monkeypatch.setattr(
        "bridge.github_install.list_installation_repos", lambda _token: _stub_repos()
    )

    response = client.post(
        "/api/tenants/acme/codebases/github/install",
        json={"installation_id": "12345"},
        headers=_auth("acme"),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["installation_id"] == "12345"
    assert body["default_repo"] == "acme/platform"
    assert body["total_repos_available"] == 3
    assert body["pending_approval"] is False
    assert len(body["bindings"]) == 3
    assert body["bindings"][0] == {"repo": "acme/platform", "default_branch": "main"}


def test_install_endpoint_requires_auth(client: TestClient) -> None:
    r = client.post(
        "/api/tenants/acme/codebases/github/install",
        json={"installation_id": "12345"},
    )
    assert r.status_code == 401


def test_install_endpoint_rejects_wrong_tenant(
    client: TestClient,
    stub_tenant_store: dict[str, dict[str, Any]],
) -> None:
    # Session for a different tenant
    r = client.post(
        "/api/tenants/acme/codebases/github/install",
        json={"installation_id": "12345"},
        headers=_auth("somebody-else"),
    )
    assert r.status_code == 403


def test_install_endpoint_rejects_empty_installation_id(
    client: TestClient,
    stub_tenant_store: dict[str, dict[str, Any]],
) -> None:
    r = client.post(
        "/api/tenants/acme/codebases/github/install",
        json={"installation_id": ""},
        headers=_auth("acme"),
    )
    assert r.status_code == 422  # Pydantic min_length=1


@pytest.mark.parametrize(
    "installation_id",
    [0, -1, "abc", 2**63, 12345.0, True],
)
def test_install_endpoint_rejects_invalid_installation_id(
    client: TestClient,
    stub_tenant_store: dict[str, dict[str, Any]],
    installation_id: Any,
) -> None:
    r = client.post(
        "/api/tenants/acme/codebases/github/install",
        json={"installation_id": installation_id},
        headers=_auth("acme"),
    )
    assert r.status_code == 422


def test_install_endpoint_returns_pending_operator_approval(
    client: TestClient,
    stub_tenant_store: dict[str, dict[str, Any]],
) -> None:
    stub_tenant_store["acme"]["codebases"]["github_installation_id"] = None
    r = client.post(
        "/api/tenants/acme/codebases/github/install",
        json={"installation_id": 12345},
        headers=_auth("acme"),
    )
    assert r.status_code == 200
    assert r.json()["ok"] is False
    assert r.json()["pending_approval"] is True


def test_install_endpoint_rejects_extra_fields(
    client: TestClient,
    stub_tenant_store: dict[str, dict[str, Any]],
) -> None:
    r = client.post(
        "/api/tenants/acme/codebases/github/install",
        json={"installation_id": "12345", "extra": "boom"},
        headers=_auth("acme"),
    )
    assert r.status_code == 422  # extra="forbid"


def test_install_endpoint_surfaces_warm_start_errors(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    stub_tenant_store: dict[str, dict[str, Any]],
) -> None:
    """A warm-start failure returns 200 with ok=false + error message,
    NOT a 5xx — the UI needs to render the error payload."""
    def raise_mint(_id: str) -> str:
        raise RuntimeError("private key not found")

    monkeypatch.setattr("bridge.github_install.get_installation_token", raise_mint)

    r = client.post(
        "/api/tenants/acme/codebases/github/install",
        json={"installation_id": "12345"},
        headers=_auth("acme"),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "private key not found" in body["error"]
