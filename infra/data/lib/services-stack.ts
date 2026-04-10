/**
 * Fargate services stack for the AgentCore platform.
 *
 * Deploys a VPC, ECS cluster, ALB (with optional HTTPS), and two
 * Fargate services (bridge + onboarding) with path-based routing.
 *
 * Bridge routes:  /slack/*, /api/*, /.well-known/*, /jwks.json, /healthz
 * Onboarding:     everything else (default target group)
 *
 * Cost-optimised for startup phase:
 *   - Public subnets only (no NAT gateway — saves ~$30/mo)
 *   - Fargate tasks with assignPublicIp for outbound internet
 *   - 0.25 vCPU / 512 MB per service (minimum Fargate size)
 *   - PAY_PER_REQUEST DynamoDB (already deployed in DataStack)
 *
 * Secrets:
 *   Pre-create two Secrets Manager secrets (NOT managed by CDK):
 *     agentcore/services/slack  → {SLACK_CLIENT_ID, SLACK_CLIENT_SECRET, SLACK_SIGNING_SECRET}
 *     agentcore/services/bridge → {BRIDGE_OAUTH_STATE_SECRET, BRIDGE_GATEWAY_JWT_PRIVATE_KEY_PEM}
 *   Pass their ARNs as context vars. ECS task definitions reference
 *   individual JSON keys via ecs.Secret.fromSecretsManager().
 */
import * as path from 'node:path';

import {
  CfnOutput,
  Duration,
  RemovalPolicy,
  Stack,
  type StackProps,
} from 'aws-cdk-lib';
import * as acm from 'aws-cdk-lib/aws-certificatemanager';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecr_assets from 'aws-cdk-lib/aws-ecr-assets';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import { Construct } from 'constructs';

export interface ServicesStackProps extends StackProps {
  /** AgentCore Runtime ARN — bridge invokes this via boto3. */
  readonly agentRuntimeArn: string;

  /** ARN of the ACM certificate for HTTPS. Omit for HTTP-only (testing). */
  readonly certificateArn?: string;

  /** Custom domain (e.g. "app.agentcore.dev"). Omit to use ALB DNS. */
  readonly domainName?: string;

  /** Secrets Manager ARN: {SLACK_CLIENT_ID, SLACK_CLIENT_SECRET, SLACK_SIGNING_SECRET}. */
  readonly slackSecretsArn: string;

  /** Secrets Manager ARN: {BRIDGE_OAUTH_STATE_SECRET, BRIDGE_GATEWAY_JWT_PRIVATE_KEY_PEM}. */
  readonly bridgeSecretsArn: string;

  /** Managed policy ARN from DataStack for the bridge task role. */
  readonly bridgeDataAccessPolicyArn: string;

  /** Managed policy ARN from DataStack for the onboarding task role. */
  readonly onboardingDataAccessPolicyArn: string;
}

