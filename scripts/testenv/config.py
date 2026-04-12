"""Rich tenant config for the manual-test rig.

``build_testenv_config(tenant_id, channel_map, github_org)`` returns a
full dict matching the shape that ``PATCH /api/tenants/{tenant_id}``
accepts. It's the substantive content that makes the test env feel like
a real customer: a fictional data-ops team ("Acme Data Co") with:

  - Custom system prompt naming the company, stack, and tone
  - All 11 catalog tools enabled
  - Per-channel personas for Q&A and alert-triage channels
  - Three escalation routes (sre, data-eng, security)
  - Three skills (runbook lookup, incident kickoff, oncall status)
  - Bot policy allowing any bot (for seeded PagerDuty/Datadog alerts)
  - Codebases populated when a github_org + installation_id are given
  - is_internal_testenv=True so the ops dashboard hides it

The config is parameterized by ``channel_map`` (name → Slack channel id)
because the escalation routes, bot_policy.open_channels, and channel
personas all need real IDs that don't exist until the channels are
created in the test workspace. The bootstrap script creates/joins the
channels first, then calls this.
"""
from __future__ import annotations

from typing import Any

# Channel names the test env expects to exist in the Slack workspace.
# Order here matches the recommended creation order in the README.
TESTENV_CHANNELS = [
    "alerts-sre",
    "alerts-data",
    "alerts-security",
    "incidents",
    "ask-data",
    "ask-platform",
    "ask-security",
    "oncall",
    "eng-general",
    "eng-random",
]


_SYSTEM_PROMPT = """You are the AgentCore Reference ops assistant for **Acme Data Co**, a ~60-person data & platform team. You live in our Slack workspace and help with three things: triaging alerts and incidents, answering questions about how our systems work, and automating workflow handoffs. You have shared memory across all channels — what you learn in one channel is available in the others.

## About Acme Data Co

- **Stack:** Python + FastAPI services, Postgres (RDS) + Snowflake, dbt for transforms, Airflow for orchestration, Kubernetes on EKS, Terraform for infra, Datadog + Sentry + PagerDuty for observability, GitHub for code.
- **Teams:** SRE (Morgan Chen leads), Data Eng (Priya Ramanathan leads), Security (Alex Diaz), Product (Jamie Park), Platform Eng (Riley Novak, Sam O'Brien, Jordan Webb), On-call rotation (Taylor Kim currently primary).
- **Key services:** `checkout-api`, `orders-api`, `user-service`, `ingest-pipeline`, `reporting-worker`.
- **Critical repos:** `acme-data-api` (the FastAPI backend), `acme-infra` (Terraform for EKS and RDS), `acme-runbooks` (markdown runbooks).

## Core principles

1. **Act, don't narrate.** When given a task, do it with tools. Don't describe what you would do.
2. **Read before you write.** Search history and docs before answering. Read threads before summarizing them. Never answer about code you haven't looked at.
3. **Cite what you find.** When you pull an answer from team history or a runbook, link the permalink or filename.
4. **Parallel over sequential.** When two tool calls are independent (search history AND search code), run them in the same turn.
5. **Match the team's tone.** Direct but warm. No corporate preamble. Lead with the answer, then the evidence.

## How you handle common requests

**Alert lands in an #alerts-* channel (from PagerDuty, Datadog, Sentry, etc.):**
Auto-triage. Search team history for similar past alerts on this service. Search runbooks (`acme-runbooks`) for the service or symptom. If there's a clear known runbook, post a thread reply with the summary and link. If severity is high and no one has acked within a few minutes, offer to `escalate` to the right team (sre for infra/checkout-api/EKS, data-eng for ingest/dbt/Snowflake, security for auth/audit).

**Question in #ask-data or #ask-platform:**
Search team history in the same channel first (someone has probably asked this before). Search docs and the relevant repo (`acme-data-api` for API questions, `acme-infra` for infra questions). Answer concisely and cite. If you don't know, say so and offer to escalate.

**Thread reference ("what's going on with this?", "catch me up"):**
Use `read_thread_context` to pull the full thread, then give a tight summary: what happened, current status, action items, who's on it.

**Incident kickoff ("start incident", `/incident`):**
Read the triggering message/thread, open a new thread in #incidents summarizing the impact and affected services, @-mention the on-call contact from the oncall route.

**Runbook lookup ("show me the runbook for X", `/runbook X`):**
Search `acme-runbooks` for X. If found, summarize the key steps and link the file. If not found, search team history for mentions and synthesize.

**On-call status ("who's oncall", `/oncall`):**
Answer from the oncall escalation route. Taylor Kim is the current primary per our config — confirm from context if there's been a handoff.

## Tool usage

- `search_team_history` — past discussions in a specific Slack channel (pass the channel name)
- `read_thread_context` — the current thread the bot was tagged in
- `search_docs` — placeholder; fall through to `code_search` on `acme-runbooks` for runbook-shaped questions
- `code_search` — lexical search across `acme-data-api` / `acme-infra` / `acme-runbooks`. Run in parallel across repos when unsure which one has the answer.
- `code_read_file` — read a specific file after `code_search` finds it
- `code_find_symbol` — find where a function/class/constant is defined
- `post_to_channel` — cross-channel post (always tell the user where you posted)
- `escalate` — hand off to sre / data-eng / security via the routing table
- `manage_config` — change your own config when users ask ("remember that Priya prefers dbt over Airflow", "trust B123 as a bot", "isolate memory for #secret-project")

## Style

- Slack, not email. One clear paragraph or a short bullet list beats three paragraphs.
- Skip preamble. Don't restate the question. Lead with the answer.
- When uncertain, say so — don't invent.
- When you cross-post or escalate, say where.
- Warm where warmth fits, crisp where speed matters. Match the channel.
"""


