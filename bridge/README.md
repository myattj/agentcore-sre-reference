# bridge

FastAPI service that bridges client transports (Slack first, more later) to the `coreAgent` on AWS Bedrock AgentCore.

## Responsibilities

- Receive client events (Slack Events API, debug HTTP, etc.)
- Resolve client identity → tenant_id (via `tenant_resolver.py`)
- Invoke coreAgent via boto3 (or local HTTP for `agentcore dev`)
- Handle the ack-then-post async pattern (Slack 3-second rule)

The bridge is intentionally thin. **No tool execution lives here** — tools belong to the agent. The bridge owns transport, identity, and async dispatch.

## Local development

Two terminals:

```bash
# Terminal 1 — agent (in coreAgent/)
cd ../coreAgent
LOCAL_DEV=1 agentcore dev

# Terminal 2 — bridge (here)
uv sync
LOCAL_DEV=1 LOCAL_AGENT_URL=http://localhost:8080 uvicorn bridge.main:app --reload --port 8000
```

`LOCAL_AGENT_URL` switches the bridge from boto3 (production AgentCore Runtime) to a local HTTP POST against the `agentcore dev` server. `LOCAL_DEV=1` selects the JSON-file fallbacks for tenant config + workspace mapping and uses the in-memory dedup store.

## Test without Slack credentials

The `/debug/message` route is registered ONLY when `LOCAL_DEV=1` (zero attack surface in production). It's synchronous — the bridge waits for the agent and returns the reply in the HTTP response.

```bash
curl -X POST http://localhost:8000/debug/message \
  -H "Content-Type: application/json" \
  -d '{"workspace_id":"demo-ws","user_id":"u1","text":"echo hello"}'
```

## Test the Slack route (still no real Slack required)

In `LOCAL_DEV=1` with no `SLACK_SIGNING_SECRET` set, HMAC verification is skipped (with a warning) and unsigned synthetic payloads are accepted:

```bash
curl -X POST http://localhost:8000/slack/events \
  -H "Content-Type: application/json" \
  -d '{
    "team_id": "T_LOCAL",
    "event_id": "Ev123",
    "event": {"text": "echo hi from fake slack", "user": "U1", "thread_ts": "1.0", "channel": "C1"}
  }'
```

With no `SLACK_BOT_TOKEN`, the reply is printed to stdout instead of posted via `chat.postMessage`.

## Going to production (Model A: shared Slack app, per-workspace bot tokens)

1. Register the shared Slack app at api.slack.com and capture the Client ID, Client Secret, and Signing Secret
2. Deploy the data layer: `cd ../infra/data && npm run deploy`
3. Attach the `AgentCoreBridgeDataAccess` managed policy to the bridge service role
4. Set env vars on the bridge:
   - `AGENT_RUNTIME_ARN` — the deployed agent runtime ARN
   - `SLACK_CLIENT_ID`, `SLACK_CLIENT_SECRET`, `SLACK_SIGNING_SECRET`
   - `SLACK_REDIRECT_URI` — the public URL of `/slack/oauth/callback`
5. Customers visit `/slack/install` to OAuth into their workspace; the callback creates the tenant row and stores the bot token in Secrets Manager.

## Tests

```bash
uv sync --extra dev
uv run pytest
```

The test suite covers SSE frame parsing, tenant resolver (LOCAL_DEV + Dynamo), Slack HMAC verification, and the dedup helper. All tests run in LOCAL_DEV mode with mocked AWS clients — no real credentials required.

## Layout

```
bridge/
├── pyproject.toml
├── tests/                       # pytest suite
└── bridge/
    ├── main.py                  # FastAPI app + routes
    ├── client.py                # boto3 invoke_agent_runtime + LOCAL_AGENT_URL + SSE parsing
    ├── tenant_resolver.py       # workspace_id → tenant_id (JSON | DynamoDB)
    ├── async_dispatcher.py      # ack-then-post background dispatcher
    ├── dedup.py                 # Slack retry dedup (InMemoryDedup | DynamoDedup)
    ├── slack_token_store.py     # per-tenant bot tokens (env var | Secrets Manager)
    ├── slack_oauth.py           # /slack/install + /slack/oauth/callback flow
    └── adapters/
        ├── core.py              # Adapter protocol + InboundMessage / OutboundMessage
        ├── slack.py             # Slack adapter (HMAC verify + chat.postMessage)
        └── debug.py             # Synchronous /debug/message adapter
```
