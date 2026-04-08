# infra/data — AgentCore data layer

AWS CDK (TypeScript) project that provisions the shared data layer for the
multi-tenant AgentCore agent:

| Resource | Purpose |
|---|---|
| `tenants` DynamoDB table | One row per tenant, PK=`tenant_id`, carries the full `TenantConfig` blob |
| `workspace_to_tenant` DynamoDB table | Maps Slack team_id / other client workspace IDs to tenant_id |
| `audit_log` DynamoDB table | Invocation + tool-call audit rows, PK=`tenant_id`, SK=`sk` |
| `processed_events` DynamoDB table | Slack event_id dedup with TTL — bridge writes here to suppress retries |
| `AgentCoreDataAccess` managed policy | IAM policy for the agent runtime: read tenants, write audit_log, read per-tenant secrets |
| `AgentCoreBridgeDataAccess` managed policy | IAM policy for the bridge: read/write tenants + workspace_to_tenant, read/write processed_events, read/write per-tenant secrets |

All three tables share a `tenant_id` partition key (or `workspace_id` in the
case of the mapping table) — **isolation is enforced at the application
layer**, not by IAM. See `CLAUDE.md` and `BUILD_PLAN.md` for the rationale.

Every row carries `created_at` and `updated_at` (ISO8601 UTC) as a cheap
hedge for future GDPR deletion / audit-trail work.

> **Important:** this is a sibling to `coreAgent/agentcore/cdk/`, which is
> managed by the `agentcore` CLI and MUST NOT be edited. This project is
> hand-authored and lives outside `coreAgent/` so CLI regeneration can
> never clobber it.

## Prerequisites

- Node.js 20+ and npm
- AWS CLI configured with a profile that has permissions to create DynamoDB
  tables and IAM managed policies in `us-west-2`
- `aws sts get-caller-identity` returns the account you want to deploy into
- `jq` (for the post-deploy attach helper)

## One-time setup

```bash
cd infra/data
npm install
```

If this is the first CDK deployment in the target account/region, also run:

```bash
npx cdk bootstrap aws://<account-id>/us-west-2
```

(Check with `aws cloudformation describe-stacks --stack-name CDKToolkit
--region us-west-2` — if that returns a stack, bootstrap already happened.)

## Deploy

```bash
# Review the CloudFormation template first:
npm run synth

# Deploy:
npm run deploy
```

The stack is named `AgentCore-coreAgent-data-us-west-2` by default. It
outputs the values you need for the next steps:

- `TenantsTableName`
- `WorkspaceToTenantTableName`
- `AuditLogTableName`
- `ProcessedEventsTableName`
- `AgentDataAccessPolicyArn`
- `BridgeDataAccessPolicyArn`

## Seed the tables from the local JSON fixtures

After the stack is deployed, migrate the existing `examples/tenants/*.json`
and `examples/workspace_to_tenant.json` into the new tables:

```bash
# Dry-run first:
uv run --with boto3 python infra/data/scripts/seed_tenants.py --dry-run

# Real seed (uses default table names):
uv run --with boto3 python infra/data/scripts/seed_tenants.py
```

The seed script is idempotent: re-running refreshes `updated_at` but
preserves the original `created_at`.

## Attach the managed policy to the agent role

`agentcore deploy` creates the agent's IAM execution role inside the
CLI-managed CDK stack. We attach our managed policy to that role via the
helper script:

```bash
bash infra/data/scripts/attach_agent_policy.sh
```

The script discovers the agent's role by describing the CloudFormation
resources of the `AgentCore-coreAgent-<target>` stack. If discovery fails
(e.g. the L3 construct's role logical ID changes), it prints a manual
fallback with the exact `aws iam attach-role-policy` command to run.

## Destroy

The tables are created with `RemovalPolicy.RETAIN` — `cdk destroy` will
delete the managed policy and the CloudFormation stack, but leave the
DynamoDB tables intact to protect customer data. To delete the tables
entirely:

```bash
aws dynamodb delete-table --table-name tenants            --region us-west-2
aws dynamodb delete-table --table-name workspace_to_tenant --region us-west-2
aws dynamodb delete-table --table-name audit_log          --region us-west-2
aws dynamodb delete-table --table-name processed_events   --region us-west-2
```

Only do this if you truly want to discard all tenant data.
