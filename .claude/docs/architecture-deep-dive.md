# Architecture Deep Dive

> Reference doc — read on demand. CLAUDE.md has the condensed version.

## Runtime path

A single user message, end to end:

1. **Slack** posts to `POST /slack/events` (or any other adapter's route)
2. **`bridge/main.py`** parses via the appropriate `Adapter` (extracts `bot_id`, `subtype`, `permalinks` from the event), returns 200 within 3s, queues a `BackgroundTask`
   - **Bot policy check:** if the event has a `bot_id`, evaluates the tenant's `BotPolicyConfig` (trusted bots → open channels → block). Blocked bots are dropped before dispatch, saving Bedrock spend.
3. **`async_dispatcher.dispatch_async`** runs in the background:
   - Calls `tenant_resolver.resolve_tenant_id(workspace_id)`
   - Calls `client.invoke(tenant_id=…, prompt=…, ctx={…})` — ctx now includes `bot_id` and `permalinks`
4. **`bridge/client.py`** chooses transport:
   - If `LOCAL_AGENT_URL` is set → HTTP POST to `agentcore dev` server (local dev path)
   - Otherwise → `boto3.bedrock-agentcore.invoke_agent_runtime` (production path)
5. **Agent** receives the payload at `coreAgent/app/coreAgent/main.py:invoke()`:
   - `load_tenant_config(tenant_id)` reads `examples/tenants/<id>.json`
   - Channel persona merge: override system_prompt, tools, memory rules per channel
   - **Context assembly pipeline** (`context_assembler.py`):
     - Resolves Slack permalinks in the message → fetches referenced threads
     - Injects current thread history (prior messages for conversational continuity)
     - Matches against tenant-defined skills → injects runbook prompt + merges required tools
   - `build_catalog_tools(allowed)` filters `tools.TOOL_REGISTRY` by the (possibly skill-augmented) whitelist
   - `build_byo_mcp_client(gateway_endpoint, auth)` returns a Strands `MCPClient` (or `None`)
   - Constructs a fresh `Agent(model=…, system_prompt=…, tools=[*catalog, mcp_client])` per invocation
   - `agent.stream_async(prompt)` → streams text chunks (prompt is now enriched with context blocks)
   - As chunks stream, accumulates them and **`yield`s** each to the runtime
6. **Memory** is handled by the `AgentCoreMemorySessionManager` (when `AGENTCORE_MEMORY_ID` is set):
   - Before agent processing: retrieves relevant long-term memories via semantic search and injects into context
   - After agent processing: saves the conversation turn as an event via `create_event()`
   - AgentCore's built-in SEMANTIC + USER_PREFERENCE strategies handle extraction automatically
   - Local dev fallback (`AGENT_LOCAL_STORES=1`): inline `extract_records()` + `InMemoryStore`
7. **Bridge** receives the buffered response, calls `adapter.reply(inbound, OutboundMessage(text))`
8. **Slack adapter** posts the reply via `chat.postMessage` (real impl) or stdout (current stub)

**Heartbeat side-channel** (parallel to the above): AgentCore Runtime polls `GET /ping` on the agent every few seconds. The `@app.ping` handler in `ping.py` checks `_inflight_tasks` (populated by `tools.start_background_task` and friends) and returns `HEALTHY_BUSY` while any tool is running in the background. This keeps the session alive for up to 8 hours.

---

## The six pillars of customization

### 1. Tools / actions
Two layers, both belong to the **agent**, not the bridge:

**Catalog** — `coreAgent/app/coreAgent/tools.py`
- In-process Python `@tool` functions in `TOOL_REGISTRY`
- Platform-owned. Adding a new catalog tool = agent redeploy.
- Tenant selects via `catalog.allowed_tools`; passes per-tool config via `catalog.tool_config`
- Tools that need request-specific context (current Slack thread, channel) read from `payload.ctx` which the bridge populates

**BYO** — `coreAgent/app/coreAgent/mcp_client/client.py`
- Customer registers Lambda functions / OpenAPI specs / Smithy models / their own MCP server with **AgentCore Gateway**
- Gateway exposes them as MCP-compatible endpoints
- Agent connects to the tenant's Gateway endpoint at invocation time as an MCP client
- New BYO tool = customer change, no agent redeploy
- The agent treats catalog and BYO tools identically once they're in the `tools=[…]` list

**Decision rule:** if it's something every tenant might want (web search, document parsing, math), make it a catalog tool. If it's tenant-specific business logic ("look up a record in Acme's Salesforce"), make it BYO via Gateway.

### 2. Memory
**Built-in strategies via AgentCore Memory SDK.** A single shared memory resource (`agentcore_shared_memory`) serves all tenants, provisioned by `infra/data/scripts/provision_memory.py`. Two built-in strategies handle extraction automatically:

- **SEMANTIC** — extracts factual memories from conversations
- **USER_PREFERENCE** — extracts user preferences

The `AgentCoreMemorySessionManager` (from `bedrock_agentcore.memory.integrations.strands`) hooks into the Strands Agent:
- **Before processing:** retrieves relevant long-term memories via semantic search and injects them into the conversation context
- **After processing:** saves the conversation turn as an event via `create_event()`; AgentCore's strategies extract records asynchronously

**Namespace mapping (workspace-per-channel):**
- Channels: `actorId = {tenant_id}_{channel_id}` — everyone in a channel shares memory
- DMs: `actorId = {tenant_id}_{user_id}` — personal memory
- `sessionId = thread_id` or `invocation_id` — groups a conversation thread

**Per-tenant isolation:** `actorId` prefix ensures `acme_C04INCIDENTS` can't retrieve `widgets_C04ENGINEERING` memories. Do NOT create one Memory resource per tenant — quota pain.

**Local dev fallback (AGENT_LOCAL_STORES=1):** `InMemoryStore` + inline `extract_records()` in `memory_store.py` — zero AWS cost, records lost on restart.

### 3. Heartbeat
**`@app.ping` custom handler** in `ping.py` returns `HEALTHY_BUSY` while any background task is in flight, keeping the session alive past the 15-minute idle timeout.

The cycle:
1. A tool wants to do long work → calls `app.add_async_task(task_id)` and adds the ID to `ping._inflight_tasks`
2. Spawns a daemon thread to do the actual work
3. `@app.ping` reports `HEALTHY_BUSY` while that set is non-empty
4. AgentCore Runtime keeps the session alive (up to 8 hours)
5. Thread finishes → calls `app.complete_async_task(task_id)` and removes from set
6. Next `@app.ping` returns `HEALTHY` again
7. After 15 min of `HEALTHY` with no traffic, the session terminates

**Critical rule:** **never block in `@app.entrypoint`**. If you do, `/ping` stalls and the runtime marks the agent unhealthy. All long work goes through `add_async_task` + a background thread.

### 4. Context Assembly
**Pre-LLM context pipeline** in `context_assembler.py`. Runs between channel-persona merge and Agent construction. Three steps, each independently toggleable via `TenantConfig.context_assembly`:

- **Permalink resolution** — detects Slack permalink URLs (passed from bridge via `ctx["permalinks"]`), fetches the referenced threads via `slack_api.fetch_thread_replies()`, prepends them as context blocks. Parallel fetching via 3-thread pool, capped at `max_permalinks` (default 3).
- **Thread history injection** — fetches the current Slack thread's recent messages via `fetch_thread_replies_raw()` so the agent has conversational continuity. Excludes the current message to avoid duplication. Depth controlled by `thread_history_depth` (default 25).
- **Skill matching** — see pillar 5 below.

The assembled context is prepended to the user message with a `---` separator. The LLM sees: `[context blocks] --- [original message]`.

### 5. Skills / Runbooks
**Tenant-defined structured workflows** stored as `SkillDef` entries in the `skills` list on `TenantConfig`. Each skill has:
- `trigger` — slash-command prefix (e.g. `/oncall-start`) or regex pattern (e.g. `(?i)escalate\s+to`)
- `name` — human-readable, logged for audit
- `prompt_template` — markdown injected into the system prompt when triggered. Supports `{user_id}`, `{channel_id}`, `{thread_id}`, `{workspace_id}` placeholders.
- `required_tools` — merged with the channel's effective tool list so the skill's tools are available even if not in the base whitelist

Matching is first-match-wins (list order matters). Slash-command triggers do case-insensitive prefix match; regex triggers are compiled and cached. Bad regex patterns are logged and skipped.

**Decision rule:** if it's a workflow any team might follow (on-call handoff, known-issues check, escalation), define it as a skill in the tenant config. If it's a one-time action ("search Slack for X"), let the LLM decide.

### 6. Bot Policy + Escalation
**Bot-to-bot interaction** is controlled by `BotPolicyConfig` (evaluated bridge-side before dispatch):
- `trusted_bot_ids` — explicitly allowed bots
- `open_channels` — any bot can trigger in these channels (alert channels)
- Default: humans only

**Escalation routing** is a configurable table in `EscalationConfig.routes`, consumed by the `escalate` catalog tool. Each route maps a team name → Slack channel + contacts. The tool formats and posts the escalation with @mentions.

**Cross-channel posting** via the `post_to_channel` catalog tool. Posts to any channel the bot is a member of. Both tools must be explicitly whitelisted in `catalog.allowed_tools`.

---

## File tree

```
agentcorePlayground/
├── coreAgent/                       # AgentCore Runtime agent
│   ├── agentcore/                   # CLI-managed: agentcore.json, aws-targets.json, CDK
│   │   └── ⚠️ DO NOT edit cdk/      # auto-generated; modify the JSON files instead
│   └── app/coreAgent/
│       ├── main.py                  # @app.entrypoint — DO NOT block here
│       ├── runtime.py               # shared `app = BedrockAgentCoreApp()` (avoids circular imports)
│       ├── tenant.py                # TenantStore Protocol (JSON | DynamoDB); load_tenant_config
│       ├── tools.py                 # ➕ ADD CATALOG TOOLS HERE (use @audited_tool)
│       ├── context_assembler.py     # Pre-LLM context pipeline: permalinks, thread history, skills
│       ├── ping.py                  # @app.ping logic + _inflight_tasks set
│       ├── memory_store.py          # Local dev: InMemoryStore + extract_records (prod uses SDK session manager)
│       ├── audit.py                 # AuditStore protocol + Null/InMemory/Dynamo impls
│       ├── request_context.py       # ContextVar-backed per-invocation context (+ escalation_routes)
│       ├── model/load.py            # Bedrock model loader (tenant-driven model_id)
│       └── mcp_client/client.py     # BYO MCP client factory (used for Gateway)
├── bridge/                          # FastAPI bridge
│   ├── tests/                       # pytest suite (LOCAL_DEV by default, no real AWS)
│   └── bridge/
│       ├── main.py                  # ➕ ADD ROUTES HERE for new client transports
│       ├── api.py                   # /api/tenants/* routes consumed by onboarding UI
│       ├── api_models.py            # Pydantic TenantConfigOut/Patch + ChannelInfo
│       ├── tenant_write.py          # GET/UPDATE/upsert helpers + deep_merge for PATCH
│       ├── slack_channels.py        # users.conversations helper for the channels page
│       ├── client.py                # boto3 + LOCAL_AGENT_URL fallback + SSE frame parser
│       ├── tenant_resolver.py       # WorkspaceResolver (JSON | DynamoDB); resolve_tenant_id
│       ├── async_dispatcher.py      # ack-then-post pattern (Slack 3s rule)
│       ├── dedup.py                 # Slack retry dedup (InMemoryDedup | DynamoDedup)
│       ├── slack_token_store.py     # per-tenant bot tokens (env | Secrets Manager)
│       ├── slack_oauth.py           # /slack/install + /slack/oauth/callback flow
│       │                            #   + state + session token helpers
│       └── adapters/
│           ├── core.py              # Adapter protocol, InboundMessage, OutboundMessage
│           ├── slack.py             # Slack adapter (HMAC verify + chat.postMessage)
│           ├── debug.py             # synchronous /debug/message (LOCAL_DEV only)
│           └── ➕ ADD NEW ADAPTERS HERE (discord.py, teams.py, web.py, …)
├── onboarding/                      # Next.js 16 onboarding UI (week 3)
│   ├── package.json                 # standalone JS service; no @aws-sdk deps by design
│   ├── app/
│   │   ├── layout.tsx               # root HTML shell
│   │   ├── page.tsx                 # public landing page with "Add to Slack" button
│   │   └── onboarding/
│   │       ├── error/page.tsx       # error landing for `?reason=<slug>`
│   │       └── [tenantId]/
│   │           ├── layout.tsx       # 4-step sidebar nav
│   │           ├── welcome/route.ts # ROUTE HANDLER — sets cookie, redirects
│   │           ├── config/page.tsx  # ➕ ADD EDITABLE FIELDS HERE
│   │           ├── config/ConfigForm.tsx  # client form
│   │           ├── config/actions.ts      # server action → bridge PATCH
│   │           ├── channels/page.tsx
│   │           ├── integrations/page.tsx  # ➕ WEEK 4 wires up real connectors
│   │           └── done/page.tsx
│   └── lib/
│       ├── env.ts                   # typed env accessor
│       ├── session.ts               # HMAC verify (matches bridge _sign_session)
│       ├── bridge.ts                # server-side fetch wrapper (Authorization: Bearer)
│       └── types.ts                 # TenantConfig mirror (KEEP IN SYNC — gotcha #21)
├── infra/                           # hand-authored infra (NOT CLI-managed)
│   └── data/                        # CDK app: DynamoDB tables + IAM managed policies
│       ├── bin/data.ts              # CDK entrypoint, deploys to us-west-2
│       ├── lib/data-stack.ts        # tenants, workspace_to_tenant, audit_log,
│       │                            #   processed_events, AgentCoreDataAccess,
│       │                            #   AgentCoreBridgeDataAccess,
│       │                            #   AgentCoreOnboardingDataAccess (STUB, unattached)
│       └── scripts/
│           ├── seed_tenants.py      # migrate examples/*.json → DDB (uses TenantStore.upsert)
│           ├── audit_query.py       # ad-hoc CLI: recent / cost / tools subcommands
│           ├── attach_agent_policy.sh  # attach managed policy to agent role post-deploy
│           ├── provision_memory.py  # create shared AgentCore Memory resource + store ID in SSM
│           └── delete_memory.py     # tear down memory resource + purge SSM params
├── .github/workflows/
│   └── ci-cd.yml                    # CI gates + deploy on merge (see header for OIDC setup)
└── examples/
    ├── tenants/                     # AGENT_LOCAL_STORES=1 source of truth (one JSON per tenant)
    │   └── demo.json
    └── workspace_to_tenant.json     # LOCAL_DEV=1 workspace mapping (bridge-side)
```
