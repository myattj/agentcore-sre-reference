"""Regression tests for tenant isolation and runtime authorization gates."""

from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any, Callable

import pytest

import tools
from tenant import (
    CodebaseBinding,
    CodebasesConfig,
    MemoryConfig,
    TenantConfig,
)


def _raw_tool(tool_obj: Any) -> Callable[..., str]:
    """Unwrap Strands and audit decorators for deterministic unit tests."""
    return tool_obj.__wrapped__.__wrapped__


def test_repo_authorization_is_exact_normalized_and_fail_closed() -> None:
    ctx = {"allowed_github_repos": ["Acme/API"]}

    assert tools._authorize_github_repo("ACME/api", ctx) == ("acme/api", None)
    assert tools._authorize_github_repo("acme/api-extra", ctx)[0] is None
    assert tools._authorize_github_repo("evil/acme/api", ctx)[0] is None
    assert tools._authorize_github_repo("acme/api", {})[0] is None
    assert (
        tools._authorize_github_repo("acme/api", {"allowed_github_repos": []})[0]
        is None
    )


@pytest.mark.parametrize(
    ("tool_obj", "args"),
    [
        (tools.code_search, ("needle", "acme/unauthorized")),
        (tools.code_read_file, ("README.md", "acme/unauthorized")),
        (tools.code_find_symbol, ("Widget", "acme/unauthorized")),
        (tools.code_list_commits, ("acme/unauthorized",)),
        (tools.propose_pr, ("acme/unauthorized", "make a safe change")),
    ],
)
def test_every_github_read_and_write_path_denies_unbound_repo_before_io(
    monkeypatch: pytest.MonkeyPatch,
    tool_obj: Any,
    args: tuple[str, ...],
) -> None:
    monkeypatch.setattr(
        tools,
        "get_context",
        lambda: {
            "tenant_id": "tenant-a",
            "channel_id": "C_CURRENT",
            "thread_id": "1.2",
            "github_installation_id": "12345",
            "allowed_github_repos": ["acme/allowed"],
        },
    )

    import code_backend

    monkeypatch.setattr(
        code_backend,
        "build_default_backend",
        lambda _installation_id: pytest.fail("GitHub backend must not be built"),
    )
    monkeypatch.setattr(
        tools,
        "_load_sandbox_coords",
        lambda: pytest.fail("sandbox coordinates must not be loaded"),
    )

    result = _raw_tool(tool_obj)(*args)

    assert "not authorized for this tenant" in result


def test_authorized_repo_is_canonicalized_before_backend_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repos_seen: list[str] = []

    class FakeBackend:
        def search_code(self, _query: str, repo: str, *, max_results: int) -> list[Any]:
            assert max_results == 20
            repos_seen.append(repo)
            return []

    monkeypatch.setattr(
        tools,
        "get_context",
        lambda: {
            "tenant_id": "tenant-a",
            "github_installation_id": "12345",
            "allowed_github_repos": ["Acme/API"],
        },
    )
    import code_backend

    monkeypatch.setattr(
        code_backend,
        "build_default_backend",
        lambda _installation_id: FakeBackend(),
    )

    result = _raw_tool(tools.code_search)("needle", "ACME/api")

    assert repos_seen == ["acme/api"]
    assert "No results" in result


@pytest.mark.parametrize(
    ("tool_obj", "kwargs", "operation_name"),
    [
        (
            tools.search_team_history,
            {"query": "incident", "channel_id": "C_TARGET"},
            "fetch_channel_history",
        ),
        (
            tools.read_thread_context,
            {"channel_id": "C_TARGET", "thread_id": "1.2"},
            "fetch_thread_replies",
        ),
        (
            tools.post_to_channel,
            {"channel_id": "C_TARGET", "message": "hello"},
            "post_message",
        ),
    ],
)
def test_cross_channel_tools_require_positive_requester_membership(
    monkeypatch: pytest.MonkeyPatch,
    tool_obj: Any,
    kwargs: dict[str, str],
    operation_name: str,
) -> None:
    slack_api = sys.modules["slack_api"]
    monkeypatch.setattr(
        tools,
        "get_context",
        lambda: {
            "tenant_id": "tenant-a",
            "user_id": "U_REQUESTER",
            "channel_id": "C_CURRENT",
        },
    )
    monkeypatch.setattr(
        slack_api, "get_bot_token", lambda _tenant: "xoxb-test", raising=False
    )
    monkeypatch.setattr(
        slack_api,
        "is_user_member_of_channel",
        lambda _token, _channel, _user: False,
        raising=False,
    )
    monkeypatch.setattr(
        slack_api,
        operation_name,
        lambda *_args, **_kwargs: pytest.fail("Slack operation must not run"),
        raising=False,
    )

    result = _raw_tool(tool_obj)(**kwargs)

    assert "must be a member" in result


