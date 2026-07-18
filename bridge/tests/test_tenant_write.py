from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from botocore.exceptions import ClientError

from bridge.tenant_write import TenantConfigConflictError, update_tenant_row


def test_update_tenant_row_compares_the_config_that_was_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class Table:
        def update_item(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    monkeypatch.delenv("LOCAL_DEV", raising=False)
    monkeypatch.setattr("bridge.tenant_write._get_table", lambda *_args: Table())

    update_tenant_row(
        "tenant-a",
        "us-west-2",
        {"tenant_id": "tenant-a", "ratio": 1.5},
        expected_config={"tenant_id": "tenant-a", "ratio": 1.0},
    )

    assert captured["ConditionExpression"] == (
        "attribute_exists(tenant_id) AND #config = :expected_config"
    )
    values = captured["ExpressionAttributeValues"]
    assert values[":config"]["ratio"] == Decimal("1.5")
    assert values[":expected_config"]["ratio"] == Decimal("1.0")


def test_update_tenant_row_distinguishes_conflict_from_deletion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Table:
        def update_item(self, **_kwargs: Any) -> None:
            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException"}},
                "UpdateItem",
            )

        def get_item(self, **kwargs: Any) -> dict[str, Any]:
            assert kwargs["ConsistentRead"] is True
            return {"Item": {"tenant_id": "tenant-a", "config": {"version": 2}}}

    monkeypatch.delenv("LOCAL_DEV", raising=False)
    monkeypatch.setattr("bridge.tenant_write._get_table", lambda *_args: Table())

    with pytest.raises(TenantConfigConflictError):
        update_tenant_row(
            "tenant-a",
            "us-west-2",
            {"version": 3},
            expected_config={"version": 1},
        )


def test_update_tenant_row_reports_deleted_row_after_failed_condition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Table:
        def update_item(self, **_kwargs: Any) -> None:
            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException"}},
                "UpdateItem",
            )

        def get_item(self, **_kwargs: Any) -> dict[str, Any]:
            return {}

    monkeypatch.delenv("LOCAL_DEV", raising=False)
    monkeypatch.setattr("bridge.tenant_write._get_table", lambda *_args: Table())

    with pytest.raises(KeyError):
        update_tenant_row(
            "tenant-a",
            "us-west-2",
            {"version": 3},
            expected_config={"version": 1},
        )
