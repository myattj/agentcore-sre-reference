# External integration seeders

Populate disposable Datadog, PagerDuty, Jira, Linear, or Sentry accounts with
realistic Acme Data Co content that mirrors the Slack fixtures. PagerDuty,
Jira, and Linear can also exercise their managed AgentCore Gateway connector
before seeding. Sentry and Datadog are content-only.

Datadog is intentionally different: its API needs both an API key and an
application key, while a direct Gateway target supports one credential
provider. The bridge rejects that unsafe two-secret connector shape. The
Datadog seeder never sends credentials to the bridge and requires an explicit
`--skip-connect` acknowledgement.

> **Before using a managed connector:** make sure the bridge task role has the
> Gateway provisioning permissions defined in `infra/data/lib/data-stack.ts`.

## Prerequisites

- You've run the main bootstrap (`./scripts/testenv-bootstrap.sh --tenant slack-t0xxxxxxx`) successfully, so the test Slack workspace + tenant row already exist.
- Your local AWS credentials can read + write Secrets Manager in the same account + region as the bridge (typically your dev profile).
- Credentials live only in Secrets Manager under `agentcore/testenv/<integration>`; never commit them.

### History-safe secret helper

Define this helper once in the shell where you run the seeders. It opens a
mode-0600 temporary JSON file in your editor, validates the JSON, creates or
rotates the named secret, and removes the file. Secret values never appear in
the command line or shell history.

```bash
umask 077
TESTENV_SECRET_FILE=
cleanup_testenv_secret() {
  if [ -n "$TESTENV_SECRET_FILE" ]; then
    rm -f "$TESTENV_SECRET_FILE"
  fi
}
trap cleanup_testenv_secret EXIT
trap 'cleanup_testenv_secret; exit 130' HUP INT TERM

store_testenv_secret() {
  TESTENV_SECRET_NAME=$1
  TESTENV_SECRET_FILE=$(mktemp)
  "${EDITOR:-vi}" "$TESTENV_SECRET_FILE"
  if ! python3 -m json.tool "$TESTENV_SECRET_FILE" >/dev/null; then
    printf 'Secret file is not valid JSON. Nothing was uploaded.\n' >&2
    rm -f "$TESTENV_SECRET_FILE"
    TESTENV_SECRET_FILE=
    return 1
  fi
  if aws secretsmanager describe-secret \
      --secret-id "$TESTENV_SECRET_NAME" >/dev/null 2>&1; then
    aws secretsmanager put-secret-value \
      --secret-id "$TESTENV_SECRET_NAME" \
      --secret-string "file://$TESTENV_SECRET_FILE"
  else
    aws secretsmanager create-secret \
      --name "$TESTENV_SECRET_NAME" \
      --secret-string "file://$TESTENV_SECRET_FILE"
  fi
  TESTENV_SECRET_STATUS=$?
  rm -f "$TESTENV_SECRET_FILE"
  TESTENV_SECRET_FILE=
  return "$TESTENV_SECRET_STATUS"
}
```

Each integration section gives the JSON shape to enter and the secret name to
pass to this helper. Use only disposable integration accounts.

## Running

Either as part of bootstrap:

```bash
./scripts/testenv-bootstrap.sh \
  --tenant slack-t0xxxxxxxxx \
  --integrations pagerduty,jira
```

Or one at a time (useful when debugging or adding integrations incrementally):

```bash
bridge/.venv/bin/python -m scripts.testenv.integrations.seed_pagerduty \
  --tenant slack-t0xxxxxxxxx
```

Managed-connector seeder flags:

- `--skip-connect` — don't POST to the bridge; populate external content only
- `--skip-seed` — connect only (useful when you just want to wire the Gateway target and test agent tool calls)
- `--force` — re-seed even if existing state is found

Datadog supports only `--skip-connect` plus `--force`; it has no connect-only
mode. Sentry is also content-only because the bridge has no Sentry route.

Each seeder is idempotent — re-running is safe. State is tracked in `scripts/testenv/integrations/.<name>-seeded.json` (gitignored).

---

## 1. Datadog

**Why:** populate a disposable account with realistic metrics-adjacent events
and monitors for demos or manual exploration. This seeder does not make those
resources available as Agent tools; a trusted credential broker would be
required before enabling the two-secret connector.

