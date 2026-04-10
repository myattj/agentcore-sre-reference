"""Pre-LLM context assembly pipeline.

Runs between the channel-persona merge and Agent construction in main.py.
Assembles additional context that the LLM needs to produce a good response:

  1. **Permalink resolution** — detects Slack permalink URLs passed in
     ``ctx["permalinks"]`` (extracted by the bridge), fetches the referenced
     threads via Slack API, and prepends them as context blocks.

  2. **Thread history injection** — fetches the current Slack thread's
     recent messages so the agent has conversational continuity even for
     messages sent before it was mentioned.

  3. **Skill matching** — checks the user message against the tenant's
     configured skills (slash-command prefix or regex trigger). If matched,
     the skill's prompt template is appended to the system prompt and its
     required tools are merged into the effective tool list.

Each step is independently toggleable via ``TenantConfig.context_assembly``.
All Slack API calls are best-effort: failures are logged and skipped so the
agent still processes the message (just with less context).
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any

from . import slack_api
from .tenant import ContextAssemblyConfig, SkillDef

log = logging.getLogger(__name__)

# Reuse a small thread pool for parallel permalink fetching.
_executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="ctx-asm")

# Per-message character limit in injected context to avoid token bloat.
_MAX_MSG_CHARS = 500


@dataclass
class SkillMatch:
    """Result of a successful skill trigger match."""
    name: str
    prompt_addition: str
    required_tools: list[str] = field(default_factory=list)


@dataclass
class AssembledContext:
    """Output of the context assembly pipeline."""
    enriched_message: str
    system_prompt: str
    extra_tools: list[str] = field(default_factory=list)
    matched_skill: str | None = None


def assemble_context(
    user_message: str,
    ctx: dict[str, Any],
    assembly_config: ContextAssemblyConfig,
    skills: list[SkillDef],
    effective_prompt: str,
    tenant_id: str,
) -> AssembledContext:
    """Run the full context assembly pipeline.

    Called from ``main.py`` between the channel-persona merge and the
    ``Agent()`` constructor. Returns an ``AssembledContext`` with the
    enriched message, possibly-modified system prompt, and any extra
    tools required by a matched skill.
    """
    context_blocks: list[str] = []

    # Step 1: Resolve permalinks
    if assembly_config.resolve_permalinks:
        block = _resolve_permalinks(ctx, assembly_config, tenant_id)
        if block:
            context_blocks.append(block)

    # Step 2: Thread history injection
    if assembly_config.inject_thread_history:
        block = _inject_thread_history(ctx, assembly_config, tenant_id)
        if block:
            context_blocks.append(block)

    # Step 3: Skill matching
    skill_match = _match_skill(user_message, skills, ctx)
    if skill_match:
        effective_prompt += (
            f"\n\n## Active Skill: {skill_match.name}\n\n"
            f"{skill_match.prompt_addition}"
        )

    # Assemble enriched message
    if context_blocks:
        enriched = "\n\n".join(context_blocks) + "\n\n---\n\n" + user_message
    else:
        enriched = user_message

    return AssembledContext(
        enriched_message=enriched,
        system_prompt=effective_prompt,
        extra_tools=skill_match.required_tools if skill_match else [],
        matched_skill=skill_match.name if skill_match else None,
    )


# ---------------------------------------------------------------------------
# Step 1: Permalink resolution
# ---------------------------------------------------------------------------

def _resolve_permalinks(
    ctx: dict[str, Any],
    config: ContextAssemblyConfig,
    tenant_id: str,
) -> str:
    """Resolve permalink URLs into thread content blocks."""
    permalinks: list[str] = ctx.get("permalinks", [])
    if not permalinks:
        return ""

    token = slack_api.get_bot_token(tenant_id)
    if not token:
        return ""

    # Cap the number of permalinks to resolve.
    permalinks = permalinks[: config.max_permalinks]

    # Parse permalink URLs into (channel_id, thread_ts) pairs.
    targets: list[tuple[str, str, str]] = []  # (url, channel_id, thread_ts)
    for url in permalinks:
        parsed = slack_api.parse_permalink(url)
        if parsed:
            targets.append((url, parsed[0], parsed[1]))

    if not targets:
        return ""

    # Fetch threads in parallel.
    blocks: list[str] = []
    futures = {
        _executor.submit(
            slack_api.fetch_thread_replies, token, channel_id, thread_ts
        ): url
        for url, channel_id, thread_ts in targets
    }
    for future in as_completed(futures):
        url = futures[future]
        try:
            thread_text = future.result()
            if thread_text and "error" not in thread_text.lower()[:20]:
                blocks.append(
                    f"## Referenced Thread\n**Source:** {url}\n\n{thread_text}"
                )
        except Exception:
            log.warning("Failed to resolve permalink %s", url, exc_info=True)

    return "\n\n".join(blocks)


# ---------------------------------------------------------------------------
# Step 2: Thread history injection
# ---------------------------------------------------------------------------

def _inject_thread_history(
    ctx: dict[str, Any],
    config: ContextAssemblyConfig,
    tenant_id: str,
) -> str:
    """Fetch the current thread's recent messages for conversational continuity."""
    channel_id = ctx.get("channel_id")
    thread_id = ctx.get("thread_id")
    if not channel_id or not thread_id:
        return ""

    token = slack_api.get_bot_token(tenant_id)
    if not token:
        return ""

    try:
        messages = slack_api.fetch_thread_replies_raw(
            token, channel_id, thread_id,
            limit=config.thread_history_depth,
        )
    except Exception:
        log.warning("Failed to fetch thread history", exc_info=True)
        return ""

    if not messages or len(messages) <= 1:
        # Single message = the current message itself, no history to inject.
        return ""

    # Format all messages except the last (which is the current message
    # the user just sent — avoid duplicating it).
    lines: list[str] = []
    for m in messages[:-1]:
        user = m.get("user", m.get("bot_id", "unknown"))
        text = m.get("text", "")
        if len(text) > _MAX_MSG_CHARS:
            text = text[:_MAX_MSG_CHARS] + "..."
        lines.append(f"**{user}**: {text}")

    if not lines:
        return ""

    return (
        f"## Current Thread History ({len(lines)} prior messages)\n\n"
        + "\n\n".join(lines)
    )


