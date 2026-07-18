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
import math
import os
import re
import threading
import time
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Callable, Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from strands import tool

import ping
from audit import build_audit_store
from metrics import build_metrics_emitter
from request_context import get_context
from runtime import app

log = logging.getLogger(__name__)

CATALOG: dict[str, Any] = {}

# Single module-level audit store. Respects LOCAL_AUDIT / AGENT_LOCAL_STORES env vars.
# Shared with main.py via a module-level import so both invocation-level
# and tool-level rows land in the same backend.
_audit = build_audit_store()

# Shared metrics emitter (EMF in production). Same factory singleton main.py
# uses, so tool-level and invocation-level EMF records land in the same
# CloudWatch Logs stream and roll up into the same Agent/Runtime namespace.
_metrics = build_metrics_emitter()


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


def audited_tool(
    name: str,
    *,
    args_summarizer: Callable[[tuple[Any, ...], dict[str, Any]], str] | None = None,
    result_summarizer: Callable[[Any], str] | None = None,
):
    """Register a catalog tool with transparent audit logging.

    Replaces `@register(name)` + `@tool`. Applies Strands' `@tool` decorator
    to an audit-wrapped version of the function and stores it in CATALOG
    under `name`.

    Usage:
        @audited_tool("echo")
        def echo(text: str) -> str:
            '''Echo the input back.'''
            return text

    Sensitive tools may provide summaries that omit raw arguments or bearer
    results. The docstring, parameter names, and type hints are preserved via
    `functools.wraps`, so Strands' schema generation sees the original signature.
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
                    _ch = ctx.get("channel_id", "")
                    _th = ctx.get("thread_id", "")
                    row: dict[str, Any] = {
                        "row_type": "tool_call",
                        "tenant_id": ctx.get("tenant_id", "unknown"),
                        "sk": _tool_call_sk(ctx.get("invocation_id", "")),
                        "invocation_id": ctx.get("invocation_id", ""),
                        "timestamp": _iso_now(),
                        "created_at": _iso_now(),
                        "user_id": ctx.get("user_id", ""),
                        "channel_id": _ch,
                        "tool_name": name,
                        "tool_args_summary": (
                            args_summarizer(args, kwargs)
                            if args_summarizer is not None
                            else _summarize_args(args, kwargs)
                        ),
                        "tool_result_summary": (
                            result_summarizer(result)
                            if success and result_summarizer is not None
                            else (repr(result) if success else (error_text or ""))
                        ),
                        "duration_ms": int((time.time() - start) * 1000),
                        "success": success,
                        "slack_message_link": (
                            f"https://slack.com/archives/{_ch}/p{_th.replace('.', '')}"
                            if _ch and _th
                            else ""
                        ),
                    }
                    _audit.write(row)
                except Exception as e:  # pragma: no cover
                    log.warning("audited_tool(%s): audit write dropped: %s", name, e)

                # CloudWatch metrics via EMF. Separate try so a metrics
                # failure can't suppress the audit row above. Read the
                # same ctx; `row`'s duration_ms is recomputed here rather
                # than reused because the audit try-block may have thrown
                # before building `row` at all.
                try:
                    _metrics.emit_tool_call(
                        tenant_id=get_context().get("tenant_id", "unknown"),
                        tool_name=name,
                        duration_ms=int((time.time() - start) * 1000),
                        success=success,
                        invocation_id=get_context().get("invocation_id", ""),
                    )
                except Exception as e:  # pragma: no cover
                    log.warning("audited_tool(%s): metrics emit dropped: %s", name, e)

        # Apply Strands' @tool AFTER the audit wrap so Strands sees the
        # audited callable and uses functools.wraps to pull the original
        # signature/docstring off `fn`.
        wrapped = tool(audited)
        CATALOG[name] = wrapped
        return wrapped

    return deco


def _iso_now() -> str:
    from datetime import datetime, timezone

    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


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


def filter_runtime_available_tools(allowed: list[str]) -> list[str]:
    """Remove tools whose deployment-level safety dependencies are absent."""
    dashboard_available = bool(os.getenv("DASHBOARD_BASE_URL", "").strip()) or (
        os.getenv("AGENT_LOCAL_STORES") == "1" or os.getenv("LOCAL_DEV") == "1"
    )
    experimental_sandbox_enabled = os.getenv("ENABLE_EXPERIMENTAL_PR_SANDBOX") == "1"

    unavailable: set[str] = set()
    if not dashboard_available:
        unavailable.add("render_dashboard")
    if not experimental_sandbox_enabled:
        unavailable.add("propose_pr")
    return [name for name in allowed if name not in unavailable]


# ----------------------------------------------------------------------------
# Sandbox helpers (Phase B — backing store for `propose_pr`)
# ----------------------------------------------------------------------------
#
# `propose_pr` fires a Fargate sandbox task that opens a PR in the tenant's
# repo. To do that, the tool needs:
#
#   - The sandbox task definition ARN, the cluster ARN, the subnet IDs,
#     and the security group IDs (so it can call ecs.run_task with
#     awsvpcConfiguration).
#   - A handle on the sandbox_jobs DDB table to write the job row before
#     launching the task.
#   - A handle on the boto3 ECS client to actually call run_task.
#
# These are all read lazily and cached at module level, mirroring the
# audit/metrics/spend pattern. The agent process hits SSM exactly once
# per cold start (the first propose_pr call), then never again for its
# lifetime. The DDB and ECS clients are also lazy.
#
# Resolution order for the four sandbox coordinates:
#   1. SSM Parameter Store at /agentcore/sandbox/* (production —
#      written by infra/data/lib/sandbox-stack.ts on cdk deploy)
#   2. AGENT_LOCAL_STORES=1 escape hatch via env vars
#      SANDBOX_TASK_DEF_ARN, SANDBOX_CLUSTER_ARN, SANDBOX_SUBNETS,
#      SANDBOX_SECURITY_GROUPS (local dev — operator hand-supplies them)
#
# In production, the agent's IAM role is granted ssm:GetParameter* on
# /agentcore/sandbox/* by the AgentCoreSandboxAccess managed policy
# (attached post-deploy via attach_agent_policy.sh).

_sandbox_coords_cache: dict[str, str] | None = None
_sandbox_coords_lock = threading.Lock()
_sandbox_jobs_table_singleton: Any | None = None
_ecs_client_singleton: Any | None = None

_SANDBOX_SSM_PREFIX = "/agentcore/sandbox/"
_SANDBOX_JOBS_TABLE_NAME = os.getenv("SANDBOX_JOBS_TABLE", "sandbox_jobs")
_GITHUB_REPO_SLUG_RE = re.compile(
    r"(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?)/"
    r"(?P<name>[A-Za-z0-9._-]{1,100})"
)


def _validate_github_repo_slug(repo: str) -> str:
    """Return a normalized, exact GitHub ``owner/name`` slug.

    This is duplicated in ``infra/sandbox/entrypoint.py`` because the agent
    and sandbox ship in separate runtimes. Keep both validators in sync.
    """
    normalized = (repo or "").strip()
    match = _GITHUB_REPO_SLUG_RE.fullmatch(normalized)
    if (
        match is None
        or "--" in match.group("owner")
        or match.group("name") in {".", ".."}
    ):
        raise ValueError("repo must be an exact GitHub owner/name slug")
    return normalized


def normalize_github_repo_slug(repo: str) -> str:
    """Return the canonical authorization key for a GitHub repository.

    GitHub owner and repository names are case-insensitive. Authorization
    therefore compares fully-qualified ``owner/name`` slugs after exact-shape
    validation and case folding; substrings and URL-like values never match.
    """
    return _validate_github_repo_slug(repo).casefold()


def _authorize_github_repo(
    repo: str,
    ctx: dict[str, Any] | None = None,
) -> tuple[str | None, str | None]:
    """Authorize an exact repo slug against the invocation's tenant allowlist.

    ``main.py`` populates ``allowed_github_repos`` from the tenant's configured
    codebase bindings on every invocation. Missing, malformed, or empty context
    denies access. The normalized repo is returned so downstream GitHub and
    sandbox calls use the same canonical value that was authorized.
    """
    try:
        normalized = normalize_github_repo_slug(repo)
    except ValueError:
        return None, (
            "Error: repo must be an exact GitHub owner/name slug from the "
            "## Connected codebases list (for example, 'acme/api')."
        )

    request_ctx = ctx if ctx is not None else get_context()
    raw_allowed = request_ctx.get("allowed_github_repos")
    if not isinstance(raw_allowed, (list, tuple, set, frozenset)):
        return None, "Error: this repository is not authorized for this tenant."

    allowed: set[str] = set()
    for configured_repo in raw_allowed:
        if not isinstance(configured_repo, str):
            continue
        try:
            allowed.add(normalize_github_repo_slug(configured_repo))
        except ValueError:
            continue

    if normalized not in allowed:
        return None, "Error: this repository is not authorized for this tenant."
    return normalized, None


def _validate_run_task_response(response: Any) -> str:
    """Require ECS to report exactly one launched task and no failures."""
    if not isinstance(response, dict):
        raise RuntimeError("ecs.run_task returned a non-object response")

    tasks = response.get("tasks")
    failures = response.get("failures")
    if not isinstance(tasks, list) or not isinstance(failures, list):
        raise RuntimeError("ecs.run_task response is missing tasks or failures")
    if failures or len(tasks) != 1:
        reasons = ", ".join(
            str(failure.get("reason", "unknown"))
            for failure in failures
            if isinstance(failure, dict)
        )
        detail = f"; reasons: {reasons}" if reasons else ""
        raise RuntimeError(
            "ecs.run_task launched "
            f"{len(tasks)} task(s) and returned {len(failures)} failure(s){detail}"
        )

    task = tasks[0]
    if not isinstance(task, dict) or not task.get("taskArn"):
        raise RuntimeError("ecs.run_task response task is missing taskArn")
    return str(task["taskArn"])


def _load_sandbox_coords() -> dict[str, str]:
    """Return the four sandbox coordinates as a dict, cached for the
    process lifetime.

    Keys: ``task_def_arn``, ``cluster_arn``, ``subnets``,
    ``security_groups``. Values are plain strings (subnets and
    security_groups are comma-joined IDs that callers .split(",") on).

    First call hits SSM (one GetParametersByPath round-trip, ~50ms);
    subsequent calls return the cached dict. The lock guards the
    initial population — concurrent first-call requests serialize
    behind it but read-only callers don't acquire it.
    """
    global _sandbox_coords_cache
    if _sandbox_coords_cache is not None:
        return _sandbox_coords_cache

    with _sandbox_coords_lock:
        if _sandbox_coords_cache is not None:
            return _sandbox_coords_cache

        # Local-dev escape hatch
        if os.getenv("AGENT_LOCAL_STORES") == "1":
            cache = {
                "task_def_arn": os.getenv("SANDBOX_TASK_DEF_ARN", ""),
                "cluster_arn": os.getenv("SANDBOX_CLUSTER_ARN", ""),
                "subnets": os.getenv("SANDBOX_SUBNETS", ""),
                "security_groups": os.getenv("SANDBOX_SECURITY_GROUPS", ""),
            }
            _sandbox_coords_cache = cache
            return cache

        import boto3

        ssm = boto3.client("ssm", region_name=os.getenv("AWS_REGION", "us-west-2"))
        try:
            resp = ssm.get_parameters_by_path(Path=_SANDBOX_SSM_PREFIX, Recursive=False)
        except Exception as e:  # noqa: BLE001
            log.exception("sandbox: SSM get_parameters_by_path failed")
            raise RuntimeError(
                f"could not load sandbox coordinates from SSM "
                f"({_SANDBOX_SSM_PREFIX}): {e}"
            ) from e

        cache = {}
        for param in resp.get("Parameters", []):
            name = param.get("Name", "")
            key = (
                name[len(_SANDBOX_SSM_PREFIX) :]
                if name.startswith(_SANDBOX_SSM_PREFIX)
                else name
            )
            cache[key] = param.get("Value", "")

        required = {"task_def_arn", "cluster_arn", "subnets", "security_groups"}
        missing = required - cache.keys()
        if missing:
            raise RuntimeError(
                f"sandbox SSM params incomplete: missing {sorted(missing)}. "
                f"Did `bash infra/data/scripts/deploy_sandbox.sh` run successfully?"
            )

        _sandbox_coords_cache = cache
        return cache


def _sandbox_jobs_table() -> Any:
    """Lazy boto3 DynamoDB Table handle for sandbox_jobs. Cached
    at module level so we don't pay the resource-construction cost
    on every propose_pr call."""
    global _sandbox_jobs_table_singleton
    if _sandbox_jobs_table_singleton is None:
        import boto3

        resource = boto3.resource(
            "dynamodb", region_name=os.getenv("AWS_REGION", "us-west-2")
        )
        _sandbox_jobs_table_singleton = resource.Table(_SANDBOX_JOBS_TABLE_NAME)
    return _sandbox_jobs_table_singleton


def _ecs_client() -> Any:
    """Lazy boto3 ECS client. First call to propose_pr triggers the
    boto3 import; subsequent calls reuse it."""
    global _ecs_client_singleton
    if _ecs_client_singleton is None:
        import boto3

        _ecs_client_singleton = boto3.client(
            "ecs", region_name=os.getenv("AWS_REGION", "us-west-2")
        )
    return _ecs_client_singleton


def _propose_pr_sk(task_id: str, event: str) -> str:
    """Build the audit-table sort key for a propose_pr row.
    Format: ``PR#{iso_ts}#{task_id}#{event}`` so the launched and
    completed rows for the same task_id sort together by time."""
    return f"PR#{_iso_now()}#{task_id}#{event}"


def _write_propose_pr_audit(
    *,
    task_id: str,
    event: str,
    status: str,
    repo: str,
    pr_url: str = "",
    error: str = "",
    ctx_snapshot: dict[str, Any] | None = None,
) -> None:
    """Write a `row_type=propose_pr` audit row. NEVER raises (per gotcha
    #10 — audit writes must not break the caller).

    `ctx_snapshot` is the request_context dict captured at tool-call
    time. The poller daemon (which runs after the entrypoint has cleared
    the ContextVar) MUST pass a snapshot here; the synchronous launch
    site can read get_context() directly. We accept either via this
    common helper.
    """
    try:
        ctx = ctx_snapshot if ctx_snapshot is not None else get_context()
        row: dict[str, Any] = {
            "row_type": "propose_pr",
            "tenant_id": ctx.get("tenant_id", "unknown"),
            "sk": _propose_pr_sk(task_id, event),
            "task_id": task_id,
            "event": event,
            "invocation_id": ctx.get("invocation_id", ""),
            "timestamp": _iso_now(),
            "created_at": _iso_now(),
            "user_id": ctx.get("user_id", ""),
            "channel_id": ctx.get("channel_id", ""),
            "thread_id": ctx.get("thread_id", ""),
            "repo": repo,
            "status": status,
        }
        if pr_url:
            row["pr_url"] = pr_url
        if error:
            row["error"] = error
        _audit.write(row)
    except Exception as e:  # pragma: no cover — gotcha #10
        log.warning(
            "propose_pr audit row dropped (event=%s task=%s): %s", event, task_id, e
        )


def _heartbeat_max_background_seconds(ctx: dict[str, Any]) -> int:
    """Return the tenant's positive background-work tracking ceiling."""
    try:
        configured = int(ctx.get("heartbeat_max_background_seconds", 3600) or 3600)
    except (TypeError, ValueError):
        configured = 3600
    return max(1, configured)


def _poll_sandbox_completion(
    task_id: str,
    repo: str,
    ctx_snapshot: dict[str, Any],
    max_background_seconds: int,
) -> None:
    """Daemon: poll sandbox_jobs until terminal status, then write the
    completion audit row and clear HealthyBusy.

    Bridge callback (`/internal/sandbox_complete`) is the path that
    posts the result to Slack — but it's INFORMATIONAL, not load-bearing
    for the agent's lifecycle. The agent observes completion via this
    DDB poll independently, so a callback failure (network blip, ALB
    503) doesn't leak HealthyBusy.

    Bounded backoff: 5s → 7s → ~10s → ... → cap at 20s, with
    total wall time bounded by the tenant's heartbeat background-work ceiling.
    The hard ceiling is critical:
    if a Fargate task crashes mid-flight without writing a terminal
    row, we DO NOT want to leak HealthyBusy forever and pin the agent
    container alive past its natural idle shutdown. The orphan branch
    marks the row and clears the inflight set so the next ping returns
    HEALTHY instead of HEALTHY_BUSY.

    `repo` and `ctx_snapshot` are passed in (rather than re-read from
    request_context) because this daemon runs AFTER the entrypoint has
    cleared the ContextVar — get_context() returns empty here.

    Cost: ~60 DDB GetItems per active PR (well under $0.001 at
    on-demand pricing). Not a real cost concern.
    """
    backoff = 5.0
    ceiling_seconds = max(1, int(max_background_seconds))
    deadline = time.monotonic() + ceiling_seconds
    final_status = "orphaned"
    final_pr_url = ""
    final_error = ""
    try:
        while time.monotonic() < deadline:
            remaining = max(0.0, deadline - time.monotonic())
            time.sleep(min(backoff, remaining))
            if time.monotonic() >= deadline:
                continue
            backoff = min(backoff * 1.4, 20.0)
            try:
                row = (
                    _sandbox_jobs_table().get_item(Key={"task_id": task_id}).get("Item")
                    or {}
                )
            except Exception:  # noqa: BLE001 — transient DDB error, retry
                continue
            status = row.get("status", "")
            if status in ("success", "error"):
                final_status = status
                final_pr_url = row.get("pr_url", "")
                final_error = row.get("error", "")
                log.info(
                    "sandbox poll: task_id=%s reached terminal=%s", task_id, status
                )
                # Record sandbox API spend against the tenant's monthly
                # counter. The sandbox writes agent_cost_cents to the row
                # on completion — we read it here and charge via the same
                # spend_tracker the main invocation path uses.
                cost_cents = int(row.get("agent_cost_cents", 0) or 0)
                tenant_id = ctx_snapshot.get("tenant_id", "")
                if cost_cents > 0 and tenant_id:
                    try:
                        from spend_tracker import build_spend_tracker

                        build_spend_tracker().record_spend(tenant_id, cost_cents)
                        log.info(
                            "sandbox poll: recorded %d cents sandbox spend for tenant=%s",
                            cost_cents,
                            tenant_id,
                        )
                    except Exception:  # noqa: BLE001
                        log.exception("sandbox poll: failed to record sandbox spend")
                break
        else:
            # Loop exited via the deadline (no break). Mark orphaned.
            log.warning(
                "sandbox poll: task_id=%s exceeded %d-second ceiling — marking orphaned",
                task_id,
                ceiling_seconds,
            )
            final_status = "orphaned"
            final_error = f"exceeded {ceiling_seconds}-second background-work ceiling"
            try:
                _sandbox_jobs_table().update_item(
                    Key={"task_id": task_id},
                    UpdateExpression="SET #s = :s, #c = :c, #e = :e",
                    ExpressionAttributeNames={
                        "#s": "status",
                        "#c": "completed_at",
                        "#e": "error",
                    },
                    ExpressionAttributeValues={
                        ":s": "orphaned",
                        ":c": _iso_now(),
                        ":e": final_error,
                    },
                )
            except Exception:  # noqa: BLE001
                log.exception(
                    "sandbox poll: failed to mark task_id=%s orphaned", task_id
                )
    finally:
        # Always: write the terminal audit row + clear HealthyBusy.
        _write_propose_pr_audit(
            task_id=task_id,
            event="completed",
            status=final_status,
            repo=repo,
            pr_url=final_pr_url,
            error=final_error,
            ctx_snapshot=ctx_snapshot,
        )
        ping.complete_task(task_id)
        app.complete_async_task(task_id)


# ----------------------------------------------------------------------------
# Catalog tools
# ----------------------------------------------------------------------------


def _register_async_task(task_id: str, busy_threshold: int) -> None:
    """Register local and AgentCore async state as one failure-safe operation."""
    ping.register_task(task_id, busy_threshold)
    try:
        app.add_async_task(task_id)
    except Exception:
        ping.complete_task(task_id)
        # The SDK may have accepted the task before surfacing an error. A
        # best-effort completion keeps that partial registration from leaking.
        try:
            app.complete_async_task(task_id)
        except Exception:  # noqa: BLE001
            log.exception("async task rollback failed for task_id=%s", task_id)
        raise


@audited_tool("echo")
def echo(text: str) -> str:
    """Echo the input text back to the user. Trivial sanity-check tool."""
    return text


@audited_tool("start_background_task")
def start_background_task(duration_seconds: int = 90) -> str:
    """Start a background task to demonstrate the HealthyBusy heartbeat lifecycle.

    Spawns a daemon thread that sleeps for `duration_seconds`. Once the
    tenant's configured concurrency threshold is reached, custom_ping returns
    HEALTHY_BUSY, which keeps the AgentCore Runtime session alive past the
    15-minute idle timeout.

    Use duration_seconds >= 60 to actually observe HEALTHY_BUSY (a 3-second
    task finishes before you can query /ping).
    """
    ctx = get_context()
    busy_threshold = max(1, int(ctx.get("heartbeat_busy_threshold", 1) or 1))
    max_seconds = _heartbeat_max_background_seconds(ctx)
    requested_seconds = max(1, int(duration_seconds))
    effective_seconds = min(requested_seconds, max_seconds)

    task_id = f"bg-{uuid.uuid4().hex[:8]}"
    _register_async_task(task_id, busy_threshold)

    def run():
        try:
            time.sleep(effective_seconds)
        finally:
            ping.complete_task(task_id)
            app.complete_async_task(task_id)

    threading.Thread(target=run, daemon=True).start()
    return (
        f"Started background task {task_id} for {effective_seconds}s. "
        "The runtime heartbeat reports HealthyBusy whenever at least "
        f"{busy_threshold} background tasks are active."
    )


def _authorize_slack_channel_access(
    token: str,
    target_channel_id: str,
    ctx: dict[str, Any] | None = None,
) -> str | None:
    """Authorize a current- or cross-channel Slack tool operation.

    A signed event proves the requester is operating in the current channel,
    so that channel is allowed without an extra API round trip. Any other
    channel requires a positive ``conversations.members`` result for the exact
    requesting Slack user. Missing context and indeterminate membership deny.
    Escalation routes intentionally bypass this helper and remain a separate,
    narrowly configured authorization mechanism.
    """
    import slack_api

    request_ctx = ctx if ctx is not None else get_context()
    tenant_id = str(request_ctx.get("tenant_id") or "").strip()
    current_channel_id = str(request_ctx.get("channel_id") or "").strip()
    requester_user_id = str(request_ctx.get("user_id") or "").strip()
    target_channel_id = (target_channel_id or "").strip()

    if not tenant_id or not current_channel_id or not target_channel_id:
        return (
            "Error: Slack channel authorization context is unavailable; "
            "the operation was denied."
        )
    if target_channel_id == current_channel_id:
        return None
    if not requester_user_id:
        return (
            "Error: Slack requester identity is unavailable; cross-channel "
            "access was denied."
        )

    membership = slack_api.is_user_member_of_channel(
        token,
        target_channel_id,
        requester_user_id,
    )
    if membership is not True:
        return (
            "Error: you must be a member of the target Slack channel "
            "before the agent can access or post to it."
        )
    return None


@audited_tool("search_team_history")
def search_team_history(
    query: str, channel_id: str | None = None, limit: int = 20
) -> str:
    """Search past Slack messages in a channel for messages matching a keyword query.

    Args:
        query: Keywords to search for in message text.
        channel_id: Slack channel ID to search. If omitted, uses the current channel.
        limit: Maximum number of messages to return (default 20, max 100).
    """
    from slack_api import fetch_channel_history, get_bot_token

    ctx = get_context()
    cid = (channel_id or ctx.get("channel_id", "") or "").strip()
    if not cid:
        return "Error: no channel_id available. Pass channel_id explicitly."
    tenant_id = ctx.get("tenant_id", "")
    token = get_bot_token(tenant_id)
    if not token:
        return "Error: no Slack bot token configured for this tenant."
    authorization_error = _authorize_slack_channel_access(token, cid, ctx)
    if authorization_error:
        return authorization_error
    return fetch_channel_history(token, cid, query, min(limit, 100))


@audited_tool("read_thread_context")
def read_thread_context(
    channel_id: str | None = None, thread_id: str | None = None
) -> str:
    """Fetch the full conversation thread from Slack.

    Call this whenever the user references "this thread" or "this conversation"
    to get the full context before responding.

    Args:
        channel_id: Slack channel ID. If omitted, uses the current channel.
        thread_id: Slack thread timestamp. If omitted, uses the current thread.
    """
    from slack_api import fetch_thread_replies, get_bot_token

    ctx = get_context()
    cid = (channel_id or ctx.get("channel_id", "") or "").strip()
    tid = thread_id or ctx.get("thread_id", "")
    if not cid or not tid:
        return "Error: both channel_id and thread_id are required."
    tenant_id = ctx.get("tenant_id", "")
    token = get_bot_token(tenant_id)
    if not token:
        return "Error: no Slack bot token configured for this tenant."
    authorization_error = _authorize_slack_channel_access(token, cid, ctx)
    if authorization_error:
        return authorization_error
    return fetch_thread_replies(token, cid, tid)


@audited_tool("post_to_channel")
def post_to_channel(channel_id: str, message: str, thread_ts: str | None = None) -> str:
    """Post in the current channel or a channel the requester belongs to.

    Use this when you need to send information to a different channel than
    the one you're currently in (e.g., posting a summary to #incidents,
    sending a notification to #team-updates, cross-posting an update).

    Args:
        channel_id: The Slack channel ID to post to (e.g. C0123456789).
        message: The message text to post. Supports Slack mrkdwn formatting.
        thread_ts: Optional thread timestamp to reply in a specific thread.
    """
    import slack_api

    ctx = get_context()
    token = slack_api.get_bot_token(ctx.get("tenant_id", ""))
    if not token:
        return "Error: no Slack bot token configured for this tenant."
    cid = (channel_id or "").strip()
    authorization_error = _authorize_slack_channel_access(token, cid, ctx)
    if authorization_error:
        return authorization_error
    return slack_api.post_message(token, cid, message, thread_ts)


@audited_tool("escalate")
def escalate(team_name: str, summary: str, severity: str = "medium") -> str:
    """Escalate an issue to a specific team using the configured routing table.

    Looks up the team in the tenant's escalation routing config, posts a
    formatted escalation message to their channel, and @-mentions the
    configured contacts.

    Args:
        team_name: The logical team name to escalate to (e.g. "sre", "data-eng", "security").
        summary: A concise summary of the issue being escalated, including what was checked and what evidence was gathered.
        severity: One of "low", "medium", "high", "critical". Determines formatting urgency.
    """
    import slack_api

    ctx = get_context()
    routes = ctx.get("escalation_routes", [])
    if not routes:
        return "No escalation routing table configured for this tenant."

    # Find the matching route (case-insensitive)
    route = next(
        (r for r in routes if r["team_name"].lower() == team_name.lower()),
        None,
    )
    if not route:
        available = [r["team_name"] for r in routes]
        return f"No escalation route for '{team_name}'. Available teams: {available}"

    # Build the escalation message
    mentions = " ".join(f"<@{uid}>" for uid in route.get("contacts", []))
    parts = [f"*[{severity.upper()}] Escalation*"]
    if mentions:
        parts.append(mentions)
    if route.get("description"):
        parts.append(f"_Routing to: {route['description']}_")
    parts.append(f"\n{summary}")

    msg = "\n".join(parts)
    token = slack_api.get_bot_token(ctx.get("tenant_id", ""))
    if not token:
        return "Error: no Slack bot token configured for this tenant."
    return slack_api.post_message(token, route["channel_id"], msg)


@audited_tool("record_feedback")
def record_feedback(
    sentiment: str,
    reason: str,
    original_question: str = "",
    original_answer: str = "",
) -> str:
    """Record feedback on a previous answer from this conversation.

    Call this when the user's message contains a clear signal about the quality
    of a prior answer — corrections ("that's wrong", "actually it was X"),
    re-asks that imply the answer missed the mark, or explicit praise
    ("perfect", "thanks, exactly what I needed").

    Do NOT call for routine conversation flow ("ok", "got it", "next question").

    Args:
        sentiment: "positive" or "negative".
        reason: Brief explanation of what the user indicated — e.g. "user corrected
                root cause: DNS change, not deployment" or "user confirmed the
                runbook steps were correct".
        original_question: The user's original question that led to the answer
                           being evaluated (abbreviated if long).
        original_answer: The bot's answer that the user is reacting to
                         (abbreviated if long).
    """
    if sentiment not in ("positive", "negative"):
        return f"Error: sentiment must be 'positive' or 'negative', got '{sentiment}'."

    ctx = get_context()
    tenant_id = ctx.get("tenant_id", "unknown")
    invocation_id = ctx.get("invocation_id", "")

    # Write a feedback audit row (structured, queryable).
    # The audited_tool wrapper already writes a tool_call row; this
    # additional row uses row_type="feedback" for the dedicated feedback
    # schema so it can be queried independently of tool_call rows.
    from datetime import datetime, timezone

    now = (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
    try:
        _audit.write(
            {
                "row_type": "feedback",
                "tenant_id": tenant_id,
                "sk": f"FB#{now}#{invocation_id}",
                "invocation_id": invocation_id,
                "timestamp": now,
                "created_at": now,
                "user_id": ctx.get("user_id", ""),
                "channel_id": ctx.get("channel_id", ""),
                "thread_id": ctx.get("thread_id", ""),
                "workspace_id": ctx.get("workspace_id", ""),
                "reaction": "",
                "sentiment": sentiment,
                "source": "conversation",
                "reason": reason[:512],
                "bot_message_ts": "",
                "question_summary": original_question[:512],
                "answer_summary": original_answer[:512],
            }
        )
    except Exception as e:
        log.warning("record_feedback: audit write dropped: %s", e)

    # Write a memory record so the feedback is retrievable in future
    # conversations. For production, the Strands session manager captures
    # the full conversation turn (including this tool call) and the
    # SEMANTIC strategy indexes it. This explicit write supplements that
    # with a structured record for local dev (InMemoryStore).
    try:
        from memory_store import build_memory_store

        store = build_memory_store()
        namespace = str(ctx.get("memory_namespace") or "")
        if not namespace:
            # Direct calls without an initialized invocation context must not
            # silently fall back to a tenant-wide memory namespace.
            raise RuntimeError("memory authorization context is unavailable")
        store.write_records(
            namespace,
            [
                {
                    "type": "user_feedback",
                    "sentiment": sentiment,
                    "reason": reason,
                    "question": original_question,
                    "answer": original_answer,
                    "extracted_via": "record_feedback_tool_v0",
                }
            ],
        )
    except Exception as e:
        log.warning("record_feedback: memory write dropped: %s", e)

    return "Feedback recorded."


@audited_tool("ask_codebase_choice")
def ask_codebase_choice(candidates: list[str]) -> str:
    """Offer a pick-one Slack button UI for choosing between codebases.

    This is a UX affordance, not a fallback for uncertainty. Reach for
    it only when you've reasoned about the message and the Connected
    codebases list and you genuinely cannot tell which repo is meant —
    AND there are two or three plausible candidates a human could pick
    between in one click. If you already have a reasonable pick, just
    use it. If a tool returned an error, reason about whether that's
    a repo-choice problem or a backend problem before re-asking.

    When you do ask, a short one-line prose intro first is fine
    ("Quick check — which one?") so the thread reads naturally even if
    Slack fails to render the buttons.

    Args:
        candidates: List of ``owner/name`` repo slugs to offer as
                    buttons, ordered by preference (best guess first).
                    Slack's action block supports up to 5 buttons;
                    extras are trimmed.
    """
    import slack_api

    ctx = get_context()
    tenant_id = ctx.get("tenant_id", "")
    channel_id = ctx.get("channel_id", "")
    thread_ts = ctx.get("thread_id", "")

    if not candidates:
        return "Error: no candidates provided. Pass at least one repo slug."
    if not tenant_id or not channel_id:
        return (
            "Error: no Slack context available (missing tenant_id or "
            "channel_id). Ask the user in plain text instead."
        )

    token = slack_api.get_bot_token(tenant_id)
    if not token:
        return "Error: no Slack bot token configured for this tenant."

    # Slack caps actions at 5 per block and requires unique action_ids
    # within a block. Encode the index in the action_id so the bridge
    # handler can dispatch on the "codebase_pick:" prefix and pull the
    # repo out of the button's `value`.
    trimmed = list(candidates)[:5]
    buttons = [
        {
            "type": "button",
            "text": {"type": "plain_text", "text": repo, "emoji": True},
            "value": repo,
            "action_id": f"codebase_pick:{i}",
        }
        for i, repo in enumerate(trimmed)
    ]
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "*Which codebase should I check?*",
            },
        },
        {
            "type": "actions",
            "block_id": "codebase_choice",
            "elements": buttons,
        },
    ]

    fallback_text = "Which codebase should I check? " + ", ".join(trimmed)
    result = slack_api.post_message(
        token=token,
        channel_id=channel_id,
        text=fallback_text,
        thread_ts=thread_ts or None,
        blocks=blocks,
    )
    if "Failed" in result:
        return (
            f"Could not post codebase-choice buttons: {result}. "
            "Ask the user in plain text instead."
        )
    return (
        f"Posted codebase-choice buttons to the thread for "
        f"{len(trimmed)} candidate(s). Wait for the user to click; "
        "the bridge will re-invoke you with their pick."
    )


