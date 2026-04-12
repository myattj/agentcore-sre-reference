"""Tests for the Phase B sandbox completion callback.

Covers:
  - verify_callback_auth (missing/wrong scheme/empty token/correct token)
  - format_completion_message (success / error / orphan / no-task variants)
  - handle_sandbox_complete (happy path / missing job row / no bot token /
    error-status payload / Slack post failure)
  - The /internal/sandbox_complete route end-to-end via TestClient
    (401 on bad auth, 200 on good auth, 400 on bad JSON)

The DDB read is mocked at the module-level `get_sandbox_job` and the
Slack post is mocked at the module-level `_post_slack_message`. Both
are deliberate test seams in `bridge/bridge/sandbox_callback.py`.
"""
from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from bridge import sandbox_callback


# ---------------------------------------------------------------------------
# verify_callback_auth
# ---------------------------------------------------------------------------

class TestVerifyCallbackAuth:
    def test_missing_header_rejected(self):
        assert sandbox_callback.verify_callback_auth(None) is False
        assert sandbox_callback.verify_callback_auth("") is False

    def test_wrong_scheme_rejected(self):
        # Basic auth, OAuth, missing scheme entirely — all rejected.
        assert sandbox_callback.verify_callback_auth("Basic dGVzdDp0ZXN0") is False
        assert sandbox_callback.verify_callback_auth("OAuth token=foo") is False
        assert sandbox_callback.verify_callback_auth("test-sandbox-secret") is False

    def test_empty_token_after_bearer_rejected(self):
        assert sandbox_callback.verify_callback_auth("Bearer ") is False
        assert sandbox_callback.verify_callback_auth("Bearer    ") is False

    def test_wrong_value_rejected(self):
        assert sandbox_callback.verify_callback_auth("Bearer not-the-secret") is False

    def test_correct_value_accepted(self):
        # conftest.py sets SANDBOX_CALLBACK_SECRET=test-sandbox-secret
        assert sandbox_callback.verify_callback_auth("Bearer test-sandbox-secret") is True

    def test_secret_unset_rejects(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("SANDBOX_CALLBACK_SECRET", raising=False)
        assert sandbox_callback.verify_callback_auth("Bearer anything") is False


# ---------------------------------------------------------------------------
# format_completion_message
# ---------------------------------------------------------------------------

class TestFormatCompletionMessage:
    def test_success_with_pr_url(self):
        job = {"repo": "acme/api", "task_description": "fix auth bug"}
        msg = sandbox_callback.format_completion_message(
            job, status="success", pr_url="https://github.com/acme/api/pull/42", error="",
        )
        assert "Opened PR: https://github.com/acme/api/pull/42" in msg
        assert "fix auth bug" in msg

    def test_success_without_task_description(self):
        job = {"repo": "acme/api"}
        msg = sandbox_callback.format_completion_message(
            job, status="success", pr_url="https://github.com/acme/api/pull/42", error="",
        )
        assert msg == "Opened PR: https://github.com/acme/api/pull/42"

    def test_error_status_includes_repo_and_error(self):
        job = {"repo": "acme/api", "task_description": "fix auth bug"}
        msg = sandbox_callback.format_completion_message(
            job, status="error", pr_url="", error="git push failed: permission denied",
        )
        assert "Couldn't open a PR" in msg
        assert "acme/api" in msg
        assert "permission denied" in msg

    def test_error_with_no_message_falls_back_to_unknown(self):
        job = {"repo": "acme/api"}
        msg = sandbox_callback.format_completion_message(
            job, status="error", pr_url="", error="",
        )
        assert "unknown error" in msg

    def test_orphaned_treated_as_failure(self):
        job = {"repo": "acme/api"}
        msg = sandbox_callback.format_completion_message(
            job, status="orphaned", pr_url="", error="task exceeded ceiling",
        )
        assert "Couldn't open a PR" in msg
        assert "task exceeded ceiling" in msg


# ---------------------------------------------------------------------------
# handle_sandbox_complete
# ---------------------------------------------------------------------------

@pytest.fixture
def patched_io(monkeypatch: pytest.MonkeyPatch):
    """Mock get_sandbox_job, slack_token_store.get_bot_token, and
    _post_slack_message. Tests configure the return values per case."""
    state: dict[str, Any] = {
        "job": None,
        "job_raises": None,
        "token": "xoxb-fake",
        "token_raises": None,
        "post_result": True,
        "posts": [],
    }

    def fake_get_job(task_id: str, region: str) -> dict[str, Any]:
        if state["job_raises"]:
            raise state["job_raises"]
        if state["job"] is None:
            raise KeyError(task_id)
        return state["job"]

    def fake_get_token(tenant_id: str) -> str:
        if state["token_raises"]:
            raise state["token_raises"]
        return state["token"]

    async def fake_post(token: str, channel: str, thread_ts: str | None, text: str) -> bool:
        state["posts"].append(
            {"token": token, "channel": channel, "thread_ts": thread_ts, "text": text}
        )
        return state["post_result"]

    monkeypatch.setattr(sandbox_callback, "get_sandbox_job", fake_get_job)
    monkeypatch.setattr(
        sandbox_callback.slack_token_store, "get_bot_token", fake_get_token
    )
    monkeypatch.setattr(sandbox_callback, "_post_slack_message", fake_post)
    return state


class TestHandleSandboxComplete:
    @pytest.mark.asyncio
    async def test_missing_task_id_rejected(self, patched_io):
        result = await sandbox_callback.handle_sandbox_complete({})
        assert result == {"ok": False, "error": "missing task_id"}

    @pytest.mark.asyncio
    async def test_unknown_task_id_returns_clean_error(self, patched_io):
        # patched_io defaults to job=None which raises KeyError
        result = await sandbox_callback.handle_sandbox_complete({"task_id": "pr-abc"})
        assert result == {"ok": False, "error": "unknown task_id"}

    @pytest.mark.asyncio
    async def test_happy_path_posts_pr_link(self, patched_io):
        patched_io["job"] = {
            "task_id": "pr-abc",
            "tenant_id": "slack-test",
            "slack_channel_id": "C123",
            "slack_thread_id": "1234.5678",
            "repo": "acme/api",
            "task_description": "fix auth",
        }
        result = await sandbox_callback.handle_sandbox_complete({
            "task_id": "pr-abc",
            "status": "success",
            "pr_url": "https://github.com/acme/api/pull/42",
        })
        assert result == {"ok": True, "posted": True}
        assert len(patched_io["posts"]) == 1
        post = patched_io["posts"][0]
        assert post["channel"] == "C123"
        assert post["thread_ts"] == "1234.5678"
        assert "Opened PR" in post["text"]
        assert "https://github.com/acme/api/pull/42" in post["text"]

    @pytest.mark.asyncio
    async def test_error_status_posts_failure_message(self, patched_io):
        patched_io["job"] = {
            "task_id": "pr-bad",
            "tenant_id": "slack-test",
            "slack_channel_id": "C456",
            "slack_thread_id": "1234.5678",
            "repo": "acme/api",
        }
        result = await sandbox_callback.handle_sandbox_complete({
            "task_id": "pr-bad",
            "status": "error",
            "pr_url": "",
            "error": "git push failed: 403",
        })
        assert result == {"ok": True, "posted": True}
        post = patched_io["posts"][0]
        # Failure path: no PR URL, error description present.
        assert "Opened PR" not in post["text"]
        assert "Couldn't open a PR" in post["text"]
        assert "git push failed" in post["text"]

    @pytest.mark.asyncio
    async def test_missing_routing_fields_skips_post(self, patched_io):
        # Row has tenant_id but no channel_id — broken.
        patched_io["job"] = {
            "task_id": "pr-broken",
            "tenant_id": "slack-test",
            "slack_channel_id": "",
            "slack_thread_id": "",
        }
        result = await sandbox_callback.handle_sandbox_complete({
            "task_id": "pr-broken",
            "status": "success",
            "pr_url": "https://github.com/x/y/pull/1",
        })
        assert result["ok"] is False
        assert "missing routing fields" in result["error"]
        assert patched_io["posts"] == []

    @pytest.mark.asyncio
    async def test_no_bot_token_skips_post_gracefully(self, patched_io):
        patched_io["job"] = {
            "task_id": "pr-test",
            "tenant_id": "slack-test",
            "slack_channel_id": "C123",
            "slack_thread_id": "",
            "repo": "acme/api",
        }
        patched_io["token_raises"] = KeyError("slack-test")
        result = await sandbox_callback.handle_sandbox_complete({
            "task_id": "pr-test",
            "status": "success",
            "pr_url": "https://github.com/acme/api/pull/9",
        })
        assert result == {"ok": False, "error": "no Slack bot token"}
        assert patched_io["posts"] == []

    @pytest.mark.asyncio
    async def test_empty_token_local_dev_returns_ok_no_post(self, patched_io):
        patched_io["job"] = {
            "task_id": "pr-local",
            "tenant_id": "slack-test",
            "slack_channel_id": "C123",
            "slack_thread_id": "",
            "repo": "acme/api",
        }
        patched_io["token"] = ""  # local-dev "stub mode"
        result = await sandbox_callback.handle_sandbox_complete({
            "task_id": "pr-local",
            "status": "success",
            "pr_url": "https://github.com/acme/api/pull/9",
        })
        assert result == {"ok": True, "posted": False}
        assert patched_io["posts"] == []

    @pytest.mark.asyncio
    async def test_failed_slack_post_returns_posted_false(self, patched_io):
        patched_io["job"] = {
            "task_id": "pr-test",
            "tenant_id": "slack-test",
            "slack_channel_id": "C123",
            "slack_thread_id": "",
            "repo": "acme/api",
        }
        patched_io["post_result"] = False
        result = await sandbox_callback.handle_sandbox_complete({
            "task_id": "pr-test",
            "status": "success",
            "pr_url": "https://github.com/acme/api/pull/9",
        })
        # Slack post failed but the orchestrator still returns ok=True
        # — the agent's poller is the load-bearing path for HealthyBusy.
        assert result == {"ok": True, "posted": False}


# ---------------------------------------------------------------------------
# /internal/sandbox_complete route via TestClient
# ---------------------------------------------------------------------------

@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch):
    """FastAPI test client with handle_sandbox_complete patched to a stub."""
    from bridge import main, sandbox_callback as sc

    async def fake_handle(payload: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "received": payload}

    monkeypatch.setattr(main, "handle_sandbox_complete", fake_handle)
    monkeypatch.setattr(sc, "handle_sandbox_complete", fake_handle)
    return TestClient(main.app)


