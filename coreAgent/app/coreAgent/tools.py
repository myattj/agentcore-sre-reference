"""Catalog tool registry.

Platform-owned, in-process Python @tool functions. Adding a new catalog tool
requires an agent redeploy. Customers select from this catalog via
`tenant.catalog.allowed_tools` and pass per-tool config via
`tenant.catalog.tool_config`.

For BYO tools (customer-registered Lambda/API/MCP servers), see
`mcp_client/client.py` and the AgentCore Gateway integration.

## Audit model

Every catalog tool is wrapped at registration time with an audit shim that
writes a `row_type=tool_call` row to the AuditStore. The shim reads the
current invocation's context (tenant_id, user_id, channel_id, invocation_id)
from `request_context.get_context()` — which `main.py:invoke` sets at the
top of each call.

The `@audited_tool(name)` decorator replaces the old `@register(name)` +
`@tool` pair. The audit wrapper runs around `fn(*args, **kwargs)`, captures
duration, success/error, and truncated arg/result summaries, and writes
the row in a `finally:` block so audit logging is unaffected by exceptions.

**Critical ordering:** the audit wrapper must be inside Strands' `@tool`
decorator, not outside. `@tool` introspects the function signature to
generate the tool schema for the model; `functools.wraps` on the audit
wrapper preserves `__wrapped__`, `__name__`, `__doc__`, and the signature
so Strands sees the user-visible function, not the shim.
"""
from __future__ import annotations

import functools
import json
import logging
import threading
import time
import uuid
from typing import Any, Callable

from strands import tool

import ping
from audit import build_audit_store
from request_context import get_context
from runtime import app

log = logging.getLogger(__name__)

CATALOG: dict[str, Any] = {}

# Single module-level audit store. Respects LOCAL_AUDIT / AGENT_LOCAL_STORES env vars.
# Shared with main.py via a module-level import so both invocation-level
# and tool-level rows land in the same backend.
_audit = build_audit_store()


def register(name: str):
    """Lower-level primitive: add a callable to the catalog under a stable name.

    New tools should prefer `@audited_tool(name)` which layers audit logging
    on top of this. `register` remains for special cases (e.g. tools that
    cannot be audited for some reason, or tool-like objects that already
    have their own audit hook).
    """
    def deco(fn):
        CATALOG[name] = fn
        return fn
    return deco


def audited_tool(name: str):
    """Register a catalog tool with transparent audit logging.

    Replaces `@register(name)` + `@tool`. Applies Strands' `@tool` decorator
    to an audit-wrapped version of the function and stores it in CATALOG
    under `name`.

    Usage:
        @audited_tool("echo")
        def echo(text: str) -> str:
            '''Echo the input back.'''
            return text

    The docstring, parameter names, and type hints are preserved via
    `functools.wraps`, so Strands' schema generation sees the original
    signature.
    """
    def deco(fn: Callable[..., Any]) -> Any:
        @functools.wraps(fn)
        def audited(*args: Any, **kwargs: Any) -> Any:
            start = time.time()
            success = True
            result: Any = None
            error_text: str | None = None
            try:
                result = fn(*args, **kwargs)
                return result
            except Exception as e:  # noqa: BLE001 — we re-raise below
                success = False
                error_text = f"{type(e).__name__}: {e}"
                raise
            finally:
                # The audit store swallows its own exceptions; the outer
                # try/except is belt-and-braces for anything that might
                # blow up before the store is even called (e.g. repr()
                # on a pathological result).
                try:
                    ctx = get_context()
                    row: dict[str, Any] = {
                        "row_type": "tool_call",
                        "tenant_id": ctx.get("tenant_id", "unknown"),
                        "sk": _tool_call_sk(ctx.get("invocation_id", "")),
                        "invocation_id": ctx.get("invocation_id", ""),
                        "timestamp": _iso_now(),
                        "created_at": _iso_now(),
                        "user_id": ctx.get("user_id", ""),
                        "channel_id": ctx.get("channel_id", ""),
                        "tool_name": name,
                        "tool_args_summary": _summarize_args(args, kwargs),
                        "tool_result_summary": (
                            repr(result) if success else (error_text or "")
                        ),
                        "duration_ms": int((time.time() - start) * 1000),
                        "success": success,
                    }
                    _audit.write(row)
                except Exception as e:  # pragma: no cover
                    log.warning("audited_tool(%s): audit write dropped: %s", name, e)

        # Apply Strands' @tool AFTER the audit wrap so Strands sees the
        # audited callable and uses functools.wraps to pull the original
        # signature/docstring off `fn`.
        wrapped = tool(audited)
        CATALOG[name] = wrapped
        return wrapped

    return deco


def _iso_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _tool_call_sk(invocation_id: str) -> str:
    """Build the audit-table sort key for a tool_call row."""
    return f"TOOL#{_iso_now()}#{invocation_id}#{uuid.uuid4().hex[:8]}"


def _summarize_args(args: tuple, kwargs: dict) -> str:
    """JSON-encode positional + keyword args for the audit row. Uses
    `default=str` so non-serializable values (bytes, objects) don't blow up.
    The audit store truncates the result to 1 KB."""
    try:
        return json.dumps({"args": list(args), "kwargs": kwargs}, default=str)
    except Exception:
        return f"<unserializable args: {len(args)} positional, {len(kwargs)} kw>"


# ----------------------------------------------------------------------------
# Catalog tools
# ----------------------------------------------------------------------------

@audited_tool("echo")
def echo(text: str) -> str:
    """Echo the input text back to the user. Trivial sanity-check tool."""
    return text


@audited_tool("start_background_task")
def start_background_task(duration_seconds: int = 90) -> str:
    """Start a background task to demonstrate the HealthyBusy heartbeat lifecycle.

    Spawns a daemon thread that sleeps for `duration_seconds`. While the task
    is in flight, custom_ping returns HEALTHY_BUSY, which keeps the AgentCore
    Runtime session alive past the 15-minute idle timeout.

    Use duration_seconds >= 60 to actually observe HEALTHY_BUSY (a 3-second
    task finishes before you can query /ping).
    """
    task_id = f"bg-{uuid.uuid4().hex[:8]}"
    ping._inflight_tasks.add(task_id)
    app.add_async_task(task_id)

    def run():
        try:
            time.sleep(duration_seconds)
        finally:
            ping._inflight_tasks.discard(task_id)
            app.complete_async_task(task_id)

    threading.Thread(target=run, daemon=True).start()
    return (
        f"Started background task {task_id} for {duration_seconds}s. "
        f"Agent is now HealthyBusy."
    )


# ----------------------------------------------------------------------------
# Selection
# ----------------------------------------------------------------------------

def build_catalog_tools(
    allowed: list[str],
    tool_config: dict[str, dict[str, Any]] | None = None,
) -> list:
    """Filter the catalog by tenant whitelist.

    `tool_config` is forwarded for tools that need per-tenant config (creds,
    endpoints). v0 catalog tools don't read from it; later tools (e.g. a real
    `jira_lookup` or `query_customer_db`) will.
    """
    return [CATALOG[name] for name in allowed if name in CATALOG]