_SKILLS: list[dict[str, Any]] = [
    {
        "trigger": r"/runbook\b|show me (?:the )?runbook",
        "name": "runbook_lookup",
        "prompt_template": (
            "The user is asking for a runbook. Search `acme-runbooks` "
            "via code_search first, then search team history in the "
            "current channel for any past mentions. Summarize the key "
            "steps and link the runbook file if found. If not found, "
            "say so and offer to draft one from past incident threads."
        ),
        "required_tools": ["code_search", "code_read_file", "search_team_history"],
        "channels": [],
    },
    {
        "trigger": r"/incident\b|start incident|declare incident",
        "name": "incident_kickoff",
        "prompt_template": (
            "The user is kicking off an incident. Read the triggering "
            "thread with read_thread_context. Identify: affected "
            "service, blast radius, suspected root cause. Post a kickoff "
            "message to #incidents summarizing impact and affected "
            "services, and @-mention the on-call primary from the "
            "escalation route. Ask the user if they want to page a "
            "second responder."
        ),
        "required_tools": ["read_thread_context", "post_to_channel", "escalate"],
        "channels": [],
    },
    {
        "trigger": r"/oncall\b|who(?:'s| is) (?:on[- ]?call|oncall)",
        "name": "oncall_status",
        "prompt_template": (
            "Answer who's currently on-call. The primary on-call is "
            "defined in the escalation.routes table under team_name "
            "'sre' (for infra/SRE pages) and 'data-eng' (for data "
            "pipeline pages). Name the contact(s) and the escalation "
            "channel. If the user asks about a specific service, map "
            "it to the right team first."
        ),
        "required_tools": [],
        "channels": [],
    },
]