@audited_tool("inspect_codebase_context")
def inspect_codebase_context() -> str:
    """Gather extra signals for choosing a codebase when the Connected
    codebases block + thread context aren't enough to reason from.

    This tool does NOT make a decision — it returns labeled signals and
    you reason about them. Reach for it when you genuinely need more
    context before picking, not as a reflex whenever you're slightly
    uncertain. Most messages already have enough signal in the thread
    and the system prompt's Connected codebases list.

    Returns a markdown block with these sections (each best-effort —
    any section that can't be populated is labeled "no signal"):

      1. **Connected codebases** — full list with aliases, default
         branch, and channel pins. Channel pins are the primary
         team-ownership signal: a repo pinned to the current channel
         usually means that team owns it.
      2. **This channel** — Slack metadata: name, topic, purpose.
         Useful when the topic/purpose names a system or team
         explicitly ("On-call for payments platform").
      3. **This user** — Slack profile: display name, real name, job
         title. Useful when the title points at a specific area
         ("Staff SRE" → likely infra repos).
      4. **Memory hint** — a repo slug from AgentCore Memory's
         SEMANTIC namespace for this scope, when there's a confident
         match. This is "what got used here recently."

    Takes 1-2 Slack API calls + one memory query (~500ms total). No
    arguments — reads the current invocation ctx for tenant, channel,
    and user.
    """
    import slack_api
    from codebase_memory import retrieve_codebase_affinity_hint
    from tenant import load_tenant_config

    ctx = get_context()
    tenant_id = ctx.get("tenant_id", "")
    channel_id = ctx.get("channel_id", "") or ""
    user_id = ctx.get("user_id", "") or ""

    if not tenant_id:
        return "Error: no tenant_id in request context."

    # Load the tenant config so we can show bindings + use the memory
    # allow_learning flag. If this fails, we degrade by only returning
    # the Slack signals.
    codebases = None
    try:
        config = load_tenant_config(tenant_id)
        codebases = config.codebases
    except Exception as e:  # noqa: BLE001
        log.warning("inspect_codebase_context: load_tenant_config failed: %s", e)

    sections: list[str] = ["## Codebase context inspection"]

    # --- Section 1: Connected codebases with channel-pin highlight ---
    if codebases is not None and codebases.bindings:
        lines = ["### Connected codebases"]
        for b in codebases.bindings:
            parts = [f"- `{b.repo}` (branch: `{b.default_branch}`)"]
            if b.aliases:
                aliases = ", ".join(repr(a) for a in b.aliases)
                parts.append(f" — aliases: {aliases}")
            if b.channels:
                pinned = ", ".join(b.channels)
                if channel_id and channel_id in b.channels:
                    parts.append(f" — **pinned to THIS channel** ({pinned})")
                else:
                    parts.append(f" — pinned channels: {pinned}")
            else:
                parts.append(" — no channel pins")
            lines.append("".join(parts))
        if codebases.default_repo:
            lines.append(f"\nInstall-time default: `{codebases.default_repo}`")
        sections.append("\n".join(lines))
    else:
        sections.append(
            "### Connected codebases\nno signal — tenant has no "
            "codebase bindings configured"
        )

    # --- Slack signals: one token fetch, reuse for channel + user ---
    token = slack_api.get_bot_token(tenant_id)

    # --- Section 2: Channel metadata ---
    channel_section = ["### This channel"]
    if token and channel_id:
        info = slack_api.get_channel_info(token, channel_id)
        if info:
            name = info.get("name") or ""
            topic = (info.get("topic") or {}).get("value") or ""
            purpose = (info.get("purpose") or {}).get("value") or ""
            is_private = info.get("is_private", False)
            channel_section.append(f"- Name: #{name}" if name else "- Name: (unknown)")
            channel_section.append(f"- Type: {'private' if is_private else 'public'}")
            channel_section.append(
                f"- Topic: {topic}" if topic else "- Topic: (not set)"
            )
            channel_section.append(
                f"- Purpose: {purpose}" if purpose else "- Purpose: (not set)"
            )
        else:
            channel_section.append(
                "no signal — conversations.info returned no data "
                "(missing scope, or bot not in channel)"
            )
    else:
        channel_section.append(
            "no signal — missing Slack token or channel_id in context"
        )
    sections.append("\n".join(channel_section))

    # --- Section 3: User metadata ---
    user_section = ["### This user"]
    if token and user_id:
        info = slack_api.get_user_info(token, user_id)
        if info:
            profile = info.get("profile") or {}
            display = profile.get("display_name") or ""
            real = profile.get("real_name") or info.get("real_name") or ""
            title = profile.get("title") or ""
            user_section.append(
                f"- Display name: {display}" if display else "- Display name: (not set)"
            )
            user_section.append(
                f"- Real name: {real}" if real else "- Real name: (not set)"
            )
            user_section.append(f"- Title: {title}" if title else "- Title: (not set)")
        else:
            user_section.append(
                "no signal — users.info returned no data "
                "(missing users:read scope, or user not visible)"
            )
    else:
        user_section.append("no signal — missing Slack token or user_id in context")
    sections.append("\n".join(user_section))

    # --- Section 4: Semantic memory hint ---
    memory_section = ["### Memory hint"]
    if (
        codebases is not None
        and codebases.enabled
        and codebases.allow_learning
        and codebases.bindings
    ):
        known_repos = [b.repo for b in codebases.bindings]
        isolated_list = config.memory.isolated_channels if codebases else []
        is_isolated = bool(channel_id and channel_id in isolated_list)
        hint = retrieve_codebase_affinity_hint(
            tenant_id=tenant_id,
            channel_id=channel_id,
            known_repos=known_repos,
            isolated=is_isolated,
            shared_across_channels=config.memory.shared_across_channels,
            user_id=user_id,
        )
        if hint:
            memory_section.append(f"- Most recently used repo in this scope: `{hint}`")
        else:
            memory_section.append(
                "no signal — no prior repo usage indexed for this "
                "(tenant, channel) scope"
            )
    else:
        memory_section.append(
            "no signal — memory learning disabled or no bindings configured"
        )
    sections.append("\n".join(memory_section))

    sections.append(
        "---\n"
        "These are hints, not a decision. Combine them with the user's "
        "message and the thread history to pick a repo."
    )
    return "\n\n".join(sections)


