"""Per-invocation request context, backed by a ContextVar.

Tool-level audit needs per-invocation context (tenant_id, user_id, channel_id,
invocation_id) available *inside* tool callables without threading them
through every signature. A `contextvars.ContextVar` is the right primitive:
it propagates across async/await boundaries, and when combined with
`contextvars.copy_context()` it also propagates into threads spawned via
`run_in_executor` / `threading.Thread(target=ctx.run, args=...)`.

Usage:
    # main.py entrypoint:
    set_context(tenant_id="acme", user_id="u1", ..., invocation_id="abc123")
    try:
        ... run agent ...
    finally:
        clear_context()

    # tools.py audit wrapper:
    ctx = get_context()
    ctx.get("tenant_id", "unknown")
"""
from __future__ import annotations

import contextvars
from typing import Any

# The stored shape is a plain dict so tools can access fields with .get()
# and never crash on missing keys (important for local dev / tests where
# set_context may not have been called).
_ctx: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "agentcore_request_context", default=None
)


def set_context(
    *,
    tenant_id: str,
    invocation_id: str,
    user_id: str = "",
    channel_id: str = "",
    thread_id: str = "",
    workspace_id: str = "",
    **extra: Any,
) -> None:
    """Populate the current invocation's context. Called once at the top of
    `@app.entrypoint` before the agent runs.

    Extra keyword arguments (e.g. ``escalation_routes``) are merged into
    the context dict so tools can read them via ``get_context()``.
    """
    ctx = {
        "tenant_id": tenant_id,
        "invocation_id": invocation_id,
        "user_id": user_id,
        "channel_id": channel_id,
        "thread_id": thread_id,
        "workspace_id": workspace_id,
    }
    ctx.update(extra)
    _ctx.set(ctx)


def get_context() -> dict[str, Any]:
    """Return the current context, or an empty dict if unset.

    Returning {} instead of raising keeps tool callables safe in contexts
    where no entrypoint has run (unit tests, direct function calls during
    local dev). Tool code should use `.get("tenant_id", "unknown")` to
    tolerate the empty case.
    """
    return _ctx.get() or {}


def merge_context(**extras: Any) -> None:
    """Merge additional fields into the current context dict.

    Used by ``main.py`` to layer post-``set_context`` state onto the
    invocation context without rebuilding the whole thing — for example,
    ``github_installation_id`` is resolved from the tenant config and
    merged in AFTER ``set_context`` has already been called, so the
    ``code_*`` catalog tools can read it via ``get_context()`` to mint
    installation tokens.

    No-op if the context hasn't been initialized — extras are dropped
    rather than implicitly creating a context, which would hide ordering
    bugs.
    """
    current = _ctx.get()
    if current is None:
        return
    current.update(extras)
    _ctx.set(current)


def clear_context() -> None:
    """Reset the context to None. Called in the entrypoint's `finally` block
    so the ContextVar doesn't leak across invocations in the same process."""
    _ctx.set(None)
