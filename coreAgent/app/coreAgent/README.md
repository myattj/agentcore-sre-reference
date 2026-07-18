# Agent core runtime

This package is the tenant-aware AI runtime behind the archived Agent
reference implementation. It runs a Strands agent on Amazon Bedrock AgentCore,
streams its response back to the transport bridge, and keeps model behavior,
tool execution, memory, audit, metrics, and heartbeat state inside the agent
boundary.

Start with the [project README](../../../README.md) for the full architecture and
the [coreAgent service guide](../../README.md) for deployment context.

## Request lifecycle

`main.py` handles each runtime invocation in this order:

1. Load and validate the tenant configuration.
2. Apply channel-specific prompt, tool, and memory overrides.
3. Assemble Slack thread, permalink, skill, and codebase context.
4. Select whitelisted in-process catalog tools.
5. Attach remote tools advertised by the tenant's Gateway/MCP server.
6. Stream the Bedrock response while collecting usage metrics.
7. Write best-effort audit, spend, feedback, and memory records.

The bridge supplies transport context such as `tenant_id`, `user_id`,
`channel_id`, and `thread_id`; the runtime returns transport-neutral text.

## Important boundaries

- `tenant.py` is the authoritative Python tenant schema. Its shape is mirrored
  in the bridge and onboarding UI because the services have separate runtimes.
- `tools.py` contains platform-owned, in-process tools. Add these with
  `@audited_tool("name")` and whitelist them through tenant configuration.
- Customer and integration tools come from AgentCore Gateway through
  `mcp_client/`. They are advertised by the connected server and do not belong
  in the local catalog. For example, document search is only available when a
  connected Gateway/MCP target exposes a corresponding search operation.
- `runtime.py` owns the shared `BedrockAgentCoreApp`; `main.py`, `tools.py`, and
  `ping.py` import it to avoid a circular dependency.
- Long-running work must use `app.add_async_task`. Blocking the entrypoint also
  blocks `/ping` and can make AgentCore mark a healthy runtime unavailable.

## Local development

Python 3.13 and [uv](https://docs.astral.sh/uv/) are required.

```bash
cd coreAgent/app/coreAgent
uv sync --frozen --extra test
uv run --frozen pytest
uv run --frozen python -m unittest test_metrics
```

Run the development runtime from the service root so the AgentCore CLI can find
`agentcore/agentcore.json`:

```bash
cd coreAgent
AGENT_LOCAL_STORES=1 agentcore dev --logs
```

`AGENT_LOCAL_STORES=1` loads tenant JSON from `examples/tenants/`, uses
in-process memory, and avoids production DynamoDB writes. Model calls still go
to Bedrock, so the process needs AWS credentials and access to the configured
model. Never set this flag on a deployed runtime.

## Configuration

The checked-in [AgentCore manifest](../../agentcore/agentcore.json) intentionally
contains no credentials or deployed resource IDs. Configure optional production
features through environment variables or AWS-managed secrets:

| Variable | Purpose |
| --- | --- |
| `AWS_REGION` | Bedrock, DynamoDB, SSM, ECS, and Secrets Manager region |
| `TENANTS_TABLE` | Tenant configuration table; defaults to `tenants` |
| `AUDIT_LOG_TABLE` | Invocation and tool-call audit table |
| `AGENTCORE_MEMORY_ID` | Shared AgentCore Memory resource |
| `AGENTCORE_SEMANTIC_STRATEGY_ID` | Optional semantic-memory strategy |
| `AGENTCORE_USER_PREF_STRATEGY_ID` | Optional preference-memory strategy |
| `GITHUB_APP_ID` | GitHub App used by repository tools |
| `SANDBOX_JOBS_TABLE` | Pull-request sandbox job coordination table |
| `DASHBOARDS_TABLE` | Ephemeral dashboard specification table |
| `DASHBOARD_BASE_URL` | Public HTTPS origin for generated dashboard links |
| `LOCAL_AUDIT=memory` | In-memory audit and metrics stores for tests/dev |

Local-only GitHub credentials may be supplied through an ignored environment
file. In production, the private key is read from Secrets Manager. Never commit
tokens, private keys, resource-specific ARNs, or deployed AgentCore state.

## Package map

| Path | Responsibility |
| --- | --- |
| `main.py` | AgentCore entrypoint and per-invocation orchestration |
| `tenant.py` | Tenant models, defaults, and JSON/DynamoDB stores |
| `tools.py` | Audited catalog tools, code workflows, and dashboards |
| `builtin_skills.py` | First-match operational skill library |
| `context_assembler.py` | Threads, permalinks, skills, integrations, codebases |
| `mcp_client/` | Tenant Gateway/MCP client construction |
| `memory_store.py` | Local memory fallback and extraction fixtures |
| `audit.py`, `metrics.py`, `spend_tracker.py` | Best-effort observability and budgets |
| `ping.py` | Healthy/HealthyBusy lifecycle for background work |
| `tests/` | Unit tests plus opt-in Bedrock evaluation scenarios |

## Deployment note

Edit `coreAgent/agentcore/agentcore.json`, not the generated CDK under
`coreAgent/agentcore/cdk/`. From the repository root, run `make aws-configure`
and `make agent-deploy`; the wrapper validates and deploys with the selected
region, then restores the tracked manifest. Review IAM, memory IDs, Gateway
endpoints, model access, and cost controls before deploying.
