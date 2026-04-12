"""Slack progress tracker for sandbox tasks.

Posts a Block Kit message on the first progress update, then updates it
in-place via ``chat.update`` as the sandbox moves through steps. The
completion callback (``sandbox_callback.py``) calls
``update_tracker_completion`` to show the final state on the same message.

Steps (in order):
  started → cloning → editing → pushing → opening_pr

Visual layout in Slack:

  ┌─────────────────────────────────────────────────┐
  │ 🔧  Opening PR in owner/repo                   │
  │                                                 │
  │ ✅ Started  ·  ✅ Cloning  ·  ⏳ Editing  ·    │
  │ ○ Pushing  ·  ○ Opening PR                      │
  │                                                 │
  │ ▓▓▓▓▓▓▓▓▓░░░░░░  60%                           │
  │                                                 │
  │ pr-XXXXXXXX · started 2 min ago                 │
  └─────────────────────────────────────────────────┘

DDB additions to sandbox_jobs rows (written by this module):
  - ``tracker_message_ts``: Slack message timestamp for chat.update
  - ``last_step``: most recently completed progress step name
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

from slack_sdk.web.async_client import AsyncWebClient

from . import slack_token_store
from .tenant_write import _get_table  # type: ignore[attr-defined]

log = logging.getLogger(__name__)

_SANDBOX_JOBS_TABLE_NAME = os.getenv("SANDBOX_JOBS_TABLE", "sandbox_jobs")

# Ordered list of sandbox milestones. Each name matches the ``step``
# value the sandbox POSTs to /internal/sandbox_progress.
STEPS = ["started", "cloning", "editing", "pushing", "opening_pr"]

_STEP_LABELS = {
    "started": "Started",
    "cloning": "Cloning",
    "editing": "Editing",
    "pushing": "Pushing",
    "opening_pr": "Opening PR",
}

_BAR_WIDTH = 15


# ---------------------------------------------------------------------------
# Block Kit builder
# ---------------------------------------------------------------------------

def _step_index(step: str) -> int:
    """Index of ``step`` in STEPS, or -1 if unknown."""
    try:
        return STEPS.index(step)
    except ValueError:
        return -1


def build_progress_blocks(
    repo: str,
    current_step: str,
    task_id: str,
    created_at: str = "",
    status: str = "in_progress",
    pr_url: str = "",
    error: str = "",
) -> list[dict[str, Any]]:
    """Build Block Kit blocks for the progress tracker.

    ``status`` is one of:
      - ``"in_progress"`` — sandbox is still running
      - ``"success"`` — PR opened
      - ``"error"`` — sandbox failed
    """
    current_idx = _step_index(current_step)

    # -- header --
    if status == "success":
        header_text = f"\u2705  PR opened in {repo}"
    elif status == "error":
        header_text = f"\u274c  PR failed in {repo}"
    else:
        header_text = f"\U0001f527  Opening PR in {repo}"

    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": header_text, "emoji": True},
        },
    ]

    # -- step indicators --
    parts: list[str] = []
    for i, step in enumerate(STEPS):
        label = _STEP_LABELS[step]
        if status == "success":
            icon = "\u2705"
        elif status == "error" and i == current_idx:
            icon = "\u274c"
        elif i < current_idx or (i == current_idx and status != "error"):
            icon = "\u2705"
        elif i == current_idx + 1 and status == "in_progress":
            icon = "\u23f3"
        else:
            icon = "\u25cb"
        parts.append(f"{icon} {label}")

    blocks.append({
        "type": "section",
        "text": {"type": "mrkdwn", "text": "  \u00b7  ".join(parts)},
    })

    # -- progress bar --
    if status == "success":
        filled = _BAR_WIDTH
        pct = 100
    else:
        filled = max(1, int((current_idx + 1) / len(STEPS) * _BAR_WIDTH))
        pct = int((current_idx + 1) / len(STEPS) * 100)
    empty = _BAR_WIDTH - filled
    bar = "\u2593" * filled + "\u2591" * empty

    if status == "success":
        bar_label = "Done"
    elif status == "error":
        bar_label = "Failed"
    else:
        bar_label = f"{pct}%"
    blocks.append({
        "type": "section",
        "text": {"type": "mrkdwn", "text": f"`{bar}`  {bar_label}"},
    })

    # -- PR link (success only) --
    if pr_url:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f":link: *<{pr_url}|View pull request>*"},
        })

    # -- error detail --
    if error:
        # Truncate long errors so the Slack message doesn't blow up.
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"```{error[:500]}```"},
        })

    # -- footer context --
    footer_parts = [f"`{task_id}`"]
    if created_at:
        try:
            created_dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            elapsed = datetime.now(timezone.utc) - created_dt
            minutes = int(elapsed.total_seconds() // 60)
            if status in ("success", "error"):
                if minutes < 1:
                    footer_parts.append("completed in <1 min")
                else:
                    footer_parts.append(f"completed in {minutes} min")
            else:
                if minutes < 1:
                    footer_parts.append("just started")
                elif minutes == 1:
                    footer_parts.append("started 1 min ago")
                else:
                    footer_parts.append(f"started {minutes} min ago")
        except (ValueError, TypeError):
            pass

    blocks.append({
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": " \u00b7 ".join(footer_parts)}],
    })

    return blocks


def _fallback_text(repo: str, step: str, status: str = "in_progress") -> str:
    """Plain-text fallback for mobile push notifications."""
    if status == "success":
        return f"PR opened in {repo}"
    if status == "error":
        return f"PR failed in {repo}"
    return f"Opening PR in {repo} \u2014 {_STEP_LABELS.get(step, step)}"


# ---------------------------------------------------------------------------
# Slack helpers
# ---------------------------------------------------------------------------

async def _post_tracker(
    token: str,
    channel: str,
    thread_ts: str | None,
    blocks: list[dict],
    text: str,
) -> str | None:
    """Post a new Block Kit message. Returns the ``message_ts`` or None."""
    client = AsyncWebClient(token=token)
    try:
        resp = await client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts or None,
            blocks=blocks,
            text=text,
        )
    except Exception:  # noqa: BLE001
        log.exception("sandbox_progress: chat.postMessage failed")
        return None
    if not resp.get("ok"):
        log.warning(
            "sandbox_progress: postMessage not ok: %s",
            resp.data if hasattr(resp, "data") else resp,
        )
        return None
    return resp.get("ts")


async def _update_tracker(
    token: str,
    channel: str,
    message_ts: str,
    blocks: list[dict],
    text: str,
) -> bool:
    """Update an existing Block Kit message. Returns True on success."""
    client = AsyncWebClient(token=token)
    try:
        resp = await client.chat_update(
            channel=channel,
            ts=message_ts,
            blocks=blocks,
            text=text,
        )
    except Exception:  # noqa: BLE001
        log.exception("sandbox_progress: chat.update failed")
        return False
    if not resp.get("ok"):
        log.warning(
            "sandbox_progress: chat_update not ok: %s",
            resp.data if hasattr(resp, "data") else resp,
        )
        return False
    return True


# ---------------------------------------------------------------------------
# DDB helpers
# ---------------------------------------------------------------------------

def _get_sandbox_job(task_id: str, region: str) -> dict[str, Any]:
    table = _get_table(region, _SANDBOX_JOBS_TABLE_NAME)
    resp = table.get_item(Key={"task_id": task_id})
    item = resp.get("Item")
    if not item:
        raise KeyError(f"No sandbox_jobs row for task_id={task_id!r}")
    return item


def _save_tracker_ts(task_id: str, region: str, message_ts: str, step: str) -> None:
    """Store the Slack message_ts and current step in the job row."""
    table = _get_table(region, _SANDBOX_JOBS_TABLE_NAME)
    table.update_item(
        Key={"task_id": task_id},
        UpdateExpression="SET tracker_message_ts = :ts, last_step = :step",
        ExpressionAttributeValues={":ts": message_ts, ":step": step},
    )


def _save_last_step(task_id: str, region: str, step: str) -> None:
    """Update just the last completed step (tracker_message_ts already set)."""
    table = _get_table(region, _SANDBOX_JOBS_TABLE_NAME)
    table.update_item(
        Key={"task_id": task_id},
        UpdateExpression="SET last_step = :step",
        ExpressionAttributeValues={":step": step},
    )


# ---------------------------------------------------------------------------
# Progress handler (called from bridge route)
# ---------------------------------------------------------------------------

async def handle_sandbox_progress(payload: dict[str, Any]) -> dict[str, Any]:
    """Process a sandbox progress update.

    Posts a new tracker message on the first call for a given task_id,
    then updates it in-place on subsequent calls.
    """
    task_id = payload.get("task_id") or ""
    step = payload.get("step") or ""

    if not task_id:
        return {"ok": False, "error": "missing task_id"}
    if not step:
        return {"ok": False, "error": "missing step"}

    region = os.getenv("AWS_REGION", "us-west-2")

    try:
        job = _get_sandbox_job(task_id, region)
    except KeyError:
        log.warning("sandbox_progress: no job row for task_id=%s", task_id)
        return {"ok": False, "error": "unknown task_id"}
    except Exception as e:  # noqa: BLE001
        log.exception("sandbox_progress: DDB read failed for %s", task_id)
        return {"ok": False, "error": f"ddb read failed: {type(e).__name__}"}

    tenant_id = job.get("tenant_id") or ""
    channel_id = job.get("slack_channel_id") or ""
    thread_id = job.get("slack_thread_id") or None
    repo = job.get("repo") or ""
    created_at = job.get("created_at") or ""
    tracker_ts = job.get("tracker_message_ts") or ""

    if not tenant_id or not channel_id:
        return {"ok": False, "error": "row missing routing fields"}

    try:
        token = slack_token_store.get_bot_token(tenant_id)
    except KeyError:
        return {"ok": False, "error": "no Slack bot token"}

    if not token:
        log.info("sandbox_progress: empty bot token for tenant %s, skipping", tenant_id)
        try:
            _save_last_step(task_id, region, step)
        except Exception:  # noqa: BLE001
            log.exception("sandbox_progress: failed to update last_step")
        return {"ok": True, "posted": False}

    blocks = build_progress_blocks(
        repo=repo,
        current_step=step,
        task_id=task_id,
        created_at=created_at,
    )
    text = _fallback_text(repo, step)

    if tracker_ts:
        # Update the existing tracker message.
        ok = await _update_tracker(token, channel_id, tracker_ts, blocks, text)
        try:
            _save_last_step(task_id, region, step)
        except Exception:  # noqa: BLE001
            log.exception("sandbox_progress: failed to update last_step")
        return {"ok": True, "updated": ok}

    # First progress call — post a new message and store the ts.
    new_ts = await _post_tracker(token, channel_id, thread_id, blocks, text)
    if new_ts:
        try:
            _save_tracker_ts(task_id, region, new_ts, step)
        except Exception:  # noqa: BLE001
            log.exception("sandbox_progress: failed to store tracker_message_ts")
    return {"ok": True, "posted": bool(new_ts)}


# ---------------------------------------------------------------------------
# Completion hook (called from sandbox_callback.py)
# ---------------------------------------------------------------------------

async def update_tracker_completion(
    job: dict[str, Any],
    status: str,
    pr_url: str = "",
    error: str = "",
) -> bool:
    """Update the tracker message with the final completion state.

    Called from ``sandbox_callback.handle_sandbox_complete`` when a
    ``tracker_message_ts`` exists on the job row. Returns True if the
    message was updated successfully, False otherwise.
    """
    tracker_ts = job.get("tracker_message_ts") or ""
    if not tracker_ts:
        return False

    tenant_id = job.get("tenant_id") or ""
    channel_id = job.get("slack_channel_id") or ""
    repo = job.get("repo") or ""
    created_at = job.get("created_at") or ""
    last_step = job.get("last_step") or STEPS[-1]
    task_id = job.get("task_id") or ""

    if not tenant_id or not channel_id:
        return False

    try:
        token = slack_token_store.get_bot_token(tenant_id)
    except KeyError:
        return False
    if not token:
        return False

    blocks = build_progress_blocks(
        repo=repo,
        current_step=last_step,
        task_id=task_id,
        created_at=created_at,
        status=status,
        pr_url=pr_url,
        error=error,
    )
    text = _fallback_text(repo, last_step, status)

    return await _update_tracker(token, channel_id, tracker_ts, blocks, text)