@audited_tool("code_search")
def code_search(query: str, repo: str, max_results: int = 20) -> str:
    """Search code across a repository for a keyword or phrase.

    Use this when the user asks "where is X", "does the codebase use Y",
    "show me usages of Z", or any question where finding the relevant
    files is the first step. Call ``code_read_file`` afterward to read
    the full contents of a specific result.

    The search runs on the repo's default branch only (no feature
    branches). Results are keyword-based — NOT semantic — so prefer
    specific symbol names over paraphrased natural language.

    Args:
        query: The keyword or phrase to search for. Quoted multi-word
               phrases work. Prefer specific identifiers over full
               sentences.
        repo: "owner/name" repo slug — required. Pick from the
              ``## Connected codebases`` block in the system prompt.
        max_results: Max number of hits to return (default 20, capped
                     at 30 by the GitHub API).
    """
    from code_backend import BackendError, build_default_backend

    ctx = get_context()
    repo = (repo or "").strip()
    installation_id = ctx.get("github_installation_id") or ""

    if not repo:
        return (
            "Error: repo is required. Pick one from the ## Connected "
            "codebases list in the system prompt and retry with "
            "repo='owner/name'."
        )
    repo, authorization_error = _authorize_github_repo(repo, ctx)
    if authorization_error:
        return authorization_error
    assert repo is not None
    if not installation_id:
        return (
            "Error: this tenant has not installed the Agent GitHub App "
            "yet. Ask the user to install it from the onboarding "
            "integrations page, then retry."
        )

    try:
        backend = build_default_backend(installation_id)
        hits = backend.search_code(query, repo, max_results=max_results)
    except BackendError as e:
        return f"Error: {e}"
    except Exception as e:  # noqa: BLE001 — friendly message to the LLM
        log.exception(
            "code_search failed for tenant=%s repo=%s",
            ctx.get("tenant_id"),
            repo,
        )
        return f"Error: code_search failed unexpectedly: {type(e).__name__}: {e}"

    if not hits:
        return f"No results for {query!r} in {repo}."

    lines = [f"Found {len(hits)} hits for {query!r} in {repo}:"]
    for i, hit in enumerate(hits, start=1):
        lines.append(f"{i}. {hit.path}  ({hit.html_url})")
        if hit.snippet:
            lines.append(f"   {hit.snippet}")
    return "\n".join(lines)


