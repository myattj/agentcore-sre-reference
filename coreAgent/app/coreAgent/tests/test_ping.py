"""Heartbeat accounting honors per-tenant busy thresholds."""
from __future__ import annotations

import pytest
from bedrock_agentcore.runtime.models import PingStatus

import ping


@pytest.fixture(autouse=True)
def _reset_tasks() -> None:
    ping.reset_tasks_for_tests()
    yield
    ping.reset_tasks_for_tests()


def test_ping_is_healthy_without_background_work() -> None:
    assert ping.custom_ping() == PingStatus.HEALTHY


def test_ping_uses_threshold_captured_for_each_task() -> None:
    ping.register_task("one", busy_threshold=2)
    assert ping.custom_ping() == PingStatus.HEALTHY

    ping.register_task("two", busy_threshold=2)
    assert ping.custom_ping() == PingStatus.HEALTHY_BUSY

    ping.complete_task("one")
    assert ping.custom_ping() == PingStatus.HEALTHY


def test_threshold_one_reports_busy_and_completion_is_idempotent() -> None:
    ping.register_task("one", busy_threshold=0)
    assert ping.custom_ping() == PingStatus.HEALTHY_BUSY

    ping.complete_task("one")
    ping.complete_task("one")
    assert ping.custom_ping() == PingStatus.HEALTHY
