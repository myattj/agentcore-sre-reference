"""Custom @app.ping handler for the HealthyBusy heartbeat lifecycle.

AgentCore Runtime polls /ping to decide whether a session is idle. Returning
HealthyBusy keeps the session alive past the 15-minute idle timeout, which
is critical for long-running tool calls (e.g. "research X for 10 minutes").

Tools that spawn background work should:
  1. Call `register_task` (so this handler reports busy)
  2. Call `app.add_async_task(task_id)` (the AgentCore SDK side)
  3. On completion, call `complete_task` and `app.complete_async_task(task_id)`
"""
from __future__ import annotations

import threading

from bedrock_agentcore.runtime.models import PingStatus

from runtime import app

_inflight_tasks: dict[str, int] = {}
_tasks_lock = threading.Lock()


def register_task(task_id: str, busy_threshold: int = 1) -> None:
    """Track a task and the tenant threshold active when it was launched."""
    threshold = max(1, int(busy_threshold))
    with _tasks_lock:
        _inflight_tasks[task_id] = threshold


def complete_task(task_id: str) -> None:
    """Remove a task from heartbeat accounting. Safe to call repeatedly."""
    with _tasks_lock:
        _inflight_tasks.pop(task_id, None)


def reset_tasks_for_tests() -> None:
    with _tasks_lock:
        _inflight_tasks.clear()


@app.ping
def custom_ping() -> PingStatus:
    with _tasks_lock:
        task_count = len(_inflight_tasks)
        thresholds = tuple(_inflight_tasks.values())
    if any(task_count >= threshold for threshold in thresholds):
        return PingStatus.HEALTHY_BUSY
    return PingStatus.HEALTHY
