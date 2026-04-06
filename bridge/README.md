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
agentcore dev

# Terminal 2 — bridge (here)
uv sync
LOCAL_AGENT_URL=http://localhost:8080 uvicorn bridge.main:app --reload --port 8000
```

`LOCAL_AGENT_URL` switches the bridge from boto3 (production AgentCore Runtime) to a local HTTP POST against the `agentcore dev` server.

## Test without Slack credentials

```bash
curl -X POST http://localhost:8000/debug/message \
  -H "Content-Type: application/json" \
  -d '{"workspace_id":"demo-ws","user_id":"u1","text":"echo hello"}'
```

The `debug` adapter is synchronous — the bridge waits for the agent and returns the reply in the HTTP response.

## Test the Slack route (still no real Slack required)

```bash
curl -X POST http://localhost:8000/slack/events \
  -H "Content-Type: application/json" \
  -d '{
    "team_id": "T_LOCAL",
    "event": {"text": "echo hi from fake slack", "user": "U1", "thread_ts": "1.0"}
  }'
```

The Slack adapter is stubbed: no signing secret check, no real `chat.postMessage`. The reply gets logged to stdout. Swap in real `slack-sdk` calls when you wire a real Slack app.

## Going to production

1. Install the optional `slack` extras: `uv sync --extra slack`
2. Set env vars: `SLACK_SIGNING_SECRET`, `SLACK_BOT_TOKEN`, `AGENT_RUNTIME_ARN`
3. Implement signing secret verification in `adapters/slack.py` (currently stubbed)
4. Swap `print(...)` for `WebClient(token=...).chat_postMessage(...)` in the same file

## Layout

```
bridge/
├── pyproject.toml
└── bridge/
    ├── main.py              # FastAPI app + routes
    ├── client.py            # boto3 invoke_agent_runtime + LOCAL_AGENT_URL fallback
    ├── tenant_resolver.py   # workspace_id → tenant_id
    ├── async_dispatcher.py  # ack-then-post background dispatcher
    └── adapters/
        ├── core.py          # Adapter protocol + InboundMessage / OutboundMessage
        ├── slack.py         # Slack adapter (stubbed for local dev)
        └── debug.py         # Synchronous /debug/message adapter
```
