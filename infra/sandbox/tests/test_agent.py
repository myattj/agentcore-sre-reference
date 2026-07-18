from __future__ import annotations

import base64
import inspect
import subprocess
from pathlib import Path

import pytest

import agent
import entrypoint


def test_model_defaults_use_official_dateless_anthropic_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SANDBOX_MODEL", raising=False)

    assert agent.DEFAULT_MODEL == "claude-sonnet-4-6"
    assert agent.TokenBudget().model == agent.DEFAULT_MODEL
    assert (
        inspect.signature(agent.run_agent_loop).parameters["model"].default
        == agent.DEFAULT_MODEL
    )
    assert (
        inspect.signature(agent.generate_pr_metadata).parameters["model"].default
        == agent.DEFAULT_MODEL
    )
    assert entrypoint.get_sandbox_model() == agent.DEFAULT_MODEL
    assert set(agent.PRICING) == {"claude-sonnet-4-6", "claude-opus-4-6"}
    assert agent.PRICING["claude-opus-4-6"] == {
        "input": 5.0,
        "output": 25.0,
        "cache_write": 6.25,
        "cache_read": 0.50,
    }


def test_sandbox_model_allows_environment_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SANDBOX_MODEL", "claude-test-model")

    assert entrypoint.get_sandbox_model() == "claude-test-model"


@pytest.mark.parametrize(
    ("name", "email"),
    [
        (entrypoint.DEFAULT_GIT_USER_NAME, entrypoint.DEFAULT_GIT_USER_EMAIL),
        ("Example Automation", "automation@users.noreply.github.com"),
    ],
)
def test_clone_repo_uses_portable_git_identity(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    email: str,
) -> None:
    calls: list[tuple[list[str], str | None, dict[str, str] | None]] = []
    monkeypatch.setattr(entrypoint.os.path, "exists", lambda _path: False)
    monkeypatch.setattr(
        entrypoint,
        "run_git",
        lambda args, cwd=None, env=None: calls.append((args, cwd, env)),
    )
    if name == entrypoint.DEFAULT_GIT_USER_NAME:
        monkeypatch.delenv("SANDBOX_GIT_USER_NAME", raising=False)
        monkeypatch.delenv("SANDBOX_GIT_USER_EMAIL", raising=False)
    else:
        monkeypatch.setenv("SANDBOX_GIT_USER_NAME", name)
        monkeypatch.setenv("SANDBOX_GIT_USER_EMAIL", email)

    entrypoint.clone_repo("owner/repo", "test-token", "/tmp/test-repo")
    resolved_target = str(Path("/tmp/test-repo").resolve())

    assert calls[-2:] == [
        (["config", "user.name", name], resolved_target, None),
        (["config", "user.email", email], resolved_target, None),
    ]
    clone_args, clone_cwd, clone_env = calls[0]
    assert clone_args == [
        "clone",
        "--depth",
        "50",
        "https://github.com/owner/repo.git",
        resolved_target,
    ]
    assert clone_cwd is None
    assert "test-token" not in " ".join(clone_args)
    assert clone_env is not None
    assert clone_env["GIT_CONFIG_COUNT"] == "1"
    assert clone_env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraHeader"
    encoded_credentials = clone_env["GIT_CONFIG_VALUE_0"].removeprefix(
        "Authorization: Basic "
    )
    assert base64.b64decode(encoded_credentials) == b"x-access-token:test-token"


def test_push_branch_reuses_process_scoped_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], str | None, dict[str, str] | None]] = []
    monkeypatch.setattr(
        entrypoint,
        "run_git",
        lambda args, cwd=None, env=None: calls.append((args, cwd, env)),
    )

    entrypoint.push_branch(
        "owner/repo", "agent/pr-1234", "push-token", "/tmp/test-repo"
    )

    assert len(calls) == 1
    args, cwd, auth_env = calls[0]
    assert args == [
        "push",
        "https://github.com/owner/repo.git",
        "agent/pr-1234:agent/pr-1234",
    ]
    assert cwd == "/tmp/test-repo"
    assert "push-token" not in " ".join(args)
    assert auth_env is not None
    encoded_credentials = auth_env["GIT_CONFIG_VALUE_0"].removeprefix(
        "Authorization: Basic "
    )
    assert base64.b64decode(encoded_credentials) == b"x-access-token:push-token"


def test_push_branch_ignores_attacker_controlled_origin(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    subprocess.run(["git", "init"], cwd=repository, check=True, capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://attacker.example/collect"],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    calls: list[tuple[list[str], str | None, dict[str, str] | None]] = []
    monkeypatch.setattr(
        entrypoint,
        "run_git",
        lambda args, cwd=None, env=None: calls.append((args, cwd, env)),
    )

    entrypoint.push_branch("owner/repo", "agent/pr-safe", "push-token", str(repository))

    assert calls[0][0] == [
        "push",
        "https://github.com/owner/repo.git",
        "agent/pr-safe:agent/pr-safe",
    ]
    assert "attacker.example" not in " ".join(calls[0][0])
    assert calls[0][2] is not None
    assert calls[0][2]["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraHeader"


@pytest.mark.parametrize(
    "repo",
    ["owner/repo", "acme-labs/data_api.v2", "A1/B-2"],
)
def test_validate_github_repo_accepts_exact_owner_name(repo: str) -> None:
    assert entrypoint.validate_github_repo(repo) == repo


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
def test_validate_github_repo_rejects_non_slug_values(repo: str) -> None:
    with pytest.raises(ValueError, match="exact GitHub owner/name"):
        entrypoint.validate_github_repo(repo)


@pytest.mark.parametrize("target", ["/", "/tmp", "/var/lib/agent-repo"])
def test_clone_repo_rejects_broad_or_non_temporary_targets(target: str) -> None:
    with pytest.raises(ValueError, match="dedicated directory"):
        entrypoint.clone_repo("owner/repo", "test-token", target)


def test_safe_path_rejects_prefix_collision_traversal(tmp_path: Path) -> None:
    work_dir = tmp_path / "repo"
    work_dir.mkdir()
    sibling = tmp_path / "repository-secrets"
    sibling.mkdir()

    with pytest.raises(ValueError, match="Path escapes work directory"):
        agent._safe_path(str(work_dir), "../repository-secrets/token.txt")


def test_safe_path_accepts_files_beneath_work_dir(tmp_path: Path) -> None:
    work_dir = tmp_path / "repo"
    nested = work_dir / "src" / "main.py"

    assert agent._safe_path(str(work_dir), "src/main.py") == str(nested)


def test_run_command_caps_timeout_without_starting_a_process(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    observed: dict[str, object] = {}

    def fake_run(command: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
        observed["command"] = command
        observed.update(kwargs)
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    monkeypatch.setattr(agent.subprocess, "run", fake_run)

    output = agent._handle_run_command(
        str(tmp_path),
        {"command": "python -m pytest", "timeout": 999},
    )

    assert output == "ok\n"
    assert observed == {
        "command": "python -m pytest",
        "shell": True,
        "cwd": str(tmp_path),
        "capture_output": True,
        "text": True,
        "timeout": 120,
    }


def test_parse_pr_metadata_has_safe_fallbacks() -> None:
    metadata = agent._parse_pr_metadata(
        "unstructured output", "Changed a file", "1 file changed"
    )

    assert metadata.commit_message == "Apply code changes"
    assert metadata.title == "Agent: code change"
    assert "Changed a file" in metadata.body
