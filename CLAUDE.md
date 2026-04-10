# Architecture & onboarding guide

> This file is auto-loaded by Claude Code into every session in this repo. Read it before making changes.

## What this is

A **multi-tenant agent platform** built on AWS Bedrock AgentCore. Customers (tenants) reach an AI agent through client transports (Slack first, more later). Each customer configures their own:
- system prompt + persona
- which tools the agent can call (catalog whitelist + their own BYO tools)
- memory rules (extraction triggers, namespace, what to store)
- heartbeat thresholds (when to consider the agent "busy")

The product separates **transport** (bridge) from **AI logic** (coreAgent) so the agent stays focused and the bridge stays thin.

## The two services

```
   ┌─────────────────┐         ┌──────────────────────┐
   │ Slack workspace │ webhook │ bridge/   (FastAPI)  │
   │ (or Discord,    ├────────▶│                      │
   │  Teams, etc.)   │         │  • adapters/         │
   └─────────────────┘         │  • tenant_resolver   │
                               │  • async_dispatcher  │
                               │  • client (boto3)    │
                               └──────────┬───────────┘
                                          │
                       boto3.invoke_agent_runtime
                       payload: {tenant_id, prompt, ctx}
                                          │
                                          ▼
                               ┌──────────────────────┐
                               │ coreAgent/  (Strands)│
                               │  on AgentCore Runtime│
                               │                      │
                               │  • main.py           │
                               │  • tenant config     │
                               │  • catalog tools     │
                               │  • BYO tools (MCP)   │
                               │  • memory store      │
                               │  • @app.ping         │
                               └──────────┬───────────┘
                                          │
                              ┌───────────┼────────────┐
                              ▼           ▼            ▼
                       Claude Sonnet  AgentCore     AgentCore
                       4.6 (Bedrock)  Memory       Gateway (per tenant)
                                      (per tenant   • Lambda targets
                                       namespace)   • OpenAPI targets
                                                    • MCP server targets
```

### Why two services and not one
- **Bridge concerns** (Slack 3-second ack rule, Events API signature verification, multiple client adapters, workspace→tenant mapping) have nothing to do with AI logic. Mixing them bloats the agent.
- **Agent concerns** (model selection, tool execution, memory, prompts) have nothing to do with HTTP transport semantics.
- The bridge can scale horizontally on Fargate/Lambda; the agent runs on AgentCore Runtime which has its own scaling model.
- Future clients (Discord, web chat, Slack of a competing workspace, voice) just add new adapters in `bridge/bridge/adapters/`. Zero agent changes.

**Anti-pattern to avoid:** putting tool execution in the bridge. Tools are agent code. The bridge is transport-only.

---

## Onboarding a new tenant (the customer journey)

This is the key mental model. Walk through it before changing anything multi-tenant.

### Step 1 — A customer signs up
Today this is manual:
1. Create `examples/tenants/<tenant_id>.json` with their config (see `examples/tenants/demo.json` for the schema)
2. Add an entry to `examples/workspace_to_tenant.json` mapping their Slack workspace ID to that tenant_id
3. Bounce the bridge so `tenant_resolver`'s LRU cache picks up the new mapping

In Phase 8, both files become DynamoDB tables backed by an admin UI. The same `load_tenant_config()` and `resolve_tenant_id()` interfaces will get DynamoDB-backed implementations; calling code doesn't change.

### Step 2 — They configure their agent
Their `TenantConfig` has six fields that drive everything (see `coreAgent/app/coreAgent/tenant.py`):

