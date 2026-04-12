# AgentCore Reference test env — manual testing rig

Provision a **realistic, persistent, customer-shaped tenant** against the live production stack so you can open Slack and drive the agent through real scenarios without a ton of setup.

This is not an automated smoke test — that's `scripts/smoke.py`. This is a **persistent environment** you build once and use for manual exploration forever after. Every run of `bootstrap.py` is idempotent.

## What gets provisioned

After bootstrap:

- A tenant in prod DynamoDB (`is_internal_testenv=true` so the ops dashboard hides it from real-customer metrics)
- A rich "Acme Data Co" tenant config: custom system prompt, 3 escalation routes (sre / data-eng / security), 3 skills (runbook lookup / incident kickoff / oncall status), 6 channel personas, codebase bindings
- 10 Slack channels seeded with ~230 realistic messages: standups, PR traffic, Q&A threads, runbook lookups, alert floods, and three full incident timelines
- Three GitHub repos the bot can search via `code_search` / `code_read_file` / `code_find_symbol`

**Memory starts empty.** It will populate organically as you drive conversation — when you ask the bot a question that causes it to `search_team_history`, those results become part of its turn transcript and the extraction triggers eventually fire. Expect "day 1" feel for the first 15 minutes of real usage.

## One-time manual setup

### 1. Create a Slack workspace

Create a free workspace at <https://slack.com/create>. Name it whatever (e.g. "agentcore-testenv"). You'll be the admin.

### 2. Install the AgentCore Reference Slack app

Visit `https://agent.example.com/slack/install` and complete the OAuth flow into the workspace you just created. This:

- Creates a tenant row in DynamoDB (`tenant_id = slack-<team_id>`)
- Stores the bot token at `agentcore/tenants/<tenant_id>/slack/bot_token`
- Redirects you to the onboarding UI

**Copy the `tenant_id` from the URL after the redirect.** You'll need it for every bootstrap run.

### 3. Create the 10 expected channels

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

The seeder will auto-join each channel using the `channels:join` scope — you don't need to invite the bot manually.

### 4. Fork / create the three GitHub repos

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
git remote add origin git@github.com:<your-org>/acme-runbooks.git
git push -u origin main
```

Rename the two forks to `acme-data-api` and `acme-infra` via **Settings → Repository name** on github.com (one click each).

### 5. Install the AgentCore Reference GitHub App on the three repos

Visit the GitHub App install URL (from the onboarding UI, Integrations step), select the org, and grant access to `acme-data-api` / `acme-infra` / `acme-runbooks`.

GitHub redirects back with an `installation_id` in the URL. Copy it — you need it for bootstrap.

### 6. Run bootstrap

```bash
./scripts/testenv-bootstrap.sh \
  --tenant slack-t0xxxxxxxxx \
  --github-org <your-org> \
  --github-installation-id 12345678
```

Or set env vars once:

```bash
export TESTENV_GITHUB_ORG=<your-org>
export TESTENV_GITHUB_INSTALLATION_ID=12345678
./scripts/testenv-bootstrap.sh --tenant slack-t0xxxxxxxxx
```

Bootstrap is idempotent and resumable — re-running it only posts messages that haven't been posted yet (tracked in `.testenv-state.json`).

**Expected duration:** ~8 minutes (Slack history seeding is ~1.2s per message at the rate limit, ~230 messages).

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
#alerts-sre → find the unacked P2 and @agentcore

# Inject a fresh alert on demand
./scripts/testenv-inject-alert.sh --tenant slack-t0xxxxxxxxx \
    --type pagerduty --severity P2 --service checkout-api
```

## Resetting

- **Re-run bootstrap** — safe, idempotent, top-ups any missing seeded messages
- **Force-reseed from scratch** — delete `scripts/testenv/.testenv-state.json`, then run bootstrap (messages will duplicate in Slack but that's fine for a test env)
- **Nuke tenant row** — delete the tenant from DDB via AWS console, then re-install the Slack app to provision a fresh default, then re-run bootstrap

## Troubleshooting

**"No Slack bot token at agentcore/tenants/…/slack/bot_token"** → You haven't installed the AgentCore Reference Slack app yet. Visit `https://agent.example.com/slack/install`.

**"Workspace is missing N expected channels"** → Create the listed channels manually in the Slack UI, then re-run.

**"Could not find a secret named agentcore/services/bridge\*"** → Your local AWS creds don't have `secretsmanager:ListSecrets` on prod. Set `BRIDGE_OAUTH_STATE_SECRET` in env as a fallback (value from the prod secret).

**"PATCH failed: 422"** → The rich config dict failed Pydantic validation on the bridge. Run with `--skip-seed` to see the error, then fix `scripts/testenv/config.py` and re-run.

**Messages posting with the wrong username/icon** → The Slack app is missing `chat:write.customize`. Re-install the app to pick up the new scope.