@audited_tool("code_read_file")
def code_read_file(path: str, repo: str, ref: str | None = None) -> str:
    """Read a file's full contents from a repository.

    Use this after ``code_search`` returns a promising path, or when
    the user asks about a specific file by name. The returned content
    is capped at 64 KB — files larger than that are truncated with a
    marker at the end.

    Args:
        path: The file path inside the repo (e.g. "src/auth/login.ts").
              Leading slashes are stripped.
        repo: "owner/name" slug — required. Pick from the
              ``## Connected codebases`` block in the system prompt.
        ref: Optional branch, tag, or commit SHA. Defaults to the
             repo's default branch (usually "main").
    """
    from code_backend import BackendError, build_default_backend

    ctx = get_context()
    repo = (repo or "").strip()
    installation_id = ctx.get("github_installation_id") or ""

    if not repo:
        return (
            "Error: repo is required. Pick one from the ## Connected "
            "codebases list in the system prompt and retry with "
            "repo='owner/name'."
        )
    repo, authorization_error = _authorize_github_repo(repo, ctx)
    if authorization_error:
        return authorization_error
    assert repo is not None
    if not installation_id:
        return (
            "Error: this tenant has not installed the Agent GitHub App "
            "yet. Ask the user to install it from the onboarding "
            "integrations page, then retry."
        )
    if not path:
        return "Error: path is required."

    try:
        backend = build_default_backend(installation_id)
        file = backend.read_file(repo, path, ref=ref)
    except BackendError as e:
        return f"Error: {e}"
    except Exception as e:  # noqa: BLE001
        log.exception(
            "code_read_file failed for tenant=%s repo=%s path=%s",
            ctx.get("tenant_id"),
            repo,
            path,
        )
        return f"Error: code_read_file failed unexpectedly: {type(e).__name__}: {e}"

    header = f"=== {file.repo}/{file.path} @ {file.ref} ({file.size} bytes) ==="
    suffix = (
        "\n\n[truncated — file is larger than the 64 KB cap]" if file.truncated else ""
    )
    return f"{header}\n{file.content}{suffix}"