### Signup

1. Visit <https://www.datadoghq.com/> and sign up. Free developer tier is enough (no card required for the first 14 days).
2. Note your **Datadog site** — the URL you land on after signup:
   - `app.datadoghq.com` → site is `datadoghq.com` (US1 — most common)
   - `app.datadoghq.eu` → site is `datadoghq.eu` (EU)
   - `app.us3.datadoghq.com` → site is `us3.datadoghq.com`
   - `app.us5.datadoghq.com` → site is `us5.datadoghq.com`
   - `app.ap1.datadoghq.com` → site is `ap1.datadoghq.com`

### Credentials

3. Get an **API key**: Organization Settings → Access → API Keys → New Key. Name it `agent-testenv`. Copy it.
4. Get an **Application key**: Organization Settings → Access → Application Keys → New Key. Name it `agent-testenv`. Copy it.

### Store in Secrets Manager

```json
{
  "api_key": "paste the API key here",
  "app_key": "paste the application key here",
  "site": "datadoghq.com"
}
```

```bash
store_testenv_secret agentcore/testenv/datadog
```

### Run

```bash
bridge/.venv/bin/python -m scripts.testenv.integrations.seed_datadog \
  --tenant slack-t0xxxxxxxxx \
  --skip-connect
```

`--skip-connect` is required and fail-closed. The local seeder reads the two
credentials from Secrets Manager and calls Datadog directly. It does not call
the bridge, provision a Gateway target, or change tenant configuration.

### What you get

- **~15 events** referencing the Feb checkout incident, ingest-pipeline contention, orders dbt regression, plus routine ops (deploys, Snowflake cost, EKS upgrade)
- **~8 monitors** on checkout-api / orders-api / user-service / RDS / ingest-pipeline with realistic thresholds and runbook links in the message field
- Every resource tagged with `acme-testenv` so you can find + clean them up later

### Verify

- Events: <https://app.datadoghq.com/event/explorer?query=tags%3Aacme-testenv>
- Monitors: <https://app.datadoghq.com/monitors/manage?q=tag%3A%22acme-testenv%22>

---

## 2. PagerDuty

**Why:** fits naturally with the alert-triage pattern. Seeded incidents let you test the agent's "any open pages?" and escalation flows.

### Signup

1. Visit <https://www.pagerduty.com/sign-up-free/> — free trial, then free tier (5 users) forever.
2. Complete the setup wizard. Pick any team name.

### Credentials

3. Generate a REST API key: Profile (top right) → User Settings → API Access Keys → Create New API User Token. Scope: Full Access (you can restrict later). Name it `agent-testenv`. Copy it.
4. Note your account email — the seeder uses it as the `From` header on incident creation calls. The seeder reads the first user on the account by default, or you can specify it explicitly.

### Store in Secrets Manager

```json
{
  "api_key": "paste the API key here",
  "from_email": "you@example.com"
}
```

```bash
store_testenv_secret agentcore/testenv/pagerduty
```

`from_email` is optional — omit it and the seeder will look up the first user on the account.

### Run

```bash
bridge/.venv/bin/python -m scripts.testenv.integrations.seed_pagerduty \
  --tenant slack-t0xxxxxxxxx
```

### What you get

- **3 escalation policies** (Acme SRE oncall, Acme Data oncall, Acme Security oncall)
- **5 services** (checkout-api, orders-api, user-service, ingest-pipeline, reporting-worker)
- **~20 incidents** across the services:
  - 12 resolved (historical incidents from the Slack seed)
  - 6 currently triggered (for the user to manually triage against)
  - 2 acknowledged / investigating

### Verify

Open `https://<your-subdomain>.pagerduty.com/incidents` — you should see the triggered ones at the top.

---

## 3. Jira

**Why:** the "file a ticket" workflow. Good for testing the agent's ability to create issues from an incident thread.

### Signup

1. Visit <https://www.atlassian.com/software/jira/free> — free for up to 10 users.
2. Pick a subdomain, e.g. `acme-testenv`. Your Jira Cloud URL is `https://acme-testenv.atlassian.net`.
3. Complete the setup wizard (pick "Software development" as the project type so the seeder can create a kanban project).