def test_current_channel_is_allowed_without_membership_round_trip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slack_api = sys.modules["slack_api"]
    monkeypatch.setattr(
        tools,
        "get_context",
        lambda: {
            "tenant_id": "tenant-a",
            "user_id": "U_REQUESTER",
            "channel_id": "C_CURRENT",
        },
    )
    monkeypatch.setattr(
        slack_api, "get_bot_token", lambda _tenant: "xoxb-test", raising=False
    )
    monkeypatch.setattr(
        slack_api,
        "is_user_member_of_channel",
        lambda *_args: pytest.fail("current channel needs no membership lookup"),
        raising=False,
    )
    monkeypatch.setattr(
        slack_api,
        "fetch_channel_history",
        lambda *_args: "current-channel-history",
        raising=False,
    )

    result = _raw_tool(tools.search_team_history)("incident")

    assert result == "current-channel-history"


def test_bot_trigger_without_user_id_can_read_current_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slack_api = sys.modules["slack_api"]
    monkeypatch.setattr(
        tools,
        "get_context",
        lambda: {
            "tenant_id": "tenant-a",
            "channel_id": "C_CURRENT",
        },
    )
    monkeypatch.setattr(
        slack_api, "get_bot_token", lambda _tenant: "xoxb-test", raising=False
    )
    monkeypatch.setattr(
        slack_api,
        "fetch_channel_history",
        lambda *_args: "bot-current-channel-history",
        raising=False,
    )

    result = _raw_tool(tools.search_team_history)("incident")

    assert result == "bot-current-channel-history"


def test_missing_requester_identity_denies_cross_channel_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slack_api = sys.modules["slack_api"]
    monkeypatch.setattr(
        tools,
        "get_context",
        lambda: {
            "tenant_id": "tenant-a",
            "channel_id": "C_CURRENT",
        },
    )
    monkeypatch.setattr(
        slack_api, "get_bot_token", lambda _tenant: "xoxb-test", raising=False
    )
    monkeypatch.setattr(
        slack_api,
        "fetch_channel_history",
        lambda *_args: pytest.fail("Slack operation must not run"),
        raising=False,
    )

    result = _raw_tool(tools.search_team_history)("incident", "C_TARGET")

    assert "requester identity is unavailable" in result


def test_cross_channel_access_allows_positive_membership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slack_api = sys.modules["slack_api"]
    membership_checks: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        tools,
        "get_context",
        lambda: {
            "tenant_id": "tenant-a",
            "user_id": "U_REQUESTER",
            "channel_id": "C_CURRENT",
        },
    )
    monkeypatch.setattr(
        slack_api, "get_bot_token", lambda _tenant: "xoxb-test", raising=False
    )

    def member(token: str, channel: str, user: str) -> bool:
        membership_checks.append((token, channel, user))
        return True

    monkeypatch.setattr(slack_api, "is_user_member_of_channel", member, raising=False)
    monkeypatch.setattr(
        slack_api,
        "post_message",
        lambda *_args: "posted",
        raising=False,
    )

    result = _raw_tool(tools.post_to_channel)("C_TARGET", "hello")

    assert result == "posted"
    assert membership_checks == [("xoxb-test", "C_TARGET", "U_REQUESTER")]


def test_manage_config_is_read_only_without_configured_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tenant

    config = TenantConfig(tenant_id="tenant-a")
    saved: list[TenantConfig] = []
    monkeypatch.setattr(tenant, "load_tenant_config", lambda _tenant: config)
    monkeypatch.setattr(tenant, "save_tenant_config", saved.append)
    monkeypatch.setattr(
        tools,
        "get_context",
        lambda: {
            "tenant_id": "tenant-a",
            "user_id": "U_REQUESTER",
        },
    )

    result = _raw_tool(tools.manage_config)("update", "system_prompt", '"new prompt"')

    assert "admin_user_ids" in result
    assert saved == []
    assert config.system_prompt != "new prompt"


