/**
 * Pull-request sandbox stack.
 *
 * Provisions a one-shot Fargate task definition for `propose_pr` PR-writing
 * runs. Each `propose_pr` tool call from the agent fires `ecs.run_task`
 * against this task def, which spawns a fresh container that:
 *   1. Reads the job row from `sandbox_jobs` DDB
 *   2. Mints a GitHub App installation token
 *   3. Clones the target repo, creates a branch, makes the change, opens a PR
 *   4. Updates the job row + POSTs to the bridge `/internal/sandbox_complete`
 *
 * The sandbox runs Claude-authored commands inside the container, so blast
 * radius is the primary design constraint:
 *   - dedicated task role with NO access to the tenants table, audit log,
 *     processed_events, or any other tenants' secrets
 *   - dedicated security group with egress-only and no ingress
 *   - dedicated execution role (NOT shared with the bridge/onboarding
 *     EcsExecRole — sharing would let the sandbox enumerate bridge secrets)
 *
 * Cluster + VPC are reused from ServicesStack via context-passed values
 * (operator pulls them from `aws cloudformation describe-stacks` outputs
 * or via the deploy_sandbox.sh wrapper script). We DON'T use Fn.importValue
 * for the VPC because Vpc.fromVpcAttributes needs availabilityZones to be
 * known at synth time and CFN imports return tokens.
 *
 * `sandbox_jobs` DDB table lives here, not in DataStack: it's high-churn
 * TTL'd operational data with a clean cdk-destroy story. Distinct from
 * DataStack's "customer config + audit log, RemovalPolicy.RETAIN" tables.
 *
 * The `AgentCoreSandboxAccess` managed policy below is attached to the
 * agent runtime role after deployment by
 * `infra/data/scripts/attach_agent_policy.sh`.
 */
import * as path from 'node:path';