export class ServicesStack extends Stack {
  constructor(scope: Construct, id: string, props: ServicesStackProps) {
    super(scope, id, props);

    // ------------------------------------------------------------------
    // VPC — public subnets only, no NAT gateway
    // ------------------------------------------------------------------
    const vpc = new ec2.Vpc(this, 'Vpc', {
      maxAzs: 2,
      natGateways: 0,
      subnetConfiguration: [
        {
          cidrMask: 24,
          name: 'Public',
          subnetType: ec2.SubnetType.PUBLIC,
        },
      ],
    });

    // ------------------------------------------------------------------
    // ECS Cluster
    // ------------------------------------------------------------------
    const cluster = new ecs.Cluster(this, 'Cluster', {
      clusterName: 'agentcore-services',
      vpc,
    });

    // ------------------------------------------------------------------
    // Application Load Balancer
    // ------------------------------------------------------------------
    const alb = new elbv2.ApplicationLoadBalancer(this, 'Alb', {
      vpc,
      internetFacing: true,
      loadBalancerName: 'agentcore-alb',
    });

    // ------------------------------------------------------------------
    // Secrets Manager references (pre-created, NOT CDK-managed)
    // ------------------------------------------------------------------
    const slackSecret = secretsmanager.Secret.fromSecretCompleteArn(
      this, 'SlackSecret', props.slackSecretsArn,
    );
    const bridgeSecret = secretsmanager.Secret.fromSecretCompleteArn(
      this, 'BridgeSecret', props.bridgeSecretsArn,
    );

    // ------------------------------------------------------------------
    // Derive the public URL (used in env vars and listener setup)
    // ------------------------------------------------------------------
    // When a custom domain + cert are provided, use HTTPS on that domain.
    // Otherwise fall back to the ALB's auto-generated DNS on HTTP (testing).
    const hasHttps = !!(props.certificateArn && props.domainName);
    const publicUrl = hasHttps
      ? `https://${props.domainName}`
      : `http://${alb.loadBalancerDnsName}`;

    // ------------------------------------------------------------------
    // Onboarding target group (defined first — it's the default)
    // ------------------------------------------------------------------
    const onboardingTg = new elbv2.ApplicationTargetGroup(this, 'OnboardingTG', {
      vpc,
      port: 3000,
      protocol: elbv2.ApplicationProtocol.HTTP,
      targetType: elbv2.TargetType.IP,
      healthCheck: {
        path: '/',
        interval: Duration.seconds(30),
        timeout: Duration.seconds(5),
        healthyThresholdCount: 2,
        unhealthyThresholdCount: 3,
        healthyHttpCodes: '200',
      },
      deregistrationDelay: Duration.seconds(30),
    });

    // ------------------------------------------------------------------
    // Bridge target group
    // ------------------------------------------------------------------
    const bridgeTg = new elbv2.ApplicationTargetGroup(this, 'BridgeTG', {
      vpc,
      port: 8000,
      protocol: elbv2.ApplicationProtocol.HTTP,
      targetType: elbv2.TargetType.IP,
      healthCheck: {
        path: '/healthz',
        interval: Duration.seconds(30),
        timeout: Duration.seconds(5),
        healthyThresholdCount: 2,
        unhealthyThresholdCount: 3,
        healthyHttpCodes: '200',
      },
      deregistrationDelay: Duration.seconds(30),
    });

    // ------------------------------------------------------------------
    // Listeners + routing
    // ------------------------------------------------------------------
    if (hasHttps) {
      const certificate = acm.Certificate.fromCertificateArn(
        this, 'Cert', props.certificateArn!,
      );

      // HTTPS listener — onboarding is the default, bridge on explicit paths
      const httpsListener = alb.addListener('HttpsListener', {
        port: 443,
        certificates: [certificate],
        defaultTargetGroups: [onboardingTg],
      });

      httpsListener.addAction('BridgeRoutes', {
        priority: 10,
        conditions: [
          elbv2.ListenerCondition.pathPatterns([
            '/slack/*', '/api/*', '/.well-known/*', '/jwks.json', '/healthz',
          ]),
        ],
        action: elbv2.ListenerAction.forward([bridgeTg]),
      });

      // HTTP → HTTPS redirect
      alb.addListener('HttpRedirect', {
        port: 80,
        defaultAction: elbv2.ListenerAction.redirect({
          protocol: 'HTTPS',
          port: '443',
          permanent: true,
        }),
      });
    } else {
      // HTTP-only (testing mode — Slack webhooks won't work)
      const httpListener = alb.addListener('HttpListener', {
        port: 80,
        defaultTargetGroups: [onboardingTg],
      });

      httpListener.addAction('BridgeRoutes', {
        priority: 10,
        conditions: [
          elbv2.ListenerCondition.pathPatterns([
            '/slack/*', '/api/*', '/.well-known/*', '/jwks.json', '/healthz',
          ]),
        ],
        action: elbv2.ListenerAction.forward([bridgeTg]),
      });
    }

    // ------------------------------------------------------------------
    // Shared ECS execution role (pull images + read secrets)
    // ------------------------------------------------------------------
    const execRole = new iam.Role(this, 'EcsExecRole', {
      assumedBy: new iam.ServicePrincipal('ecs-tasks.amazonaws.com'),
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName(
          'service-role/AmazonECSTaskExecutionRolePolicy',
        ),
      ],
    });
    slackSecret.grantRead(execRole);
    bridgeSecret.grantRead(execRole);