def test_manage_config_requires_exact_context_requester_admin_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tenant

    config = TenantConfig(tenant_id="tenant-a", admin_user_ids=["U_ADMIN"])
    saved: list[TenantConfig] = []
    monkeypatch.setattr(tenant, "load_tenant_config", lambda _tenant: config)
    monkeypatch.setattr(tenant, "save_tenant_config", saved.append)
    monkeypatch.setattr(
        tools,
        "get_context",
        lambda: {
            "tenant_id": "tenant-a",
            "user_id": "u_admin",
            "config_admin_user_ids": ("U_ADMIN",),
        },
    )
    denied = _raw_tool(tools.manage_config)("update", "system_prompt", '"wrong case"')
    assert "admin_user_ids" in denied
    assert saved == []

    monkeypatch.setattr(
        tools,
        "get_context",
        lambda: {
            "tenant_id": "tenant-a",
            "user_id": "U_ADMIN",
            "config_admin_user_ids": ("U_ADMIN",),
        },
    )
    allowed = _raw_tool(tools.manage_config)(
        "update", "system_prompt", '"admin prompt"'
    )
    assert "Updated 'system_prompt'" in allowed
    assert saved == [config]
    assert config.system_prompt == "admin prompt"


@pytest.mark.parametrize(
    "data",
    [
        '["echo", 7]',
        '["echo", "echo"]',
        '["echo", "not_a_registered_tool"]',
    ],
)
def test_manage_config_rejects_malformed_catalog_tool_lists(
    monkeypatch: pytest.MonkeyPatch,
    data: str,
) -> None:
    import tenant

    config = TenantConfig(tenant_id="tenant-a", admin_user_ids=["U_ADMIN"])
    saved: list[TenantConfig] = []
    monkeypatch.setattr(tenant, "load_tenant_config", lambda _tenant: config)
    monkeypatch.setattr(tenant, "save_tenant_config", saved.append)
    monkeypatch.setattr(
        tools,
        "get_context",
        lambda: {
            "tenant_id": "tenant-a",
            "user_id": "U_ADMIN",
            "config_admin_user_ids": ("U_ADMIN",),
        },
    )

    result = _raw_tool(tools.manage_config)("update", "catalog_tools", data)

    assert result.startswith("Error:")
    assert saved == []


def test_manage_config_non_admin_view_redacts_sensitive_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tenant

    config = TenantConfig(
        tenant_id="tenant-a",
        system_prompt="SECRET INTERNAL PROMPT",
        admin_user_ids=["U_ADMIN"],
    )
    monkeypatch.setattr(tenant, "load_tenant_config", lambda _tenant: config)
    monkeypatch.setattr(
        tools,
        "get_context",
        lambda: {
            "tenant_id": "tenant-a",
            "user_id": "U_OTHER",
        },
    )

    result = _raw_tool(tools.manage_config)("view", "all")

    assert "SECRET INTERNAL PROMPT" not in result
    assert "U_ADMIN" not in result
    assert "redacted" in result
    assert "admin_user_ids" not in result


def test_manage_config_has_no_admin_allowlist_section(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        tools,
        "get_context",
        lambda: {
            "tenant_id": "tenant-a",
            "user_id": "U_ADMIN",
        },
    )

    result = _raw_tool(tools.manage_config)(
        "update", "admin_user_ids", '["U_ATTACKER"]'
    )

    assert "unknown section" in result


def test_default_memory_policy_is_channel_scoped() -> None:
    assert MemoryConfig().shared_across_channels is False


@pytest.mark.parametrize(
    ("memory", "ctx", "fallback", "expected"),
    [
        (MemoryConfig(), {"channel_id": "C_ONE"}, "invoke-1", "tenants/tenant-a/channels/C_ONE"),
        (
            MemoryConfig(shared_across_channels=True),
            {"channel_id": "C_ONE"},
            "invoke-2",
            "tenants/tenant-a",
        ),
        (
            MemoryConfig(shared_across_channels=True, isolated_channels=["C_ONE"]),
            {"channel_id": "C_ONE"},
            "invoke-3",
            "tenants/tenant-a/channels/C_ONE",
        ),
        (MemoryConfig(), {"user_id": "U_ONE"}, "invoke-4", "tenants/tenant-a/users/U_ONE"),
        (MemoryConfig(), {}, "invoke-5", "tenants/tenant-a/invocations/invoke-5"),
    ],
)
def test_local_memory_namespace_isolates_every_supported_scope(
    memory: MemoryConfig,
    ctx: dict[str, str],
    fallback: str,
    expected: str,
) -> None:
    import main

    config = TenantConfig(tenant_id="tenant-a", memory=memory)
    assert main._local_memory_namespace("tenant-a", ctx, config, fallback) == expected


