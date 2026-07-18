# Architecture deep dive

> This is reference documentation for an archived example. Start with the
> [root README](../../README.md) for project status and the supported demo path.

## System boundaries

~~~text
Slack workspace
      │ signed webhook / OAuth
      ▼
FastAPI bridge
      │ invoke_agent_runtime or local HTTP
      ▼
Bedrock AgentCore runtime
      │
      ├── Bedrock model
      ├── AgentCore Memory
      ├── catalog tools
      └── optional AgentCore Gateway tools

Next.js onboarding and ops UI ── authenticated HTTP ──► bridge API
~~~

The bridge is transport infrastructure. It owns Slack protocol details, OAuth,
tenant resolution, session authentication, asynchronous dispatch, and outbound
messages. The core agent owns prompts, models, tools, memory, audit behavior,
metrics, and tenant-specific reasoning. Tool execution does not belong in the
bridge.

## Message path

1. Slack posts an event to <code>POST /slack/events</code>.
2. The Slack adapter verifies and normalizes it. The bridge applies event dedup
   and tenant bot policy, acknowledges within three seconds, and schedules
   background dispatch.
3. The dispatcher resolves Slack <code>team_id</code> to a tenant. Production
   reads DynamoDB; <code>LOCAL_DEV=1</code> reads the checked-in workspace map.
4. The bridge invokes the AgentCore runtime through boto3. When
   <code>LOCAL_AGENT_URL</code> is set, it instead posts to the local AgentCore
   development server.
5. The agent loads the tenant configuration from DynamoDB, or JSON fixtures when
   <code>AGENT_LOCAL_STORES=1</code>.
6. Context assembly merges channel settings, Slack thread history and
   permalinks, matching tenant skills, and any enabled memory context.
7. The runtime builds a fresh Strands agent with the tenant's model, system
   prompt, allowed catalog tools, and optional Gateway MCP client.
8. The model streams its response. Audited tools record best-effort invocation
   rows; audit failures never fail the user request.
9. The bridge posts the completed response back through the Slack adapter.

Long-running work is never awaited directly in the AgentCore entrypoint. It is
scheduled with <code>app.add_async_task</code> so <code>@app.ping</code> remains
responsive.

## Tenant customization

The authoritative shape is
[coreAgent/app/coreAgent/tenant.py](../../coreAgent/app/coreAgent/tenant.py).
Tenant configuration spans the following control surfaces:

| Area | Examples |
|---|---|
| Model | Bedrock model ID |
| Persona | System prompt and channel overrides |
| Catalog | Allowed built-in tools |
| Bring-your-own tools | Gateway endpoint and authentication |
| Memory | Extraction rules, namespace, and strategies |
| Runtime policy | Heartbeat thresholds and monthly cost caps |
| Workspace behavior | Channel personas and operator-managed admin IDs |
| Bot policy | Trusted bots and open channels |
| Context assembly | Thread depth and permalink handling |
| Skills | Triggered runbooks and required tools |
| Escalation | Team, channel, and contact routes |
| Codebases | GitHub installation binding, repositories, and channel routing |

Because bridge and agent deployments are independent, the tenant shape is
intentionally duplicated in the bridge API models and the onboarding TypeScript
types. Any shape change must update all three representations and the bridge's
default configuration builder together.

## Storage and isolation

<code>DataStack</code> creates five DynamoDB tables:

- <code>tenants</code> stores per-tenant configuration.
- <code>workspace_to_tenant</code> maps Slack workspaces to tenants.
- <code>audit_log</code> stores agent and tool audit events.
- <code>processed_events</code> provides Slack idempotency.
- <code>dashboards</code> stores expiring dashboard payloads.

The optional sandbox stack adds <code>sandbox_jobs</code>. Tenant IDs and memory
namespaces provide logical isolation; IAM policies separate agent, bridge,
onboarding, and sandbox access. This repository is an implementation example,
not a security or compliance certification.

AgentCore Memory is provisioned separately and referenced through environment
variables. The runtime manifest deliberately keeps <code>memory: none</code> so
the CLI does not create an unrelated short-term resource.

## Optional integrations

### AgentCore Gateway

Gateway exposes tenant-selected external services as MCP tools. The CDK Gateway
stack packages the request interceptor and JWT support resources. A separate
provisioning script creates or updates the actual AgentCore Gateway and writes
its coordinates to SSM. The bridge can provision single-credential integration
targets for Confluence, Notion, Jira, Linear, PagerDuty, and GitHub. Datadog's
two-secret API is deliberately disabled until a trusted credential broker is
placed in front of it.

### GitHub App and PR sandbox

Tenants can connect a GitHub App installation. The experimental sandbox worker
clones an authorized repository, performs bounded work, reports progress through
authenticated bridge callbacks, and can open a pull request. It is an optional
reference subsystem with its own job table, secrets, task role, and deployment
wrapper.

### Temporary dashboards

The agent can publish a visualization spec to DynamoDB and return a short-lived
URL rendered by the Next.js app. The token in that URL is the bearer credential;
anyone holding it can view the dashboard until its TTL expires. Public
deployments require an HTTPS <code>DASHBOARD_BASE_URL</code>.

## Deployment topology

The infrastructure application defines:

- Always: <code>DataStack</code> and <code>ObservabilityStack</code>
- With a bridge public URL: <code>GatewayStack</code>
- With an AgentCore runtime ARN: <code>ServicesStack</code> for the bridge and
  onboarding Fargate services
- With sandbox secret and VPC contexts: <code>SandboxStack</code>

The production GitHub Actions deployment is manual-only. Pull requests and
pushes run validation; a production deployment requires manual dispatch and an
explicit boolean confirmation. Memory and actual Gateway provisioning remain
operator steps.

## Repository map

~~~text
bridge/                     FastAPI transport, OAuth, tenant API, callbacks
coreAgent/                  AgentCore runtime, tenant behavior, tools, memory
onboarding/                 Next.js onboarding, workspace, ops, dashboards
infra/data/                 CDK data, observability, Gateway, services, sandbox
infra/sandbox/              Experimental sandbox worker image and tests
workers/gateway_interceptor Gateway request authorization interceptor
examples/                   Local tenant and workspace fixtures
~~~

For setup commands and deployment cautions, see
[the development guide](dev-guide.md).
