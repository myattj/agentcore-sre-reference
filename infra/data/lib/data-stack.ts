/**
 * Data layer stack for the AgentCore multi-tenant agent.
 *
 * Five shared DynamoDB tables plus IAM managed policies scoped to them and
 * to the relevant Secrets Manager prefixes. Tenant isolation is enforced in
 * the application layer; review every access path before production use.
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
 *   - dashboards               : short-lived bearer-link dashboard specs
 *
 * The managed policies are exported as CfnOutputs; they must be attached
 * to their respective execution roles AFTER `agentcore deploy` creates the
 * agent role and after the bridge deployment creates its task role.
 * `infra/data/scripts/attach_agent_policy.sh` does the agent attachment;
 * ServicesStack attaches the bridge policy directly.
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
  public readonly dashboardsTable: Table;
  public readonly agentDataAccessPolicy: ManagedPolicy;
  public readonly bridgeDataAccessPolicy: ManagedPolicy;
  public readonly onboardingDataAccessPolicy: ManagedPolicy;

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

    // dashboards: ephemeral dashboard specs generated by the agent's
    // render_dashboard tool. Each item holds a JSON panel spec (charts,
    // tables, stats, text) served by the onboarding service at /d/{token}.
    //
    // Schema: { token: str (PK) }
    // Application-level fields: tenant_id, created_by, created_at, title,
    // panels[], ttl.
    //
    // TTL: auto-deletes stale dashboards (default 7 days from creation).
    // DESTROY on `cdk destroy` — these may contain customer data, but are
    // deliberately ephemeral and should not survive stack teardown.
    this.dashboardsTable = new Table(this, 'DashboardsTable', {
      tableName: 'dashboards',
      partitionKey: { name: 'token', type: AttributeType.STRING },
      billingMode: BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: false },
      encryption: TableEncryption.AWS_MANAGED,
      removalPolicy: RemovalPolicy.DESTROY,
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
    //     for tenant-provided integrations)
    //   - read Secrets Manager under agentcore/platform/* (platform-level
    //     secrets shared across all tenants — currently just the GitHub App
    //     private key used by coreAgent/scm_github.py to mint installation
    //     tokens and any other shared platform credentials)
    //
    // Not granted:
    //   - ListTables / DescribeTable (agent doesn't need fleet visibility)
    //   - Secrets Manager write (secrets are managed by the onboarding
    //     service, not the agent)
    //   - DDB item-level condition keys (LeadingKeys) — isolation remains
    //     application-level. Add stronger principal or session-level
    //     isolation where the production threat model requires it.

    this.agentDataAccessPolicy = new ManagedPolicy(this, 'AgentDataAccessPolicy', {
      managedPolicyName: 'AgentCoreDataAccess',
      description: 'Grants the AgentCore runtime role read/write to the data layer tables and read on tenant secrets.',
      statements: [
        new PolicyStatement({
          sid: 'TenantConfigReadWrite',
          effect: Effect.ALLOW,
          actions: [
            'dynamodb:GetItem',
            'dynamodb:BatchGetItem',
            'dynamodb:Query',
            'dynamodb:UpdateItem',  // spend_tracker.py: atomic increment of monthly_spend_cents
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
          sid: 'DashboardsWrite',
          effect: Effect.ALLOW,
          actions: [
            'dynamodb:PutItem',
          ],
          resources: [this.dashboardsTable.tableArn],
        }),
        new PolicyStatement({
          sid: 'TenantSecretsRead',
          effect: Effect.ALLOW,
          actions: ['secretsmanager:GetSecretValue'],
          resources: [
            `arn:aws:secretsmanager:${this.region}:${this.account}:secret:agentcore/tenants/*`,
          ],
        }),
        // Platform-level secrets shared across tenants (GitHub App private
        // key, future: observability API keys, etc.). Separate statement
        // from TenantSecretsRead so the ARN scope stays readable.
        new PolicyStatement({
          sid: 'PlatformSecretsRead',
          effect: Effect.ALLOW,
          actions: ['secretsmanager:GetSecretValue'],
          resources: [
            `arn:aws:secretsmanager:${this.region}:${this.account}:secret:agentcore/platform/*`,
          ],
        }),
        // Memory data plane: create events, retrieve/write memory records.
        // Resource-level ARNs not supported for AgentCore Memory yet.
        new PolicyStatement({
          sid: 'MemoryDataPlane',
          effect: Effect.ALLOW,
          actions: [
            'bedrock-agentcore:CreateEvent',
            'bedrock-agentcore:GetEvent',
            'bedrock-agentcore:ListEvents',
            'bedrock-agentcore:RetrieveMemoryRecords',
            'bedrock-agentcore:ListMemoryRecords',
            'bedrock-agentcore:GetMemoryRecord',
            'bedrock-agentcore:BatchCreateMemoryRecords',
            'bedrock-agentcore:ListSessions',
            'bedrock-agentcore:ListActors',
          ],
          resources: ['*'],
        }),
        // Memory control plane read: agent verifies the memory resource
        // exists at startup. No write access — provisioning is done by
        // infra/data/scripts/provision_memory.py with operator credentials.
        new PolicyStatement({
          sid: 'MemoryControlPlaneRead',
          effect: Effect.ALLOW,
          actions: [
            'bedrock-agentcore-control:GetMemory',
            'bedrock-agentcore-control:ListMemories',
          ],
          resources: ['*'],
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
    //   - secretsmanager agentcore/platform/*: GetSecretValue only
    //     (read the GitHub App private key for install-time warm-start —
    //     the bridge mints its own installation token after the GitHub
    //     App OAuth handshake to fetch the tenant's repo list for
    //     ranking and default-repo seeding).

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
            'dynamodb:Scan',
            'dynamodb:UpdateItem',
            'dynamodb:PutItem',
            'dynamodb:TransactWriteItems',
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
          sid: 'DashboardsRead',
          effect: Effect.ALLOW,
          actions: [
            'dynamodb:GetItem',
          ],
          resources: [this.dashboardsTable.tableArn],
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
        // Platform-level secrets: read-only. The bridge reads the GitHub
        // App private key during install-time warm-start to mint an
        // installation token and fetch
        // the tenant's repo list for ranking. No write access — platform
        // secrets are provisioned out-of-band by operators.
        new PolicyStatement({
          sid: 'PlatformSecretsRead',
          effect: Effect.ALLOW,
          actions: ['secretsmanager:GetSecretValue'],
          resources: [
            `arn:aws:secretsmanager:${this.region}:${this.account}:secret:agentcore/platform/*`,
          ],
        }),
        // CloudWatch metrics read — powers the tenant-scoped metrics page
        // in the onboarding/admin UI and the /ops operator dashboard.
        // Both surfaces read the Agent/Runtime namespace via the bridge
        // (filtering is enforced in the bridge handler, not by IAM — the
        // GetMetricData API has no resource-level ARN for a namespace).
        // cloudwatch:ListMetrics is included so the /ops roster can
        // discover tenants without hardcoding the list.
        new PolicyStatement({
          sid: 'CloudWatchMetricsRead',
          effect: Effect.ALLOW,
          actions: [
            'cloudwatch:GetMetricData',
            'cloudwatch:GetMetricStatistics',
            'cloudwatch:ListMetrics',
          ],
          resources: ['*'],
        }),
        // Bedrock AgentCore Gateway control plane — BYO integration
        // provisioning. When a tenant connects a new integration
        // (PagerDuty, Jira, and the other supported single-secret targets) via
        // `POST /api/tenants/{id}/integrations/{integration}`, the bridge
        // calls `gateway_provisioner.ensure_credential_provider()` +
        // `ensure_gateway_target()` which fan out to these APIs.
        //
        // Without this statement, the first provisioning attempt dies with
        // `AccessDeniedException on bedrock-agentcore:ListApiKeyCredentialProviders`.
        //
        // Resources are scoped to our account + region. AgentCore's ARN
        // shapes for credential providers and gateway targets are
        // undocumented; `token-vault/default/apikeycredentialprovider/*`
        // matches the resource AWS reports in access-denied errors, and
        // `gateway/*` covers the shared Gateway + any tenant targets
        // regardless of whether AgentCore names them as sub-resources or
        // siblings. The `*` on token-vault/default is for the container
        // itself (needed by list/create).
        new PolicyStatement({
          sid: 'GatewayControlPlaneProvisioning',
          effect: Effect.ALLOW,
          actions: [
            // API key credential providers (AgentCore token vault)
            'bedrock-agentcore:ListApiKeyCredentialProviders',
            'bedrock-agentcore:CreateApiKeyCredentialProvider',
            'bedrock-agentcore:GetApiKeyCredentialProvider',
            'bedrock-agentcore:UpdateApiKeyCredentialProvider',
            'bedrock-agentcore:DeleteApiKeyCredentialProvider',
            // Gateway targets
            'bedrock-agentcore:ListGatewayTargets',
            'bedrock-agentcore:CreateGatewayTarget',
            'bedrock-agentcore:GetGatewayTarget',
            'bedrock-agentcore:UpdateGatewayTarget',
            'bedrock-agentcore:DeleteGatewayTarget',
          ],
          resources: [
            `arn:aws:bedrock-agentcore:${this.region}:${this.account}:token-vault/default`,
            `arn:aws:bedrock-agentcore:${this.region}:${this.account}:token-vault/default/apikeycredentialprovider/*`,
            `arn:aws:bedrock-agentcore:${this.region}:${this.account}:gateway/*`,
          ],
        }),
        // SSM parameters for the shared Gateway coordinates written by
        // `infra/data/scripts/provision_gateway.py`. The bridge reads
        // `/agentcore/gateway/id` and `/agentcore/gateway/url` once per
        // process via `gateway_provisioner._gateway_coordinates()` and
        // lru_caches the result. Scoped to the `/agentcore/gateway/`
        // prefix so the bridge can't enumerate other SSM params.
        new PolicyStatement({
          sid: 'GatewaySsmParametersRead',
          effect: Effect.ALLOW,
          actions: [
            'ssm:GetParameter',
            'ssm:GetParameters',
          ],
          resources: [
            `arn:aws:ssm:${this.region}:${this.account}:parameter/agentcore/gateway/*`,
          ],
        }),
      ],
    });

    // ------------------------------------------------------------------
    // Reserved onboarding IAM managed policy.
    //
    // The onboarding service talks to the bridge API and does not need direct
    // AWS data access. This legacy policy/output remains for compatibility
    // with stacks synthesized by earlier versions, but ServicesStack does not
    // attach it. Keeping that boundary gives the DDB merge semantics one
    // implementation, in the bridge.
    //
    // If a fork later adds direct server-side reads, review and replace this
    // policy rather than attaching it automatically.
    //
    // Narrower than `AgentCoreBridgeDataAccess`:
    //   - tenants: GetItem + UpdateItem only (no PutItem — only OAuth
    //     callback creates tenants)
    //   - workspace_to_tenant: GetItem only (read-only — the bridge
    //     owns workspace mapping writes during OAuth)
    //   - NO Secrets Manager access (bot-token storage is the bridge's job)
    //   - NO processed_events access (Slack retry dedup is bridge-only)
    // ------------------------------------------------------------------

    this.onboardingDataAccessPolicy = new ManagedPolicy(this, 'OnboardingDataAccessPolicy', {
      managedPolicyName: 'AgentCoreOnboardingDataAccess',
      description:
        'Reserved and deliberately unattached. Narrower than ' +
        'AgentCoreBridgeDataAccess; review before any future use.',
      statements: [
        new PolicyStatement({
          sid: 'TenantConfigReadWrite',
          effect: Effect.ALLOW,
          actions: [
            'dynamodb:GetItem',
            'dynamodb:UpdateItem',
          ],
          resources: [this.tenantsTable.tableArn],
        }),
        new PolicyStatement({
          sid: 'WorkspaceMappingRead',
          effect: Effect.ALLOW,
          actions: [
            'dynamodb:GetItem',
            'dynamodb:Query',
          ],
          resources: [this.workspaceToTenantTable.tableArn],
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

    new CfnOutput(this, 'DashboardsTableName', {
      value: this.dashboardsTable.tableName,
      description: 'Name of the dashboards DynamoDB table (ephemeral bot-generated dashboards)',
      exportName: `${this.stackName}-DashboardsTableName`,
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

    new CfnOutput(this, 'OnboardingDataAccessPolicyArn', {
      value: this.onboardingDataAccessPolicy.managedPolicyArn,
      description:
        'ARN of the reserved, deliberately unattached onboarding data policy.',
      exportName: `${this.stackName}-OnboardingDataAccessPolicyArn`,
    });
  }
}