    // ------------------------------------------------------------------
    // Bridge task role (what the container can do at runtime)
    // ------------------------------------------------------------------
    const bridgeTaskRole = new iam.Role(this, 'BridgeTaskRole', {
      assumedBy: new iam.ServicePrincipal('ecs-tasks.amazonaws.com'),
      description:
        'Bridge Fargate task role. DDB + Secrets Manager (via managed policy) ' +
        '+ bedrock-agentcore:InvokeAgentRuntime.',
    });
    bridgeTaskRole.addManagedPolicy(
      iam.ManagedPolicy.fromManagedPolicyArn(
        this, 'BridgeDataPolicy', props.bridgeDataAccessPolicyArn,
      ),
    );
    bridgeTaskRole.addToPolicy(new iam.PolicyStatement({
      sid: 'InvokeAgentRuntime',
      actions: [
        'bedrock-agentcore:InvokeAgentRuntime',
        'bedrock-agentcore:InvokeAgentRuntimeForUser',
      ],
      resources: [
        props.agentRuntimeArn,
        `${props.agentRuntimeArn}/*`,
      ],
    }));

    // ------------------------------------------------------------------
    // Onboarding task role
    // ------------------------------------------------------------------
    const onboardingTaskRole = new iam.Role(this, 'OnboardingTaskRole', {
      assumedBy: new iam.ServicePrincipal('ecs-tasks.amazonaws.com'),
      description:
        'Onboarding Fargate task role. Currently no direct AWS access ' +
        '(all data flows through bridge /api/*). Managed policy attached ' +
        'for future direct DDB reads if SSR performance demands it.',
    });
    onboardingTaskRole.addManagedPolicy(
      iam.ManagedPolicy.fromManagedPolicyArn(
        this, 'OnboardingDataPolicy', props.onboardingDataAccessPolicyArn,
      ),
    );

    // ------------------------------------------------------------------
    // Docker image assets (CDK handles ECR repos automatically)
    // ------------------------------------------------------------------
    // __dirname at runtime is infra/data/dist/lib/ (compiled JS).
    // Four levels up: dist/lib → dist → data → infra → repo root.
    const repoRoot = path.resolve(__dirname, '..', '..', '..', '..');

    const bridgeImage = new ecr_assets.DockerImageAsset(this, 'BridgeImage', {
      directory: path.join(repoRoot, 'bridge'),
      platform: ecr_assets.Platform.LINUX_AMD64,
    });

    const onboardingImage = new ecr_assets.DockerImageAsset(this, 'OnboardingImage', {
      directory: path.join(repoRoot, 'onboarding'),
      platform: ecr_assets.Platform.LINUX_AMD64,
      buildArgs: {
        NEXT_PUBLIC_BRIDGE_INSTALL_URL: hasHttps
          ? `https://${props.domainName}/slack/install`
          // Fallback for HTTP-only testing — will be wrong until domain is set,
          // but avoids a hard failure during the first deploy.
          : `http://localhost:8000/slack/install`,
      },
    });

    // ------------------------------------------------------------------
    // Bridge Fargate service
    // ------------------------------------------------------------------
    const bridgeTaskDef = new ecs.FargateTaskDefinition(this, 'BridgeTaskDef', {
      memoryLimitMiB: 512,
      cpu: 256,
      taskRole: bridgeTaskRole,
      executionRole: execRole,
    });

    bridgeTaskDef.addContainer('bridge', {
      image: ecs.ContainerImage.fromDockerImageAsset(bridgeImage),
      portMappings: [{ containerPort: 8000 }],
      logging: ecs.LogDrivers.awsLogs({
        streamPrefix: 'bridge',
        logGroup: new logs.LogGroup(this, 'BridgeLogs', {
          logGroupName: '/ecs/agentcore-bridge',
          retention: logs.RetentionDays.ONE_MONTH,
          removalPolicy: RemovalPolicy.DESTROY,
        }),
      }),
      environment: {
        AWS_REGION: this.region,
        AGENT_RUNTIME_ARN: props.agentRuntimeArn,
        BRIDGE_PUBLIC_URL: publicUrl,
        SLACK_REDIRECT_URI: `${publicUrl}/slack/oauth/callback`,
        ONBOARDING_BASE_URL: publicUrl,
      },
      secrets: {
        SLACK_CLIENT_ID: ecs.Secret.fromSecretsManager(slackSecret, 'SLACK_CLIENT_ID'),
        SLACK_CLIENT_SECRET: ecs.Secret.fromSecretsManager(slackSecret, 'SLACK_CLIENT_SECRET'),
        SLACK_SIGNING_SECRET: ecs.Secret.fromSecretsManager(slackSecret, 'SLACK_SIGNING_SECRET'),
        BRIDGE_OAUTH_STATE_SECRET: ecs.Secret.fromSecretsManager(bridgeSecret, 'BRIDGE_OAUTH_STATE_SECRET'),
        BRIDGE_GATEWAY_JWT_PRIVATE_KEY_PEM: ecs.Secret.fromSecretsManager(bridgeSecret, 'BRIDGE_GATEWAY_JWT_PRIVATE_KEY_PEM'),
      },
    });