class TestSandboxCompleteRoute:
    def test_missing_auth_returns_401(self, client: TestClient):
        resp = client.post("/internal/sandbox_complete", json={"task_id": "pr-x"})
        assert resp.status_code == 401

    def test_wrong_secret_returns_401(self, client: TestClient):
        resp = client.post(
            "/internal/sandbox_complete",
            json={"task_id": "pr-x"},
            headers={"Authorization": "Bearer wrong-secret"},
        )
        assert resp.status_code == 401

    def test_correct_secret_returns_200(self, client: TestClient):
        resp = client.post(
            "/internal/sandbox_complete",
            json={"task_id": "pr-x", "status": "success"},
            headers={"Authorization": "Bearer test-sandbox-secret"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["received"]["task_id"] == "pr-x"

    def test_invalid_json_returns_400(self, client: TestClient):
        resp = client.post(
            "/internal/sandbox_complete",
            content="not json at all",
            headers={
                "Authorization": "Bearer test-sandbox-secret",
                "Content-Type": "application/json",
            },
        )
        assert resp.status_code == 400

    def test_non_object_payload_returns_400(self, client: TestClient):
        resp = client.post(
            "/internal/sandbox_complete",
            json=["not", "an", "object"],
            headers={"Authorization": "Bearer test-sandbox-secret"},
        )
        assert resp.status_code == 400
