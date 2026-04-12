# Secret leaked in logs

> **Owner:** Security · **Last updated:** 2026-03-11

If you're reading this because a secret is actually in CloudWatch or a log file, **page Alex immediately** (`@alex` in `#alerts-security`), then follow this runbook.

## Rotation before cleanup, ALWAYS

The single most important thing: **rotate the leaked secret first**, before you try to scrub logs. Logs may already be indexed by other systems. Cleanup is best-effort; rotation is the real fix.

## Step 1 — Rotate

Depends on the secret:

- **AWS access key** → see `security/iam-key-rotation.md`
- **Database password** → see `security/rds-password-rotation.md`
- **Third-party API key** → rotate via the provider's UI or API, then update AWS Secrets Manager
- **Slack / GitHub / other OAuth tokens** → revoke in provider UI, re-issue

## Step 2 — Identify scope

Where was the secret logged? Pull the CloudWatch log events:

```bash
aws logs filter-log-events \
  --log-group-name /aws/ecs/<service> \
  --filter-pattern '"<secret-or-fragment>"' \
  --start-time $(date -v-24H +%s)000
```

Note the log group and log stream names. You'll need them for cleanup.

## Step 3 — Check downstream indexing

CloudWatch logs are copied to:

- Datadog logs (retention: 15 days)
- S3 log archive (retention: 90 days)
- Sentry (filtered — usually doesn't pick up this shape)

Check each system for the secret value. Treat anywhere it lands as "leaked".

## Step 4 — Request log redaction

File a ticket with security-ops:

```
title: Redact leaked secret from <log group>
fields:
  secret_type: <type>
  rotated_at: <timestamp>
  log_group: <name>
  log_stream: <name>
  time_range: <from>-<to>
```

They can purge specific log events from CloudWatch (within 24h of the event). Datadog's log redaction is best-effort.

## Step 5 — Postmortem

Even if the scope was small, write a short postmortem. The point is the prevention action item: why did the secret get logged, and how do we stop it from happening again? Usual answers:

- Logging the whole request/response body
- A third-party library that logs on init
- A debug print statement that made it to prod

## Related

- `security/iam-key-rotation.md`
- `security/rds-password-rotation.md`
