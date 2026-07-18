# Bridge

> [!NOTE]
> This service is part of the archived Agent reference implementation. Start with the [root README](../README.md) before deploying it.

The bridge is a FastAPI transport service between Slack and the shared AgentCore runtime. It owns webhook timing, OAuth, tenant resolution, session boundaries, and outbound Slack messages. It does not own agent tools or reasoning.

## Responsibilities

- Verify Slack requests, acknowledge events within three seconds, and dispatch agent work in the background.
- Resolve a Slack workspace to a tenant and invoke AgentCore through boto3 or a local AgentCore server.
- Provision new tenant rows and store per-workspace Slack bot tokens after OAuth.
- Expose the authenticated tenant API used by the Next.js service.
- Publish Gateway JWT discovery keys and provision integration targets.
- Receive progress and completion callbacks from the experimental PR sandbox.
- Serve short-lived dashboard specs to the Next.js renderer.

## Route families

| Routes | Purpose |
|---|---|
| <code>/healthz</code> | Service health |
| <code>/slack/events</code>, <code>/slack/interactions</code> | Slack events, reactions, and Block Kit actions |
| <code>/slack/install</code>, <code>/slack/oauth/callback</code> | Shared Slack app installation |
| <code>/api/tenants/{id}</code>, <code>/channels</code>, <code>/integrations/*</code>, <code>/codebases/*</code>, <code>/metrics</code> | Session-authenticated tenant configuration and integrations |
| <code>/api/ops/metrics/*</code>, <code>/api/ops/tenants/{id}/codebases/github/approve</code> | Shared-secret operator metrics and GitHub installation approval APIs |
| <code>/.well-known/openid-configuration</code>, <code>/jwks.json</code> | Gateway JWT discovery |
| <code>/internal/sandbox_progress</code>, <code>/internal/sandbox_complete</code> | Authenticated sandbox callbacks |
| <code>/internal/dashboard</code> | Read an unexpired dashboard; pass the bearer token in the <code>X-Dashboard-Token</code> header |
| <code>/debug/message</code> | Synchronous debug transport, registered only with <code>LOCAL_DEV=1</code> |

## Local development

Use Python 3.13. The tracked [<code>.env.example</code>](./.env.example) contains the local routing variables and blank Slack credentials.

For a no-cloud first look, run <code>make setup && make demo</code> from the
repository root. That starts this bridge with its local dashboard store and the
Next.js renderer; it does not start the agent or require AWS/Slack credentials.
The commands below are the separate full AgentCore development loop.

Start the local agent first:

~~~bash
cd coreAgent
AGENT_LOCAL_STORES=1 agentcore dev --logs
~~~

Then start the bridge:

~~~bash
cd bridge
uv sync --frozen --extra dev
../scripts/setup.sh --env-only
uv run uvicorn bridge.main:app --reload --port 8000 --env-file .env.local
~~~

The flag split is intentional:

- <code>AGENT_LOCAL_STORES=1</code> selects agent JSON fixtures.
- <code>LOCAL_DEV=1</code> selects the bridge JSON workspace mapping, in-memory dedup, and debug route.

Do not use <code>LOCAL_DEV</code> as the agent store flag. The AgentCore CLI reserves that variable.

### Test without Slack

~~~bash
curl -X POST http://localhost:8000/debug/message \
  -H 'Content-Type: application/json' \
  -d '{"workspace_id":"demo-ws","user_id":"u1","text":"Investigate this alert"}'
~~~

That request reaches the local AgentCore server and can call Bedrock, so an actual model reply still requires valid AWS credentials and model access.

You can exercise Slack parsing without real Slack credentials in local mode:

~~~bash
curl -X POST http://localhost:8000/slack/events \
  -H 'Content-Type: application/json' \
  -d '{
    "team_id": "T_LOCAL",
    "event_id": "Ev123",
    "event": {
      "text": "synthetic alert",
      "user": "U1",
      "thread_ts": "1.0",
      "channel": "C1"
    }
  }'
~~~

When <code>SLACK_SIGNING_SECRET</code> is blank, local mode skips HMAC verification with a warning. Never run that configuration publicly.

## Tests

~~~bash
uv sync --frozen --extra dev
uv run --frozen pytest
~~~

The suite mocks external services and covers Slack signatures, OAuth/session isolation, tenant APIs, Gateway provisioning, GitHub setup, metrics, sandbox callbacks, dashboards, and retry deduplication.

Public dashboard reads are protected by a per-source token bucket and a bounded
concurrent-read pool. Tune them with <code>DASHBOARD_READS_PER_MINUTE</code>
(default 60) and <code>DASHBOARD_MAX_CONCURRENT_READS</code> (default 16).
These controls are per bridge process; add AWS WAF or an equivalent distributed
edge limit for an internet-facing production deployment.
The reference CDK also sets <code>DASHBOARD_TRUST_X_FORWARDED_FOR=1</code>
because its security group accepts bridge traffic only from an ALB that appends
the real peer address. Do not enable that flag behind an untrusted proxy.

## Reference deployment

A real deployment needs:

1. Your own Slack app, based on [<code>slack_manifest.json</code>](./slack_manifest.json).
2. The data and IAM stacks from [<code>infra/data</code>](../infra/data/README.md).
3. A deployed AgentCore runtime ARN.
4. One HTTPS public origin on a domain you control, with both bridge and
   onboarding routes behind it. The Slack OAuth callback sets a host-scoped
   <code>tenant_session</code> HttpOnly cookie before redirecting to onboarding;
   separate public hosts will not receive that cookie. The install endpoint also
   binds its signed OAuth state to a short-lived HttpOnly, SameSite cookie and
   consumes that cookie at the callback, preventing cross-browser login CSRF.
5. Slack, bridge, and optional sandbox secrets in Secrets Manager.
6. <code>LOCAL_DEV</code> unset.

The Slack secret must include <code>SLACK_APP_ID</code>; reaction-derived
feedback fails closed without an exact event/message app-identity match. The
bridge secret must include <code>ADMIN_SECRET</code> in addition to the OAuth
and Gateway-signing keys. See the
[infrastructure guide](../infra/data/README.md#bridge-and-onboarding-services)
for exact JSON shapes and a history-safe creation example.

GitHub App codebase access has a separate operator-controlled trust binding.
Before the warm-start endpoint can mint an installation token or list
repositories, approve the exact numeric installation ID and expected GitHub
owner through the checked-in helper. It reads <code>ADMIN_SECRET</code> from the
environment and requires HTTPS except for loopback development URLs:

~~~bash
read -rsp 'Operator secret: ' ADMIN_SECRET
printf '\n'
export ADMIN_SECRET
python3.13 scripts/approve_github_installation.py \
  tenant-id 123456 expected-github-owner \
  --bridge-url https://agent.example.com
unset ADMIN_SECRET
~~~

The approval verifies the installation with GitHub and creates an exclusive
tenant binding. The session-authenticated tenant PATCH API intentionally cannot
create or change it.

The first approval for an installation performs a strongly consistent scan for
bindings created by older releases, then writes an authoritative lock row.
Subsequent approvals use that O(1) lock lookup. For a large existing tenant
table, backfill lock rows during a reviewed migration before opening approvals
to operators.

Agent-side <code>manage_config</code> writes remain read-only until an operator
adds exact Slack user IDs to <code>admin_user_ids</code> in the tenant's stored
configuration. That field is likewise not tenant-editable.

Keep onboarding session bearers out of URLs. If you split the bridge and
onboarding across different public hosts, add a reviewed one-time code exchange
or another explicit cookie handoff; do not restore a session-token query
parameter.

Gateway target names use the versioned form
<code>tenant-v1-&lt;base32(tenant_id)&gt;-&lt;integration&gt;</code>. The request
interceptor decodes that owner and compares the complete tenant ID; it never
uses a prefix match. Tenant IDs must be lowercase ASCII slugs of at most 40
characters; integration names use the same slug form with a 24-character
limit. Legacy <code>tenant-&lt;tenant_id&gt;-&lt;integration&gt;</code> targets deliberately
fail closed. When upgrading an existing deployment, reprovision every
integration to create its versioned target, deploy the strict interceptor
during a planned cutover, verify tool calls, and then delete the legacy targets
and credential providers. Do not rely on mixed naming as a long-term
compatibility mode.

The repository workflow runs validation on pull requests and pushes. Production deployment is manual only through <code>workflow_dispatch</code> with <code>deploy_production=true</code>.

Review the Slack scopes, IAM policies, session secret rotation, Gateway target authorization, dashboard bearer-link behavior, and sandbox callback secret before exposing the service. See [SECURITY.md](../SECURITY.md).

## Layout

~~~text
bridge/
├── .env.example
├── pyproject.toml
├── slack_manifest.json
├── tests/
└── bridge/
    ├── main.py                 FastAPI routes and Slack event boundary
    ├── api.py                  Tenant, integration, codebase, and metrics API
    ├── api_models.py           Bridge-side TenantConfig validation
    ├── client.py               Local HTTP and AgentCore boto3 transports
    ├── async_dispatcher.py     Acknowledge first, invoke and reply later
    ├── tenant_resolver.py      Workspace to tenant resolution
    ├── tenant_write.py         Local JSON and DynamoDB config persistence
    ├── gateway_jwt.py          Gateway JWT issuer and JWKS
    ├── gateway_provisioner.py  Per-integration Gateway targets
    ├── github_approval.py      Operator-only installation binding
    ├── github_install.py       GitHub App installation warm start
    ├── metrics_reader.py       Tenant and operator CloudWatch views
    ├── public_origin.py        Trusted public-origin validation
    ├── rate_limit.py           Bounded public dashboard reads
    ├── reaction_feedback.py    App-attributed Slack reaction feedback
    ├── dashboard_store.py      Expiring dashboard reads
    ├── sandbox_progress.py     Slack progress tracker
    └── adapters/               Slack and local debug transports
~~~
