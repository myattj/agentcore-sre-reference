# agentcorePlayground

Multi-tenant agent platform built on AWS Bedrock AgentCore. Two services in this monorepo:

- **`coreAgent/`** — AgentCore Runtime agent (Strands + Claude Sonnet 4.6 via Bedrock). Stateless, multi-tenant. Owns tools, memory, heartbeat. Tenant behavior is hydrated from config at invocation time.
- **`bridge/`** — FastAPI server. Transports client events (Slack first, more adapters later) into agent invocations. Handles tenant resolution and the ack-then-post async pattern. Calls AgentCore Runtime via boto3.

## Architecture

```
[Slack/etc.] → [bridge/]  → [coreAgent/]
                  ↑              ↓
            tenant config    catalog tools (in-process)
                             BYO tools (via AgentCore Gateway / MCP)
                             memory (self-managed contract)
                             heartbeat (HealthyBusy)
```

Each customer (tenant) has a `TenantConfig` defining: model, system prompt, allowed catalog tools, BYO Gateway endpoint, memory rules, and heartbeat thresholds. Examples in `examples/tenants/`.

## Prereqs

- Node 20+ (verified: v22)
- Python 3.13 via uv (`uv python install 3.13`)
- AWS CLI v2, AWS CDK v2, AgentCore CLI (`@aws/agentcore`)
- AWS credentials configured (`aws configure` or SSO)
- Bedrock model access for `anthropic.claude-sonnet-4-6` in `us-west-2`

## Local development

Two terminals:

```bash
# Terminal 1 — agent
cd coreAgent
uv sync                          # creates .venv with Python 3.13
agentcore dev                    # local server on 0.0.0.0:8080

# Terminal 2 — bridge
cd bridge
uv sync
LOCAL_AGENT_URL=http://localhost:8080 uvicorn bridge.main:app --reload
```

Test the bridge → agent flow without Slack:

```bash
curl -X POST http://localhost:8000/debug/message \
  -H "Content-Type: application/json" \
  -d '{"workspace_id":"demo-ws","user_id":"u1","text":"echo hello"}'
```

## Layout

```
agentcorePlayground/
├── coreAgent/                   # AgentCore Runtime agent
│   ├── agentcore/               # CLI-managed config + CDK (don't edit cdk/)
│   └── app/coreAgent/
│       ├── main.py              # @app.entrypoint, builds per-invocation Agent
│       ├── tenant.py            # TenantConfig + JSON loader
│       ├── tools.py             # CATALOG: in-process @tool registry
│       ├── ping.py              # custom @app.ping (HealthyBusy)
│       ├── memory_store.py      # MemoryStore protocol + InMemoryStore
│       ├── model/load.py        # Bedrock model loader (tenant-driven)
│       └── mcp_client/client.py # BYO via AgentCore Gateway (MCP client)
├── bridge/                      # FastAPI bridge
│   └── bridge/
│       ├── main.py              # routes
│       ├── client.py            # boto3 invoke wrapper
│       ├── tenant_resolver.py   # workspace → tenant
│       ├── async_dispatcher.py  # ack-then-post
│       └── adapters/{core,slack,debug}.py
└── examples/
    ├── tenants/demo.json
    └── workspace_to_tenant.json
```

## Status

Local-only scaffold. **No `agentcore deploy` yet** — review IAM, cost, and Gateway provisioning first. See `/path/to/project` for the full plan.
