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
# CloudWatch Logs stream and roll up into the same AgentCore Reference/Agent namespace.
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
                        "tool_args_summary": _summarize_args(args, kwargs),
                        "tool_result_summary": (
                            repr(result) if success else (error_text or "")
                        ),
                        "duration_ms": int((time.time() - start) * 1000),
                        "success": success,
                        "slack_message_link": (
                            f"https://slack.com/archives/{_ch}/p{_th.replace('.', '')}"
                            if _ch and _th else ""
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


@audited_tool("search_team_history")
def search_team_history(query: str, channel_id: str | None = None, limit: int = 20) -> str:
    """Search past Slack messages in a channel for messages matching a keyword query.

    Args:
        query: Keywords to search for in message text.
        channel_id: Slack channel ID to search. If omitted, uses the current channel.
        limit: Maximum number of messages to return (default 20, max 100).
    """
    from slack_api import fetch_channel_history, get_bot_token

    ctx = get_context()
    cid = channel_id or ctx.get("channel_id", "")
    if not cid:
        return "Error: no channel_id available. Pass channel_id explicitly."
    tenant_id = ctx.get("tenant_id", "")
    token = get_bot_token(tenant_id)
    if not token:
        return "Error: no Slack bot token configured for this tenant."
    return fetch_channel_history(token, cid, query, min(limit, 100))


@audited_tool("read_thread_context")
def read_thread_context(channel_id: str | None = None, thread_id: str | None = None) -> str:
    """Fetch the full conversation thread from Slack.

    Call this whenever the user references "this thread" or "this conversation"
    to get the full context before responding.

    Args:
        channel_id: Slack channel ID. If omitted, uses the current channel.
        thread_id: Slack thread timestamp. If omitted, uses the current thread.
    """
    from slack_api import fetch_thread_replies, get_bot_token

    ctx = get_context()
    cid = channel_id or ctx.get("channel_id", "")
    tid = thread_id or ctx.get("thread_id", "")
    if not cid or not tid:
        return "Error: both channel_id and thread_id are required."
    tenant_id = ctx.get("tenant_id", "")
    token = get_bot_token(tenant_id)
    if not token:
        return "Error: no Slack bot token configured for this tenant."
    return fetch_thread_replies(token, cid, tid)


@audited_tool("search_docs")
def search_docs(query: str) -> str:
    """Search across all configured documentation sources.

    This tool checks which documentation integrations are available (Confluence,
    Notion, etc.) and lists them so you can call their specific search tools.
    Use the individual integration tools (e.g. confluence search_content,
    notion search) to perform the actual searches.

    Args:
        query: The search query to run across doc sources.
    """
    return (
        f"To search for '{query}', use the available documentation tools from "
        "your connected integrations. Check your tool list for Confluence "
        "(search_content), Notion (search), or other doc tools, and call each "
        "one individually with this query."
    )


@audited_tool("post_to_channel")
def post_to_channel(channel_id: str, message: str, thread_ts: str | None = None) -> str:
    """Post a message to any Slack channel the bot is a member of.

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
    return slack_api.post_message(token, channel_id, message, thread_ts)


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
                    parts.append(
                        f" — **pinned to THIS channel** ({pinned})"
                    )
                else:
                    parts.append(f" — pinned channels: {pinned}")
            else:
                parts.append(" — no channel pins")
            lines.append("".join(parts))
        if codebases.default_repo:
            lines.append(
                f"\nInstall-time default: `{codebases.default_repo}`"
            )
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
            channel_section.append(
                f"- Name: #{name}" if name else "- Name: (unknown)"
            )
            channel_section.append(
                f"- Type: {'private' if is_private else 'public'}"
            )
            channel_section.append(
                f"- Topic: {topic}" if topic else "- Topic: (not set)"
            )
            channel_section.append(
                f"- Purpose: {purpose}"
                if purpose
                else "- Purpose: (not set)"
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
                f"- Display name: {display}"
                if display
                else "- Display name: (not set)"
            )
            user_section.append(
                f"- Real name: {real}"
                if real
                else "- Real name: (not set)"
            )
            user_section.append(
                f"- Title: {title}" if title else "- Title: (not set)"
            )
        else:
            user_section.append(
                "no signal — users.info returned no data "
                "(missing users:read scope, or user not visible)"
            )
    else:
        user_section.append(
            "no signal — missing Slack token or user_id in context"
        )
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
            user_id=user_id,
        )
        if hint:
            memory_section.append(
                f"- Most recently used repo in this scope: `{hint}`"
            )
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
    if not installation_id:
        return (
            "Error: this tenant has not installed the AgentCore Reference GitHub App "
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
    if not installation_id:
        return (
            "Error: this tenant has not installed the AgentCore Reference GitHub App "
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

    header = (
        f"=== {file.repo}/{file.path} @ {file.ref} ({file.size} bytes) ==="
    )
    suffix = (
        "\n\n[truncated — file is larger than the 64 KB cap]"
        if file.truncated
        else ""
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
    if not installation_id:
        return (
            "Error: this tenant has not installed the AgentCore Reference GitHub App "
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
    if not installation_id:
        return (
            "Error: this tenant has not installed the AgentCore Reference GitHub App "
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
        return (
            f"Error: code_list_commits failed unexpectedly: "
            f"{type(e).__name__}: {e}"
        )

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
            f"{i}. {c.short_sha} — {c.message}  "
            f"[{c.author}, {date}]  ({c.html_url})"
        )
    return "\n".join(lines)


@audited_tool("manage_config")
def manage_config(action: str, section: str, data: str | None = None) -> str:
    """View or update this bot's configuration.

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
        "skills", "bot_policy", "escalation", "context_assembly",
        "catalog_tools", "system_prompt", "channels", "all",
    }
    if section not in allowed_sections:
        return f"Error: unknown section '{section}'. Choose from: {sorted(allowed_sections)}"

    if action == "view":
        config = load_tenant_config(tenant_id)
        if section == "all":
            return json.dumps({
                "skills": [s.model_dump() for s in config.skills],
                "bot_policy": config.bot_policy.model_dump(),
                "escalation": config.escalation.model_dump(),
                "context_assembly": config.context_assembly.model_dump(),
                "catalog_tools": config.catalog.allowed_tools,
                "channels": {cid: p.model_dump() for cid, p in config.channels.items()},
                "system_prompt": config.system_prompt[:200] + (
                    "..." if len(config.system_prompt) > 200 else ""
                ),
            }, indent=2)
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
        if section == "all":
            return "Error: cannot update 'all' at once. Update individual sections."
        if not data:
            return "Error: 'data' is required for updates. Provide a JSON string."
        try:
            parsed = json.loads(data)
        except json.JSONDecodeError as e:
            return f"Error: invalid JSON in data: {e}"

        config = load_tenant_config(tenant_id)
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
                config.catalog.allowed_tools = parsed
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