### Credentials

4. Generate an API token at <https://id.atlassian.com/manage-profile/security/api-tokens> → Create API token. Label it `agent-testenv`. Copy it — you won't see it again.
5. Note your Atlassian account email (the one you use to log in).

### Store in Secrets Manager

```json
{
  "email": "you@example.com",
  "api_token": "paste the API token here",
  "domain": "acme-testenv"
}
```

```bash
store_testenv_secret agentcore/testenv/jira
```

`domain` is the subdomain — just the part before `.atlassian.net`. Do NOT include `https://` or `.atlassian.net`.

### Run

```bash
bridge/.venv/bin/python -m scripts.testenv.integrations.seed_jira \
  --tenant slack-t0xxxxxxxxx
```

### What you get

- **ACME project** (software, kanban-style)
- **~25 issues** across Bug / Task / Story / Spike types
- Varied priorities (Highest / High / Medium / Low) and statuses (To Do / In Progress / Done)
- Labels reference the same services + incidents as the Slack seed

### Verify

Open `https://<your-domain>.atlassian.net/jira/software/projects/ACME/board`. You should see ~25 issues distributed across the board columns.

### Gotchas

- **Transitions may fail**: the seeder tries to transition issues to In Progress / Done, but Jira workflows vary. If the workflow doesn't have a transition with those exact names, you'll see `transition failed` warnings. The issues are created regardless — they just stay in the initial state.
- **Issue type mismatch**: the seeder assumes your project has Bug / Task / Story / Spike issue types. A "Software development" project template includes all of these. If you picked a different template, some issues may fail to create.

---

## 4. Linear

**Why:** modern alternative to Jira. Tests the agent's ability to work with a GraphQL backend.

### Signup

1. Visit <https://linear.app/> — free plan. Sign up with email.
2. Create a workspace (any name). Create a team (e.g. "Engineering"). The seeder uses the **first team** it finds.

### Credentials

3. Get a personal API key: Avatar (top left) → Settings → API → Personal API keys → Create new key. Scope: full workspace. Name it `agent-testenv`. Copy it.

### Store in Secrets Manager

```json
{
  "api_key": "paste the API key here"
}
```

```bash
store_testenv_secret agentcore/testenv/linear
```

### Run

```bash
bridge/.venv/bin/python -m scripts.testenv.integrations.seed_linear \
  --tenant slack-t0xxxxxxxxx
```

### What you get

- **~15 issues** in the first team, mirroring the Jira seed content
- Labels created automatically as needed
- Priorities varied (Urgent / High / Medium / Low)

### Verify

Open `https://linear.app/` and switch to your first team — you should see the seeded issues in the backlog.

---

## 5. Sentry

**Why:** error tracking. Lets you test the agent's ability to correlate Slack alerts with Sentry issues (e.g. "is this 504 in Sentry yet?").

> **Note:** Sentry has no bridge connect route. The seeder skips connection and
> only populates content. If a trusted managed connector is added later, wire
> it into `seed_sentry.py` explicitly.

### Signup

1. Visit <https://sentry.io/signup/> — free plan is 5k events/month.
2. Create an organization and project. Pick Python as the platform. Note the **organization slug** and **project slug** (visible in the URL: `https://sentry.io/organizations/<org-slug>/projects/<project-slug>/`).
3. On the project's setup page, copy the **DSN** (starts with `https://<key>@o12345.ingest.sentry.io/67890`).
4. Create an internal integration token: Settings → Developer Settings → New Internal Integration. Name it `agent-testenv`, give it `event:read` + `event:write` + `project:read` scopes, save, then copy the Token.

### Store in Secrets Manager

```json
{
  "auth_token": "paste the internal integration token here",
  "dsn": "paste the full DSN here",
  "organization": "your-org-slug",
  "project": "your-project-slug"
}
```

```bash
store_testenv_secret agentcore/testenv/sentry
```

### Run

```bash
bridge/.venv/bin/python -m scripts.testenv.integrations.seed_sentry \
  --tenant slack-t0xxxxxxxxx
```

### What you get

- **~10 error events** POSTed via the envelope endpoint
- Events have distinct fingerprints so Sentry groups them into ~10 separate issues
- Tags reference the same services and incidents as the Slack seed

