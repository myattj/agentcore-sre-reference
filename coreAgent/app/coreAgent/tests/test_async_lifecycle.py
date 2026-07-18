"""Failure-path tests for AgentCore background-task registration."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Callable

import pytest
from bedrock_agentcore.runtime.models import PingStatus

import ping
import tools as catalog_tools
from tenant import TenantConfig


def setup_function() -> None:
    ping.reset_tasks_for_tests()


def teardown_function() -> None:
    ping.reset_tasks_for_tests()


@pytest.mark.asyncio
async def test_context_assembly_runs_off_the_agentcore_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import context_assembler
    import main

    delegated: list[tuple[Callable[..., Any], dict[str, Any]]] = []

    def stopped_assembly(**_kwargs: object) -> None:
        raise RuntimeError("stop after assembly")

    async def recording_to_thread(
        function: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        delegated.append((function, kwargs))
        return function(*args, **kwargs)

    monkeypatch.setattr(
        main,
        "load_tenant_config",
        lambda _tenant: TenantConfig(tenant_id="tenant-a"),
    )
    monkeypatch.setattr(context_assembler, "assemble_context", stopped_assembly)
    monkeypatch.setattr(main.asyncio, "to_thread", recording_to_thread)
    monkeypatch.setattr(main, "_audit", SimpleNamespace(write=lambda _row: None))
    monkeypatch.setattr(
        main,
        "_metrics",
        SimpleNamespace(emit_invocation=lambda **_kwargs: None),
    )
    monkeypatch.setattr(
        main,
        "_spend",
        SimpleNamespace(
            check_budget=lambda *_args: (True, 0),
            record_spend=lambda *_args: None,
        ),
    )

    invocation = main.invoke(
        {"tenant_id": "tenant-a", "prompt": "hello", "ctx": {}},
        None,
    )
    with pytest.raises(RuntimeError, match="stop after assembly"):
        await anext(invocation)

    assert len(delegated) == 1
    assert delegated[0][0] is stopped_assembly
    assert delegated[0][1]["tenant_id"] == "tenant-a"


def test_async_registration_rolls_back_local_and_sdk_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completed: list[str] = []

    def reject(_task_id: str) -> None:
        raise RuntimeError("registration unavailable")

    monkeypatch.setattr(catalog_tools.app, "add_async_task", reject)
    monkeypatch.setattr(
        catalog_tools.app,
        "complete_async_task",
        lambda task_id: completed.append(task_id),
    )

    with pytest.raises(RuntimeError, match="registration unavailable"):
        catalog_tools._register_async_task("task-1", busy_threshold=1)

    assert completed == ["task-1"]
    assert ping.custom_ping() == PingStatus.HEALTHY


@pytest.mark.parametrize(
    ("context", "expected"),
    [
        ({}, 3600),
        ({"heartbeat_max_background_seconds": 45}, 45),
        ({"heartbeat_max_background_seconds": 0}, 3600),
        ({"heartbeat_max_background_seconds": -10}, 1),
        ({"heartbeat_max_background_seconds": "invalid"}, 3600),
    ],
)
def test_background_work_ceiling_is_positive_and_failure_safe(
    context: dict[str, object], expected: int
) -> None:
    assert catalog_tools._heartbeat_max_background_seconds(context) == expected


def test_sandbox_poller_honors_tenant_background_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeTable:
        def __init__(self) -> None:
            self.updates: list[dict[str, object]] = []

        def get_item(self, **_kwargs: object) -> dict[str, object]:
            raise AssertionError("poller must not read after the deadline")

        def update_item(self, **kwargs: object) -> None:
            self.updates.append(kwargs)

    clock = {"now": 0.0}
    sleeps: list[float] = []
    table = FakeTable()
    ping_completed: list[str] = []
    sdk_completed: list[str] = []
    audit_rows: list[dict[str, object]] = []

    monkeypatch.setattr(catalog_tools.time, "monotonic", lambda: clock["now"])

    def advance(seconds: float) -> None:
        sleeps.append(seconds)
        clock["now"] += seconds

    monkeypatch.setattr(catalog_tools.time, "sleep", advance)
    monkeypatch.setattr(catalog_tools, "_sandbox_jobs_table", lambda: table)
    monkeypatch.setattr(
        catalog_tools,
        "_write_propose_pr_audit",
        lambda **kwargs: audit_rows.append(kwargs),
    )
    monkeypatch.setattr(
        catalog_tools.ping,
        "complete_task",
        lambda task_id: ping_completed.append(task_id),
    )
    monkeypatch.setattr(
        catalog_tools.app,
        "complete_async_task",
        lambda task_id: sdk_completed.append(task_id),
    )

    catalog_tools._poll_sandbox_completion(
        "task-low-cap",
        "owner/repo",
        {"tenant_id": "tenant-a"},
        max_background_seconds=2,
    )

    assert sleeps == [2.0]
    assert ping_completed == sdk_completed == ["task-low-cap"]
    values = table.updates[0]["ExpressionAttributeValues"]
    assert isinstance(values, dict)
    assert values[":s"] == "orphaned"
    assert values[":e"] == "exceeded 2-second background-work ceiling"
    assert values[":c"]
    assert audit_rows[0]["status"] == "orphaned"


@pytest.mark.parametrize(
    "repo",
    ["owner/repo", "acme-labs/data_api.v2", "A1/B-2"],
)
def test_github_repo_slug_accepts_exact_owner_name(repo: str) -> None:
    assert catalog_tools._validate_github_repo_slug(repo) == repo


@pytest.mark.parametrize(
    "repo",
    [
        "https://github.com/owner/repo",
        "owner/repo/extra",
        "owner/repo?redirect=evil",
        "owner/../repo",
        "owner--name/repo",
        "-owner/repo",
        "owner-/repo",
        "owner/repo name",
        "owner/..",
    ],
)
def test_github_repo_slug_rejects_non_slug_values(repo: str) -> None:
    with pytest.raises(ValueError, match="exact GitHub owner/name"):
        catalog_tools._validate_github_repo_slug(repo)


def test_run_task_response_accepts_one_task_and_no_failures() -> None:
    task_arn = "arn:aws:ecs:us-west-2:123456789012:task/cluster/task-id"

    assert (
        catalog_tools._validate_run_task_response(
            {"tasks": [{"taskArn": task_arn}], "failures": []}
        )
        == task_arn
    )


@pytest.mark.parametrize(
    "response",
    [
        {"tasks": [], "failures": []},
        {
            "tasks": [{"taskArn": "arn:task"}],
            "failures": [{"arn": "arn:task-definition", "reason": "capacity"}],
        },
        {"tasks": [{"taskArn": "arn:one"}, {"taskArn": "arn:two"}], "failures": []},
        {"tasks": [{}], "failures": []},
        {"tasks": [{"taskArn": "arn:task"}]},
        None,
    ],
)
def test_run_task_response_rejects_incomplete_or_failed_launch(
    response: object,
) -> None:
    with pytest.raises(RuntimeError, match="ecs.run_task"):
        catalog_tools._validate_run_task_response(response)


def test_bad_run_task_response_uses_existing_launch_rollback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeTable:
        def __init__(self) -> None:
            self.puts: list[dict] = []
            self.updates: list[dict] = []

        def put_item(self, **kwargs: object) -> None:
            self.puts.append(kwargs)

        def update_item(self, **kwargs: object) -> None:
            self.updates.append(kwargs)

    class FakeEcs:
        def run_task(self, **_kwargs: object) -> dict:
            return {
                "tasks": [],
                "failures": [{"reason": "RESOURCE:CPU"}],
            }

    table = FakeTable()
    registered: list[str] = []
    ping_completed: list[str] = []
    sdk_completed: list[str] = []
    monkeypatch.setattr(
        catalog_tools,
        "get_context",
        lambda: {
            "tenant_id": "tenant-a",
            "github_installation_id": "123",
            "allowed_github_repos": ["owner/repo"],
            "channel_id": "C123",
            "thread_id": "1.2",
            "heartbeat_busy_threshold": 1,
        },
    )
    monkeypatch.setattr(
        catalog_tools,
        "_load_sandbox_coords",
        lambda: {
            "cluster_arn": "cluster",
            "task_def_arn": "task-definition",
            "subnets": "subnet-1",
            "security_groups": "sg-1",
        },
    )
    monkeypatch.setattr(catalog_tools, "_sandbox_jobs_table", lambda: table)
    monkeypatch.setattr(catalog_tools, "_ecs_client", lambda: FakeEcs())
    monkeypatch.setattr(
        catalog_tools,
        "_register_async_task",
        lambda task_id, _threshold: registered.append(task_id),
    )
    monkeypatch.setattr(
        catalog_tools.ping,
        "complete_task",
        lambda task_id: ping_completed.append(task_id),
    )
    monkeypatch.setattr(
        catalog_tools.app,
        "complete_async_task",
        lambda task_id: sdk_completed.append(task_id),
    )

    propose_pr_function = catalog_tools.propose_pr.__wrapped__.__wrapped__
    result = propose_pr_function("owner/repo", "Make a safe change")

    assert "failed to launch the sandbox task" in result
    assert len(table.puts) == 1
    assert len(table.updates) == 1
    error_values = table.updates[0]["ExpressionAttributeValues"]
    assert error_values[":s"] == "error"
    assert "RESOURCE:CPU" in error_values[":e"]
    assert len(registered) == 1
    assert registered == ping_completed == sdk_completed
