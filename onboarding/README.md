# Onboarding and operations UI

This Next.js application provides the human-facing setup and operations surfaces
for the self-hosted AgentCore project. It talks to the bridge API and
does not access DynamoDB or the agent runtime directly.

See the [root README](../README.md) for project status and the end-to-end local
demo.

## Included surfaces

| Route | Purpose |
|---|---|
| <code>/</code> | Public Slack installation entry point |
| <code>/onboarding/[tenantId]</code> | Integrations, completion, and error flow |
| <code>/workspace/[tenantId]</code> | Tenant overview, prompt, channels, skills, automations, and metrics |
| <code>/ops</code> | Operator login, tenant roster, and tenant detail views |
| <code>/d/[token]</code> | Experimental public dashboard addressed by a short-lived bearer token |
| <code>/github/installed</code> | GitHub App installation return handler with session-bound CSRF state |

The UI supports Slack channel settings, GitHub, and optional managed connectors
for Confluence, Notion, Jira, Linear, and PagerDuty. Its Datadog surface is
content-only guidance because the bridge deliberately rejects Datadog's unsafe
two-secret direct connector shape.

## Local development

Node.js 22 is recommended.

The easiest complete local path is <code>make setup && make demo</code> from the
repository root. It starts this UI and the bridge with a synthetic incident
dashboard, then shuts down both on one <kbd>Ctrl</kbd>+<kbd>C</kbd>. No AgentCore,
AWS, Slack, or API keys are involved.

To work on this component by itself:

~~~bash
cd onboarding
../scripts/setup.sh --env-only
npm ci
npm run dev
~~~

The app is then available at <http://localhost:3000>. Start the bridge on port
8000 before using flows that read or update tenant data.

The tracked [.env.example](.env.example) documents the intended local values:

| Variable | Purpose |
|---|---|
| <code>BRIDGE_URL</code> | Server-side bridge API base URL |
| <code>ONBOARDING_PUBLIC_URL</code> | Canonical browser origin for safe server-side redirects |
| <code>BRIDGE_OAUTH_STATE_SECRET</code> | HMAC secret for bridge-minted onboarding sessions |
| <code>NEXT_PUBLIC_BRIDGE_INSTALL_URL</code> | Public Slack install URL rendered by the browser |
| <code>ADMIN_SECRET</code> | Optional operator login secret |
| <code>GITHUB_APP_SLUG</code> | Optional GitHub App slug for installation links |

Use the same onboarding shared secret in the bridge's local environment. Do not
commit real credentials.

## OAuth and GitHub trust handoffs

The Slack OAuth callback is the only component that mints an onboarding session.
It sets <code>tenant_session</code> as an HttpOnly cookie and redirects to a clean
onboarding URL; the bearer never belongs in a query parameter. In production,
route the bridge callback and onboarding UI through one HTTPS public origin so
the host-scoped cookie reaches the UI. The local two-port setup works because
both services use the same <code>localhost</code> host. The signed OAuth state is
additionally bound to a short-lived HttpOnly, SameSite browser cookie that the
callback consumes.

The GitHub install link sends GitHub a short-lived, session-bound CSRF state,
not the onboarding bearer. Repository access remains disabled until a
privileged operator binds the returned numeric installation ID to
<code>codebases.github_installation_id</code> on the matching tenant row. The
tenant settings API cannot set or change that field; this prevents one tenant
from claiming another tenant's GitHub App installation.

Run the operator approval from the repository root after GitHub redirects back
with the pending installation:

~~~bash
read -rsp 'Operator secret: ' ADMIN_SECRET
printf '\n'
export ADMIN_SECRET
python3.13 scripts/approve_github_installation.py \
  tenant-id 123456 expected-github-owner \
  --bridge-url https://agent.example.com
unset ADMIN_SECRET
~~~

The UI keeps the integration pending until that verified, exclusive binding
exists. Agent-side configuration writes are separately read-only unless an
operator grants exact Slack user IDs through the non-tenant-editable
<code>admin_user_ids</code> field.

## Validation

~~~bash
cd onboarding
npm ci
BRIDGE_OAUTH_STATE_SECRET=$(openssl rand -hex 32) npm test
NEXT_PUBLIC_BRIDGE_INSTALL_URL=https://ci.test/slack/install npm run build
~~~

## Manual deployment

The optional Services CDK stack builds and runs this app as an ECS Fargate
service behind an Application Load Balancer. It is enabled only when the
required runtime and secret contexts are supplied. See
[infra/data/README.md](../infra/data/README.md).

Keep the supported stack's shared public-origin routing for the bridge and this
UI. A split-host deployment needs a separately reviewed one-time code exchange
or explicit cookie-domain design; never pass the onboarding session in a URL.

The production GitHub Actions workflow is manual-only, requires explicit
confirmation, and refuses to deploy without a domain and TLS certificate.
Configure secrets, least-privilege IAM policies, and budget controls before
deploying.

## Layout

~~~text
onboarding/
├── app/
│   ├── onboarding/[tenantId]/  # Tenant setup flow
│   ├── workspace/[tenantId]/   # Tenant configuration workspace
│   ├── ops/                    # Operator console
│   ├── d/[token]/              # Temporary public dashboards
│   └── github/installed/       # GitHub App callback surface
├── lib/                        # Bridge client, sessions, and shared types
├── public/                     # Static assets
└── .env.example               # Safe local configuration template
~~~
