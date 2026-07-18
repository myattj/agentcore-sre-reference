# Architecture & onboarding guide

> Auto-loaded every session. Reference docs in `.claude/docs/` have deeper detail — read them on demand.

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
                       Claude Sonnet AgentCore     AgentCore
                       4.6 (Bedrock)  Memory       Gateway (shared reference)
                                      (tenant and   • Lambda targets
                                       channel      • OpenAPI targets
                                       namespaces)  • MCP server targets
```

**Anti-pattern:** putting tool execution in the bridge. Tools are agent code. The bridge is transport-only.

---

## Onboarding a new tenant (mental model)

Walk through this before changing anything multi-tenant.

1. **Customer signs up** → tenant config created in DynamoDB (or `examples/tenants/<id>.json` locally). Workspace ID mapped to tenant ID.
2. **They configure their agent** via `TenantConfig` (see `coreAgent/app/coreAgent/tenant.py` for the authoritative model): model and persona, catalog and Gateway tools, memory, heartbeat and cost policy, channels and bot policy, context assembly, skills, escalation, and codebases.
3. **Slack workspace connects** → OAuth install → bridge resolves `team_id` → `tenant_id`, acks within 3s, dispatches async.
4. **User sends a message** → full runtime path in `.claude/docs/architecture-deep-dive.md`.
5. **Tools + memory accumulate** — catalog tools per their whitelist, BYO tools via Gateway, memory records isolated by namespace.

---

## Common changes — where to make them

| Want to... | Edit |
|---|---|
| Add a new catalog tool every tenant could use | `coreAgent/app/coreAgent/tools.py` (add `@audited_tool("name")`) |
| Change the default model for all tenants | `coreAgent/app/coreAgent/model/load.py` (`DEFAULT_MODEL_ID`) |
| Override the model for one tenant | their tenant row in DynamoDB (or `examples/tenants/<id>.json` if `AGENT_LOCAL_STORES=1`) |
| Add a new client transport (e.g. Discord) | `bridge/bridge/adapters/discord.py` + register in `bridge/main.py` |
| Add a new memory extraction rule | `memory_store.extract_records()` + reference the rule name in the tenant config |
| Onboard a new customer (local dev) | new file in `examples/tenants/` + new entry in `examples/workspace_to_tenant.json` |
| Onboard a new customer (production) | `PutItem` into `tenants` and `workspace_to_tenant` DynamoDB tables |
| Enable BYO tools for a tenant | their tenant config: set `byo.enabled: true`, `byo.gateway_endpoint`, `byo.gateway_auth` |
| Change heartbeat behavior globally | `coreAgent/app/coreAgent/ping.py` |
| Change DDB table schemas or IAM policy scope | `infra/data/lib/data-stack.ts` — then `npm run deploy` in `infra/data/` |
| Add a new audit row type or field | `coreAgent/app/coreAgent/audit.py` + the writer call-site in `main.py` or `tools.py` |
| Add an editable TenantConfig field to the UI | THREE-place schema edit: (1) `coreAgent/app/coreAgent/tenant.py`, (2) `bridge/bridge/api_models.py` + `bridge/bridge/tenant_write.py:build_default_config_dict`, (3) `onboarding/lib/types.ts`; then update the relevant workspace component |
| Add a tenant skill/runbook | their tenant config: add to `skills[]` with trigger, name, prompt_template, required_tools |
| Configure bot-to-bot interaction | their tenant config: set `bot_policy.trusted_bot_ids` and/or `bot_policy.open_channels` |
| Add an escalation route | their tenant config: add to `escalation.routes[]` with team_name, channel_id, contacts |
| Tune context assembly (permalinks, thread depth) | their tenant config: modify `context_assembly.*` |
| Change the prompt form labels / styling | `onboarding/app/workspace/[tenantId]/prompt/ConfigForm.tsx` |
| Change the session token TTL | `bridge/bridge/slack_oauth.py:_SESSION_TTL_SECONDS` AND `onboarding/lib/session.ts:SESSION_TTL_SECONDS` (must match) |
| Change what happens after OAuth install | `bridge/bridge/slack_oauth.py:handle_oauth_callback` |
| Change the "Coming soon" integration list | `onboarding/app/onboarding/[tenantId]/integrations/page.tsx` |
| Add/change a CI test gate | `.github/workflows/ci-cd.yml` |
| Change deploy config (ARNs, domain) | GitHub repo variables (Settings > Actions > Variables) |

---

## Critical rules

These prevent bugs. Full detail + 14 more situational gotchas in `.claude/docs/gotchas-full.md`.

1. **Never block in `@app.entrypoint`.** Stalls `/ping`. All long work via `app.add_async_task` + background thread.
2. **Never hardcode the model ID.** Read from `tenant.model_id`. Only `model/load.py:DEFAULT_MODEL_ID` names a model literally.
3. **Tools live in the agent, not the bridge.** Bridge is transport-only.
4. **Keep `memory: none` in agentcore.json.** `shortTerm` creates orphaned AWS resources.
5. **Slack 3s ack rule.** Don't move agent invocation into the request handler.
6. **CDK in `coreAgent/agentcore/cdk/` is auto-generated.** Edit `agentcore.json` instead.
7. **Python 3.13 only.** uv pins it to match AgentCore Runtime. 3.14 breaks SDK deps.
8. **`runtime.py` avoids circular imports.** `main.py`, `tools.py`, `ping.py` all import `app` from `runtime.py`. Don't move `app` back.
9. **`LOCAL_DEV=1` (bridge) vs `AGENT_LOCAL_STORES=1` (agent).** Separate names because AgentCore CLI hardcodes `LOCAL_DEV`. Never ship `AGENT_LOCAL_STORES=1` to production.
10. **Audit writes must never throw.** They swallow exceptions. Diagnose via CloudWatch logs.
11. **Use `@audited_tool("name")` for new catalog tools.** Replaces the old `@register + @tool` pattern.
12. **Data CDK lives at `infra/data/`, not `coreAgent/agentcore/cdk/`.** The latter is CLI-regenerated.
13. **Bridge and coreAgent have separate venvs.** Don't cross-import. Duplicate shared shapes with cross-reference comments.
14. **Tenant config shape lives in THREE places.** (1) `coreAgent/app/coreAgent/tenant.py` (authoritative), (2) `bridge/bridge/api_models.py` + `tenant_write.py:build_default_config_dict`, (3) `onboarding/lib/types.ts`. Update all three in one commit.
15. **Bridge default config mirrors the agent schema** in `bridge/bridge/tenant_write.py:build_default_config_dict`; Slack OAuth calls that helper. Mirror changes from `tenant.py` without cross-importing services.
16. **Next.js 16 tripwires:** `cookies()` is async; can't set cookies in Server Components (use Route Handlers); `fetch()` is aggressively cached (use `cache: "no-store"`); `params`/`searchParams` are `Promise<...>`; `redirect()` throws `NEXT_REDIRECT` — don't swallow it.

---

## Communication style

1. **Separate phases of work into distinct messages.** If you announce a plan ("Let me check X and Y"), then report findings, those are two separate thoughts — separate them with a blank line and a clear transition. Never concatenate "here's what I'll do" and "here's what I found" into one unbroken paragraph.
2. **One idea per block.** When reporting multiple findings, use headings, bullets, or numbered lists — not run-on prose.

---

## Reference docs (read on demand)

| Doc | Contents |
|---|---|
| `.claude/docs/architecture-deep-dive.md` | Runtime path (end-to-end message flow), customization surfaces, file tree |
| `.claude/docs/dev-guide.md` | Local dev setup (3 terminals), production dev loop, full env var table |
| `.claude/docs/gotchas-full.md` | All 30 gotchas with complete detail (Slack HMAC, session tokens, memory provisioning, bot policy, etc.) |

## Reference files

- **Product vision**: `NORTH_STAR.md` (six archived exploration pillars)
- **AgentCore CLI conventions**: `coreAgent/AGENTS.md` (auto-generated; schema-first authority rule)
- **AgentCore CLI schema types**: `coreAgent/agentcore/.llm-context/*.ts`
- **Strands docs**: https://strandsagents.com/
- **AgentCore docs**: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/
- **AgentCore Gateway** (BYO tools): https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway.html
- **AgentCore Memory self-managed strategy**: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/memory-self-managed-strategies.html

## Skill routing

When the user's request matches an available skill, ALWAYS invoke it using the Skill
tool as your FIRST action. Do NOT answer directly, do NOT use other tools first.
The skill has specialized workflows that produce better results than ad-hoc answers.

Key routing rules:
- Product ideas, "is this worth building", brainstorming → invoke office-hours
- Bugs, errors, "why is this broken", 500 errors → invoke investigate
- Ship, deploy, push, create PR → invoke ship
- QA, test the site, find bugs → invoke qa
- Code review, check my diff → invoke review
- Update docs after shipping → invoke document-release
- Weekly retro → invoke retro
- Design system, brand → invoke design-consultation
- Visual audit, design polish → invoke design-review
- Architecture review → invoke plan-eng-review
- Save progress, checkpoint, resume → invoke checkpoint
- Code quality, health check → invoke health
