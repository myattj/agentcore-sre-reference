# Acme Data Co runbooks

Internal operational runbooks for SRE, Data, Security, and Platform teams.

## Structure

- `incidents/` — incident kickoff, comms template, postmortems
- `infra/` — EKS, RDS, Terraform, networking
- `data/` — Snowflake, dbt, Airflow, ingest pipelines
- `security/` — IAM, SSO, credential rotation, audit
- `deploys/` — deploy procedures and rollbacks
- `oncall/` — handoff checklists, rotation norms

## Conventions

- One runbook per file, kebab-case filenames
- Every runbook starts with a **tl;dr** section answering "what do I do right now?"
- Every runbook lists the owning team and the last-updated date
- Concrete commands and queries over prose
- Link to other runbooks by relative path

## How the bot uses these

The AgentCore Reference agent has a GitHub App install on this repo. When you ask it "what's the runbook for X" in Slack, it runs `code_search` here first, then summarizes the key steps and links the file. The agent refreshes its view on every Slack invocation, so edits here are live within ~1 minute of pushing to `main`.
