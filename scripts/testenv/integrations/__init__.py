"""External integration seeders for the AgentCore Reference test env.

Each ``seed_<integration>.py`` module in this package:

  1. Loads credentials from Secrets Manager at
     ``agentcore/testenv/<integration>``
  2. Calls the bridge's ``POST /api/tenants/{id}/integrations/<integration>``
     route to provision a Gateway target + credential provider
     (unless ``--no-connect`` is passed)
  3. Calls the integration's own API to populate realistic Acme Data Co
     content that matches the Slack seed: same services, same incidents,
     same teams

The content in each integration intentionally references the same
fictional events as the Slack seed (Feb checkout retry storm, ingest
pipeline contention, orders dbt regression) so that cross-tool
correlation — the main reason to connect integrations at all — is
actually observable in the test env.

See ``README.md`` for the one-time signup + credential-storage recipe
for each integration.
"""