```python
TenantConfig(
    tenant_id="acme",
    model_id="global.anthropic.claude-sonnet-4-6",  # could be different per tenant
    system_prompt="You are Acme's research assistant…",
    catalog=CatalogConfig(
        allowed_tools=["echo", "web_search", "jira_lookup"],  # whitelist into TOOL_REGISTRY
        tool_config={                                          # per-tool creds/endpoints
            "jira_lookup": {"base_url": "...", "secret_arn": "..."},
        },
    ),
    byo=ByoConfig(
        enabled=True,
        gateway_endpoint="https://gateway.acme-agentcore.aws/mcp",
        gateway_auth={"headers": {"Authorization": "Bearer …"}},
    ),
    memory=MemoryConfig(
        triggers=MemoryTriggers(message_count=6, token_count=1000, idle_timeout_seconds=1800),
        namespace="tenants/acme",
        extraction=MemoryExtraction(enabled=True, rules=["user_preferences", "facts"]),
    ),
    heartbeat=HeartbeatConfig(busy_threshold=1, max_background_seconds=3600),
)
```

This dataclass IS the contract. Anything per-customer goes here.

### Step 3 — Their Slack workspace connects
1. They install your Slack app in their workspace
2. Slack sends Events API webhooks to `bridge/slack/events`
3. Bridge resolves their `team_id` → `tenant_id` via `tenant_resolver`
4. Bridge ack's within 3s, then dispatches the actual agent invocation in a background task
5. Agent loads their config and serves the request

