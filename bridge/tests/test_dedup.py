"""Tests for `bridge.dedup.InMemoryDedup` and the `is_duplicate` helper.

DynamoDedup is exercised with mocked boto3 elsewhere; the in-memory impl
is the one we actually run in tests + LOCAL_DEV.
"""
from __future__ import annotations

import time

import pytest

from bridge import dedup
from bridge.dedup import InMemoryDedup, is_duplicate


def test_first_call_returns_false_and_marks_seen():
    d = InMemoryDedup()
    assert d.seen("evt-1") is False
    assert d.seen("evt-1") is True


def test_distinct_event_ids_are_independent():
    d = InMemoryDedup()
    assert d.seen("evt-a") is False
    assert d.seen("evt-b") is False
    assert d.seen("evt-a") is True
    assert d.seen("evt-b") is True


def test_ttl_evicts_expired_entries(monkeypatch: pytest.MonkeyPatch):
    """An event_id should be re-marked as fresh after the TTL expires."""
    d = InMemoryDedup(ttl_seconds=1)
    fake_time = [1_000.0]

    def fake_time_fn() -> float:
        return fake_time[0]

    monkeypatch.setattr(time, "time", fake_time_fn)

    assert d.seen("evt-1") is False
    assert d.seen("evt-1") is True

    # Jump 2 seconds into the future — past the 1s TTL.
    fake_time[0] = 1_002.0

    assert d.seen("evt-1") is False  # treated as fresh again


def test_module_level_is_duplicate_uses_in_memory_under_local_dev():
    # conftest sets LOCAL_DEV=1; the singleton should be InMemoryDedup.
    dedup.reset_dedup_for_tests()
    assert is_duplicate("only-once") is False
    assert is_duplicate("only-once") is True


def test_empty_event_id_is_never_duplicate():
    """The bridge passes an empty string when Slack omits event_id;
    we treat that as 'not a duplicate' rather than swallowing the request."""
    dedup.reset_dedup_for_tests()
    assert is_duplicate("") is False
    assert is_duplicate("") is False
    assert is_duplicate(None) is False  # type: ignore[arg-type]
