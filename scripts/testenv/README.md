# Agent test env — manual testing rig

Provision a **realistic, persistent, customer-shaped tenant** against a deployed
development or staging stack so you can open Slack and drive the agent through
real scenarios without a ton of setup. Do not point this synthetic-data rig at
a customer environment.

This is not an automated smoke test — that's `scripts/smoke.py`. This is a **persistent environment** you build once and use for manual exploration forever after. Every run of `bootstrap.py` is idempotent.

## What gets provisioned

After bootstrap:

- A tenant in the configured DynamoDB table (`is_internal_testenv=true` so the ops dashboard excludes it from customer metrics)
- A rich "Acme Data Co" tenant config: custom system prompt, 13 catalog tools, 3 escalation routes (sre / data-eng / security), 3 skills (runbook lookup / incident kickoff / oncall status), 7 channel personas, and optional codebase bindings
- 10 Slack channels seeded with ~230 realistic messages: standups, PR traffic, Q&A threads, runbook lookups, alert floods, and three full incident timelines
- Three GitHub repos the bot can search via `code_search` / `code_read_file` / `code_find_symbol`

Bootstrap keeps trust and enforcement fields off the tenant session API. It
marks only `config.is_internal_testenv` with a scoped DynamoDB update, approves
an optional GitHub installation through the operator-only endpoint, and then
PATCHes the tenant-editable config. The PATCH payload never contains BYO
credentials, a memory namespace, cost-cap policy, an installation ID, or the
internal-test marker.

**Memory starts empty.** It will populate organically as you drive conversation — when you ask the bot a question that causes it to `search_team_history`, those results become part of its turn transcript and the extraction triggers eventually fire. Expect "day 1" feel for the first 15 minutes of real usage.

## One-time manual setup

### 1. Create a Slack workspace

Create a free workspace at <https://slack.com/create>. Name it whatever (e.g. "agent-testenv"). You'll be the admin.

### 2. Install the disposable Slack seeder app

Create a second Slack app **only in this synthetic workspace** by importing
[`slack_seeder_manifest.json`](slack_seeder_manifest.json) at
<https://api.slack.com/apps>. Install it to the workspace, copy its **Bot User
OAuth Token**, and export it in the shell that will run the test rig:

```bash
export SLACK_SEEDER_BOT_TOKEN='xoxb-token-from-the-disposable-seeder-app'
```

This app alone receives `channels:join` and `chat:write.customize`, which let the
fixture generator join the public lab channels and render synthetic personas.
Never add those test-only scopes to the customer-facing Agent app. Revoke or
delete the seeder app when you retire the lab.

### 3. Install the Agent Slack app

Set `BRIDGE_BASE_URL` to your deployed bridge origin, then visit `$BRIDGE_BASE_URL/slack/install` and complete the OAuth flow into the workspace you just created. This:

- Creates a tenant row in DynamoDB (`tenant_id = slack-<team_id>`)
- Stores the bot token at `agentcore/tenants/<tenant_id>/slack/bot_token`
- Redirects you to the onboarding UI

**Copy the `tenant_id` from the URL after the redirect.** You'll need it for every bootstrap run.

### 4. Create the 10 expected channels