@audited_tool("code_find_symbol")
def code_find_symbol(symbol: str, repo: str, max_results: int = 15) -> str:
    """Find files where a symbol (function, class, constant) appears.

    Use this when the user asks "where is function foo defined", "who
    calls X", or "what uses this type". The lookup is LEXICAL — it
    matches the literal symbol name anywhere in a file's contents,
    which means false positives on overloaded names are real. The
    first result is usually the definition but not always; follow up
    with ``code_read_file`` to confirm.

    Args:
        symbol: The symbol to find (e.g. "authenticateUser",
                "RetryConfig").
        repo: "owner/name" slug — required. Pick from the
              ``## Connected codebases`` block in the system prompt.
        max_results: Max hits to return (default 15).
    """
    from code_backend import BackendError, build_default_backend

    ctx = get_context()
    repo = (repo or "").strip()
    installation_id = ctx.get("github_installation_id") or ""

    if not repo:
        return (
            "Error: repo is required. Pick one from the ## Connected "
            "codebases list in the system prompt and retry with "
            "repo='owner/name'."
        )
    repo, authorization_error = _authorize_github_repo(repo, ctx)
    if authorization_error:
        return authorization_error
    assert repo is not None
    if not installation_id:
        return (
            "Error: this tenant has not installed the Agent GitHub App "
            "yet. Ask the user to install it from the onboarding "
            "integrations page, then retry."
        )

    try:
        backend = build_default_backend(installation_id)
        hits = backend.find_symbol(symbol, repo, max_results=max_results)
    except BackendError as e:
        return f"Error: {e}"
    except Exception as e:  # noqa: BLE001
        log.exception(
            "code_find_symbol failed for tenant=%s repo=%s symbol=%s",
            ctx.get("tenant_id"),
            repo,
            symbol,
        )
        return f"Error: code_find_symbol failed unexpectedly: {type(e).__name__}: {e}"

    if not hits:
        return f"No files mention {symbol!r} in {repo}."

    lines = [
        f"Found {len(hits)} files mentioning {symbol!r} in {repo} "
        f"(lexical match — confirm with code_read_file):"
    ]
    for i, hit in enumerate(hits, start=1):
        lines.append(f"{i}. {hit.path}  ({hit.html_url})")
    return "\n".join(lines)


@audited_tool("code_list_commits")
def code_list_commits(
    repo: str,
    ref: str | None = None,
    path: str | None = None,
    limit: int = 10,
) -> str:
    """List recent commits on a repo.

    Use this when the user asks "what's the latest commit", "what
    shipped recently", "who changed X last", or any question about
    recent repo activity. Unlike ``code_search`` this isn't subject
    to the 30-requests-per-minute search limit — it uses the general
    REST API and is fine for interactive Q&A.

    Args:
        repo: "owner/name" slug — required. Pick from the
              ``## Connected codebases`` block in the system prompt.
        ref: Optional branch, tag, or commit SHA. Defaults to the
             repo's default branch.
        path: Optional file path to filter commits that touched it —
              useful for "what changed in src/auth.ts recently".
        limit: Max commits to return (default 10, capped at 100).
    """
    from code_backend import BackendError, build_default_backend

    ctx = get_context()
    repo = (repo or "").strip()
    installation_id = ctx.get("github_installation_id") or ""

    if not repo:
        return (
            "Error: repo is required. Pick one from the ## Connected "
            "codebases list in the system prompt and retry with "
            "repo='owner/name'."
        )
    repo, authorization_error = _authorize_github_repo(repo, ctx)
    if authorization_error:
        return authorization_error
    assert repo is not None
    if not installation_id:
        return (
            "Error: this tenant has not installed the Agent GitHub App "
            "yet. Ask the user to install it from the onboarding "
            "integrations page, then retry."
        )

    try:
        backend = build_default_backend(installation_id)
        commits = backend.list_commits(repo, ref=ref, path=path, limit=limit)
    except BackendError as e:
        return f"Error: {e}"
    except Exception as e:  # noqa: BLE001
        log.exception(
            "code_list_commits failed for tenant=%s repo=%s ref=%s path=%s",
            ctx.get("tenant_id"),
            repo,
            ref,
            path,
        )
        return f"Error: code_list_commits failed unexpectedly: {type(e).__name__}: {e}"

    if not commits:
        scope = f" on {ref}" if ref else ""
        scope += f" touching {path}" if path else ""
        return f"No commits found in {repo}{scope}."

    header_ref = ref or "default branch"
    header = f"Recent commits in {repo} ({header_ref}):"
    lines = [header]
    for i, c in enumerate(commits, start=1):
        date = c.date.split("T", 1)[0] if c.date else "?"
        lines.append(
            f"{i}. {c.short_sha} — {c.message}  [{c.author}, {date}]  ({c.html_url})"
        )
    return "\n".join(lines)


