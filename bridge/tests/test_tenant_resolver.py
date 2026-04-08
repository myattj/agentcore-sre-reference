"""Tests for `bridge.tenant_resolver`.

Covers both the LOCAL_DEV (JSON file) path and the production (Dynamo) path,
plus the LRU cache and the `KeyError` semantics on unknown workspaces.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from bridge import tenant_resolver
from bridge.tenant_resolver import (
    DynamoWorkspaceResolver,
    JsonFileWorkspaceResolver,
    resolve_tenant_id,
)


# ---------------------------------------------------------------------------
# JSON file resolver (LOCAL_DEV path)
# ---------------------------------------------------------------------------

def test_json_resolver_known_workspace_returns_tenant_id():
    # The repo ships with examples/workspace_to_tenant.json containing
    # demo-ws → demo. We rely on the real file rather than mocking the
    # filesystem, since `JsonFileWorkspaceResolver` walks parents to
    # find it and that walk is part of what we're testing.
    resolver = JsonFileWorkspaceResolver()
    assert resolver.resolve("demo-ws") == "demo"


def test_json_resolver_unknown_workspace_falls_back_to_demo():
    # The LOCAL_DEV resolver returns "demo" for any unknown workspace,
    # so curl /debug/message works without manual config edits.
    resolver = JsonFileWorkspaceResolver()
    assert resolver.resolve("never-heard-of-this-ws") == "demo"


def test_module_resolve_tenant_id_uses_json_in_local_dev(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LOCAL_DEV", "1")
    tenant_resolver.reset_resolver_for_tests()
    assert resolve_tenant_id("demo-ws") == "demo"


# ---------------------------------------------------------------------------
# Dynamo resolver (production path)
# ---------------------------------------------------------------------------

def _make_fake_table(items: dict[str, dict]) -> MagicMock:
    """Build a fake boto3 Table that responds to get_item with the
    provided dict (keyed by workspace_id)."""
    table = MagicMock()

    def fake_get_item(Key: dict) -> dict:
        wid = Key.get("workspace_id")
        item = items.get(wid)
        return {"Item": item} if item else {}

    table.get_item.side_effect = fake_get_item
    return table


def test_dynamo_resolver_known_workspace():
    fake_table = _make_fake_table({"T_ACME": {"workspace_id": "T_ACME", "tenant_id": "acme"}})
    fake_resource = MagicMock()
    fake_resource.Table.return_value = fake_table

    with patch("boto3.resource", return_value=fake_resource):
        resolver = DynamoWorkspaceResolver(table_name="workspace_to_tenant")
        assert resolver.resolve("T_ACME") == "acme"


def test_dynamo_resolver_unknown_workspace_raises_key_error():
    fake_table = _make_fake_table({})
    fake_resource = MagicMock()
    fake_resource.Table.return_value = fake_table

    with patch("boto3.resource", return_value=fake_resource):
        resolver = DynamoWorkspaceResolver(table_name="workspace_to_tenant")
        with pytest.raises(KeyError):
            resolver.resolve("T_NOPE")


def test_dynamo_resolver_caches_lookups():
    """Second resolve() for the same workspace should not call get_item again."""
    fake_table = _make_fake_table({"T_ACME": {"workspace_id": "T_ACME", "tenant_id": "acme"}})
    fake_resource = MagicMock()
    fake_resource.Table.return_value = fake_table

    with patch("boto3.resource", return_value=fake_resource):
        resolver = DynamoWorkspaceResolver(table_name="workspace_to_tenant")
        resolver.resolve("T_ACME")
        resolver.resolve("T_ACME")
        resolver.resolve("T_ACME")

    assert fake_table.get_item.call_count == 1


def test_dynamo_resolver_cache_clear_forces_refetch():
    fake_table = _make_fake_table({"T_ACME": {"workspace_id": "T_ACME", "tenant_id": "acme"}})
    fake_resource = MagicMock()
    fake_resource.Table.return_value = fake_table

    with patch("boto3.resource", return_value=fake_resource):
        resolver = DynamoWorkspaceResolver(table_name="workspace_to_tenant")
        resolver.resolve("T_ACME")
        resolver.cache_clear()
        resolver.resolve("T_ACME")

    assert fake_table.get_item.call_count == 2


def test_dynamo_resolver_row_without_tenant_id_raises():
    fake_table = _make_fake_table({"T_BROKEN": {"workspace_id": "T_BROKEN"}})
    fake_resource = MagicMock()
    fake_resource.Table.return_value = fake_table

    with patch("boto3.resource", return_value=fake_resource):
        resolver = DynamoWorkspaceResolver(table_name="workspace_to_tenant")
        with pytest.raises(KeyError):
            resolver.resolve("T_BROKEN")
