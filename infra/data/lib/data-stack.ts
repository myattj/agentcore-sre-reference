/**
 * Data layer stack for the AgentCore multi-tenant agent.
 *
 * Four shared DynamoDB tables (isolation via tenant_id partition key,
 * enforced at the application layer — see CLAUDE.md and BUILD_PLAN.md) plus
 * two IAM managed policies (one for the agent, one for the bridge) scoped
 * to them and to a Secrets Manager prefix.
 *
 * Tables:
 *   - tenants                  : per-tenant config (read by agent + bridge)
 *   - workspace_to_tenant      : Slack team_id → tenant_id (read by bridge,
 *                                also read by agent for completeness)
 *   - audit_log                : per-invocation/per-tool-call rows (written
 *                                by agent)
 *   - processed_events         : Slack event_id dedup with TTL (written by
 *                                bridge — Slack retries failed events 3x
 *                                with backoff and we MUST NOT double-spend
 *                                Bedrock or write duplicate audit rows)
 *
 * The managed policies are exported as CfnOutputs; they must be attached
 * to their respective execution roles AFTER `agentcore deploy` creates the
 * agent role and AFTER the bridge deployment creates its task/lambda role.
 * `infra/data/scripts/attach_agent_policy.sh` does the agent attachment;
 * the bridge attachment is manual until the bridge has its own infra
 * (Fargate stack TBD).
 *
 * **Why not one table per tenant?** DynamoDB has a 2,500-table-per-region
 * quota and per-tenant tables would make every sign-up a CloudFormation
 * change. Every row carries a `tenant_id` partition key plus `created_at` /
 * `updated_at` so future GDPR deletion / compliance work is tractable.
 */
import {
  CfnOutput,
  RemovalPolicy,
  Stack,
  type StackProps,
} from 'aws-cdk-lib';
import {
  AttributeType,
  BillingMode,
  Table,
  TableEncryption,
} from 'aws-cdk-lib/aws-dynamodb';
import {
  ManagedPolicy,
  PolicyStatement,
  Effect,
} from 'aws-cdk-lib/aws-iam';
import { Construct } from 'constructs';

export class DataStack extends Stack {
  public readonly tenantsTable: Table;
  public readonly workspaceToTenantTable: Table;
  public readonly auditLogTable: Table;
  public readonly processedEventsTable: Table;
  public readonly agentDataAccessPolicy: ManagedPolicy;
  public readonly bridgeDataAccessPolicy: ManagedPolicy;