### Verify

Open `https://sentry.io/organizations/<org-slug>/issues/`. Events take ~30 seconds to process before they appear as grouped issues.

---

## Running all bootstrap-supported seeders

```bash
./scripts/testenv-bootstrap.sh \
  --tenant slack-t0xxxxxxxxx \
  --github-org YOUR_ORG \
  --github-installation-id 12345678 \
  --integrations all
```

Order of execution: Slack seed first, then `pagerduty`, `jira`, `linear`, and
`sentry`. Datadog is excluded from `--integrations all` by design; run its
content-only command separately with `--skip-connect`.

Integration failures do NOT abort the bootstrap — each seeder is independent. The final summary shows a `✓` / `✗` per integration; failed ones can be re-run individually.

---

## Troubleshooting

### "No secret at agentcore/testenv/X"

The seeder couldn't find the Secrets Manager secret. Re-run the `aws secretsmanager create-secret` command for that integration.

### "bridge connect failed for X: HTTP 500 — provisioning failed: AccessDeniedException"

This applies to the PagerDuty, Jira, or Linear managed connectors, not the
content-only Datadog and Sentry seeders.

The bridge's IAM role is missing Gateway permissions. Use the same
<code>AWS_PROFILE</code> and <code>AWS_REGION</code> as the bridge deployment,
then deploy the matching regional data stack:

```bash
: "${AWS_REGION:?set AWS_REGION to the bridge deployment region}"
cd infra/data
npm run build
npx cdk deploy "AgentCore-coreAgent-data-${AWS_REGION}" \
  -c "region=${AWS_REGION}"
```

### "bridge connect failed for X: HTTP 401"

The bridge session token the seeder minted isn't being accepted. Check that `BRIDGE_OAUTH_STATE_SECRET` in Secrets Manager (under `agentcore/services/bridge-*`) hasn't been rotated. If it has, the seeder auto-reads the latest value, so retry once.

### "bridge connect failed for X: HTTP 400 — invalid API key"

The bridge successfully reached the integration's API but the key you provided didn't validate. Regenerate the key at the integration's UI and update the Secrets Manager secret.

### "found existing state (N seeded issues) — pass --force to re-seed"

The seeder skipped because `.<integration>-seeded.json` already has content. If you want to re-seed anyway (will create duplicates), pass `--force`. To start completely fresh, delete the state file and also manually clean the integration account.

### "transition failed" (Jira only)

Jira workflows vary per project. The seeder assumes standard To Do / In Progress / Done states. If your project has a custom workflow, some transitions will fail silently — the issues still exist, they just stay in the initial state. Not a real error.

### "no teams found on the Linear workspace" (Linear only)

Linear requires at least one team to exist. Create one via the Linear UI first, then re-run.

---

## Cleanup

To remove all seeded content from an integration, use the integration's own UI or API. The seeder does NOT have a cleanup mode — dropping fake content is a destructive operation on shared state and I didn't want a bug in a cleanup script to wipe real data.

Minimal cleanup recipes:

- **Datadog**: search for `tags:acme-testenv` in Events and Monitors, delete the matches
- **PagerDuty**: delete the services named `checkout-api` / `orders-api` / etc. — that cascades to their incidents
- **Jira**: delete the ACME project from Project Settings → Details → Move to trash
- **Linear**: delete the issues you seeded (use the state file `.linear-seeded.json` for the list of IDs)
- **Sentry**: issues auto-expire on the free tier after 30 days, or delete them manually

---

## Adding a new integration

1. Add `integrations/seed_<name>.py` with a direct, idempotent content-seeding path.
2. If one credential provider can represent the API's authentication safely,
   add a managed connector route in `bridge/bridge/api.py`. Otherwise keep the
   seeder content-only and make that limitation explicit.
3. Add bootstrap-safe seeders to `_BOOTSTRAP_INTEGRATIONS` in
   `scripts/testenv/bootstrap.py`. Never add a guaranteed-failure connector.
4. Add its credential shape and operating mode to this README.
5. Test `--integrations <name>` end to end before merging, or document and test
   the explicit direct-seeder command for content-only integrations.