_CHANNEL_PERSONAS: dict[str, dict[str, Any]] = {
    # Alert channels: focused on triage, search-heavy tool set.
    "alerts-sre": {
        "system_prompt": (
            "You are on alert-triage duty in #alerts-sre. When a new "
            "alert lands (from PagerDuty, Datadog, Sentry), search "
            "acme-runbooks and team history in parallel for similar "
            "past incidents. Post a thread reply with the runbook link "
            "and past-incident summary. Be terse — oncall is reading "
            "this at 3am."
        ),
        "allowed_tools": [
            "search_team_history", "read_thread_context", "code_search",
            "code_read_file", "escalate", "post_to_channel",
        ],
        "memory_rules": ["incident_learnings", "runbook_pointers", "service_owners"],
    },
    "alerts-data": {
        "system_prompt": (
            "You are on alert-triage duty in #alerts-data. Data pipeline "
            "failures: check acme-runbooks for the failing DAG/model, "
            "check team history for recent similar failures, and cite "
            "the owner. Escalate to data-eng if it's breaking downstream "
            "reporting."
        ),
        "allowed_tools": [
            "search_team_history", "read_thread_context", "code_search",
            "code_read_file", "escalate", "post_to_channel",
        ],
        "memory_rules": ["incident_learnings", "runbook_pointers", "service_owners"],
    },
    "alerts-security": {
        "system_prompt": (
            "You are on alert-triage duty in #alerts-security. Treat "
            "security alerts as high-priority until triaged: search "
            "acme-runbooks for the alert type, read the full thread, "
            "and do NOT post details about credentials or sensitive data "
            "cross-channel. Escalate to Alex on the security team if "
            "uncertain."
        ),
        "allowed_tools": [
            "search_team_history", "read_thread_context", "code_search",
            "escalate",
        ],
        "memory_rules": ["incident_learnings", "runbook_pointers"],
    },

    # Q&A channels: heavy on search, light on write.
    "ask-data": {
        "system_prompt": (
            "You answer questions in #ask-data. Data platform, "
            "Snowflake, dbt, Airflow, ingest pipelines, reporting. "
            "Search team history first (someone has usually asked "
            "this before), then code_search acme-data-api and "
            "acme-runbooks. Cite your sources."
        ),
        "allowed_tools": [
            "search_team_history", "code_search", "code_read_file",
            "code_find_symbol", "read_thread_context",
        ],
        "memory_rules": ["team_preferences", "faq_answers"],
    },
    "ask-platform": {
        "system_prompt": (
            "You answer questions in #ask-platform. Platform eng: "
            "Terraform, EKS, deploys, internal tooling. Search team "
            "history + acme-infra + acme-runbooks. For 'where is X "
            "defined' questions, run code_find_symbol across all "
            "bound repos in parallel."
        ),
        "allowed_tools": [
            "search_team_history", "code_search", "code_read_file",
            "code_find_symbol", "read_thread_context",
        ],
        "memory_rules": ["team_preferences", "faq_answers", "codebase_affinity"],
    },
    "ask-security": {
        "system_prompt": (
            "You answer questions in #ask-security. Auth, SSO, IAM, "
            "access reviews, compliance. Lean on team history and "
            "acme-runbooks. Never echo credentials or session tokens. "
            "Escalate to Alex if the question touches active incidents "
            "or credential rotation."
        ),
        "allowed_tools": [
            "search_team_history", "code_search", "code_read_file",
            "escalate",
        ],
        "memory_rules": ["team_preferences"],
    },

    # Incident channel: full context, all tools, post-heavy.
    "incidents": {
        "system_prompt": (
            "You assist in #incidents. Every message here is about a "
            "live or recent incident. Always read_thread_context first. "
            "Help with: incident kickoff, affected-service lookup, "
            "runbook retrieval, postmortem drafts from the thread. Be "
            "crisp; nobody wants filler during an incident."
        ),
        "allowed_tools": [
            "read_thread_context", "search_team_history", "code_search",
            "code_read_file", "code_find_symbol", "post_to_channel",
            "escalate",
        ],
        "memory_rules": ["incident_learnings", "runbook_pointers", "service_owners"],
    },
}


