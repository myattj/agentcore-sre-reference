"""Built-in skills that ship with every tenant by default.

These are "reason to install" skills — they work out of the box with zero
config. Tenants don't opt in; they get these automatically. A tenant can
override any built-in by adding a skill with the same ``name`` to their
``skills[]`` config.

Design principles:
  - All regex triggers, no slash commands. The bot recognizes what's
    happening from natural conversation and acts — users don't need to
    learn any commands.
  - Prompts tell the bot to scale its response to the situation. A quick
    mention gets a quick answer; a full incident thread gets a full
    investigation.
  - No channel scoping. The triggers are tight enough to avoid false
    positives; let the bot reason about relevance.
  - Tools referenced in ``required_tools`` are merged into the effective
    tool list at match time, so skills work even if the tenant hasn't
    explicitly whitelisted those tools.
  - Graceful degradation: if a tool returns "not configured" (e.g.
    escalate with no routes, search_docs with no sources), the bot
    adapts. The prompts never hard-require a specific result.
"""
from __future__ import annotations

from tenant import SkillDef

# ---------------------------------------------------------------------------
# Skill definitions
# ---------------------------------------------------------------------------
# Order matters: first match wins. All triggers are regex patterns searched
# against the full message text (case-insensitive via inline (?i) flag).

# NOTE on ordering: first match wins. Specific intents (postmortem,
# status update, handoff, runbook, deploy) MUST come before the broad
# incident-response catch-all, otherwise phrases like "draft a retro
# for the outage" get swallowed by the "outage" pattern.

