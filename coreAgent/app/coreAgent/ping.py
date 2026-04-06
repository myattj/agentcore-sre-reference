"""Custom @app.ping handler for the HealthyBusy heartbeat lifecycle.

AgentCore Runtime polls /ping to decide whether a session is idle. Returning
HealthyBusy keeps the session alive past the 15-minute idle timeout, which
is critical for long-running tool calls (e.g. "research X for 10 minutes").

Tools that spawn background work should:
  1. Add their task_id to `_inflight_tasks` (so this handler reports busy)
  2. Call `app.add_async_task(task_id)` (the AgentCore SDK side)
  3. On completion, call `_inflight_tasks.discard(task_id)` and
     `app.complete_async_task(task_id)`

The threshold (>0 in-flight = busy) is intentionally hardcoded for v0.
Phase 7+ will read it from the per-invocation tenant config.
"""
from bedrock_agentcore.runtime.models import PingStatus

from runtime import app

_inflight_tasks: set[str] = set()


@app.ping
def custom_ping() -> PingStatus:
    if len(_inflight_tasks) > 0:
        return PingStatus.HEALTHY_BUSY
    return PingStatus.HEALTHY
