# Development Guide

> Reference doc — read on demand. CLAUDE.md has the condensed version.

## Local development

Three terminals as of week 3. Each side has its own local-stores flag —
see gotcha #12 in gotchas-full.md for why the names are different.

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

---

## Environment variables

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
| `SLACK_APP_ID` | — | Bridge — the Slack app's own App ID (e.g. `A0123456789`). Used by bot policy filtering in `main.py` to drop self-messages and prevent infinite loops. Set it in production; unset in local dev (self-message filtering is skipped). |
| `AGENTCORE_MEMORY_ID` | — | **Agent** — memory resource ID (e.g. `mem-xxx`). Set after running `provision_memory.py`. When set and `AGENT_LOCAL_STORES` is not `1`, the agent uses `AgentCoreMemorySessionManager` for real persistent memory. When unset, falls back to `InMemoryStore`. |