    const bridgeService = new ecs.FargateService(this, 'BridgeService', {
      serviceName: 'agentcore-bridge',
      cluster,
      taskDefinition: bridgeTaskDef,
      desiredCount: 1,
      assignPublicIp: true,
      minHealthyPercent: 100,
      circuitBreaker: { enable: true, rollback: true },
      vpcSubnets: { subnetType: ec2.SubnetType.PUBLIC },
    });
    bridgeService.attachToApplicationTargetGroup(bridgeTg);
    bridgeService.connections.allowFrom(alb, ec2.Port.tcp(8000), 'ALB to bridge');

    // ------------------------------------------------------------------
    // Onboarding Fargate service
    // ------------------------------------------------------------------
    const onboardingTaskDef = new ecs.FargateTaskDefinition(this, 'OnboardingTaskDef', {
      memoryLimitMiB: 512,
      cpu: 256,
      taskRole: onboardingTaskRole,
      executionRole: execRole,
    });

    onboardingTaskDef.addContainer('onboarding', {
      image: ecs.ContainerImage.fromDockerImageAsset(onboardingImage),
      portMappings: [{ containerPort: 3000 }],
      logging: ecs.LogDrivers.awsLogs({
        streamPrefix: 'onboarding',
        logGroup: new logs.LogGroup(this, 'OnboardingLogs', {
          logGroupName: '/ecs/agentcore-onboarding',
          retention: logs.RetentionDays.ONE_MONTH,
          removalPolicy: RemovalPolicy.DESTROY,
        }),
      }),
      environment: {
        NODE_ENV: 'production',
        BRIDGE_URL: publicUrl,
        // NEXT_PUBLIC_* is inlined at build time for the client bundle,
        // but lib/env.ts also validates it at runtime on the server.
        NEXT_PUBLIC_BRIDGE_INSTALL_URL: hasHttps
          ? `https://${props.domainName}/slack/install`
          : `http://localhost:8000/slack/install`,
      },
      secrets: {
        BRIDGE_OAUTH_STATE_SECRET: ecs.Secret.fromSecretsManager(bridgeSecret, 'BRIDGE_OAUTH_STATE_SECRET'),
      },
    });

    const onboardingService = new ecs.FargateService(this, 'OnboardingService', {
      serviceName: 'agentcore-onboarding',
      cluster,
      taskDefinition: onboardingTaskDef,
      desiredCount: 1,
      assignPublicIp: true,
      minHealthyPercent: 100,
      circuitBreaker: { enable: true, rollback: true },
      vpcSubnets: { subnetType: ec2.SubnetType.PUBLIC },
    });
    onboardingService.attachToApplicationTargetGroup(onboardingTg);
    onboardingService.connections.allowFrom(alb, ec2.Port.tcp(3000), 'ALB to onboarding');

    // ------------------------------------------------------------------
    // Outputs
    // ------------------------------------------------------------------
    new CfnOutput(this, 'AlbDnsName', {
      value: alb.loadBalancerDnsName,
      description: 'ALB DNS name. CNAME your custom domain to this.',
      exportName: `${this.stackName}-AlbDnsName`,
    });

    new CfnOutput(this, 'PublicUrl', {
      value: publicUrl,
      description: 'Public URL of the services (custom domain or ALB DNS).',
      exportName: `${this.stackName}-PublicUrl`,
    });

    new CfnOutput(this, 'BridgeServiceArn', {
      value: bridgeService.serviceArn,
      description: 'ARN of the bridge ECS service.',
    });

    new CfnOutput(this, 'OnboardingServiceArn', {
      value: onboardingService.serviceArn,
      description: 'ARN of the onboarding ECS service.',
    });

    new CfnOutput(this, 'ClusterName', {
      value: cluster.clusterName,
      description: 'ECS cluster name for CLI operations.',
    });
  }
}
