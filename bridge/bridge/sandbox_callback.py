"""Bridge handler for ``POST /internal/sandbox_complete``.

Phase B: the Fargate sandbox container POSTs here when its PR-writing
work finishes (success or error). The bridge:

  1. Verifies the ``Authorization: Bearer <SANDBOX_CALLBACK_SECRET>``
     header via ``hmac.compare_digest`` — same shared secret is injected
     into both the bridge task def and the sandbox task def by
     ``infra/data/lib/sandbox-stack.ts`` (via the
     ``agentcore/services/sandbox`` Secrets Manager secret).
  2. Reads the matching row from the ``sandbox_jobs`` DDB table to find
     out which Slack thread the original ``propose_pr`` call came from.
  3. Posts the result message (PR link on success, error description
     on failure) to that thread via ``chat.postMessage``.

The sandbox ALSO writes the terminal status to ``sandbox_jobs`` directly
before calling this endpoint. The agent's ``propose_pr`` poller watches
that DDB row independently to clear HealthyBusy — the bridge callback is
purely for the user-facing Slack message. So a callback failure (network
blip, ALB 503) doesn't leak HealthyBusy on the agent.

Scope discipline: this module DOES NOT trust anything from the sandbox
payload except the ``task_id``. Everything else (tenant_id, channel_id,
thread_id, repo) comes from the DDB row, which was written by
``propose_pr`` BEFORE the sandbox launched. The sandbox can't fake a
post into a different tenant's thread by lying in the payload.

Test seams: ``get_sandbox_job`` and ``_post_slack_message`` are
module-level so tests can monkeypatch them. The DDB read reuses the
existing ``tenant_write._get_table`` lazy-init pattern.
"""
from __future__ import annotations

import hmac
import logging
import os
from typing import Any

from slack_sdk.web.async_client import AsyncWebClient

from . import slack_token_store
from .tenant_write import _get_table  # type: ignore[attr-defined]

log = logging.getLogger(__name__)


_SANDBOX_JOBS_TABLE_NAME = os.getenv("SANDBOX_JOBS_TABLE", "sandbox_jobs")
_BEARER_PREFIX = "Bearer "


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _callback_secret() -> str:
    """Return the shared callback secret. Raises ``RuntimeError`` if unset.

    The bridge task def gets ``SANDBOX_CALLBACK_SECRET`` injected at
    container startup via ``ecs.Secret.fromSecretsManager(sandboxSecret,
    'CALLBACK_SECRET')`` in services-stack.ts. The sandbox task def gets
    the SAME value injected from the SAME secret. Both sides hmac-compare
    the value passed in the ``Authorization: Bearer`` header.
    """
    secret = os.getenv("SANDBOX_CALLBACK_SECRET")
    if not secret:
        raise RuntimeError(
            "SANDBOX_CALLBACK_SECRET env var is required for the "
            "/internal/sandbox_complete callback. Set it via the "
            "agentcore/services/sandbox secret in Secrets Manager."
        )
    return secret


def verify_callback_auth(authorization_header: str | None) -> bool:
    """Verify a Bearer header against the callback secret.

    Constant-time compare via ``hmac.compare_digest``. Returns False on:
      - missing header
      - wrong scheme (anything other than "Bearer ")
      - empty token after the scheme
      - mismatched value

    Logging is debug-only — we don't want a malicious caller spamming
    INFO logs with bad-auth attempts.
    """
    if not authorization_header:
        return False
    if not authorization_header.startswith(_BEARER_PREFIX):
        return False
    presented = authorization_header[len(_BEARER_PREFIX):].strip()
    if not presented:
        return False
    try:
        expected = _callback_secret()
    except RuntimeError:
        log.error("verify_callback_auth: SANDBOX_CALLBACK_SECRET not set")
        return False
    return hmac.compare_digest(expected, presented)


# ---------------------------------------------------------------------------
# DDB read
# ---------------------------------------------------------------------------

def get_sandbox_job(task_id: str, region: str) -> dict[str, Any]:
    """Fetch a single row from ``sandbox_jobs`` by ``task_id``.

    Reuses the lazy ``_get_table`` from ``tenant_write`` so we don't
    construct a separate boto3 resource just for this read.

    Raises:
        KeyError: if no row exists for ``task_id``.
    """
    table = _get_table(region, _SANDBOX_JOBS_TABLE_NAME)
    response = table.get_item(Key={"task_id": task_id})
    item = response.get("Item")
    if not item:
        raise KeyError(f"No sandbox_jobs row for task_id={task_id!r}")
    return item