def test_local_feedback_records_from_different_channels_never_share_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import main

    writes: list[tuple[str, list[dict[str, Any]]]] = []
    monkeypatch.setattr(main, "_MEMORY_ID", "")
    monkeypatch.setattr(
        main,
        "_memory",
        SimpleNamespace(
            write_records=lambda namespace, records: writes.append(
                (namespace, records)
            )
        ),
    )
    config = TenantConfig(tenant_id="tenant-a")
    feedback = {
        "sentiment": "positive",
        "reaction": "+1",
        "user_question": "Is it healthy?",
        "bot_answer": "Yes.",
    }

    main._write_feedback_memory(
        "tenant-a", {"channel_id": "C_ONE"}, "invoke-1", config, feedback
    )
    main._write_feedback_memory(
        "tenant-a", {"channel_id": "C_TWO"}, "invoke-2", config, feedback
    )

    assert [namespace for namespace, _records in writes] == [
        "tenants/tenant-a/channels/C_ONE",
        "tenants/tenant-a/channels/C_TWO",
    ]
    assert all(records[0]["type"] == "user_feedback" for _, records in writes)


def test_memory_session_actor_respects_channel_scope_and_shared_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import main
    import bedrock_agentcore.memory.integrations.strands.config as memory_config_module
    import bedrock_agentcore.memory.integrations.strands.session_manager as manager_module

    created_configs: list[SimpleNamespace] = []

    class FakeMemoryConfig(SimpleNamespace):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            created_configs.append(self)

    class FakeManager(SimpleNamespace):
        def __init__(self, *, agentcore_memory_config: Any, region_name: str) -> None:
            super().__init__(config=agentcore_memory_config, region=region_name)

    monkeypatch.setattr(main, "_MEMORY_ID", "mem-test")
    monkeypatch.setattr(main, "_SEMANTIC_STRATEGY_ID", "")
    monkeypatch.setattr(main, "_USER_PREF_STRATEGY_ID", "")
    monkeypatch.setattr(memory_config_module, "AgentCoreMemoryConfig", FakeMemoryConfig)
    monkeypatch.setattr(manager_module, "AgentCoreMemorySessionManager", FakeManager)

    default_config = TenantConfig(tenant_id="tenant-a")
    main._build_memory_session_manager(
        "tenant-a",
        {"channel_id": "C_ONE", "user_id": "U_ONE"},
        "invoke-1",
        default_config,
    )
    assert created_configs[-1].actor_id == "tenant-a_C_ONE"

    main._build_memory_session_manager(
        "tenant-a",
        {"channel_id": "C_ONE", "user_id": "U_ONE"},
        "invoke-missing-policy",
        None,
    )
    assert created_configs[-1].actor_id == "tenant-a_C_ONE"

    shared_config = TenantConfig(
        tenant_id="tenant-a",
        memory=MemoryConfig(shared_across_channels=True),
    )
    main._build_memory_session_manager(
        "tenant-a",
        {"channel_id": "C_ONE", "user_id": "U_ONE"},
        "invoke-2",
        shared_config,
    )
    assert created_configs[-1].actor_id == "tenant-a"

    isolated_config = TenantConfig(
        tenant_id="tenant-a",
        memory=MemoryConfig(
            shared_across_channels=True,
            isolated_channels=["C_ONE"],
        ),
    )
    main._build_memory_session_manager(
        "tenant-a",
        {"channel_id": "C_ONE", "user_id": "U_ONE"},
        "invoke-3",
        isolated_config,
    )
    assert created_configs[-1].actor_id == "tenant-a_C_ONE"


def test_runtime_repo_allowlist_comes_only_from_enabled_bindings() -> None:
    import main

    disabled = TenantConfig(
        tenant_id="tenant-a",
        codebases=CodebasesConfig(
            enabled=False,
            bindings=[CodebaseBinding(repo="Acme/API")],
        ),
    )
    assert main._normalized_allowed_github_repos(disabled) == []

    enabled = TenantConfig(
        tenant_id="tenant-a",
        codebases=CodebasesConfig(
            enabled=True,
            bindings=[
                CodebaseBinding(repo="Acme/API"),
                CodebaseBinding(repo="acme/api"),
                CodebaseBinding(repo="acme/other"),
            ],
        ),
    )
    assert main._normalized_allowed_github_repos(enabled) == [
        "acme/api",
        "acme/other",
    ]
