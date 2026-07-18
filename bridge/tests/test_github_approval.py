"""Security tests for operator-approved GitHub App installation bindings."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient

from bridge.github_app import (
    InstallationMetadata,
    _exchange_jwt_for_installation_token,
    get_installation_metadata,
)
from bridge.main import app
from bridge.tenant_write import (
    GitHubInstallationBindingConflict,
    approve_github_installation_binding,
    build_default_config_dict,
    find_tenant_by_github_installation,
)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("ADMIN_SECRET", "operator-secret")
    return TestClient(app)


def _operator_headers(token: str = "operator-secret") -> dict[str, str]:
    return {"X-Admin-Token": token}


def test_malformed_token_response_never_reflects_installation_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    leaked_token = "ghs_test_secret_that_must_not_escape"

    class Response:
        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps({"token": leaked_token}).encode()

    monkeypatch.setattr(
        "bridge.github_app.urllib.request.urlopen",
        lambda *_args, **_kwargs: Response(),
    )

    with pytest.raises(RuntimeError) as error:
        _exchange_jwt_for_installation_token("app-jwt", "12345")

    assert leaked_token not in str(error.value)


def test_approval_requires_operator_secret(client: TestClient) -> None:
    body = {"installation_id": 12345, "expected_account_login": "acme"}
    assert (
        client.post(
            "/api/ops/tenants/acme/codebases/github/approve",
            json=body,
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/api/ops/tenants/acme/codebases/github/approve",
            json=body,
            headers=_operator_headers("wrong-secret"),
        ).status_code
        == 401
    )


@pytest.mark.parametrize(
    "installation_id",
    [0, -1, "not-a-number", 2**63, 12345.0, True],
)
def test_approval_rejects_non_positive_or_non_64_bit_ids(
    client: TestClient,
    installation_id: Any,
) -> None:
    response = client.post(
        "/api/ops/tenants/acme/codebases/github/approve",
        headers=_operator_headers(),
        json={
            "installation_id": installation_id,
            "expected_account_login": "acme",
        },
    )
    assert response.status_code == 422


def test_approval_verifies_account_before_binding(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        "bridge.github_approval.get_installation_metadata",
        lambda installation_id: InstallationMetadata(
            installation_id=str(installation_id),
            account_login="Acme-Org",
        ),
    )
    monkeypatch.setattr(
        "bridge.github_approval.approve_github_installation_binding",
        lambda tenant_id, installation_id, region: calls.append(
            (tenant_id, installation_id, region)
        ),
    )

    response = client.post(
        "/api/ops/tenants/acme/codebases/github/approve",
        headers=_operator_headers(),
        json={
            "installation_id": "0012345",
            "expected_account_login": "acme-org",
        },
    )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "approved": True,
        "tenant_id": "acme",
        "installation_id": "12345",
        "account_login": "Acme-Org",
    }
    assert calls == [("acme", "12345", "us-west-2")]


def test_approval_rejects_account_mismatch_without_binding(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bound: list[str] = []
    monkeypatch.setattr(
        "bridge.github_approval.get_installation_metadata",
        lambda _installation_id: InstallationMetadata("12345", "other-org"),
    )
    monkeypatch.setattr(
        "bridge.github_approval.approve_github_installation_binding",
        lambda *_args: bound.append("called"),
    )

    response = client.post(
        "/api/ops/tenants/acme/codebases/github/approve",
        headers=_operator_headers(),
        json={"installation_id": 12345, "expected_account_login": "acme"},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "GitHub installation approval conflict"
    assert bound == []


def test_approval_maps_duplicate_binding_to_conflict(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "bridge.github_approval.get_installation_metadata",
        lambda _installation_id: InstallationMetadata("12345", "acme"),
    )

    def conflict(*_args: Any) -> None:
        raise GitHubInstallationBindingConflict("already bound to another tenant")

    monkeypatch.setattr(
        "bridge.github_approval.approve_github_installation_binding",
        conflict,
    )
    response = client.post(
        "/api/ops/tenants/acme/codebases/github/approve",
        headers=_operator_headers(),
        json={"installation_id": 12345, "expected_account_login": "acme"},
    )
    assert response.status_code == 409
    assert "another tenant" not in response.text


def test_approval_maps_github_lookup_failure_to_safe_502(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "bridge.github_approval.get_installation_metadata",
        lambda _installation_id: (_ for _ in ()).throw(
            RuntimeError("private upstream sentinel")
        ),
    )
    response = client.post(
        "/api/ops/tenants/acme/codebases/github/approve",
        headers=_operator_headers(),
        json={"installation_id": 12345, "expected_account_login": "acme"},
    )
    assert response.status_code == 502
    assert "private upstream sentinel" not in response.text


def test_get_installation_metadata_uses_app_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class Response:
        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps({"id": 12345, "account": {"login": "acme-org"}}).encode()

    def fake_open(request: Any, timeout: int) -> Response:
        captured["url"] = request.full_url
        captured["authorization"] = request.headers["Authorization"]
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr("bridge.github_app._mint_app_jwt", lambda: "app-jwt")
    monkeypatch.setattr("bridge.github_app.urllib.request.urlopen", fake_open)

    metadata = get_installation_metadata("0012345")

    assert metadata == InstallationMetadata("12345", "acme-org")
    assert captured == {
        "url": "https://api.github.com/app/installations/12345",
        "authorization": "Bearer app-jwt",
        "timeout": 15,
    }


def test_local_binding_is_idempotent_and_exclusive(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "bridge.tenant_write._find_local_tenants_dir",
        lambda: tmp_path,
    )
    for tenant_id in ("acme", "globex"):
        (tmp_path / f"{tenant_id}.json").write_text(
            json.dumps(build_default_config_dict(tenant_id))
        )

    approve_github_installation_binding("acme", "12345", "local")
    approve_github_installation_binding("acme", "12345", "local")

    assert find_tenant_by_github_installation("12345", "local") == "acme"
    with pytest.raises(GitHubInstallationBindingConflict):
        approve_github_installation_binding("globex", "12345", "local")
    globex = json.loads((tmp_path / "globex.json").read_text())
    assert globex["codebases"]["github_installation_id"] is None


def test_production_binding_uses_exclusive_atomic_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LOCAL_DEV", raising=False)
    transaction_client = MagicMock()
    tenant = build_default_config_dict("acme")

    class FakeTable:
        name = "tenants"
        meta = SimpleNamespace(client=transaction_client)

        def scan(self, **_kwargs: Any) -> dict[str, Any]:
            return {"Items": []}

        def get_item(self, *, Key: dict[str, str], **_kwargs: Any) -> dict[str, Any]:
            if Key["tenant_id"] == "acme":
                return {"Item": {"tenant_id": "acme", "config": tenant}}
            return {}

    table = FakeTable()
    monkeypatch.setattr(
        "bridge.tenant_write._get_table",
        lambda _region, _table_name: table,
    )

    approve_github_installation_binding("acme", "12345", "us-west-2")

    transaction_client.transact_write_items.assert_called_once()
    items = transaction_client.transact_write_items.call_args.kwargs[
        "TransactItems"
    ]
    assert len(items) == 2
    lock_update = items[0]["Update"]
    tenant_update = items[1]["Update"]
    assert lock_update["Key"]["tenant_id"] == {
        "S": "__github_installation__#12345"
    }
    assert "bound_tenant_id = :tenant" in lock_update["ConditionExpression"]
    assert "attribute_type" in tenant_update["ConditionExpression"]
    assert "= :installation" in tenant_update["ConditionExpression"]
    stored_id = tenant_update["ExpressionAttributeValues"][":config"]["M"][
        "codebases"
    ]["M"]["github_installation_id"]
    assert stored_id == {"S": "12345"}


def test_production_binding_lookup_uses_lock_without_table_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LOCAL_DEV", raising=False)

    class FakeTable:
        def get_item(self, **_kwargs: Any) -> dict[str, Any]:
            return {
                "Item": {
                    "tenant_id": "__github_installation__#12345",
                    "bound_tenant_id": "acme",
                }
            }

        def scan(self, **_kwargs: Any) -> dict[str, Any]:
            raise AssertionError("an existing lock must avoid a tenants-table scan")

    monkeypatch.setattr(
        "bridge.tenant_write._get_table",
        lambda _region, _table_name: FakeTable(),
    )

    assert find_tenant_by_github_installation("12345", "us-west-2") == "acme"


def _transaction_error(code: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": "private database detail"}},
        "TransactWriteItems",
    )


@pytest.mark.parametrize("lock_owner", ["other-private-tenant", None, "acme"])
def test_production_binding_classifies_transaction_cancellation_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    lock_owner: str | None,
) -> None:
    monkeypatch.delenv("LOCAL_DEV", raising=False)
    tenant = build_default_config_dict("acme")
    transaction_attempted = False

    def cancel_transaction(**_kwargs: Any) -> None:
        nonlocal transaction_attempted
        transaction_attempted = True
        raise _transaction_error("TransactionCanceledException")

    transaction_client = MagicMock()
    transaction_client.transact_write_items.side_effect = cancel_transaction

    class FakeTable:
        name = "tenants"
        meta = SimpleNamespace(client=transaction_client)

        def scan(self, **_kwargs: Any) -> dict[str, Any]:
            return {"Items": []}

        def get_item(self, *, Key: dict[str, str], **_kwargs: Any) -> dict[str, Any]:
            if Key["tenant_id"] == "acme":
                return {"Item": {"tenant_id": "acme", "config": tenant}}
            if transaction_attempted and lock_owner:
                return {
                    "Item": {
                        "tenant_id": Key["tenant_id"],
                        "bound_tenant_id": lock_owner,
                    }
                }
            return {}

    monkeypatch.setattr(
        "bridge.tenant_write._get_table",
        lambda _region, _table_name: FakeTable(),
    )

    with pytest.raises(GitHubInstallationBindingConflict) as error:
        approve_github_installation_binding("acme", "12345", "us-west-2")

    assert "other-private-tenant" not in str(error.value)
    assert transaction_client.transact_write_items.call_count == 1


def test_production_binding_detects_tenant_deleted_during_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LOCAL_DEV", raising=False)
    tenant = build_default_config_dict("acme")
    transaction_attempted = False

    def cancel_transaction(**_kwargs: Any) -> None:
        nonlocal transaction_attempted
        transaction_attempted = True
        raise _transaction_error("TransactionCanceledException")

    transaction_client = MagicMock()
    transaction_client.transact_write_items.side_effect = cancel_transaction

    class FakeTable:
        name = "tenants"
        meta = SimpleNamespace(client=transaction_client)

        def scan(self, **_kwargs: Any) -> dict[str, Any]:
            return {"Items": []}

        def get_item(self, *, Key: dict[str, str], **_kwargs: Any) -> dict[str, Any]:
            if Key["tenant_id"] == "acme" and not transaction_attempted:
                return {"Item": {"tenant_id": "acme", "config": tenant}}
            return {}

    monkeypatch.setattr(
        "bridge.tenant_write._get_table",
        lambda _region, _table_name: FakeTable(),
    )

    with pytest.raises(KeyError, match="acme"):
        approve_github_installation_binding("acme", "12345", "us-west-2")


def test_production_binding_reraises_non_cancellation_database_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LOCAL_DEV", raising=False)
    transaction_client = MagicMock()
    transaction_client.transact_write_items.side_effect = _transaction_error(
        "AccessDeniedException"
    )
    tenant = build_default_config_dict("acme")

    class FakeTable:
        name = "tenants"
        meta = SimpleNamespace(client=transaction_client)

        def scan(self, **_kwargs: Any) -> dict[str, Any]:
            return {"Items": []}

        def get_item(self, *, Key: dict[str, str], **_kwargs: Any) -> dict[str, Any]:
            if Key["tenant_id"] == "acme":
                return {"Item": {"tenant_id": "acme", "config": tenant}}
            return {}

    monkeypatch.setattr(
        "bridge.tenant_write._get_table",
        lambda _region, _table_name: FakeTable(),
    )

    with pytest.raises(ClientError) as error:
        approve_github_installation_binding("acme", "12345", "us-west-2")

    assert error.value.response["Error"]["Code"] == "AccessDeniedException"