def build_testenv_config(
    tenant_id: str,
    channel_map: dict[str, str],
    github_org: str | None = None,
    github_installation_id: str | None = None,
) -> dict[str, Any]:
    """Build the full Acme Data Co tenant config dict.

    Args:
      tenant_id: real tenant_id from OAuth (typically ``slack-<team_id>``).
      channel_map: name → Slack channel id for the ten TESTENV_CHANNELS.
      github_org: GitHub org where the forked repos live. When None,
        codebases stays disabled (no bindings populated).
      github_installation_id: numeric install id from the GitHub App
        post-install callback. When both ``github_org`` and this are
        set, ``codebases.enabled`` flips to True.

    Returns:
      Dict ready to PATCH to /api/tenants/{id}. Every deep-mergeable
      section is present.
    """
    # Map channel name → id, falling back to the name itself if the id
    # isn't known. The seeder will refuse to run with unknown channels,
    # so by the time we're called here the map should be complete.
    def cid(name: str) -> str:
        return channel_map.get(name, name)

    # Per-channel persona dict keyed by CHANNEL ID (not name) because
    # that's how the agent looks them up at runtime (from Slack event
    # channel_id).
    channels_dict = {
        cid(name): persona for name, persona in _CHANNEL_PERSONAS.items()
        if name in channel_map
    }

    # Codebases: only populated when both org + installation_id are
    # known. Otherwise bindings stay empty and enabled=False — the
    # tenant still functions without code tools until the user runs
    # the GitHub App install.
    if github_org and github_installation_id:
        codebases = {
            "enabled": True,
            "github_installation_id": str(github_installation_id),
            "default_repo": f"{github_org}/acme-data-api",
            "bindings": [
                {
                    "repo": f"{github_org}/acme-data-api",
                    "default_branch": "main",
                    "aliases": ["acme-data-api", "data-api", "api", "the api", "backend", "fastapi backend"],
                    "channels": [cid("ask-data"), cid("ask-platform"), cid("incidents")],
                },
                {
                    "repo": f"{github_org}/acme-infra",
                    "default_branch": "main",
                    "aliases": ["acme-infra", "infra", "terraform", "infrastructure", "eks config", "rds config"],
                    "channels": [cid("ask-platform"), cid("alerts-sre"), cid("incidents")],
                },
                {
                    "repo": f"{github_org}/acme-runbooks",
                    "default_branch": "main",
                    "aliases": ["acme-runbooks", "runbooks", "the runbooks", "docs", "ops docs"],
                    "channels": [
                        cid("alerts-sre"), cid("alerts-data"), cid("alerts-security"),
                        cid("incidents"), cid("ask-platform"), cid("ask-data"),
                        cid("ask-security"), cid("oncall"),
                    ],
                },
            ],
            "allow_learning": True,
        }
    else:
        codebases = {
            "enabled": False,
            "github_installation_id": None,
            "default_repo": None,
            "bindings": [],
            "allow_learning": True,
        }

    return {
        "model_id": "global.anthropic.claude-sonnet-4-6",
        "system_prompt": _SYSTEM_PROMPT,
        "catalog": {
            "allowed_tools": [
                "echo",
                "start_background_task",
                "search_team_history",
                "read_thread_context",
                "search_docs",
                "post_to_channel",
                "escalate",
                "ask_codebase_choice",
                "code_search",
                "code_read_file",
                "code_find_symbol",
            ],
            "tool_config": {},
        },
        "byo": {
            "enabled": False,
            "gateway_endpoint": None,
            "gateway_auth": None,
            "connected_integrations": [],
        },
        "memory": {
            "triggers": {
                "message_count": 6,
                "token_count": 1000,
                "idle_timeout_seconds": 1800,
            },
            "namespace": f"tenants/{tenant_id}",
            "extraction": {
                "enabled": True,
                "rules": [
                    "team_preferences",
                    "runbook_pointers",
                    "incident_learnings",
                    "service_owners",
                    "codebase_affinity",
                    "faq_answers",
                    "user_preferences",
                    "facts",
                ],
            },
            "isolated_channels": [],
        },
        "heartbeat": {
            "busy_threshold": 1,
            "max_background_seconds": 3600,
        },
        "cost_cap": {
            "monthly_limit_dollars": 100.0,
            "enabled": True,
        },
        "channels": channels_dict,
        "bot_policy": {
            "allow_all_bots": True,
            "trusted_bot_ids": [],
            # All alert channels are open to bots — the seeded alerts
            # post through chat:write.customize but with a real alert
            # feel; this makes the agent's bot-policy path observable
            # in practice.
            "open_channels": [
                cid("alerts-sre"),
                cid("alerts-data"),
                cid("alerts-security"),
            ],
        },
        "context_assembly": {
            "resolve_permalinks": True,
            "inject_thread_history": True,
            "thread_history_depth": 50,
            "max_permalinks": 5,
        },
        "skills": _SKILLS,
        "escalation": {
            "routes": [
                {
                    "team_name": "sre",
                    "channel_id": cid("alerts-sre"),
                    "description": (
                        "Site Reliability Engineering — EKS, RDS, checkout-api, "
                        "orders-api, user-service, Datadog/PagerDuty infra "
                        "alerts, capacity, latency, availability."
                    ),
                    "contacts": ["Morgan Chen", "Taylor Kim"],
                },
                {
                    "team_name": "data-eng",
                    "channel_id": cid("alerts-data"),
                    "description": (
                        "Data Engineering — Snowflake, dbt, Airflow, "
                        "ingest-pipeline, reporting-worker, data quality, "
                        "schema drift, pipeline failures."
                    ),
                    "contacts": ["Priya Ramanathan", "Jordan Webb"],
                },
                {
                    "team_name": "security",
                    "channel_id": cid("alerts-security"),
                    "description": (
                        "Security — auth, SSO, IAM, credential rotation, "
                        "audit findings, suspicious login, access review."
                    ),
                    "contacts": ["Alex Diaz"],
                },
            ],
        },
        "codebases": codebases,
        "is_internal_testenv": True,
    }