In your test workspace, create these public channels (copy-paste names, order doesn't matter):

```
alerts-sre
alerts-data
alerts-security
incidents
ask-data
ask-platform
ask-security
oncall
eng-general
eng-random
```

The disposable seeder app will auto-join each channel. Invite the Agent bot to
the channels where you want to exercise it; the customer-facing app intentionally
does not request permission to join channels by itself.

### 5. Fork / create the three GitHub repos

Create a GitHub org you control (or use an existing one). Fork these upstream repos into it:

| Upstream | Purpose |
|---|---|
| [`tiangolo/full-stack-fastapi-template`](https://github.com/tiangolo/full-stack-fastapi-template) | Fake `acme-data-api` — Python/FastAPI/SQLAlchemy code for `code_find_symbol` |
| [`hashicorp/learn-terraform-provision-eks-cluster`](https://github.com/hashicorp/learn-terraform-provision-eks-cluster) | Fake `acme-infra` — Terraform for `code_search` |

Then push a new repo `acme-runbooks` from the template in `scripts/testenv/runbooks-repo-template/`:

```bash
cd scripts/testenv/runbooks-repo-template/
git init -b main
git add .
git commit -m "initial commit: Acme Data Co runbooks"
git remote add origin git@github.com:YOUR_ORG/acme-runbooks.git
git push -u origin main
```

Rename the two forks to `acme-data-api` and `acme-infra` via **Settings → Repository name** on github.com (one click each).

### 6. Install the Agent GitHub App on the three repos

Visit the GitHub App install URL (from the onboarding UI, Integrations step), select the org, and grant access to `acme-data-api` / `acme-infra` / `acme-runbooks`.

GitHub redirects back with an `installation_id` in the URL. Copy it — you need it for bootstrap.

### 7. Run bootstrap

Use AWS credentials for the account that hosts the deployed tenant. They need
read/update access to the `tenants` table and, unless you provide the two
secrets in the environment, read access to the existing bridge secret in
Secrets Manager. Set the deployed bridge origin as well:

```bash
export BRIDGE_BASE_URL='https://your-bridge.example.com'
```

Bootstrap reads `ADMIN_SECRET` and `BRIDGE_OAUTH_STATE_SECRET` from the
environment first. If either is absent, it reads that key from the existing
`agentcore/services/bridge-*` Secrets Manager JSON. Secret values are sent only
in their designated request headers or signing operation and are never printed.

```bash
./scripts/testenv-bootstrap.sh \
  --tenant slack-t0xxxxxxxxx \
  --github-org YOUR_ORG \
  --github-installation-id 12345678
```

Or set env vars once:

```bash
export TESTENV_GITHUB_ORG=YOUR_ORG
export TESTENV_GITHUB_INSTALLATION_ID=12345678
./scripts/testenv-bootstrap.sh --tenant slack-t0xxxxxxxxx
```

When GitHub arguments are present, bootstrap first sends the numeric
installation ID and expected account login to
`POST /api/ops/tenants/{tenant_id}/codebases/github/approve` using
`X-Admin-Token`. Only a successful approval is followed by the tenant PATCH
that enables repo bindings. Any approval failure stops the run. Omit both
GitHub arguments to keep codebase tools disabled; providing only one is an
error. Because this call carries an operator secret, a remote bridge URL must
use HTTPS; plain HTTP is accepted only for a loopback development bridge.

Bootstrap is idempotent and resumable — re-running it only posts messages that haven't been posted yet (tracked in `.testenv-state.json`).

**Expected duration:** ~8 minutes (Slack history seeding is ~1.2s per message at the rate limit, ~230 messages).

### Optional external account fixtures

See [`integrations/README.md`](integrations/README.md) to populate disposable
PagerDuty, Jira, Linear, Sentry, or Datadog accounts. `--integrations all`
runs the four bootstrap-safe seeders and intentionally excludes Datadog.
Datadog is content-only and must be run separately with the explicit
`--skip-connect` acknowledgement shown in that guide.

## Daily testing flow

Open the test Slack workspace and drive the agent:

```
# Ask a question the bot can answer from seeded history
#ask-data  → "what does the team think about dbt vs airflow?"
#ask-platform → "where is the User model defined?"

# Trigger the runbook skill
#ask-platform → "/runbook rds-password-rotation"

# Trigger incident flow
#incidents → "catch me up on the Feb checkout incident"

# Trigger alert triage on a seeded unacked alert
#alerts-sre → find the unacked P2 and @agent

# Inject a fresh alert on demand
./scripts/testenv-inject-alert.sh --tenant slack-t0xxxxxxxxx \
    --type pagerduty --severity P2 --service checkout-api
```

## Resetting

- **Re-run bootstrap** — safe, idempotent, top-ups any missing seeded messages
- **Force-reseed from scratch** — delete `scripts/testenv/.testenv-state.json`, then run bootstrap (messages will duplicate in Slack but that's fine for a test env)
- **Nuke tenant row** — delete the tenant from DDB via AWS console, then re-install the Slack app to provision a fresh default, then re-run bootstrap

## Troubleshooting

**"SLACK_SEEDER_BOT_TOKEN is required"** → Install the disposable test app
from `slack_seeder_manifest.json`, export its `xoxb-...` token, and retry. Do not
use the Agent app's tenant token.

**"Workspace is missing N expected channels"** → Create the listed channels manually in the Slack UI, then re-run.

**"Could not find a secret named agentcore/services/bridge\*"** → Your local AWS creds don't have `secretsmanager:ListSecrets` in the configured account. Set `BRIDGE_OAUTH_STATE_SECRET` and, when using GitHub setup, `ADMIN_SECRET` in your shell from the deployed bridge configuration.

**"GitHub installation approval failed"** → Confirm the bridge URL, numeric
installation ID, GitHub org login, and `ADMIN_SECRET`. Approval is fail-closed:
the tenant PATCH does not run after this error.

**"could not set config.is_internal_testenv"** → The active AWS identity needs
`dynamodb:UpdateItem` on the `tenants` table. The bootstrap only updates that
single nested flag and will not fall back to a broad admin API.

**"PATCH failed: 422"** → The rich config dict failed Pydantic validation on the bridge. Run with `--skip-seed` to see the error, then fix `scripts/testenv/config.py` and re-run.

**Messages posting with the wrong username/icon** → The disposable seeder app
is missing `chat:write.customize`. Re-import its test manifest and reinstall
that app; do not expand the Agent app's scopes.
