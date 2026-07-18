# Core agent

The core agent is the AI runtime for this archived reference project. It runs on
Amazon Bedrock AgentCore Runtime and keeps tenant-specific behavior, tools,
memory, audit logging, metrics, and heartbeat handling out of the transport
bridge.

See the [root README](../README.md) for project status, architecture, and the
supported local demo path.

## What it owns

- Loading and validating tenant configuration
- Selecting the tenant's Bedrock model and system prompt
- Catalog and tenant-provided Gateway tools
- AgentCore Memory integration and local memory fixtures
- Audited tool execution and runtime metrics
- Heartbeat responses through <code>@app.ping</code>
- GitHub App, sandbox, pull request, and temporary dashboard workflows

The bridge invokes the runtime with a tenant ID, prompt, and transport context.
The agent assembles context, resolves the tenant's tools and memory settings,
runs the model, records best-effort audit data, and returns a transport-neutral
response.

## Local development

Python 3.13 and <code>uv</code> are required.

~~~bash
cd coreAgent/app/coreAgent
uv sync --frozen --extra test
uv run --frozen pytest
uv run --frozen python -m unittest test_metrics
~~~

To run the AgentCore development server with the checked-in tenant fixtures:

~~~bash
# From the repository root:
export AWS_PROFILE=my-sandbox-profile  # omit for the default credential chain
export AWS_REGION=eu-west-1
make aws-configure
cd coreAgent
AGENT_LOCAL_STORES=1 agentcore dev --logs
~~~

<code>AGENT_LOCAL_STORES=1</code> switches tenant, workspace, and memory storage
to local fixtures. Model calls still use Amazon Bedrock, so valid AWS credentials
and model access are required.

## Configuration

The runtime manifest is [agentcore/agentcore.json](agentcore/agentcore.json).
Edit that schema-backed file, not the generated CDK under
<code>agentcore/cdk/</code>. The authoritative tenant configuration dataclasses
live in [app/coreAgent/tenant.py](app/coreAgent/tenant.py).

Important environment variables include:

| Variable | Purpose |
|---|---|
| <code>AGENT_LOCAL_STORES</code> | Use checked-in local tenant and memory fixtures |
| <code>LOCAL_AUDIT</code> | Write local audit output while developing |
| <code>TENANTS_TABLE</code> / <code>AUDIT_LOG_TABLE</code> | Production DynamoDB tables |
| <code>AGENTCORE_MEMORY_ID</code> | Provisioned AgentCore Memory resource |
| <code>AGENTCORE_SEMANTIC_STRATEGY_ID</code> / <code>AGENTCORE_USER_PREF_STRATEGY_ID</code> | Optional memory strategies |
| <code>GITHUB_APP_ID</code> | GitHub App used by codebase workflows |
| <code>SANDBOX_JOBS_TABLE</code> | Sandbox job coordination table |
| <code>DASHBOARDS_TABLE</code> / <code>DASHBOARD_BASE_URL</code> | Temporary dashboard publishing |
| <code>AWS_REGION</code> | AWS region for Bedrock and AgentCore resources |

Never put private keys or tokens in <code>agentcore.json</code>. Use AWS Secrets
Manager or local environment files that are excluded from Git.

## Reference deployment

Validate and deploy the runtime from the repository root:

~~~bash
export AWS_PROFILE=my-sandbox-profile  # omit for the default credential chain
export AWS_REGION=eu-west-1
make aws-configure
make agent-deploy
REGION="$AWS_REGION" bash infra/data/scripts/attach_agent_policy.sh
~~~

The configuration helper derives the 12-digit account from STS, checks that the
AgentCore control plane is reachable in the selected region, and writes only the
ignored <code>agentcore/aws-targets.json</code>. It never copies an account ID
into the tracked runtime manifest. The region must appear in the pinned CLI
allowlist at
[<code>scripts/agentcore_cli_regions.txt</code>](../scripts/agentcore_cli_regions.txt).
The deployment wrapper temporarily injects the selected region and other
non-secret runtime settings into <code>agentcore.json</code>, runs validation and
deployment, and atomically restores the tracked manifest on success, failure,
or a trappable signal. It also rejects concurrent deployments.

<code>SIGKILL</code> cannot run cleanup. If a killed process leaves
<code>coreAgent/agentcore/.agentcore-deploy.lock/</code>, first confirm that no
AgentCore deployment is still running. If the lock contains
<code>agentcore.json.original</code>, move that file back to
<code>coreAgent/agentcore/agentcore.json</code>; then remove the lock's PID file
and any rendered temporary manifest before removing the empty directory. Do not
simply delete a lock that still holds the original manifest.

Provision memory separately from <code>infra/data</code> with
<code>uv run --with boto3 python infra/data/scripts/provision_memory.py
--region "$AWS_REGION"</code>, then configure the resulting resource and strategy
IDs. Gateway provisioning, sandbox support, and public dashboards are optional
integrations described in
[infra/data/README.md](../infra/data/README.md).

The repository's production workflow is manual-only and requires explicit
confirmation. Review AWS region availability, IAM scope, secrets, domains, and
costs before deploying an archived example.

## Layout

~~~text
coreAgent/
├── agentcore/              # Runtime manifest and generated deployment output
├── app/coreAgent/          # Runtime, tenant model, tools, memory, audit, metrics
└── README.md
~~~

Local tenant fixtures live in the repository-level <code>examples/</code>
directory. Memory and IAM provisioning helpers live under
<code>infra/data/scripts/</code>.
