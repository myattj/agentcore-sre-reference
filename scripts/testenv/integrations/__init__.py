"""External integration seeders for the Agent test env.

Every ``seed_<integration>.py`` module in this package:

  1. Loads credentials from Secrets Manager at
     ``agentcore/testenv/<integration>``
  2. Calls the integration's own API to populate realistic Acme Data Co
     content that matches the Slack seed: same services, same incidents,
     same teams

PagerDuty, Jira, and Linear can also call the bridge's managed connector
route before seeding. Sentry is content-only because it has no bridge route.
Datadog is content-only because its two-secret authentication shape cannot be
represented safely by a direct one-credential-provider Gateway target; its
CLI therefore requires an explicit ``--skip-connect`` acknowledgement.

The content in each integration intentionally references the same
fictional events as the Slack seed (Feb checkout retry storm, ingest
pipeline contention, orders dbt regression) so that cross-tool
correlation — the main reason to connect integrations at all — is
actually observable in the test env.

See ``README.md`` for the one-time signup + credential-storage recipe
for each integration.
"""