  constructor(scope: Construct, id: string, props: StackProps) {
    super(scope, id, props);

    // ------------------------------------------------------------------
    // DynamoDB tables
    // ------------------------------------------------------------------
    // PAY_PER_REQUEST: no capacity planning, scales to zero. Correct for
    // the low-traffic startup phase.
    //
    // PITR: on. Cheap and means "oh shit, we corrupted the table" is
    // recoverable.
    //
    // Encryption: AWS-managed KMS. Upgrade to customer-managed CMK when
    // the first enterprise customer demands it.
    //
    // RemovalPolicy: RETAIN. These tables hold customer data. A `cdk
    // destroy` must NOT delete them by accident. Operators destroy them
    // explicitly via the console or `aws dynamodb delete-table`.

    this.tenantsTable = new Table(this, 'TenantsTable', {
      tableName: 'tenants',
      partitionKey: { name: 'tenant_id', type: AttributeType.STRING },
      billingMode: BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      encryption: TableEncryption.AWS_MANAGED,
      removalPolicy: RemovalPolicy.RETAIN,
    });

    this.workspaceToTenantTable = new Table(this, 'WorkspaceToTenantTable', {
      tableName: 'workspace_to_tenant',
      partitionKey: { name: 'workspace_id', type: AttributeType.STRING },
      billingMode: BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      encryption: TableEncryption.AWS_MANAGED,
      removalPolicy: RemovalPolicy.RETAIN,
    });

    // audit_log has a composite key so we can Query all rows for a tenant
    // in timestamp order. SK format:
    //   INV#{iso_ts}#{invocation_id}
    //   TOOL#{iso_ts}#{invocation_id}#{uuid8}
    // ttl attribute reserved for future auto-expiration (no default TTL
    // set — ops enables it per retention policy).
    this.auditLogTable = new Table(this, 'AuditLogTable', {
      tableName: 'audit_log',
      partitionKey: { name: 'tenant_id', type: AttributeType.STRING },
      sortKey: { name: 'sk', type: AttributeType.STRING },
      billingMode: BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      encryption: TableEncryption.AWS_MANAGED,
      removalPolicy: RemovalPolicy.RETAIN,
      timeToLiveAttribute: 'ttl',
    });

    // processed_events: Slack retry dedup. Slack retries any non-200 event
    // 3x with backoff, and the bridge MUST not invoke the agent more than
    // once per logical event (would double-spend Bedrock and write
    // duplicate audit rows).
    //
    // Schema: { event_id: str (PK), ttl: number }
    // Lookup: PutItem with ConditionExpression="attribute_not_exists(event_id)"
    //         — succeeds first time, ConditionalCheckFailedException on retries.
    //
    // TTL: ~1 hour from write. Slack's retry window is 3 attempts over
    // ~15 minutes; 1h gives a comfortable buffer without growing the table
    // forever. Item retention is "low-value, high-churn" — TTL handles
    // cleanup automatically without a separate scanner.
    this.processedEventsTable = new Table(this, 'ProcessedEventsTable', {
      tableName: 'processed_events',
      partitionKey: { name: 'event_id', type: AttributeType.STRING },
      billingMode: BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      encryption: TableEncryption.AWS_MANAGED,
      // RemovalPolicy: RETAIN matches the other tables — even though this
      // is high-churn, accidental `cdk destroy` should not lose dedup
      // history during a deploy.
      removalPolicy: RemovalPolicy.RETAIN,
      timeToLiveAttribute: 'ttl',
    });

    // ------------------------------------------------------------------
    // IAM managed policy — attached to the agent's execution role
    // post-deploy via infra/data/scripts/attach_agent_policy.sh.
    // ------------------------------------------------------------------
    // Scoped narrowly:
    //   - read tenants + workspace_to_tenant (agent loads its config)
    //   - read/write audit_log (write path plus audit-log tooling)
    //   - read Secrets Manager under agentcore/tenants/* (per-tenant creds
    //     for BYO integrations — week 4+ populates these)
    //
    // Not granted:
    //   - ListTables / DescribeTable (agent doesn't need fleet visibility)
    //   - Secrets Manager write (secrets are managed by the onboarding
    //     service, not the agent)
    //   - DDB item-level condition keys (LeadingKeys) — application-level
    //     isolation is sufficient for week 1; revisit if enterprise tier
    //     demands hard IAM enforcement.

    this.agentDataAccessPolicy = new ManagedPolicy(this, 'AgentDataAccessPolicy', {
      managedPolicyName: 'AgentCoreDataAccess',
      description: 'Grants the AgentCore runtime role read/write to the data layer tables and read on tenant secrets.',
      statements: [
        new PolicyStatement({
          sid: 'TenantConfigRead',
          effect: Effect.ALLOW,
          actions: [
            'dynamodb:GetItem',
            'dynamodb:BatchGetItem',
            'dynamodb:Query',
          ],
          resources: [
            this.tenantsTable.tableArn,
            this.workspaceToTenantTable.tableArn,
          ],
        }),
        new PolicyStatement({
          sid: 'AuditLogWrite',
          effect: Effect.ALLOW,
          actions: [
            'dynamodb:PutItem',
            'dynamodb:BatchWriteItem',
            'dynamodb:Query',
          ],
          resources: [this.auditLogTable.tableArn],
        }),
        new PolicyStatement({
          sid: 'TenantSecretsRead',
          effect: Effect.ALLOW,
          actions: ['secretsmanager:GetSecretValue'],
          resources: [
            `arn:aws:secretsmanager:${this.region}:${this.account}:secret:agentcore/tenants/*`,
          ],
        }),
      ],
    });

    // ------------------------------------------------------------------
    // Bridge IAM managed policy — attached to the bridge's task/lambda
    // execution role. Separate from the agent policy because the bridge
    // has different permissions: it writes new tenants (OAuth callback),
    // writes Secrets Manager (storing per-tenant Slack bot tokens), and
    // writes processed_events (Slack retry dedup), but does NOT write to
    // the audit log (the agent owns that).
    // ------------------------------------------------------------------
    // Granted:
    //   - tenants / workspace_to_tenant: read (resolve tenant_id, fetch
    //     tenant config) AND write (OAuth callback creates new rows).
    //     Uses UpdateItem rather than PutItem so the if_not_exists
    //     created_at semantics work.
    //   - audit_log: NONE (agent owns it).
    //   - processed_events: PutItem (with ConditionExpression for dedup)
    //     + GetItem (read-back diagnostics; not strictly required but
    //     cheap to allow).
    //   - secretsmanager agentcore/tenants/*: GetSecretValue (fetch per-
    //     tenant Slack bot tokens for chat.postMessage) AND
    //     CreateSecret/PutSecretValue (OAuth callback stores new tokens).

    this.bridgeDataAccessPolicy = new ManagedPolicy(this, 'BridgeDataAccessPolicy', {
      managedPolicyName: 'AgentCoreBridgeDataAccess',
      description: 'Grants the bridge service role read/write to tenant tables, processed_events, and tenant secrets.',
      statements: [
        new PolicyStatement({
          sid: 'TenantTablesReadWrite',
          effect: Effect.ALLOW,
          actions: [
            'dynamodb:GetItem',
            'dynamodb:BatchGetItem',
            'dynamodb:Query',
            'dynamodb:UpdateItem',
            'dynamodb:PutItem',
          ],
          resources: [
            this.tenantsTable.tableArn,
            this.workspaceToTenantTable.tableArn,
          ],
        }),
        new PolicyStatement({
          sid: 'ProcessedEventsReadWrite',
          effect: Effect.ALLOW,
          actions: [
            'dynamodb:PutItem',
            'dynamodb:GetItem',
          ],
          resources: [this.processedEventsTable.tableArn],
        }),
        new PolicyStatement({
          sid: 'TenantSecretsReadWrite',
          effect: Effect.ALLOW,
          actions: [
            'secretsmanager:GetSecretValue',
            'secretsmanager:CreateSecret',
            'secretsmanager:PutSecretValue',
            'secretsmanager:DescribeSecret',
          ],
          resources: [
            `arn:aws:secretsmanager:${this.region}:${this.account}:secret:agentcore/tenants/*`,
          ],
        }),
      ],
    });

    // ------------------------------------------------------------------
    // Outputs — consumed by the seed script and the attach helper script.
    // ------------------------------------------------------------------
    new CfnOutput(this, 'TenantsTableName', {
      value: this.tenantsTable.tableName,
      description: 'Name of the tenants DynamoDB table',
      exportName: `${this.stackName}-TenantsTableName`,
    });

    new CfnOutput(this, 'WorkspaceToTenantTableName', {
      value: this.workspaceToTenantTable.tableName,
      description: 'Name of the workspace_to_tenant DynamoDB table',
      exportName: `${this.stackName}-WorkspaceToTenantTableName`,
    });

    new CfnOutput(this, 'AuditLogTableName', {
      value: this.auditLogTable.tableName,
      description: 'Name of the audit_log DynamoDB table',
      exportName: `${this.stackName}-AuditLogTableName`,
    });

    new CfnOutput(this, 'ProcessedEventsTableName', {
      value: this.processedEventsTable.tableName,
      description: 'Name of the processed_events DynamoDB table (Slack retry dedup)',
      exportName: `${this.stackName}-ProcessedEventsTableName`,
    });

    new CfnOutput(this, 'AgentDataAccessPolicyArn', {
      value: this.agentDataAccessPolicy.managedPolicyArn,
      description: 'ARN of the managed policy to attach to the agent execution role',
      exportName: `${this.stackName}-AgentDataAccessPolicyArn`,
    });

    new CfnOutput(this, 'BridgeDataAccessPolicyArn', {
      value: this.bridgeDataAccessPolicy.managedPolicyArn,
      description: 'ARN of the managed policy to attach to the bridge service role',
      exportName: `${this.stackName}-BridgeDataAccessPolicyArn`,
    });
  }
}