@audited_tool("propose_pr")
def propose_pr(
    repo: str,
    task_description: str,
    context_hint: str = "",
) -> str:
    """Open a pull request in the named repository.

    Use this when the user has asked for a code change you understand
    well enough to specify, AND you've done enough discovery
    (``code_search``, ``code_read_file``, ``code_find_symbol``,
    ``code_list_commits``) to know what to change. The actual diff is
    written by a Claude agent running in a sandbox container — this
    tool just queues the work and returns immediately. The sandbox
    typically takes 1-10 minutes to clone, edit, and open the PR;
    when it's done the bridge will post the PR link to this Slack
    thread automatically.

    The agent stays HealthyBusy until the sandbox finishes (a daemon
    poller watches the sandbox_jobs DDB table). DO NOT call this tool
    a second time for the same change while the first one is still
    in flight — the user will see two PRs.

    Args:
        repo: ``"owner/name"`` slug — REQUIRED. Pick from the
              ``## Connected codebases`` block in the system prompt.
              There is no silent default; an omitted repo is an error.
        task_description: One or two sentences describing the change
              the user wants. The sandbox agent reads this as its
              primary instruction — be specific about what to change.
        context_hint: Optional research notes you've already gathered
              from ``code_search`` / ``code_read_file`` etc. Saves the
              sandbox agent from re-doing discovery work. Think "here
              are the files I read and what I learned."
    """
    ctx = get_context()
    tenant_id = ctx.get("tenant_id", "") or ""
    installation_id = ctx.get("github_installation_id", "") or ""
    channel_id = ctx.get("channel_id", "") or ""
    thread_id = ctx.get("thread_id", "") or ""

    if not (repo or "").strip():
        return (
            "Error: repo is required. Pick one from the ## Connected "
            "codebases list in the system prompt and retry with "
            "repo='owner/name'."
        )
    repo, authorization_error = _authorize_github_repo(repo, ctx)
    if authorization_error:
        return authorization_error
    assert repo is not None
    if not installation_id:
        return (
            "Error: this tenant has not installed the Agent GitHub App "
            "yet. Ask the user to install it from the onboarding "
            "integrations page, then retry."
        )
    if not tenant_id or not channel_id:
        return (
            "Error: no Slack context available (missing tenant_id or "
            "channel_id). Cannot route the PR-ready callback to a "
            "Slack thread."
        )

    # Resolve sandbox coordinates BEFORE writing the row, so a
    # configuration error fails fast without leaving an orphan
    # `pending` row in the table.
    try:
        coords = _load_sandbox_coords()
    except Exception as e:  # noqa: BLE001
        log.exception("propose_pr: sandbox coordinates unavailable")
        return (
            f"Error: sandbox is not configured for this deployment "
            f"({type(e).__name__}: {e}). Cannot open a PR right now."
        )

    task_id = f"pr-{uuid.uuid4().hex[:8]}"
    now = _iso_now()
    job_row = {
        "task_id": task_id,
        "status": "pending",
        "tenant_id": tenant_id,
        "installation_id": installation_id,
        "repo": repo,
        "task_description": task_description or "",
        "context_hint": context_hint or "",
        "slack_channel_id": channel_id,
        "slack_thread_id": thread_id,
        "created_at": now,
        # 30-day TTL for cleanup. Successful PRs are landed long
        # before this; orphans get auto-purged.
        "ttl": int(time.time()) + 30 * 24 * 3600,
    }

    # 1. Write the job row BEFORE adding to inflight or firing the
    #    task. If DDB write fails, no HealthyBusy leak; just bail.
    try:
        _sandbox_jobs_table().put_item(Item=job_row)
    except Exception as e:  # noqa: BLE001
        log.exception("propose_pr: failed to write sandbox_jobs row")
        return (
            f"Error: could not record the PR job in DynamoDB "
            f"({type(e).__name__}: {e}). Try again in a moment."
        )

    # 2. Mark inflight + register async task BEFORE firing the
    #    Fargate task. Order matters: heartbeat registration must be
    #    populated before app.add_async_task so the next /ping
    #    response sees >0 inflight (gotcha noted in
    #    start_background_task above).
    busy_threshold = max(1, int(ctx.get("heartbeat_busy_threshold", 1) or 1))
    try:
        _register_async_task(task_id, busy_threshold)
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
        log.exception("propose_pr: failed to register async task")
        try:
            _sandbox_jobs_table().update_item(
                Key={"task_id": task_id},
                UpdateExpression="SET #s = :s, completed_at = :c, #e = :e",
                ExpressionAttributeNames={"#s": "status", "#e": "error"},
                ExpressionAttributeValues={
                    ":s": "error",
                    ":c": _iso_now(),
                    ":e": error,
                },
            )
        except Exception:  # noqa: BLE001
            log.exception("propose_pr: failed to record async registration error")
        return f"Error: could not register the PR job with AgentCore ({error})."

    # 3. Fire the Fargate sandbox task. On failure, roll back the
    #    HealthyBusy state immediately so the agent doesn't get
    #    stuck pinned alive on a launch error.
    try:
        ecs = _ecs_client()
        response = ecs.run_task(
            cluster=coords["cluster_arn"],
            taskDefinition=coords["task_def_arn"],
            launchType="FARGATE",
            count=1,
            networkConfiguration={
                "awsvpcConfiguration": {
                    "subnets": [s for s in coords["subnets"].split(",") if s],
                    "securityGroups": [
                        s for s in coords["security_groups"].split(",") if s
                    ],
                    "assignPublicIp": "ENABLED",
                }
            },
            overrides={
                "containerOverrides": [
                    {
                        "name": "sandbox",
                        "environment": [
                            {"name": "TASK_ID", "value": task_id},
                        ],
                    }
                ]
            },
        )
        _validate_run_task_response(response)
    except Exception as e:  # noqa: BLE001
        log.exception("propose_pr: ecs.run_task failed for task_id=%s", task_id)
        try:
            _sandbox_jobs_table().update_item(
                Key={"task_id": task_id},
                UpdateExpression="SET #s = :s, #e = :e, #c = :c",
                ExpressionAttributeNames={
                    "#s": "status",
                    "#e": "error",
                    "#c": "completed_at",
                },
                ExpressionAttributeValues={
                    ":s": "error",
                    ":e": f"ecs.run_task failed: {type(e).__name__}: {e}",
                    ":c": _iso_now(),
                },
            )
        except Exception:  # noqa: BLE001
            log.exception("propose_pr: also failed to mark row as error")
        ping.complete_task(task_id)
        app.complete_async_task(task_id)
        return (
            f"Error: failed to launch the sandbox task "
            f"({type(e).__name__}: {e}). The PR was not opened."
        )

    # 4. Snapshot the request context for the daemon poller. The
    #    poller runs in a background thread AFTER the entrypoint has
    #    called clear_context(), so get_context() returns empty there.
    #    Capturing now means the audit row written on completion has
    #    the same tenant/user/channel/thread as the launch row.
    ctx_snapshot = dict(ctx)

    # 5. Audit the launched event. _audit.write swallows its own
    #    exceptions; the helper wraps that in another try for safety
    #    so a context-read failure here can never break the caller.
    _write_propose_pr_audit(
        task_id=task_id,
        event="launched",
        status="launched",
        repo=repo,
        ctx_snapshot=ctx_snapshot,
    )

    # 6. Spawn the daemon poller. It will write the completion audit
    #    row and clear HealthyBusy when the sandbox writes a terminal
    #    status (or when the tenant's background-work ceiling hits). The bridge callback
    #    path posts the Slack message in parallel — both signals are
    #    independent so a callback failure doesn't leak HealthyBusy.
    poll_ceiling_seconds = _heartbeat_max_background_seconds(ctx)
    threading.Thread(
        target=_poll_sandbox_completion,
        args=(task_id, repo, ctx_snapshot, poll_ceiling_seconds),
        daemon=True,
    ).start()

    return (
        f"Opening a PR for {repo} (task_id={task_id}). The sandbox "
        "typically takes 1-10 minutes; when it finishes the bridge "
        "will post the PR link in this thread automatically.\n\n"
        "You can check progress at any time with "
        f"``check_task_status(task_id='{task_id}')`` — use it if the "
        "user asks for an update, or if ~10 minutes pass with no link. "
        "Don't call propose_pr again for this same change."
    )


@audited_tool("check_task_status")
def check_task_status(task_id: str = "") -> str:
    """Check the status of a sandbox task (PR, code change, etc.).

    Call this whenever a user asks about the progress of a previously
    queued task — e.g. "is the PR up yet?", "what happened to that
    code change?", "did it finish?". Also call this proactively after
    ``propose_pr`` if the PR link hasn't appeared in the thread —
    this is your observability into the sandbox. If the status is
    ``error``, relay the error message to the user.

    If ``task_id`` is provided, returns the detailed status of that
    specific task. If omitted, lists the most recent tasks for this
    tenant (up to 10, sorted newest first).

    Possible statuses:
    - **pending**: task row written, ECS task not yet started
    - **running**: sandbox container is executing
    - **success**: PR opened successfully (``pr_url`` included)
    - **error**: sandbox failed (``error`` message included)
    - **orphaned**: the tenant's background-work ceiling elapsed without terminal status

    Args:
        task_id: The ``pr-XXXXXXXX`` identifier returned by
                 ``propose_pr``. Omit to list recent tasks.
    """
    ctx = get_context()
    tenant_id = ctx.get("tenant_id", "") or ""
    if not tenant_id:
        return (
            "Error: no tenant context available (missing tenant_id). "
            "Cannot check task status."
        )

    table = _sandbox_jobs_table()

    if task_id:
        task_id = task_id.strip()
        try:
            resp = table.get_item(Key={"task_id": task_id})
        except Exception as e:  # noqa: BLE001
            log.exception("check_task_status: DDB get_item failed for %s", task_id)
            return f"Error: could not read task status ({type(e).__name__}: {e})."

        row = resp.get("Item")
        if not row or row.get("tenant_id") != tenant_id:
            return f"No task found with id `{task_id}`."

        return _format_task_row(row)

    # No task_id — list recent tasks for this tenant.
    try:
        resp = table.scan(
            FilterExpression="tenant_id = :tid",
            ExpressionAttributeValues={":tid": tenant_id},
        )
    except Exception as e:  # noqa: BLE001
        log.exception("check_task_status: DDB scan failed for tenant %s", tenant_id)
        return f"Error: could not list tasks ({type(e).__name__}: {e})."

    items = resp.get("Items", [])
    if not items:
        return "No sandbox tasks found for this tenant."

    # Sort by created_at descending, take the 10 most recent.
    items.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    items = items[:10]

    lines = [f"Recent tasks (showing {len(items)}):"]
    for row in items:
        status = row.get("status", "unknown")
        tid = row.get("task_id", "?")
        repo = row.get("repo", "?")
        created = row.get("created_at", "?")
        entry = f"- {tid} | {status} | {repo} | {created}"
        pr_url = row.get("pr_url", "")
        if pr_url:
            entry += f" | {pr_url}"
        lines.append(entry)

    return "\n".join(lines)


def _format_task_row(row: dict[str, Any]) -> str:
    """Format a sandbox_jobs row into a human-readable status report."""
    status = row.get("status", "unknown")
    task_id = row.get("task_id", "?")
    repo = row.get("repo", "?")
    created = row.get("created_at", "?")
    completed = row.get("completed_at", "")
    pr_url = row.get("pr_url", "")
    error = row.get("error", "")

    lines = [
        f"Task {task_id}",
        f"- Status: {status}",
        f"- Repo: {repo}",
        f"- Created: {created}",
    ]
    if completed:
        lines.append(f"- Completed: {completed}")
    if pr_url:
        lines.append(f"- PR: {pr_url}")
    if error:
        lines.append(f"- Error: {error}")

    if status == "pending":
        lines.append("\nThe sandbox container hasn't started yet.")
    elif status == "running":
        lines.append(
            "\nThe sandbox is working on it — cloning, editing, and opening the PR."
        )
    elif status == "success":
        lines.append("\nThe PR is open and ready for review.")
    elif status == "error":
        lines.append("\nThe sandbox failed. See the error above.")
    elif status == "orphaned":
        lines.append(
            "\nThe task exceeded its configured background-work deadline. "
            "The sandbox container may have crashed. Try re-queuing."
        )

    return "\n".join(lines)