# ---------------------------------------------------------------------------
# Slack post
# ---------------------------------------------------------------------------

async def _post_slack_message(
    token: str,
    channel: str,
    thread_ts: str | None,
    text: str,
) -> bool:
    """Post a message to Slack. Returns True on success, False otherwise.

    Module-level (not nested inside the handler) so tests can
    monkeypatch ``bridge.sandbox_callback._post_slack_message`` with a
    fake that records its calls without going to Slack.
    """
    client = AsyncWebClient(token=token)
    try:
        response = await client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts or None,
            text=text,
        )
    except Exception:  # noqa: BLE001
        log.exception("sandbox_complete: chat.postMessage raised")
        return False
    if not response.get("ok"):
        log.warning(
            "sandbox_complete: chat.postMessage returned not-ok: %s",
            response.data if hasattr(response, "data") else response,
        )
        return False
    return True


# ---------------------------------------------------------------------------
# Message formatting
# ---------------------------------------------------------------------------

def format_completion_message(job: dict[str, Any], status: str, pr_url: str, error: str) -> str:
    """Build the user-facing Slack message for a sandbox completion.

    Mirrors the friendly tone of the existing agent replies.
    """
    repo = job.get("repo", "")
    task_description = job.get("task_description", "")
    if status == "success" and pr_url:
        head = f"Opened PR: {pr_url}"
        if task_description:
            return f"{head}\n_Task: {task_description}_"
        return head
    # Failure / orphaned / unknown
    repo_label = f" for `{repo}`" if repo else ""
    err_label = error or "unknown error"
    return f"Couldn't open a PR{repo_label}: {err_label}"


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

async def handle_sandbox_complete(payload: dict[str, Any]) -> dict[str, Any]:
    """Process a sandbox completion callback. Returns a JSON-serializable
    response dict for the FastAPI handler.

    Resilience: every step is best-effort. If we can't find the job row,
    we return ``{"ok": False, "error": ...}`` with a 200 status — the
    sandbox doesn't need to retry, the user just won't see a Slack
    message. Failed Slack posts are also non-fatal; the agent's poller
    has already cleared HealthyBusy by the time this runs.
    """
    task_id = payload.get("task_id") or ""
    if not task_id:
        return {"ok": False, "error": "missing task_id"}

    status = payload.get("status") or "unknown"
    pr_url = payload.get("pr_url") or ""
    error = payload.get("error") or ""

    region = os.getenv("AWS_REGION", "us-west-2")
    try:
        job = get_sandbox_job(task_id, region)
    except KeyError:
        log.warning("sandbox_complete: no job row for task_id=%s", task_id)
        return {"ok": False, "error": "unknown task_id"}
    except Exception as e:  # noqa: BLE001
        log.exception("sandbox_complete: ddb read failed for task_id=%s", task_id)
        return {"ok": False, "error": f"ddb read failed: {type(e).__name__}"}

    # Source of truth for routing comes from the DDB row, NOT the
    # sandbox payload. The sandbox can only nudge us via task_id; it
    # can't lie about whose thread we post into.
    tenant_id = job.get("tenant_id") or ""
    channel_id = job.get("slack_channel_id") or ""
    thread_id = job.get("slack_thread_id") or None

    if not tenant_id or not channel_id:
        log.warning(
            "sandbox_complete: row missing tenant_id/channel_id for task_id=%s",
            task_id,
        )
        return {"ok": False, "error": "row missing routing fields"}

    try:
        token = slack_token_store.get_bot_token(tenant_id)
    except KeyError:
        log.warning(
            "sandbox_complete: no Slack bot token for tenant_id=%s", tenant_id,
        )
        return {"ok": False, "error": "no Slack bot token"}

    if not token:
        # LOCAL_DEV with no SLACK_BOT_TOKEN set — log and return ok
        # so tests don't trip on the missing token.
        log.info(
            "sandbox_complete: empty bot token (local-dev?) for tenant_id=%s, "
            "skipping Slack post",
            tenant_id,
        )
        return {"ok": True, "posted": False}

    text = format_completion_message(job, status, pr_url, error)
    posted = await _post_slack_message(token, channel_id, thread_id, text)

    return {"ok": True, "posted": posted}