### Step 4 — A user in their workspace sends a message
See [Runtime path](#runtime-path) below.

### Step 5 — Their tools and memory accumulate
- **Catalog tools** they enabled get called per their needs
- **BYO tools** (if they registered any with their AgentCore Gateway) are listed lazily and exposed alongside catalog tools
- **Memory** records get written under `tenants/acme/...` namespace, isolated from every other tenant
- **Heartbeat** kicks in for long-running tools, keeping their session alive past the 15-minute idle timeout

---

## Runtime path

A single user message, end to end:

1. **Slack** posts to `POST /slack/events` (or any other adapter's route)
2. **`bridge/main.py`** parses via the appropriate `Adapter`, returns 200 within 3s, queues a `BackgroundTask`
3. **`async_dispatcher.dispatch_async`** runs in the background:
   - Calls `tenant_resolver.resolve_tenant_id(workspace_id)`
   - Calls `client.invoke(tenant_id=…, prompt=…, ctx={…})`
4. **`bridge/client.py`** chooses transport:
   - If `LOCAL_AGENT_URL` is set → HTTP POST to `agentcore dev` server (local dev path)
   - Otherwise → `boto3.bedrock-agentcore.invoke_agent_runtime` (production path)
5. **Agent** receives the payload at `coreAgent/app/coreAgent/main.py:invoke()`:
   - `load_tenant_config(tenant_id)` reads `examples/tenants/<id>.json`
   - `build_catalog_tools(allowed)` filters `tools.TOOL_REGISTRY` by the whitelist
   - `build_byo_mcp_client(gateway_endpoint, auth)` returns a Strands `MCPClient` (or `None`)
   - Constructs a fresh `Agent(model=…, system_prompt=…, tools=[*catalog, mcp_client])` per invocation
   - `agent.stream_async(prompt)` → streams text chunks
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

## The three pillars of customization

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

---

## Where things live (and where to add new things)

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
│       ├── ping.py                  # @app.ping logic + _inflight_tasks set
│       ├── memory_store.py          # Local dev: InMemoryStore + extract_records (prod uses SDK session manager)
│       ├── audit.py                 # AuditStore protocol + Null/InMemory/Dynamo impls
│       ├── request_context.py       # ContextVar-backed per-invocation context
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

### Common changes — where to make them

| Want to… | Edit |
|---|---|
| Add a new catalog tool every tenant could use | `coreAgent/app/coreAgent/tools.py` (add `@audited_tool("name")`) |
| Change the default model for all tenants | `coreAgent/app/coreAgent/model/load.py` (`DEFAULT_MODEL_ID`) |
| Override the model for one tenant | their tenant row in DynamoDB (or `examples/tenants/<id>.json` if `AGENT_LOCAL_STORES=1`) |
| Add a new client transport (e.g. Discord) | `bridge/bridge/adapters/discord.py` + register in `bridge/main.py` |
| Add a new memory extraction rule | `memory_store.extract_records()` + reference the rule name in the tenant config |
| Onboard a new customer (local dev) | new file in `examples/tenants/` (read by agent under `AGENT_LOCAL_STORES=1`) + new entry in `examples/workspace_to_tenant.json` (read by bridge under `LOCAL_DEV=1`) |
| Onboard a new customer (production) | `PutItem` into the `tenants` and `workspace_to_tenant` DynamoDB tables (week-3 onboarding UI automates this) |
| Enable BYO tools for a tenant | their tenant config: set `byo.enabled: true`, `byo.gateway_endpoint`, `byo.gateway_auth` |
| Change heartbeat behavior globally | `coreAgent/app/coreAgent/ping.py` |
| Change DDB table schemas or IAM policy scope | `infra/data/lib/data-stack.ts` — then `npm run deploy` in `infra/data/` |
| Add a new audit row type or field | `coreAgent/app/coreAgent/audit.py` + the writer call-site in `main.py` or `tools.py` |
| Add an editable TenantConfig field to the onboarding form | THREE-place edit: (1) `coreAgent/app/coreAgent/tenant.py` (authoritative Pydantic), (2) `bridge/bridge/api_models.py:TenantConfigOut`+`TenantConfigPatch` AND `bridge/bridge/tenant_write.py:build_default_config_dict`, (3) `onboarding/lib/types.ts` + a control in `onboarding/app/onboarding/[tenantId]/config/ConfigForm.tsx`. See gotcha #21. |
| Change the form labels / styling | `onboarding/app/onboarding/[tenantId]/config/ConfigForm.tsx` |
| Change the session token TTL | `bridge/bridge/slack_oauth.py:_SESSION_TTL_SECONDS` AND `onboarding/lib/session.ts:SESSION_TTL_SECONDS` (must match — see gotcha #22) |
| Change what happens after OAuth install | `bridge/bridge/slack_oauth.py:handle_oauth_callback` (the redirect target) |
| Change the "Coming soon" integration list | `onboarding/app/onboarding/[tenantId]/integrations/page.tsx` |
| Add/change a CI test gate | `.github/workflows/ci-cd.yml` (add a job under the "Test gates" section) |
| Change deploy config (ARNs, domain) | GitHub repo variables (Settings > Actions > Variables) — see workflow header |
| Update agentcore CLI version for CI | `.github/workflows/ci-cd.yml` (`npm install -g @aws/agentcore@<version>`) |

---

## Critical conventions and gotchas

1. **Never block in `@app.entrypoint`.** Stalls `/ping`. All long work via `app.add_async_task` + background thread.
2. **Never hardcode the model ID** in agent code. Always read from `tenant.model_id`. Only `model/load.py:DEFAULT_MODEL_ID` is allowed to name a model literally.
3. **Tools live in the agent, not the bridge.** Bridge is transport-only. If you find yourself adding business logic to the bridge, stop and put it in `tools.py`.
4. **`memory: none` in agentcore.json — keep it.** Picking `shortTerm` here creates orphaned AWS-managed memory resources we don't use; we own the memory layer.
5. **Slack 3-second ack rule** — `/slack/events` MUST return within 3 seconds. The async dispatcher pattern handles this. Don't move agent invocation into the request handler.
6. **Slack signing-secret HMAC verification** lives in `bridge/bridge/adapters/slack.py:verify_signature` (`v0=` scheme, 5-min replay window, `hmac.compare_digest`). It's called from `bridge/main.py:slack_events` BEFORE any JSON parsing or dispatch. When `SLACK_SIGNING_SECRET` is unset (LOCAL_DEV), verification is skipped silently with a WARNING log — production deployments MUST set it or every request will be accepted regardless of provenance.
7. **CDK is auto-generated** by the AgentCore CLI. Edit `agentcore.json`, not `cdk/`. See `coreAgent/AGENTS.md` for the schema-first authority rule.
8. **Python version** — uv pins 3.13 to match the AgentCore Runtime version (`runtimeVersion: PYTHON_3_13` in `agentcore.json`). System Python 3.14 is NOT compatible with AgentCore SDK transitive deps; never use it directly.
9. **One Memory resource, many tenants** via namespace isolation. Don't create one Memory resource per tenant — quota pain.
10. **Per-tenant Gateway is fine for evaluation, won't scale** to thousands of tenants. Eventual pattern is multi-tenant Gateway sharing with target tagging.
11. **`runtime.py` exists to avoid circular imports** between `main.py`, `tools.py`, and `ping.py`. All three import `app` from `runtime.py`. Don't move `app` back into `main.py`.
12. **Local-dev env var split: `LOCAL_DEV=1` (bridge) and `AGENT_LOCAL_STORES=1` (agent).** Both flip their respective code paths to JSON-file fallbacks (bridge reads `examples/workspace_to_tenant.json`; agent reads `examples/tenants/<id>.json`) and away from DynamoDB. They are **separate names on purpose**: the AgentCore CLI hardcodes `LOCAL_DEV=1` into every `agentcore dev` subprocess as a reserved signal for "use `.env.local` credentials". If we used `LOCAL_DEV` in the agent code, the CLI would silently force the JSON path even in production-mode-locally smoke tests, so the agent uses `AGENT_LOCAL_STORES` instead. Set BOTH for full local mode; unset BOTH (well, only `AGENT_LOCAL_STORES` — `LOCAL_DEV` will get re-set by the CLI no matter what) for end-to-end against real DynamoDB. **Never ship with `AGENT_LOCAL_STORES=1` in the deployed agent's environment.**
13. **Audit writes must never fail the caller.** `tools.py`'s audit wrapper and `main.py`'s invocation-row writer both swallow exceptions from the `AuditStore`. If audit rows stop appearing in CloudWatch, diagnose via CloudWatch logs (`AuditStore.write dropped`) — don't make the write path throw.
14. **`audited_tool` replaces the old `@register + @tool` pattern.** New catalog tools should use `@audited_tool("name")`. The old `@register` stays in `tools.py` as an escape hatch for tools that somehow can't be audited, but nothing in the catalog currently uses it directly.
15. **Data-layer CDK lives at `infra/data/` and is hand-authored.** Do not move it into `coreAgent/agentcore/cdk/` — that directory is CLI-regenerated.
16. **Slack onboarding follows Model A** — one shared Slack app on the marketplace (Client ID/Secret/Signing Secret are global, set as bridge env vars), per-workspace bot tokens stored in Secrets Manager at `agentcore/tenants/<tenant_id>/slack/bot_token`. The OAuth callback in `bridge/bridge/slack_oauth.py` provisions a new tenant per workspace; `tenant_id = f"slack-{team_id.lower()}"`. The other Slack-app model (each customer registers their own app) is *not* what we're doing — see `BUILD_PLAN.md` week 2 for the rationale.
17. **Slack retry dedup MUST happen in the bridge** before `client.invoke()`. `bridge/bridge/dedup.py` is the canonical helper; `bridge/main.py:slack_events` calls `is_duplicate(event_id)` BEFORE dispatching. Skipping dedup means duplicate Bedrock spend AND duplicate audit rows on Slack's 3x retry. Dedup fails OPEN on backend errors (better to risk a duplicate than drop a real event).
18. **`bridge/bridge/slack_oauth.py:_build_default_config_dict` is a duplicate of `coreAgent.tenant.build_default_config()`.** The bridge and the agent are separate Python packages with separate venvs, so the bridge can't import from coreAgent. **When you change the default tenant config shape in `coreAgent/app/coreAgent/tenant.py`, mirror the change in `slack_oauth.py`** — the file has a "keep in sync" comment block. There's no automated check; it's a discipline thing.
19. **`/debug/message` is registered ONLY when `LOCAL_DEV=1`.** The production bridge has no `/debug/*` routes at all — zero attack surface. If you need to poke at the deployed bridge, add a header-token-gated route (don't drop the LOCAL_DEV-only conditional).
20. **Bridge imports are isolated from coreAgent imports.** They're separate packages with separate venvs (`bridge/.venv` vs `coreAgent/app/coreAgent/.venv`). Don't try to `from coreAgent.tenant import ...` from the bridge — duplicate the small amounts of shared shape with cross-reference comments instead. The seed script (`infra/data/scripts/seed_tenants.py`) is the one place that imports from coreAgent, and it does so by injecting `coreAgent/app/coreAgent/` onto `sys.path` at runtime.
21. **Tenant config shape lives in THREE places now (week 3).** (1) authoritative Pydantic in `coreAgent/app/coreAgent/tenant.py:TenantConfig`, (2) bridge-side Pydantic in `bridge/bridge/api_models.py:TenantConfigOut`/`TenantConfigPatch` PLUS the default-shape dict in `bridge/bridge/tenant_write.py:build_default_config_dict`, (3) TypeScript mirror in `onboarding/lib/types.ts`. The bridge Pydantic is the runtime validation boundary — bad shapes from the onboarding UI surface as 422 from `PATCH /api/tenants/{id}` rather than as silent corruption. Still: when you add a field, update all three in one commit. There's no automated parity check; it's a discipline thing. The week 2 default-prompt bug (`system_prompt=""`) is the kind of thing that happens when only one copy gets updated.
22. **Onboarding session tokens share `BRIDGE_OAUTH_STATE_SECRET` with OAuth state tokens.** Format `{tenant_id}.{nonce}.{ts}.{hmac}` (4 parts, 60-min TTL) vs state `{nonce}.{ts}.{hmac}` (3 parts, 10-min TTL). Both are HMAC-SHA256 over the same secret. Only the **bridge** mints session tokens (in `slack_oauth.py:make_session_token`); the onboarding UI only verifies (`onboarding/lib/session.ts:verifySessionToken`). The cross-tenant isolation is enforced by `bridge/bridge/api.py:require_session_token`, which asserts the token's embedded tenant matches the URL path's `tenant_id`. Tenant IDs must stay period-free (`make_session_token` asserts this); the OAuth-derived `slack-<team_id.lower()>` format guarantees it today. **Rotating `BRIDGE_OAUTH_STATE_SECRET` invalidates all in-flight installs AND all active onboarding sessions** — users re-run `/slack/install`.
23. **`AgentCoreOnboardingDataAccess` managed policy in `infra/data/lib/data-stack.ts` is a STUB**, intentionally not attached to any role. The onboarding service runs locally via `npm run dev` and never touches AWS directly. The policy is in CDK so the IAM scaffolding is in place when Phase 8 moves the onboarding UI into a real Fargate task; then attach it to that task role. Narrower than `AgentCoreBridgeDataAccess` — no `processed_events` access, no Secrets Manager writes.
24. **Onboarding (Next.js) server NEVER talks to AWS directly.** All tenant config reads/writes and Slack channel listings flow through bridge `/api/tenants/*` routes. This is why there's no `@aws-sdk/*` dep in `onboarding/package.json` — if you find yourself adding one, stop and put the logic in the bridge. The benefit: ONE implementation of the DDB merge semantics (in Python, in `bridge/bridge/tenant_write.py:deep_merge`), and ONE place where the validation boundary lives (Pydantic in `bridge/bridge/api_models.py`). The cost: every page render is one network hop bridge-ward, which is fine while everything's local but worth measuring once we deploy.
25. **Memory provisioning is a one-time operation.** Run `uv run --with boto3 python infra/data/scripts/provision_memory.py` after `npm run deploy` in `infra/data/`. The script creates the shared memory resource and stores the `memory_id` in SSM at `/agentcore/memory/id`. Set `AGENTCORE_MEMORY_ID=<id>` on the agent runtime (via `agentcore.json` envVars or shell). Use `delete_memory.py` to tear down. Memory data (events, extracted records) lives in the AWS resource and survives agent redeploys.
26. **Next.js 16 server-component tripwires.** (a) `cookies()` is async — `const c = await cookies()`. (b) **You cannot set cookies inside a Server Component.** Cookie modification must happen in a Server Function (server action) or a Route Handler. That's why `onboarding/app/onboarding/[tenantId]/welcome/route.ts` is a `route.ts` not a `page.tsx`. (c) `fetch()` in server components is aggressively cached — `lib/bridge.ts` always passes `cache: "no-store"` or stale config shows up after PATCH. (d) Server actions must call `revalidatePath('/onboarding/${tenantId}/config')` after a successful PATCH or the form re-renders with stale values. (e) `redirect()` works by throwing `NEXT_REDIRECT` — don't wrap `requireSession` in a swallow-all try/catch. (f) `params` and `searchParams` are both `Promise<...>` in pages and route handlers; await them.

---

## Local development

Three terminals as of week 3. Each side has its own local-stores flag —
see gotcha #12 above for why the names are different.

```bash
# Terminal 1 — agent
cd coreAgent
AGENT_LOCAL_STORES=1 agentcore dev --logs         # serves on http://127.0.0.1:8080
# (the CLI also injects LOCAL_DEV=1 into the subprocess as a reserved signal;
#  AGENT_LOCAL_STORES is what our code reads.)

# Terminal 2 — bridge
cd bridge
LOCAL_DEV=1 LOCAL_AGENT_URL=http://localhost:8080 \
  BRIDGE_OAUTH_STATE_SECRET=dev-shared-secret-32-chars-long \
  ONBOARDING_BASE_URL=http://localhost:3000 \
  .venv/bin/uvicorn bridge.main:app --port 8000

# Terminal 3 — onboarding (Next.js, week 3+)
cd onboarding
# .env.local: BRIDGE_OAUTH_STATE_SECRET MUST match the bridge's value
cp .env.example .env.local && $EDITOR .env.local
npm run dev                                       # serves on http://localhost:3000
```

**Smoke test the audit pipeline** by adding `LOCAL_AUDIT=memory` on the agent
terminal — it swaps `NullAuditStore` for `InMemoryAuditStore` so you can
inspect rows from the REPL (or from a test harness that imports `audit._audit`).

Verify with:
```bash
# Sanity check
curl http://localhost:8000/healthz

# Synchronous debug call
curl -X POST http://localhost:8000/debug/message \
  -H "Content-Type: application/json" \
  -d '{"workspace_id":"demo-ws","user_id":"u1","text":"echo hello"}'

# Heartbeat lifecycle (in two terminals)
curl -X POST http://localhost:8000/debug/message \
  -d '{"workspace_id":"demo-ws","user_id":"u1","text":"call start_background_task with duration_seconds=60"}'
# then immediately:
curl http://localhost:8080/ping       # → HealthyBusy
# 65 seconds later:
curl http://localhost:8080/ping       # → Healthy
```

**Prereqs that bite:**
- AWS credentials (`aws sts get-caller-identity` must succeed)
- Bedrock model access for `anthropic.claude-sonnet-4-6` in `us-west-2` (Bedrock Console → Model access)
- CDK bootstrap (only needed for `agentcore deploy` + `infra/data/` deploys, not for `agentcore dev`)

---

## Production dev loop (hosted, against real AWS)

Once the data layer is deployed (see `infra/data/README.md`) and
`agentcore deploy` has published the agent runtime, the bridge runs
against real AWS instead of `LOCAL_AGENT_URL`:

```bash
# Deploy/seed the data layer (one-time per environment)
cd infra/data && npm install && npm run deploy
uv run --with boto3 python infra/data/scripts/seed_tenants.py

# Provision the shared memory resource (one-time)
uv run --with boto3 python infra/data/scripts/provision_memory.py
# Note the memory_id from the output (also stored in SSM /agentcore/memory/id)

# Deploy the agent
cd coreAgent && agentcore deploy

# Attach the managed policy to the agent role (one-time per agent stack)
bash infra/data/scripts/attach_agent_policy.sh

# Run the bridge against the deployed agent
cd bridge
AGENT_RUNTIME_ARN=<arn-from-agentcore-deploy> \
  .venv/bin/uvicorn bridge.main:app --port 8000

# Verify: same curl as local, but it routes through boto3 to AWS now.
curl -X POST http://localhost:8000/debug/message \
  -d '{"workspace_id":"demo-ws","user_id":"u1","text":"echo hello"}'
```

Environment variables that control the production path:

| Var | Default | Purpose |
|---|---|---|
| `LOCAL_DEV` | unset | **Bridge** — set to `1` to use JSON files instead of DynamoDB for the workspace resolver and to register `/debug/message`. Note: the AgentCore CLI also injects `LOCAL_DEV=1` into the agent subprocess unconditionally, but the agent's code does NOT read it (see `AGENT_LOCAL_STORES` and gotcha #12). |
| `AGENT_LOCAL_STORES` | unset | **Agent** — set to `1` to use JSON file stores instead of DynamoDB for tenant config and audit rows. Separate name from `LOCAL_DEV` to avoid the AgentCore CLI's reserved-variable collision. |
| `LOCAL_AUDIT` | unset | Set to `memory` to use `InMemoryAuditStore` for smoke tests |
| `TENANTS_TABLE` | `tenants` | Agent + bridge — DDB table name for tenant rows |
| `WORKSPACE_TO_TENANT_TABLE` | `workspace_to_tenant` | Bridge — DDB table name for `resolve_tenant_id` |
| `AUDIT_LOG_TABLE` | `audit_log` | Agent — DDB table name for audit rows |
| `PROCESSED_EVENTS_TABLE` | `processed_events` | Bridge — DDB table name for Slack retry dedup |
| `AWS_REGION` | `us-west-2` | Region for all AWS clients |
| `AGENT_RUNTIME_ARN` | — | Bridge — AgentCore Runtime ARN (used when `LOCAL_AGENT_URL` is unset) |
| `LOCAL_AGENT_URL` | — | Bridge — HTTP URL of `agentcore dev` (local-only) |
| `SLACK_CLIENT_ID` | — | Bridge — shared Slack app's Client ID (Model A) |
| `SLACK_CLIENT_SECRET` | — | Bridge — shared Slack app's Client Secret |
| `SLACK_SIGNING_SECRET` | — | Bridge — shared Slack app's Signing Secret (HMAC verification) |
| `SLACK_REDIRECT_URI` | — | Bridge — public URL of `/slack/oauth/callback` |
| `BRIDGE_OAUTH_STATE_SECRET` | — | Bridge **and onboarding** — HMAC key for OAuth state tokens AND week-3 onboarding session tokens. Falls back to `SLACK_SIGNING_SECRET` on the bridge side. The onboarding service requires it explicitly (no fallback). The two services MUST agree or every onboarding session fails with `bad_session`. |
| `ONBOARDING_BASE_URL` | `http://localhost:3000` | Bridge — public origin of the onboarding Next.js service. The `/slack/oauth/callback` redirects here on success/failure. |
| `BRIDGE_URL` | — | Onboarding — base URL of the bridge API (`http://localhost:8000` in dev). Server-side only; never exposed to the browser. |
| `NEXT_PUBLIC_BRIDGE_INSTALL_URL` | — | Onboarding — public URL of `/slack/install` on the bridge, embedded in the landing page "Add to Slack" button. The only `NEXT_PUBLIC_*` var. |
| `SLACK_BOT_TOKEN` | — | Bridge — LOCAL_DEV-only fallback bot token used by `EnvSlackTokenStore` (production reads per-tenant tokens from Secrets Manager instead) |
| `AGENTCORE_MEMORY_ID` | — | **Agent** — memory resource ID (e.g. `mem-xxx`). Set after running `provision_memory.py`. When set and `AGENT_LOCAL_STORES` is not `1`, the agent uses `AgentCoreMemorySessionManager` for real persistent memory. When unset, falls back to `InMemoryStore`. |

---

## What's NOT done yet (Phase 8+)

In-code work that's still pending:
- ~~Real **AgentCore Memory resource** + extraction pipeline~~ **landed (week 6)**. Uses built-in SEMANTIC + USER_PREFERENCE strategies via `AgentCoreMemorySessionManager` — no custom Lambda/SNS/S3 pipeline needed. Provisioned by `infra/data/scripts/provision_memory.py`. Memory is workspace-per-channel (shared within a channel), per-user for DMs.
- Real **AgentCore Gateway** provisioning per tenant + worker tooling to register Lambda/OpenAPI targets
- ~~**Onboarding UI** (week 3) — replaces the placeholder HTML returned by `/slack/oauth/callback`~~ **landed (week 3, local-only)**. Production deploy of the `onboarding/` Next.js service (Fargate / Vercel / Amplify) is still pending; the `AgentCoreOnboardingDataAccess` policy is in CDK as a stub waiting to be attached.
- **Pattern 1 catalog tools** (week 4) — `triage_alert`, Datadog/PagerDuty/GitHub connectors via Gateway
- **Pattern 2 + 3 catalog tools** (week 5) — Confluence/Notion/Jira/Linear connectors, channel-aware personas, FAQ memory rule
- **Discord / Teams / web-chat adapters** (post-MVP)
- **Auth on the bridge** beyond Slack HMAC (Cognito, signed webhooks for non-Slack callers)
- **Multi-environment AWS accounts** (dev/staging/prod)
- **Observability beyond CloudWatch defaults** (Langfuse, Phoenix, OTel export)
- **Eval harness** for prompt regressions
- ~~**CI/CD**~~ **landed** — `.github/workflows/ci-cd.yml`: bridge pytest, onboarding build, CDK synth as PR gates; automated agent + services deploy on merge to main via GitHub OIDC. See setup instructions in the workflow file header.
- **Devcontainer, pre-commit hooks** (low priority until 2nd engineer)
- ~~**Bridge service infra**~~ **landed (week 7)** — bridge + onboarding on Fargate behind ALB at `app.novari.dev`.

External actions blocking week-2 verification (in-repo work is done):
- **Register the shared Slack app** at api.slack.com using the manifest in BUILD_PLAN.md
- **`cd infra/data && npm run deploy`** to provision `processed_events` + `AgentCoreBridgeDataAccess`
- **First `agentcore deploy`** of the agent runtime
- **Slack marketplace submission** (long lead time — start in parallel)

The code is structured so each of these is additive. The interfaces (`MemoryStore`, `TenantStore`, `WorkspaceResolver`, `AuditStore`, `Adapter`, `TenantConfig`, `Dedup`, `SlackTokenStore`, `build_byo_mcp_client`) absorb the migration without rippling changes through the rest of the codebase.

---

## Reference files

- **Current scaffold plan** (the source-of-truth for what was built): `~/.claude/plans/temporal-popping-duckling.md`
- **AgentCore CLI conventions**: `coreAgent/AGENTS.md` (auto-generated; schema-first authority rule)
- **AgentCore CLI schema types**: `coreAgent/agentcore/.llm-context/*.ts`
- **Strands docs**: https://strandsagents.com/
- **AgentCore docs**: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/
- **AgentCore Gateway** (BYO tools): https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway.html
- **AgentCore Memory self-managed strategy**: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/memory-self-managed-strategies.html