import {
  ArnFormat,
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
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecr_assets from 'aws-cdk-lib/aws-ecr-assets';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';

export interface SandboxStackProps extends StackProps {
  /**
   * VPC ID from ServicesStack. Pulled from
   * `${servicesStackName}-VpcId` CFN export by the deploy script and
   * passed via `--context sandboxVpcId=...`.
   */
  readonly vpcId: string;

  /**
   * Availability zones the ServicesStack VPC uses. Must be the actual
   * AZ list (not a CFN token), because Vpc.fromVpcAttributes uses the
   * count at synth time to validate subnet bindings. ServicesStack
   * uses `maxAzs: 2` so this is typically `['us-west-2a','us-west-2b']`.
   */
  readonly availabilityZones: string[];

  /**
   * Public subnet IDs from ServicesStack. Pulled from
   * `${servicesStackName}-VpcPublicSubnetIds`. Must align 1:1 with
   * `availabilityZones` order.
   */
  readonly publicSubnetIds: string[];

  /**
   * ECS cluster name from ServicesStack. Sandbox tasks run in this
   * cluster. Pulled from `${servicesStackName}-ClusterName`.
   */
  readonly clusterName: string;

  /**
   * ECS cluster ARN from ServicesStack. Used by the agent's
   * `propose_pr` for the `ecs.run_task` call. Pulled from
   * `${servicesStackName}-ClusterArn`.
   */
  readonly clusterArn: string;

  /**
   * Secrets Manager ARN for `agentcore/services/sandbox`. Same secret
   * passed to ServicesStack so the bridge and the sandbox container
   * agree on the Bearer token used for `/internal/sandbox_complete`.
   */
  readonly sandboxSecretsArn: string;

  /**
   * GitHub App ID — baked into the sandbox env so it can mint
   * installation tokens via `scm_github.py`. Use the same App ID as the
   * bridge and agent.
   */
  readonly githubAppId: string;

  /**
   * Public URL the sandbox uses to POST callbacks back to the bridge.
   * E.g. `https://agent.example.com/internal/sandbox_complete`. Used as
   * `SANDBOX_CALLBACK_URL` env var in the sandbox container.
   */
  readonly callbackUrl: string;

  /**
   * Secrets Manager ARN for `agentcore/platform/anthropic_api_key`.
   * The sandbox's inner Claude agent loop calls api.anthropic.com
   * directly (not Bedrock) for prompt caching support. The secret
   * value is the bare API key string (not JSON).
   *
   * Created out-of-band:
   * ```
   * aws secretsmanager create-secret \
   *   --name agentcore/platform/anthropic_api_key \
   *   --secret-string 'sk-ant-...'
   * ```
   */
  readonly anthropicSecretsArn?: string;
}

export class SandboxStack extends Stack {
  public readonly sandboxJobsTable: Table;
  public readonly sandboxAccessPolicy: iam.ManagedPolicy;

  constructor(scope: Construct, id: string, props: SandboxStackProps) {
    super(scope, id, props);

    const githubAppSecretsArn = this.formatArn({
      service: 'secretsmanager',
      resource: 'secret',
      resourceName: 'agentcore/platform/github_app/*',
      arnFormat: ArnFormat.COLON_RESOURCE_NAME,
    });
    const sandboxSsmParametersArn = this.formatArn({
      service: 'ssm',
      resource: 'parameter',
      resourceName: 'agentcore/sandbox/*',
      arnFormat: ArnFormat.SLASH_RESOURCE_NAME,
    });

    if (props.availabilityZones.length !== props.publicSubnetIds.length) {
      throw new Error(
        `availabilityZones (${props.availabilityZones.length}) and ` +
          `publicSubnetIds (${props.publicSubnetIds.length}) must have the ` +
          `same length. Pulled stale CloudFormation outputs?`,
      );
    }

    // ------------------------------------------------------------------
    // Rehydrate VPC + cluster from ServicesStack values.
    // No CFN cross-stack reference — sandbox can be cdk-destroyed
    // without touching bridge/onboarding.
    // ------------------------------------------------------------------
    const vpc = ec2.Vpc.fromVpcAttributes(this, 'ImportedVpc', {
      vpcId: props.vpcId,
      availabilityZones: props.availabilityZones,
      publicSubnetIds: props.publicSubnetIds,
    });

    const cluster = ecs.Cluster.fromClusterAttributes(this, 'ImportedCluster', {
      clusterName: props.clusterName,
      clusterArn: props.clusterArn,
      vpc,
      securityGroups: [],
    });

    // ------------------------------------------------------------------
    // sandbox_jobs DDB table — operational, high-churn, TTL'd.
    //
    // Lives here (not in DataStack) on purpose: DataStack is for
    // customer config + audit, RemovalPolicy.RETAIN. sandbox_jobs is
    // ephemeral job state that should be cdk-destroyable cleanly. We
    // still set RETAIN so an accidental `cdk destroy` won't nuke
    // in-flight jobs, but the destroy story is meaningfully different.
    // ------------------------------------------------------------------
    this.sandboxJobsTable = new Table(this, 'SandboxJobsTable', {
      tableName: 'sandbox_jobs',
      partitionKey: { name: 'task_id', type: AttributeType.STRING },
      billingMode: BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      encryption: TableEncryption.AWS_MANAGED,
      timeToLiveAttribute: 'ttl',
      removalPolicy: RemovalPolicy.RETAIN,
    });

    // ------------------------------------------------------------------
    // Sandbox container image. Build context lives at top-level
    // `infra/sandbox/` (sibling to `bridge/` and `onboarding/`).
    // ------------------------------------------------------------------
    // __dirname at runtime is infra/data/dist/lib/. Four parents up
    // gets us back to repo root, same as services-stack.ts.
    const repoRoot = path.resolve(__dirname, '..', '..', '..', '..');
    const sandboxImage = new ecr_assets.DockerImageAsset(this, 'SandboxImage', {
      directory: path.join(repoRoot, 'infra', 'sandbox'),
      platform: ecr_assets.Platform.LINUX_AMD64,
    });

    // ------------------------------------------------------------------
    // CloudWatch log group
    // ------------------------------------------------------------------
    const logGroup = new logs.LogGroup(this, 'SandboxLogs', {
      logGroupName: '/ecs/agentcore-sandbox',
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: RemovalPolicy.DESTROY,
    });

    // ------------------------------------------------------------------
    // Sandbox secret reference (pre-created out-of-band)
    // ------------------------------------------------------------------
    const sandboxSecret = secretsmanager.Secret.fromSecretCompleteArn(
      this, 'SandboxSecret', props.sandboxSecretsArn,
    );

    // ------------------------------------------------------------------
    // Anthropic API key secret (pre-created out-of-band).
    // Optional — sandbox degrades gracefully if not provided (the
    // entrypoint will fail at agent.run_agent_loop when the SDK
    // can't find ANTHROPIC_API_KEY, which writes a clean error row).
    // ------------------------------------------------------------------
    const anthropicSecret = props.anthropicSecretsArn
      ? secretsmanager.Secret.fromSecretCompleteArn(
          this, 'AnthropicSecret', props.anthropicSecretsArn,
        )
      : undefined;

    // ------------------------------------------------------------------
    // Task role — what the sandbox container can do AT RUNTIME.
    //
    // Scope is the entire reason the sandbox lives in its own stack.
    // The sandbox runs arbitrary Claude-authored bash; assume anything
    // it can read is exfiltrated. Grant the absolute minimum:
    //   - sandbox_jobs: full R/W on its own row
    //   - GitHub App private key in agentcore/platform/github_app/*
    //   - the sandbox callback secret (this very secret)
    //
    // EXPLICITLY NOT GRANTED:
    //   - tenants, workspace_to_tenant, audit_log, processed_events
    //   - any tenant secrets under agentcore/tenants/*
    //   - bridge secrets under agentcore/services/bridge (which holds
    //     BRIDGE_GATEWAY_JWT_PRIVATE_KEY_PEM — crown jewel)
    //   - Bedrock anything (the sandbox calls api.anthropic.com
    //     directly via its own ANTHROPIC_API_KEY, not Bedrock)
    // ------------------------------------------------------------------
    const taskRole = new iam.Role(this, 'SandboxTaskRole', {
      assumedBy: new iam.ServicePrincipal('ecs-tasks.amazonaws.com'),
      description:
        'Sandbox container runtime role. Tightly scoped: sandbox_jobs ' +
        'R/W, github_app private key read, sandbox callback secret read.',
    });

    this.sandboxJobsTable.grantReadWriteData(taskRole);

    taskRole.addToPolicy(new iam.PolicyStatement({
      sid: 'GitHubAppKeyRead',
      effect: iam.Effect.ALLOW,
      actions: ['secretsmanager:GetSecretValue'],
      resources: [githubAppSecretsArn],
    }));

    sandboxSecret.grantRead(taskRole);
    if (anthropicSecret) {
      anthropicSecret.grantRead(taskRole);
    }

    // ------------------------------------------------------------------
    // Execution role — Fargate's image-pull / secret-injection role.
    // SEPARATE from ServicesStack's EcsExecRole (do not share — the
    // bridge's exec role can read agentcore/services/bridge secrets,
    // and the sandbox should NEVER see those).
    //
    // The AWS managed AmazonECSTaskExecutionRolePolicy already grants
    // ECR pull + log writes to all log groups; we only need to grant
    // read on the sandbox secret for ecs.Secret.fromSecretsManager
    // injection at container start.
    // ------------------------------------------------------------------
    const executionRole = new iam.Role(this, 'SandboxExecutionRole', {
      assumedBy: new iam.ServicePrincipal('ecs-tasks.amazonaws.com'),
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName(
          'service-role/AmazonECSTaskExecutionRolePolicy',
        ),
      ],
      description:
        'Sandbox Fargate execution role. Image pull + container log ' +
        'shipping + sandbox callback secret read at boot.',
    });
    sandboxSecret.grantRead(executionRole);
    if (anthropicSecret) {
      anthropicSecret.grantRead(executionRole);
    }

    // ------------------------------------------------------------------
    // Security group — egress only, no ingress.
    //
    // Sandbox makes outbound calls to:
    //   - api.github.com (clone, PR creation)
    //   - github.com (git push via HTTPS)
    //   - api.anthropic.com (Claude agent loop)
    //   - dynamodb.<region>.amazonaws.com (sandbox_jobs read/write)
    //   - secretsmanager.<region>.amazonaws.com (token mint)
    //   - the configured public bridge domain (callback POST)
    // No inbound traffic — never receives a connection.
    // ------------------------------------------------------------------
    const sandboxSg = new ec2.SecurityGroup(this, 'SandboxSg', {
      vpc,
      description: 'Sandbox Fargate task - egress-only, no ingress.',
      allowAllOutbound: true,
    });

    // ------------------------------------------------------------------
    // Task definition
    //
    // 0.5 vCPU / 1024 MB. The Claude agent loop is I/O-bound (API
    // calls + file reads), not CPU-bound, so 0.5 vCPU is adequate.
    // Memory headroom is for the cloned repo + subprocess execution.
    // ------------------------------------------------------------------
    const taskDef = new ecs.FargateTaskDefinition(this, 'SandboxTaskDef', {
      family: 'agentcore-sandbox',
      memoryLimitMiB: 1024,
      cpu: 512,
      taskRole,
      executionRole,
    });

    taskDef.addContainer('sandbox', {
      image: ecs.ContainerImage.fromDockerImageAsset(sandboxImage),
      essential: true,
      logging: ecs.LogDrivers.awsLogs({
        streamPrefix: 'sandbox',
        logGroup,
      }),
      environment: {
        AWS_REGION: this.region,
        SANDBOX_JOBS_TABLE: this.sandboxJobsTable.tableName,
        SANDBOX_CALLBACK_URL: props.callbackUrl,
        GITHUB_APP_ID: props.githubAppId,
        // Agent loop configuration. Model and budget are plain env vars
        // (not secrets) — they're operational config, not credentials.
        SANDBOX_MODEL: 'claude-sonnet-4-6',
        SANDBOX_PR_BUDGET: '5.0',
      },
      secrets: {
        SANDBOX_CALLBACK_SECRET: ecs.Secret.fromSecretsManager(sandboxSecret, 'CALLBACK_SECRET'),
        // Anthropic API key — injected from Secrets Manager. The agent
        // loop calls api.anthropic.com directly (not Bedrock) for prompt
        // caching support. Optional: if the secret ARN isn't provided,
        // the entrypoint will fail at the agent.run_agent_loop call and
        // write a clean error row.
        ...(anthropicSecret ? {
          ANTHROPIC_API_KEY: ecs.Secret.fromSecretsManager(anthropicSecret),
        } : {}),
      },
    });

    // ------------------------------------------------------------------
    // SSM parameters — sandbox coordinates the agent reads at first
    // propose_pr call. Stored as plain StringParameters (no SecureString;
    // these are not secrets, just resource ARNs and IDs).
    //
    // Path /agentcore/sandbox/* mirrors the existing /agentcore/gateway/*
    // pattern from infra/data/scripts/provision_gateway.py. The agent
    // grants ssm:GetParametersByPath on the prefix via the
    // AgentCoreSandboxAccess managed policy below.
    // ------------------------------------------------------------------
    new ssm.StringParameter(this, 'SsmTaskDefArn', {
      parameterName: '/agentcore/sandbox/task_def_arn',
      stringValue: taskDef.taskDefinitionArn,
      description: 'Sandbox Fargate task definition ARN - read by propose_pr in coreAgent/tools.py',
    });

    new ssm.StringParameter(this, 'SsmClusterArn', {
      parameterName: '/agentcore/sandbox/cluster_arn',
      stringValue: cluster.clusterArn,
      description: 'ECS cluster ARN to run sandbox tasks in.',
    });

    new ssm.StringParameter(this, 'SsmSubnets', {
      parameterName: '/agentcore/sandbox/subnets',
      stringValue: props.publicSubnetIds.join(','),
      description: 'Comma-joined public subnet IDs for sandbox awsvpcConfiguration.',
    });

    new ssm.StringParameter(this, 'SsmSecurityGroups', {
      parameterName: '/agentcore/sandbox/security_groups',
      stringValue: sandboxSg.securityGroupId,
      description: 'Sandbox security group ID for awsvpcConfiguration.',
    });

    // ------------------------------------------------------------------
    // AgentCoreSandboxAccess managed policy
    //
    // Attached to the agent runtime role after deployment by
    // infra/data/scripts/attach_agent_policy.sh. This stays separate from
    // AgentCoreDataAccess (orthogonal blast radius — sandbox perms
    // are about firing tasks, not about reading customer data).
    //
    // Grants:
    //   - ecs:RunTask on this task def
    //   - iam:PassRole on the task role and execution role (REQUIRED
    //     for ecs:RunTask to work — otherwise you get a cryptic
    //     `User ... is not authorized to perform: iam:PassRole` error)
    //   - ssm:GetParameter*/GetParametersByPath on /agentcore/sandbox/*
    //   - dynamodb:PutItem/UpdateItem/GetItem on sandbox_jobs (so
    //     propose_pr can write the row before launch and the poller
    //     can read status)
    // ------------------------------------------------------------------
    this.sandboxAccessPolicy = new iam.ManagedPolicy(this, 'AgentSandboxAccessPolicy', {
      managedPolicyName: 'AgentCoreSandboxAccess',
      description:
        'Grants the AgentCore agent runtime role permission to ' +
        'launch sandbox Fargate tasks, read sandbox SSM coordinates, and ' +
        'write to the sandbox_jobs table. Attach via attach_agent_policy.sh.',
      statements: [
        new iam.PolicyStatement({
          sid: 'RunSandboxTask',
          effect: iam.Effect.ALLOW,
          actions: ['ecs:RunTask'],
          resources: [taskDef.taskDefinitionArn],
        }),
        new iam.PolicyStatement({
          sid: 'PassSandboxRoles',
          effect: iam.Effect.ALLOW,
          actions: ['iam:PassRole'],
          resources: [taskRole.roleArn, executionRole.roleArn],
          conditions: {
            StringEquals: {
              'iam:PassedToService': 'ecs-tasks.amazonaws.com',
            },
          },
        }),
        new iam.PolicyStatement({
          sid: 'ReadSandboxSsmCoords',
          effect: iam.Effect.ALLOW,
          actions: [
            'ssm:GetParameter',
            'ssm:GetParameters',
            'ssm:GetParametersByPath',
          ],
          resources: [sandboxSsmParametersArn],
        }),
        new iam.PolicyStatement({
          sid: 'WriteSandboxJobs',
          effect: iam.Effect.ALLOW,
          actions: [
            'dynamodb:PutItem',
            'dynamodb:UpdateItem',
            'dynamodb:GetItem',
          ],
          resources: [this.sandboxJobsTable.tableArn],
        }),
      ],
    });

    // ------------------------------------------------------------------
    // Outputs
    // ------------------------------------------------------------------
    new CfnOutput(this, 'SandboxTaskDefArn', {
      value: taskDef.taskDefinitionArn,
      description: 'Sandbox Fargate task definition ARN.',
      exportName: `${this.stackName}-SandboxTaskDefArn`,
    });

    new CfnOutput(this, 'SandboxJobsTableName', {
      value: this.sandboxJobsTable.tableName,
      description: 'Sandbox jobs DDB table name.',
      exportName: `${this.stackName}-SandboxJobsTableName`,
    });

    new CfnOutput(this, 'SandboxLogGroupName', {
      value: logGroup.logGroupName,
      description: 'CloudWatch log group for sandbox container output.',
    });

    new CfnOutput(this, 'AgentSandboxAccessPolicyArn', {
      value: this.sandboxAccessPolicy.managedPolicyArn,
      description: 'Managed policy ARN - attach to agent role via attach_agent_policy.sh.',
      exportName: `${this.stackName}-AgentSandboxAccessPolicyArn`,
    });

    new CfnOutput(this, 'SandboxSecurityGroupId', {
      value: sandboxSg.securityGroupId,
      description: 'Sandbox security group ID - egress-only.',
    });
  }
}
