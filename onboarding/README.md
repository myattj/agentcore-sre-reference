# onboarding

Next.js 16 onboarding UI for the agent-core multi-tenant Slack agent
platform. Customers land here after the bridge OAuth callback to
configure their tenant before chatting with the bot in Slack.

This is the third service in the monorepo, after `bridge/` (Python
FastAPI) and `coreAgent/` (Python Strands). It's TypeScript-only and has
its own `package.json` / `node_modules`. See `CLAUDE.md` at the repo
root for the architecture overview and the gotchas (especially #21–25
covering the onboarding service).

## Run locally

Three terminals, in this order:

```bash
# 1. agent
cd ../coreAgent && AGENT_LOCAL_STORES=1 agentcore dev --logs

# 2. bridge (real Slack creds + ngrok URL)
cd ../bridge && \
  LOCAL_DEV=1 \
  LOCAL_AGENT_URL=http://localhost:8081 \
  ONBOARDING_BASE_URL=http://localhost:3000 \
  BRIDGE_OAUTH_STATE_SECRET=dev-shared-secret \
  SLACK_CLIENT_ID=... SLACK_CLIENT_SECRET=... SLACK_SIGNING_SECRET=... \
  SLACK_REDIRECT_URI=https://<ngrok>/slack/oauth/callback \
  .venv/bin/uvicorn bridge.main:app --port 8000 --reload

# 3. onboarding (this service)
cp .env.example .env.local
# edit .env.local: BRIDGE_OAUTH_STATE_SECRET MUST match the bridge's value
npm run dev
```

Open <http://localhost:3000>, click "Add to Slack", finish the OAuth
consent. The bridge mints a session token, redirects here, and you land
on the config form.

## Architecture in one paragraph

All bridge calls happen server-side from Next.js (server components +
server actions). The browser never talks to the bridge directly — no
CORS, no exposed session token. The session is an HMAC-signed cookie on
this origin. The bridge mints session tokens; this service only verifies
them. Cross-tenant isolation is enforced on the bridge side (the
`/api/tenants/*` route asserts the URL tenant_id matches the token's
embedded tenant_id). See `lib/session.ts` and `bridge/bridge/api.py` for
the contract.

## What's here vs week 4

This week ships: landing, config (system prompt + catalog tools),
channels (read-only), integrations (disabled stubs), done. Week 4 wires
up the real integration OAuth flows + AgentCore Gateway provisioning,
adds `channels:read` to the Slack manifest, and ships per-channel
personas. Don't add `@aws-sdk/*` deps here — all AWS access flows
through the bridge by design (CLAUDE.md gotcha #24).