@audited_tool("manage_config")
def manage_config(action: str, section: str, data: str | None = None) -> str:
    """View this bot's configuration or update it as an authorized admin.

    Call this when a user asks to view or change the bot's settings: adding
    skills/runbooks, modifying bot-to-bot policy, updating escalation routes,
    toggling context assembly, changing which tools are enabled, or
    configuring per-channel personas.

    Args:
        action: "view" to see current settings, "update" to change them.
        section: Which part to view/update. One of: "skills", "bot_policy",
                 "escalation", "context_assembly", "catalog_tools",
                 "system_prompt", "channels", or "all" (view-only).
        data: JSON string with the new value (required for "update"). Shape
              must match the section being updated. For "channels", provide
              a dict of channel_id -> persona object; keys are merged into
              the existing channels dict (set a persona to null to remove it).
    """
    from tenant import (
        BotPolicyConfig,
        CatalogConfig,
        ChannelPersona,
        ContextAssemblyConfig,
        EscalationConfig,
        SkillDef,
        load_tenant_config,
        save_tenant_config,
    )

    ctx = get_context()
    tenant_id = ctx.get("tenant_id", "")
    if not tenant_id:
        return "Error: no tenant_id in request context."

    allowed_sections = {
        "skills",
        "bot_policy",
        "escalation",
        "context_assembly",
        "catalog_tools",
        "system_prompt",
        "channels",
        "all",
    }
    if section not in allowed_sections:
        return f"Error: unknown section '{section}'. Choose from: {sorted(allowed_sections)}"

    config = load_tenant_config(tenant_id)
    requester_user_id = str(ctx.get("user_id") or "")
    configured_admin_ids = ctx.get("config_admin_user_ids")
    is_admin = bool(
        requester_user_id
        and isinstance(configured_admin_ids, (tuple, list))
        and requester_user_id in configured_admin_ids
    )

    if action == "view":
        # Non-admin reads expose capability summaries, not prompt text,
        # channel IDs, escalation contacts, or allowlisted identity IDs.
        if not is_admin:
            safe_sections: dict[str, Any] = {
                "skills": [
                    {"name": skill.name, "trigger": skill.trigger}
                    for skill in config.skills
                ],
                "bot_policy": {
                    "allow_all_bots": config.bot_policy.allow_all_bots,
                    "trusted_bot_count": len(config.bot_policy.trusted_bot_ids),
                    "open_channel_count": len(config.bot_policy.open_channels),
                },
                "escalation": {
                    "teams": [route.team_name for route in config.escalation.routes],
                    "route_count": len(config.escalation.routes),
                },
                "context_assembly": config.context_assembly.model_dump(),
                "catalog_tools": config.catalog.allowed_tools,
                "channels": {"configured_count": len(config.channels)},
                "system_prompt": "redacted; configured admin access required",
            }
            if section == "all":
                return json.dumps(safe_sections, indent=2)
            return json.dumps(safe_sections[section], indent=2)

        if section == "all":
            return json.dumps(
                {
                    "skills": [s.model_dump() for s in config.skills],
                    "bot_policy": config.bot_policy.model_dump(),
                    "escalation": config.escalation.model_dump(),
                    "context_assembly": config.context_assembly.model_dump(),
                    "catalog_tools": config.catalog.allowed_tools,
                    "channels": {
                        cid: p.model_dump() for cid, p in config.channels.items()
                    },
                    "system_prompt": config.system_prompt[:200]
                    + ("..." if len(config.system_prompt) > 200 else ""),
                },
                indent=2,
            )
        if section == "skills":
            return json.dumps([s.model_dump() for s in config.skills], indent=2)
        if section == "bot_policy":
            return json.dumps(config.bot_policy.model_dump(), indent=2)
        if section == "escalation":
            return json.dumps(config.escalation.model_dump(), indent=2)
        if section == "context_assembly":
            return json.dumps(config.context_assembly.model_dump(), indent=2)
        if section == "catalog_tools":
            return json.dumps(config.catalog.allowed_tools, indent=2)
        if section == "channels":
            return json.dumps(
                {cid: p.model_dump() for cid, p in config.channels.items()},
                indent=2,
            )
        if section == "system_prompt":
            return config.system_prompt
        return "Error: unhandled section."

    if action == "update":
        if not is_admin:
            return (
                "Error: configuration updates require the requesting Slack "
                "user to be listed in this tenant's admin_user_ids."
            )
        if section == "all":
            return "Error: cannot update 'all' at once. Update individual sections."
        if not data:
            return "Error: 'data' is required for updates. Provide a JSON string."
        try:
            parsed = json.loads(data)
        except json.JSONDecodeError as e:
            return f"Error: invalid JSON in data: {e}"

        try:
            if section == "skills":
                config.skills = [SkillDef.model_validate(s) for s in parsed]
            elif section == "bot_policy":
                config.bot_policy = BotPolicyConfig.model_validate(parsed)
            elif section == "escalation":
                config.escalation = EscalationConfig.model_validate(parsed)
            elif section == "context_assembly":
                config.context_assembly = ContextAssemblyConfig.model_validate(parsed)
            elif section == "catalog_tools":
                if not isinstance(parsed, list):
                    return "Error: catalog_tools must be a list of tool IDs."
                if not all(isinstance(item, str) and item.strip() for item in parsed):
                    return (
                        "Error: catalog_tools must contain only non-empty string "
                        "tool IDs."
                    )
                normalized = [item.strip() for item in parsed]
                if len(set(normalized)) != len(normalized):
                    return "Error: catalog_tools must not contain duplicate tool IDs."
                unknown = [item for item in normalized if item not in CATALOG]
                if unknown:
                    return f"Error: unknown catalog tool IDs: {sorted(unknown)}"
                config.catalog = CatalogConfig.model_validate(
                    {
                        **config.catalog.model_dump(),
                        "allowed_tools": normalized,
                    }
                )
            elif section == "channels":
                if not isinstance(parsed, dict):
                    return "Error: channels must be a dict of channel_id -> persona object."
                for cid, val in parsed.items():
                    if val is None:
                        config.channels.pop(cid, None)
                    else:
                        config.channels[cid] = ChannelPersona.model_validate(val)
            elif section == "system_prompt":
                if not isinstance(parsed, str):
                    return "Error: system_prompt must be a string."
                if not parsed.strip():
                    return "Error: system_prompt cannot be empty."
                config.system_prompt = parsed
        except Exception as e:
            return f"Error: invalid data for '{section}': {e}"

        save_tenant_config(config)
        return f"Updated '{section}' successfully. Changes take effect on the next message."

    return f"Error: unknown action '{action}'. Use 'view' or 'update'."


# ----------------------------------------------------------------------------
# Ephemeral dashboards
# ----------------------------------------------------------------------------
#
# `render_dashboard` creates a multi-panel dashboard (charts, tables, stats,
# text, lists) and hosts it at a short-lived URL on the onboarding service.
# Dashboards auto-expire via DynamoDB TTL (default 7 days).
#
# The spec is a JSON document stored in the `dashboards` DDB table. The
# onboarding service reads it via the bridge at GET /internal/dashboard
# with the bearer token in a request header
# and renders it with React/Recharts.

_DASHBOARDS_TABLE_NAME = os.getenv("DASHBOARDS_TABLE", "dashboards")
_DASHBOARD_TTL_DAYS = 7
_DASHBOARD_MAX_ITEM_BYTES = 256 * 1024
_DASHBOARD_MAX_PANELS = 24
_DASHBOARD_MAX_POINTS = 500
_DASHBOARD_MAX_RENDER_CELLS = 10_000

_dashboards_table_singleton: Any | None = None


class _DashboardModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)


class _ChartDataset(_DashboardModel):
    label: str = Field(min_length=1, max_length=80)
    data: list[int | float] = Field(min_length=1, max_length=_DASHBOARD_MAX_POINTS)

    @field_validator("data")
    @classmethod
    def _finite_data(cls, values: list[int | float]) -> list[int | float]:
        if any(
            isinstance(value, float) and not math.isfinite(value) for value in values
        ):
            raise ValueError("chart data values must be finite")
        return values


class _ChartPanel(_DashboardModel):
    type: Literal["chart"]
    title: str | None = Field(default=None, min_length=1, max_length=160)
    chart_type: Literal["line", "bar", "pie", "area", "scatter"]
    labels: list[str] = Field(min_length=1, max_length=_DASHBOARD_MAX_POINTS)
    datasets: list[_ChartDataset] = Field(min_length=1, max_length=8)
    x_label: str | None = Field(default=None, min_length=1, max_length=80)
    y_label: str | None = Field(default=None, min_length=1, max_length=80)

    @field_validator("labels")
    @classmethod
    def _valid_labels(cls, values: list[str]) -> list[str]:
        if any(not value.strip() or len(value) > 160 for value in values):
            raise ValueError("chart labels must be 1-160 non-whitespace characters")
        return [value.strip() for value in values]

    @model_validator(mode="after")
    def _matching_series(self) -> "_ChartPanel":
        if len({dataset.label for dataset in self.datasets}) != len(self.datasets):
            raise ValueError("chart dataset labels must be unique")
        if any(len(dataset.data) != len(self.labels) for dataset in self.datasets):
            raise ValueError("each chart dataset must have one value per label")
        if self.chart_type == "pie":
            if len(self.datasets) != 1:
                raise ValueError("pie charts support exactly one dataset")
            pie_values = self.datasets[0].data
            if any(value < 0 for value in pie_values):
                raise ValueError("pie chart values must be non-negative")
            if not any(value > 0 for value in pie_values):
                raise ValueError("pie charts require at least one positive value")
        return self


_TableCell = str | int | float | bool | None


class _TablePanel(_DashboardModel):
    type: Literal["table"]
    title: str | None = Field(default=None, min_length=1, max_length=160)
    columns: list[str] = Field(min_length=1, max_length=24)
    rows: list[list[_TableCell]] = Field(max_length=1000)

    @field_validator("columns")
    @classmethod
    def _valid_columns(cls, values: list[str]) -> list[str]:
        if any(not value.strip() or len(value) > 120 for value in values):
            raise ValueError("table columns must be 1-120 non-whitespace characters")
        if len(set(values)) != len(values):
            raise ValueError("table column names must be unique")
        return [value.strip() for value in values]

    @field_validator("rows")
    @classmethod
    def _valid_rows(cls, rows: list[list[_TableCell]]) -> list[list[_TableCell]]:
        for row in rows:
            for value in row:
                if isinstance(value, float) and not math.isfinite(value):
                    raise ValueError("table numbers must be finite")
                if isinstance(value, str) and len(value) > 1000:
                    raise ValueError(
                        "table cell strings must be at most 1000 characters"
                    )
        return rows

    @model_validator(mode="after")
    def _matching_rows(self) -> "_TablePanel":
        if any(len(row) != len(self.columns) for row in self.rows):
            raise ValueError("each table row must have one cell per column")
        return self