BUILTIN_SKILLS: list[SkillDef] = [
    # ------------------------------------------------------------------
    # 1. Post-mortem drafter (before incident-response: "outage" overlap)
    # ------------------------------------------------------------------
    SkillDef(
        trigger=(
            r"(?i)("
            r"post.?mort(?:em|al)"
            r"|write (?:up |)(?:the |a |an )(?:retro|rca|root cause|incident (?:report|review))"
            r"|draft (?:the |a |an )(?:retro|postmortem|incident (?:report|review|summary))"
            r"|incident retrospective"
            r"|blameless review"
            r")"
        ),
        name="postmortem-drafter",
        prompt_template=(
            "{user_id} wants a post-mortem drafted in {channel_id}.\n\n"
            "## Your task\n\n"
            "Draft a structured post-mortem from the incident thread and available context.\n\n"
            "### Investigation\n"
            "- Call `read_thread_context` to get the full incident thread.\n"
            "- Call `search_team_history` for related discussions in other channels.\n"
            "- Extract: when it started, who noticed, what was tried, what fixed it, when it resolved.\n\n"
            "### Format:\n\n"
            "**Post-Mortem Draft**\n"
            "**Incident:** [title] | **Date:** [date] | **Severity:** [if known]\n"
            "**Duration:** [start to resolution]\n\n"
            "**Summary** — 2-3 sentences\n\n"
            "**Timeline**\n"
            "| Time | Event |\n"
            "|---|---|\n"
            "| ... | ... |\n\n"
            "**Root Cause** — what broke and why\n\n"
            "**Impact** — users affected, duration, data loss\n\n"
            "**What Went Well** / **What Could Be Improved**\n\n"
            "**Action Items**\n"
            "| Action | Owner | Priority |\n"
            "|---|---|---|\n"
            "| ... | [suggest from thread] | P1/P2/P3 |\n\n"
            "### Rules\n"
            "- This is a DRAFT — remind the user to review and fill gaps.\n"
            "- Pull real timestamps from messages. Mark unknowns as [NEEDS INPUT].\n"
            "- Never assign blame. Post-mortems are blameless.\n"
            "- If the user is just discussing writing a postmortem (not asking you to draft one), respond normally."
        ),
        required_tools=["read_thread_context", "search_team_history", "search_docs"],
        channels=[],
    ),
    # ------------------------------------------------------------------
    # 2. Status update drafter (before incident-response: "outage" overlap)
    # ------------------------------------------------------------------
    SkillDef(
        trigger=(
            r"(?i)("
            r"status (?:page |)update"
            r"|draft (?:a |the |)(?:status|customer).{0,10}(?:update|notice|communication)"
            r"|customer.{0,10}(?:facing |)(?:update|notice|comms?)"
            r"|what do we tell (?:customers|users)"
            r")"
        ),
        name="status-update-drafter",
        prompt_template=(
            "{user_id} needs a customer-facing status update in {channel_id}.\n\n"
            "## Your task\n\n"
            "Draft a status page update based on the current incident context.\n\n"
            "### Investigation\n"
            "- Call `read_thread_context` to understand the current state.\n"
            "- Identify: what's affected, phase (investigating/identified/monitoring/resolved), customer impact.\n\n"
            "### Format:\n\n"
            "**Status Update Draft**\n"
            "**Component:** [service/feature as customers know it]\n"
            "**Status:** Investigating / Identified / Monitoring / Resolved\n\n"
            "[2-4 sentences: what customers are experiencing, what we're doing, when the next update is]\n\n"
            "**Tone check:**\n"
            "- No internal jargon or infra details\n"
            "- Honest about impact without being alarmist\n"
            "- Next update time (if unresolved) or apology (if resolved)\n\n"
            "### Rules\n"
            "- Write for CUSTOMERS, not engineers.\n"
            "- \"Some users may experience slower load times\" not \"Redis cluster failover in us-east-1.\"\n"
            "- Never include: root cause details, internal team names, infrastructure specifics.\n"
            "- If the user is just discussing status updates (not asking for a draft), respond normally."
        ),
        required_tools=["read_thread_context", "search_team_history"],
        channels=[],
    ),
    # ------------------------------------------------------------------
    # 3. Oncall handoff
    # ------------------------------------------------------------------
    SkillDef(
        trigger=(
            r"(?i)("
            r"hand(?:off|ing off|ing over|over)"
            r"|end of (?:my |the )?shift"
            r"|shift (?:change|transition|summary|report)"
            r"|passing the pager"
            r"|taking over oncall"
            r"|oncall (?:transition|summary)"
            r")"
        ),
        name="oncall-handoff",
        prompt_template=(
            "{user_id} is talking about an oncall handoff in {channel_id}.\n\n"
            "## Your task\n\n"
            "Help with the shift transition. Figure out from context whether they're:\n"
            "- Handing off (outgoing) — generate a shift summary\n"
            "- Taking over (incoming) — catch them up on what's active\n"
            "- Just discussing handoff logistics — answer their question normally\n\n"
            "### For a shift summary:\n"
            "- Call `search_team_history` in {channel_id} to find recent activity. "
            "Use your judgment on what search terms matter based on the channel.\n"
            "- For any open threads or active incidents, call `read_thread_context` to get status.\n\n"
            "**Oncall Handoff Summary**\n"
            "**Outgoing:** {user_id}\n\n"
            "**Open incidents:**\n"
            "- **[Title]** — severity if known\n"
            "  - Status / what's been tried / what's pending / who's involved\n\n"
            "**Resolved during shift:**\n"
            "- [description] — what fixed it\n\n"
            "**Heads up for next shift:**\n"
            "- [anything worth watching — derive from patterns in the activity]\n\n"
            "### Rules\n"
            "- Clean shift? Say so. That's useful information.\n"
            "- Keep items to 2-3 lines. The incoming oncall needs to scan fast.\n"
            "- Only include sections that have content."
        ),
        required_tools=["search_team_history", "read_thread_context"],
        channels=[],
    ),
    # ------------------------------------------------------------------
    # 4. Runbook lookup
    # ------------------------------------------------------------------
    SkillDef(
        trigger=(
            r"(?i)("
            r"runbook"
            r"|playbook"
            r"|remediation steps"
            r"|how do (?:I|we) (?:fix|resolve|remediate|restart|recover)"
            r"|steps to (?:fix|resolve|recover|restart|remediate)"
            r"|what.{0,5}the (?:fix|procedure|process) for"
            r")"
        ),
        name="runbook-lookup",
        prompt_template=(
            "{user_id} is looking for operational guidance in {channel_id}.\n\n"
            "## Your task\n\n"
            "Find the relevant runbook or remediation steps and walk them through it.\n\n"
            "### Investigation\n"
            "- Call `read_thread_context` to understand the situation.\n"
            "- Call `search_docs` for matching runbooks or operational docs.\n"
            "- Call `search_team_history` for how this was handled before.\n\n"
            "### If you find a matching runbook:\n\n"
            "**Runbook: [title]**\n"
            "**Source:** [where you found it]\n\n"
            "**Steps:**\n"
            "1. [ ] [step with specific commands]\n"
            "2. [ ] [next step]\n\n"
            "**Verification:** how to confirm it's fixed\n"
            "**Escalation:** who to contact if this doesn't work\n\n"
            "### If no runbook exists:\n"
            "Say so, share what you found from past incidents, and suggest creating one after resolution.\n\n"
            "### Rules\n"
            "- Show commands in code blocks, ready to copy-paste.\n"
            "- Flag destructive steps clearly (restarts, failovers, data changes).\n"
            "- Don't dump the whole runbook — present steps in logical groups."
        ),
        required_tools=["search_docs", "search_team_history", "read_thread_context"],
        channels=[],
    ),
    # ------------------------------------------------------------------
    # 5. Deploy watchdog
    # ------------------------------------------------------------------
    SkillDef(
        trigger=(
            r"(?i)\b("
            r"just deployed"
            r"|deployed to"
            r"|shipping to prod"
            r"|rolled out to"
            r"|rollout complete"
            r"|deploy complete"
            r"|pushed to prod"
            r")\b"
        ),
        name="deploy-watchdog",
        prompt_template=(
            "A deployment was mentioned by {user_id} in {channel_id}.\n\n"
            "## Your task\n\n"
            "Help the team assess this deploy. Only activate if a deploy is actually happening — "
            "if the word was used casually, just respond normally.\n\n"
            "### Investigation\n"
            "- Call `read_thread_context` to understand what was deployed.\n"
            "- If a repo is mentioned, call `code_list_commits` to see what changed.\n"
            "- Call `search_team_history` for recent incidents involving the same service.\n"
            "- Call `search_docs` for known issues with the deployed service.\n\n"
            "### Structure your response as:\n\n"
            "**Deploy Watch: [service name]**\n"
            "| Field | Detail |\n"
            "|---|---|\n"
            "| **Service** | ... |\n"
            "| **What changed** | summary |\n"
            "| **Key changes** | notable commits/PRs |\n\n"
            "**Risk signals:**\n"
            "- [green/yellow/red] [area] — [why]\n\n"
            "**What to watch:**\n"
            "- [specific metric/behavior] — why it matters for these changes\n\n"
            "**Recent incidents with this service:**\n"
            "- [any found, or none]\n\n"
            "**Rollback trigger:**\n"
            "- [condition that should trigger a rollback]\n\n"
            "### Rules\n"
            "- Be specific — tie watch items to actual changes, not generic advice.\n"
            "- If you can't identify what was deployed, ask."
        ),
        required_tools=["search_team_history", "search_docs", "read_thread_context", "code_list_commits"],
        channels=[],
    ),
    # ------------------------------------------------------------------
    # 6. Incident response — LAST because it's the broadest catch-all.
    #    Specific intents (postmortem, status update, handoff, runbook,
    #    deploy) must match before this swallows them.
    # ------------------------------------------------------------------
    SkillDef(
        trigger=(
            r"(?i)("
            r"alert fir(?:ed|ing)"
            r"|outage"
            r"|incident.{0,5}(?:reported|opened|declared)"
            r"|(?:service|system|api|app|site|endpoint).{0,20}(?:is |went |going )down"
            r"|pages? going off"
            r"|p[0-4]\b"
            r"|sev[- ]?[1-4]\b"
            r")"
        ),
        name="incident-response",
        prompt_template=(
            "An incident or alert was reported by {user_id} in {channel_id}.\n\n"
            "## Your task\n\n"
            "Respond to this incident. Scale your response to the situation:\n"
            "- If it's a quick alert mention, give a fast severity read + immediate next steps.\n"
            "- If it's a full incident report with context, do a thorough investigation.\n"
            "- If the user is asking for help mid-incident, read the thread and figure out what they need.\n\n"
            "### Investigation steps\n\n"
            "**1. Gather context** (run these in parallel):\n"
            "- Call `read_thread_context` to understand what's happening — look for metric names, "
            "service tags, error messages, stack traces, and dashboard links.\n"
            "- Call `search_team_history` for similar past incidents — how were they resolved?\n"
            "- Call `search_docs` for relevant runbooks or known-issue documentation.\n\n"
            "**2. Pull production metrics** (if monitoring tools are available):\n"
            "- If your tools include `query_metrics`, query the relevant metrics around the incident "
            "timeframe. Look for anomalies, spikes, or drops that correlate with the reported symptoms.\n"
            "- If your tools include `get_recent_alerts`, check for related alerts that fired around "
            "the same time — this can reveal cascading failures or shared root causes.\n"
            "- If your tools include `search_logs`, search for error patterns in the affected service's logs.\n"
            "- If these tools are not available, note that no monitoring integration is connected and "
            "work with the context you have.\n\n"
            "**3. Cross-reference with code changes:**\n"
            "- Call `code_list_commits` for the affected service's repo, filtering to the timeframe "
            "around the incident start. Recent deploys are the most common cause of production issues.\n"
            "- If you find a suspicious commit, call `code_read_file` to inspect the changed files.\n"
            "- Call `code_search` to find related code if you have error messages or function names "
            "from the thread or logs.\n"
            "- If you can't identify the right repo, call `ask_codebase_choice` to ask the user.\n\n"
            "**4. Synthesize findings:**\n"
            "- Correlate the metrics timeline with code changes. A deploy at 2:13 PM followed by a "
            "latency spike at 2:15 PM is a strong signal.\n"
            "- If you find the likely cause, link to the specific commit and file.\n\n"
            "### For a full triage, structure your response as:\n\n"
            "**Incident Triage**\n"
            "| Field | Detail |\n"
            "|---|---|\n"
            "| **Service/system** | what's affected |\n"
            "| **Symptom** | what users/monitors are seeing |\n"
            "| **Severity** | SEV-1 critical / SEV-2 major / SEV-3 minor / SEV-4 low — why |\n"
            "| **Blast radius** | who/what is impacted |\n"
            "| **Started** | when, if known |\n\n"
            "**Metrics** (if monitoring tools are available):\n"
            "- [metric name] — [what it shows, when the anomaly started]\n\n"
            "**Likely cause** (ranked):\n"
            "1. [cause] — [evidence: commit hash, metric correlation, log pattern]\n\n"
            "**Code changes in window:**\n"
            "- [commit] by [author] at [time] — [summary of what changed]\n\n"
            "**Similar past incidents:**\n"
            "- [reference] — what happened, how it was fixed\n\n"
            "**Next steps:**\n"
            "1. ...\n\n"
            "**Runbooks found:**\n"
            "- [title] — relevance\n\n"
            "### Rules\n"
            "- If this looks SEV-1 or SEV-2, suggest escalation via the `escalate` tool.\n"
            "- Be concise. SREs are busy during incidents.\n"
            "- If there isn't enough info to assess, ask 2-3 targeted questions.\n"
            "- If the mention is casual (not a real incident report), just respond normally.\n"
            "- Never fabricate metric values, commit hashes, or log entries. Only report what "
            "the tools actually returned."
        ),
        required_tools=[
            "search_team_history",
            "search_docs",
            "read_thread_context",
            "escalate",
            "code_search",
            "code_read_file",
            "code_list_commits",
            "code_find_symbol",
        ],
        channels=[],
    ),
]


def merge_skills(
    tenant_skills: list[SkillDef],
    builtins: list[SkillDef] | None = None,
) -> list[SkillDef]:
    """Merge tenant-configured skills with built-in defaults.

    Tenant skills override built-ins by ``name``: if a tenant defines a
    skill with the same name as a built-in, the tenant version wins.
    Tenant skills come first (higher priority in first-match-wins order),
    then un-overridden built-ins.
    """
    if builtins is None:
        builtins = BUILTIN_SKILLS
    overridden = {s.name for s in tenant_skills}
    return list(tenant_skills) + [b for b in builtins if b.name not in overridden]