# ---------------------------------------------------------------------------
# Step 3: Skill matching
# ---------------------------------------------------------------------------

# Cache compiled regex patterns to avoid recompiling on every invocation.
_compiled_triggers: dict[str, re.Pattern[str]] = {}


def _match_skill(
    user_message: str,
    skills: list[SkillDef],
    ctx: dict[str, Any],
) -> SkillMatch | None:
    """Match the user message against configured skills. First match wins.

    Skills with a non-empty ``channels`` list only fire in those channels.
    Skills with an empty ``channels`` list fire everywhere.
    """
    text = user_message.strip()
    if not text or not skills:
        return None

    channel_id = ctx.get("channel_id", "")

    for skill in skills:
        # Channel whitelist: skip if this skill is restricted and we're
        # not in one of the whitelisted channels.
        if skill.channels and channel_id not in skill.channels:
            continue

        trigger = skill.trigger
        if trigger.startswith("/"):
            # Slash-command: exact prefix match
            if text.lower().startswith(trigger.lower()):
                return _build_skill_match(skill, ctx)
        else:
            # Regex match
            pattern = _compiled_triggers.get(trigger)
            if pattern is None:
                try:
                    pattern = re.compile(trigger, re.IGNORECASE)
                except re.error:
                    log.warning("Invalid skill trigger regex: %s", trigger)
                    continue
                _compiled_triggers[trigger] = pattern
            if pattern.search(text):
                return _build_skill_match(skill, ctx)

    return None


def _build_skill_match(skill: SkillDef, ctx: dict[str, Any]) -> SkillMatch:
    """Build a SkillMatch with resolved placeholders in the prompt template."""
    placeholders = defaultdict(
        str,
        {
            "user_id": ctx.get("user_id", ""),
            "channel_id": ctx.get("channel_id", ""),
            "thread_id": ctx.get("thread_id", ""),
            "workspace_id": ctx.get("workspace_id", ""),
        },
    )
    try:
        resolved = skill.prompt_template.format_map(placeholders)
    except (KeyError, ValueError):
        # If the template has bad placeholders, use it as-is.
        resolved = skill.prompt_template

    return SkillMatch(
        name=skill.name,
        prompt_addition=resolved,
        required_tools=list(skill.required_tools),
    )