class _StatPanel(_DashboardModel):
    type: Literal["stat"]
    title: str | None = Field(default=None, min_length=1, max_length=160)
    value: str = Field(min_length=1, max_length=160)
    delta: str | None = Field(default=None, min_length=1, max_length=80)
    trend: Literal["up", "down", "flat"] | None = None


class _TextPanel(_DashboardModel):
    type: Literal["text"]
    title: str | None = Field(default=None, min_length=1, max_length=160)
    content: str = Field(min_length=1, max_length=20_000)


class _ListItem(_DashboardModel):
    key: str = Field(min_length=1, max_length=120)
    value: str = Field(max_length=1000)


class _ListPanel(_DashboardModel):
    type: Literal["list"]
    title: str | None = Field(default=None, min_length=1, max_length=160)
    items: list[_ListItem] = Field(min_length=1, max_length=200)


_Panel = Annotated[
    _ChartPanel | _TablePanel | _StatPanel | _TextPanel | _ListPanel,
    Field(discriminator="type"),
]


class _DashboardInput(_DashboardModel):
    title: str = Field(min_length=1, max_length=200)
    panels: list[_Panel] = Field(min_length=1, max_length=_DASHBOARD_MAX_PANELS)

    @model_validator(mode="after")
    def _bounded_render_complexity(self) -> "_DashboardInput":
        # The serialized-item limit protects DynamoDB, but compact numeric
        # tables can remain small on disk while creating a huge browser DOM.
        # Bound the cells/points the public renderer and its accessible data
        # tables will materialize in one page.
        render_cells = 0
        for panel in self.panels:
            if isinstance(panel, _ChartPanel):
                render_cells += len(panel.labels) * (len(panel.datasets) + 1)
            elif isinstance(panel, _TablePanel):
                render_cells += len(panel.columns) * (len(panel.rows) + 1)
            elif isinstance(panel, _ListPanel):
                render_cells += len(panel.items) * 2
            else:
                render_cells += 1
        if render_cells > _DASHBOARD_MAX_RENDER_CELLS:
            raise ValueError(
                "dashboard render complexity exceeds "
                f"{_DASHBOARD_MAX_RENDER_CELLS} cells or points"
            )
        return self


def _dashboard_base_url() -> str:
    """Return a safe, portable public base URL for dashboard links."""
    raw = os.getenv("DASHBOARD_BASE_URL", "").strip()
    if not raw:
        if os.getenv("AGENT_LOCAL_STORES") == "1" or os.getenv("LOCAL_DEV") == "1":
            return "http://localhost:3000"
        raise RuntimeError("DASHBOARD_BASE_URL is required outside local development")

    parsed = urlsplit(raw)
    is_loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("DASHBOARD_BASE_URL must be an absolute http(s) URL")
    if parsed.scheme != "https" and not is_loopback:
        raise ValueError("DASHBOARD_BASE_URL must use HTTPS outside localhost")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError(
            "DASHBOARD_BASE_URL must not contain credentials, query, or fragment"
        )
    if parsed.path not in {"", "/"}:
        raise ValueError("DASHBOARD_BASE_URL must be an origin without a path")
    return raw.rstrip("/")


def _validation_summary(exc: ValidationError) -> str:
    details = []
    for error in exc.errors(include_url=False, include_input=False)[:5]:
        location = ".".join(str(part) for part in error["loc"])
        details.append(f"{location}: {error['msg']}")
    return "; ".join(details)


def _validate_dashboard(title: str, panels: list[dict]) -> dict[str, Any]:
    try:
        validated = _DashboardInput.model_validate({"title": title, "panels": panels})
    except ValidationError as exc:
        raise ValueError(f"Invalid dashboard: {_validation_summary(exc)}") from exc
    return validated.model_dump(mode="python")


def _floats_to_decimal(obj: Any) -> Any:
    """Convert finite floats recursively for boto3's DynamoDB serializer."""
    if isinstance(obj, float):
        if not math.isfinite(obj):
            raise ValueError("dashboard numbers must be finite")
        return Decimal(str(obj))
    if isinstance(obj, Decimal):
        if not obj.is_finite():
            raise ValueError("dashboard numbers must be finite")
        return obj
    if isinstance(obj, dict):
        return {key: _floats_to_decimal(value) for key, value in obj.items()}
    if isinstance(obj, list):
        return [_floats_to_decimal(value) for value in obj]
    return obj


def _local_dashboards_dir() -> Path:
    configured = os.getenv("DASHBOARD_LOCAL_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    current = Path(__file__).resolve()
    for parent in current.parents:
        examples = parent / "examples"
        if examples.is_dir():
            return examples / "dashboards"
    raise FileNotFoundError(f"Could not find examples/ above {current}")


def _put_dashboard_spec(spec: dict[str, Any]) -> None:
    if os.getenv("AGENT_LOCAL_STORES") == "1":
        directory = _local_dashboards_dir()
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        target = directory / f"{spec['token']}.json"
        temporary = directory / f".{spec['token']}.{uuid.uuid4().hex}.tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(spec, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        temporary.replace(target)
        return
    _dashboards_table().put_item(Item=_floats_to_decimal(spec))


def _dashboards_table() -> Any:
    """Lazy boto3 DynamoDB Table handle for dashboards."""
    global _dashboards_table_singleton
    if _dashboards_table_singleton is None:
        import boto3

        resource = boto3.resource(
            "dynamodb", region_name=os.getenv("AWS_REGION", "us-west-2")
        )
        _dashboards_table_singleton = resource.Table(_DASHBOARDS_TABLE_NAME)
    return _dashboards_table_singleton


def _dashboard_audit_args(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
    """Summarize dashboard shape without retaining panel content."""
    title = kwargs.get("title", args[0] if args else "")
    panels = kwargs.get("panels", args[1] if len(args) > 1 else [])
    panel_types = (
        sorted(
            {
                str(panel.get("type", "unknown"))
                for panel in panels
                if isinstance(panel, dict)
            }
        )
        if isinstance(panels, list)
        else []
    )
    return json.dumps(
        {
            "title": str(title)[:200],
            "panel_count": len(panels) if isinstance(panels, list) else 0,
            "panel_types": panel_types,
        }
    )


def _dashboard_audit_result(_result: Any) -> str:
    return "<dashboard bearer URL redacted>"


@audited_tool(
    "render_dashboard",
    args_summarizer=_dashboard_audit_args,
    result_summarizer=_dashboard_audit_result,
)
def render_dashboard(title: str, panels: list[dict]) -> str:
    """Create an interactive dashboard and return a shareable URL.

    Use this whenever the user would benefit from a visual display of data
    rather than a plain-text reply — charts, tables, key metrics, timelines,
    or any combination. The dashboard is hosted at a short-lived URL (7 days)
    that anyone with the link can view.

    Args:
        title: Dashboard title shown at the top of the page.
        panels: List of panel objects. Each panel has a "type" field and
                type-specific data fields. Supported panel types:

            chart — Interactive chart (line, bar, pie, area, scatter).
              {
                "type": "chart",
                "title": "CPU over time",
                "chart_type": "line",
                "labels": ["10:00", "10:05", "10:10"],
                "datasets": [
                  {"label": "web-prod", "data": [42, 45, 38]},
                  {"label": "web-staging", "data": [20, 22, 19]}
                ],
                "x_label": "Time",
                "y_label": "CPU %"
              }

            table — Data table.
              {
                "type": "table",
                "title": "Top endpoints by latency",
                "columns": ["Endpoint", "P50 (ms)", "P99 (ms)", "RPM"],
                "rows": [
                  ["/api/users", 12, 89, 4200],
                  ["/api/orders", 34, 210, 1800]
                ]
              }

            stat — Big-number KPI card with optional trend.
              {
                "type": "stat",
                "title": "Error rate",
                "value": "2.3%",
                "delta": "+0.5%",
                "trend": "up"
              }

            text — Plain text block for context.
              {
                "type": "text",
                "title": "Summary",
                "content": "CPU spiked at 10:05 due to a deploy..."
              }

            list — Key-value pairs or simple items.
              {
                "type": "list",
                "title": "On-call contacts",
                "items": [
                  {"key": "Primary", "value": "@alice"},
                  {"key": "Secondary", "value": "@bob"}
                ]
              }

    Returns:
        The dashboard URL, e.g. "https://agent.example.com/d/f7a2c..."
    """
    return _render_dashboard(title, panels)


def _render_dashboard(title: str, panels: list[dict]) -> str:
    """Validated implementation kept separate from the Strands wrapper for tests."""
    from datetime import datetime, timezone

    validated = _validate_dashboard(title, panels)
    base_url = _dashboard_base_url()
    ctx = get_context()
    token = uuid.uuid4().hex

    spec = {
        "token": token,
        "tenant_id": ctx.get("tenant_id", "unknown"),
        "created_by": ctx.get("user_id", ""),
        "created_at": datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z"),
        "ttl": int(time.time()) + _DASHBOARD_TTL_DAYS * 86400,
        "title": validated["title"],
        "panels": validated["panels"],
    }

    try:
        encoded = json.dumps(
            spec,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid dashboard: spec is not JSON serializable") from exc
    if len(encoded) > _DASHBOARD_MAX_ITEM_BYTES:
        raise ValueError(
            f"Invalid dashboard: serialized spec exceeds {_DASHBOARD_MAX_ITEM_BYTES} bytes"
        )

    _put_dashboard_spec(spec)

    url = f"{base_url}/d/{token}"
    # The URL is a bearer credential. Never put the token or full URL in
    # application logs; return it only to the requesting agent invocation.
    log.info(
        "render_dashboard: created dashboard (%d panels, tenant=%s)",
        len(panels),
        spec["tenant_id"],
    )
    return url


# ----------------------------------------------------------------------------
# Selection
# ----------------------------------------------------------------------------


def build_catalog_tools(
    allowed: list[str],
    tool_config: dict[str, dict[str, Any]] | None = None,
) -> list:
    """Filter the catalog by tenant whitelist.

    `tool_config` is reserved for per-tenant settings consumed by registered
    in-process tools. Credentials and endpoints for remote tools belong in the
    Gateway/MCP configuration, not in this catalog.
    """
    missing = [name for name in allowed if name not in CATALOG]
    if missing:
        log.warning(
            "Tenant config lists tools not registered in CATALOG (skipped): %s",
            missing,
        )
    return [CATALOG[name] for name in allowed if name in CATALOG]
