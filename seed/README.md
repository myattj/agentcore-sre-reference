# Synthetic incident lab

These scripts stage a small, fictional production incident across Datadog and
Slack so you can exercise Agent's cross-system investigation flow. The story is
synthetic; the writes to your Datadog account and Slack workspace are real.

Both scripts are safe by default: without `--apply`, they only build and preview
the dataset. Re-running with `--apply` creates more metrics or duplicate Slack
threads, so use disposable test resources.

## The incident story

A deploy to the fictional `acme-labs/data-api` repository introduced an N+1 query
on `/api/v1/items/export`. The endpoint enriches each item with an individual
`session.get()` call. A second change reduced the database connection-pool timeout
to three seconds, turning rising latency into customer-visible failures.

| Relative time | Event |
|---|---|
| T-4h to T-2h | Healthy baseline |
| T-2h | The N+1 export endpoint deploys |
| T-2h to T-1h | Export latency climbs while traffic stays flat |
| T-1h | Latency crosses 2,000 ms and a P1 alert fires |
| T-1h to now | The pool saturates and timeout errors begin |

The conversation references these fictional repositories:

- `acme-labs/data-api` — FastAPI and PostgreSQL service
- `acme-labs/platform-infra` — infrastructure configuration
- `acme-labs/incident-runbooks` — operational runbooks

## Requirements

- [uv](https://docs.astral.sh/uv/) — commands below install `requests` into a
  temporary, isolated environment; they do not modify global Python packages.
- Python 3.13, matching the rest of this repository.

Preview both datasets without credentials or network calls:

```bash
uv run --python 3.13 --with requests==2.32.5 python seed/seed_datadog_metrics.py
uv run --python 3.13 --with requests==2.32.5 python seed/seed_slack_threads.py
```

The Datadog values are reproducible for a given `--seed` (default `20260412`).
Timestamps are anchored to the run time so the four-hour incident remains recent.

## 1. Datadog metrics

The Datadog script generates six one-minute metric series:

| Metric | Type | Signal |
|---|---|---|
| `acme.api.request.duration` (export) | gauge | 200 ms baseline rising above 5 s |
| `acme.api.request.duration` (items) | gauge | Healthy 50–120 ms contrast |
| `acme.api.request.count` | count | Traffic remains steady |
| `acme.api.error.count` | count | Timeouts begin at T-1h |
| `acme.db.query.duration` | gauge | Individual queries remain fast—an N+1 clue |
| `acme.db.connection_pool.active` | gauge | Connections ramp to full saturation |

Use short-lived test credentials and prompt for them before exporting. This
keeps the values out of the command line and shell history; environment
variables are still sensitive, so unset them when the run finishes:

```bash
read -rsp 'Datadog API key: ' DD_API_KEY
printf '\n'
read -rsp 'Datadog application key: ' DD_APP_KEY
printf '\n'
export DD_API_KEY DD_APP_KEY
export DD_SITE='datadoghq.com'  # optional; defaults to the US1 site

uv run --python 3.13 --with requests==2.32.5 \
  python seed/seed_datadog_metrics.py --seed 20260412 --apply

unset DD_API_KEY DD_APP_KEY
```

`DD_SITE` must be one of Datadog's supported public site hostnames. Schemes,
paths, ports, and arbitrary hosts are rejected before any request is made.

## 2. Slack threads

The Slack script creates three threads: a P1 monitor alert, a customer escalation,
and an engineer's debugging request. Each converges on the same N+1 and pool
exhaustion story, and each ends with a prompt for Agent.

### Use a separate seeder app

Create a dedicated, disposable Slack app for this script. Give it only the
`chat:write` bot scope and invite it only to the disposable channel. **Do not use
Agent's own bot token**: messages posted by that bot may be ignored as self-events,
and reusing its production credential needlessly increases the blast radius.

The seeder credential deliberately uses the distinct
`SLACK_SEEDER_BOT_TOKEN` variable. `AGENT_BOT_USER_ID` is not a credential; it is
the public Slack user ID that the synthetic messages should mention.

```bash
read -rsp 'Disposable Slack seeder bot token: ' SLACK_SEEDER_BOT_TOKEN
printf '\n'
export SLACK_SEEDER_BOT_TOKEN
export SLACK_SEED_CHANNEL_ID='C0123456789'
export AGENT_BOT_USER_ID='U0123456789'  # optional; enables real @mentions

uv run --python 3.13 --with requests==2.32.5 \
  python seed/seed_slack_threads.py --apply

unset SLACK_SEEDER_BOT_TOKEN
```

The channel and user inputs must be Slack IDs, not channel names or URLs. Remove
the seeded messages and revoke or delete the disposable seeder app when the lab
is finished.

## Tests

The tests cover generation determinism, incident-shape invariants, and input
validation. They mock nothing because they exercise only pure functions and make
no network calls.

```bash
uv run --python 3.13 --with requests==2.32.5 \
  python -m unittest discover -s seed/tests -v
```
