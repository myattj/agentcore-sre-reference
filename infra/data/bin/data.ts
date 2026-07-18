#!/usr/bin/env node
/**
 * CDK app entrypoint for the AgentCore data layer.
 *
 * Deploys one DataStack using the account and region selected by the CDK
 * credential chain. The stack name mirrors the
 * naming convention of the CLI-managed agent stack:
 *   AgentCore-coreAgent-data-<region>
 *
 * Override the profile region by passing --context region=... .
 */
import { App, Fn } from 'aws-cdk-lib';
import { DataStack } from '../lib/data-stack';
import { GatewayStack } from '../lib/gateway-stack';
import { ObservabilityStack } from '../lib/observability-stack';
import { SandboxStack } from '../lib/sandbox-stack';
import { ServicesStack } from '../lib/services-stack';
import {
  validateAgentRuntimeArn,
  validateAwsTarget,
  validateRegionalArn,
} from '../lib/aws-target';

const app = new App();

function requiredContext(name: string): string {
  const value = app.node.tryGetContext(name) as string | undefined;
  if (!value) {
    throw new Error(`${name} context is required for this stack.`);
  }
  return value;
}

// Keep every related stack in the region selected by the active CDK profile.
// An explicit context value wins, which lets CI and deployment wrappers use
// one DEPLOY_REGION value without mutating cdk.json. The final fallback keeps
// credential-free synth deterministic; real deployments should always have a
// profile or explicit region.
const account = process.env.CDK_DEFAULT_ACCOUNT;
const region =
  (app.node.tryGetContext('region') as string | undefined) ??
  process.env.CDK_DEFAULT_REGION ??
  process.env.AWS_REGION ??
  process.env.AWS_DEFAULT_REGION ??
  'us-west-2';

if (!account) {
  throw new Error(
    'CDK_DEFAULT_ACCOUNT is not set. Run `aws sts get-caller-identity` to confirm ' +
      'your AWS credentials are active, then retry (CDK picks this up automatically ' +
      'from your default profile).',
  );
}
validateAwsTarget(account, region);

new DataStack(app, `AgentCore-coreAgent-data-${region}`, {
  env: { account, region },
  description: 'DynamoDB tables + IAM managed policy for the AgentCore multi-tenant agent',
  tags: {
    'agentcore:project-name': 'coreAgent',
    'agentcore:stack-type': 'data',
  },
});

// ObservabilityStack — CloudWatch dashboard + SNS alarms for
// the platform-wide metrics the agent emits via EMF (see
// coreAgent/app/coreAgent/metrics.py).
//
// Optional context:
//   --context alarmEmail=ops@example.com
//     Email address to subscribe to the operator alarms SNS topic.
//     When omitted, the topic is created with no subscription (operator
//     can wire one up in the console later).
//
// Always deploys — no required context. Empty until the agent is
// redeployed with the EMF emitter, at which point widgets populate
// automatically on the next invocation.
new ObservabilityStack(app, `AgentCore-coreAgent-observability-${region}`, {
  env: { account, region },
  description:
    'CloudWatch dashboard + SNS alarms consuming EMF metrics emitted by the agent.',
  tags: {
    'agentcore:project-name': 'coreAgent',
    'agentcore:stack-type': 'observability',
  },
  alarmEmail: app.node.tryGetContext('alarmEmail') as string | undefined,
});

// GatewayStack — deploys the request interceptor Lambda
// and the IAM role the shared AgentCore Gateway assumes. The Gateway
// resource itself is created by infra/data/scripts/provision_gateway.py
// after this stack deploys (CDK has no L2 construct for AgentCore Gateway
// as of 2026-04).
//
// Required context:
//   --context bridgePublicUrl=https://<your-bridge-or-ngrok>.example
//     The public origin of the bridge — interceptor fetches /jwks.json
//     from this and the Gateway authorizer fetches the OIDC discovery
//     doc. Must be reachable from the Lambda's network (default: public
//     internet) and from the AgentCore control plane.
//
//   --context gatewayJwtIssuer=https://<same-as-above>
//     Expected `iss` claim. Defaults to bridgePublicUrl when omitted.
//     Set explicitly only if your bridge has BRIDGE_PUBLIC_URL pointing
//     somewhere different from the public origin (rare).
//
// Skipped silently when bridgePublicUrl is not set, so existing
// `npm run deploy` workflows that only target DataStack still work.
const bridgePublicUrl = app.node.tryGetContext('bridgePublicUrl') as string | undefined;
if (bridgePublicUrl) {
  const gatewayJwtIssuer =
    (app.node.tryGetContext('gatewayJwtIssuer') as string | undefined) ?? bridgePublicUrl;

  new GatewayStack(app, `AgentCore-coreAgent-gateway-${region}`, {
    env: { account, region },
    description:
      'Gateway interceptor Lambda + IAM roles. Sibling to data-stack; ' +
      'the Gateway resource itself is created by provision_gateway.py.',
    tags: {
      'agentcore:project-name': 'coreAgent',
      'agentcore:stack-type': 'gateway',
    },
    bridgePublicUrl,
    gatewayJwtIssuer,
  });
}

// ServicesStack — deploys VPC + ECS cluster + ALB + two Fargate
// services (bridge + onboarding) with path-based routing.
//
// Required context:
//   --context agentRuntimeArn=arn:aws:bedrock-agentcore:...
//   --context slackSecretsArn=arn:aws:secretsmanager:...
//   --context bridgeSecretsArn=arn:aws:secretsmanager:...
//   --context sandboxSecretsArn=arn:aws:secretsmanager:... (PR sandbox)
//
// Optional context:
//   --context certificateArn=arn:aws:acm:... (HTTPS; omit for HTTP-only testing)
//   --context domainName=app.agentcore.dev  (custom domain; omit to use ALB DNS)
//   --context githubAppId=123456             (numeric GitHub App ID)
//   --context githubAppSlug=my-agent-app     (GitHub App URL slug)
//
// Skipped silently when agentRuntimeArn is not set.
const agentRuntimeArn = app.node.tryGetContext('agentRuntimeArn') as string | undefined;
const sandboxSecretsArn = app.node.tryGetContext('sandboxSecretsArn') as string | undefined;
if (agentRuntimeArn) {
  const dataStackName = `AgentCore-coreAgent-data-${region}`;

  const validatedRuntimeArn = validateAgentRuntimeArn(
    'agentRuntimeArn',
    agentRuntimeArn,
    account,
    region,
  );
  const slackSecretsArn = validateRegionalArn(
    'slackSecretsArn',
    requiredContext('slackSecretsArn'),
    'secretsmanager',
    'secret:',
    account,
    region,
  );
  const bridgeSecretsArn = validateRegionalArn(
    'bridgeSecretsArn',
    requiredContext('bridgeSecretsArn'),
    'secretsmanager',
    'secret:',
    account,
    region,
  );
  const certificateArn = app.node.tryGetContext('certificateArn') as string | undefined;
  const validatedCertificateArn = certificateArn
    ? validateRegionalArn(
        'certificateArn',
        certificateArn,
        'acm',
        'certificate/',
        account,
        region,
      )
    : undefined;
  const validatedSandboxSecretsArn = sandboxSecretsArn
    ? validateRegionalArn(
        'sandboxSecretsArn',
        sandboxSecretsArn,
        'secretsmanager',
        'secret:',
        account,
        region,
      )
    : undefined;

  new ServicesStack(app, `AgentCore-coreAgent-services-${region}`, {
    env: { account, region },
    description:
      'VPC + ECS + ALB + Fargate services for the AgentCore bridge and onboarding UI.',
    tags: {
      'agentcore:project-name': 'coreAgent',
      'agentcore:stack-type': 'services',
    },
    agentRuntimeArn: validatedRuntimeArn,
    certificateArn: validatedCertificateArn,
    domainName: app.node.tryGetContext('domainName') as string | undefined,
    slackSecretsArn,
    bridgeSecretsArn,
    sandboxSecretsArn: validatedSandboxSecretsArn,
    githubAppId: app.node.tryGetContext('githubAppId') as string | undefined,
    githubAppSlug: app.node.tryGetContext('githubAppSlug') as string | undefined,
    bridgeDataAccessPolicyArn: Fn.importValue(`${dataStackName}-BridgeDataAccessPolicyArn`),
  });
}

// SandboxStack — deploys the Fargate task definition that opens
// PRs via `propose_pr`, plus the sandbox_jobs DDB table and the
// AgentCoreSandboxAccess managed policy.
//
// Required context (all populated by `infra/data/scripts/deploy_sandbox.sh`
// from ServicesStack outputs — don't pass these by hand, run the wrapper):
//   --context sandboxSecretsArn=arn:aws:secretsmanager:...
//   --context sandboxVpcId=vpc-...
//   --context sandboxAvailabilityZones=us-west-2a,us-west-2b
//   --context sandboxPublicSubnetIds=subnet-...,subnet-...
//   --context sandboxClusterName=agentcore-services
//   --context sandboxClusterArn=arn:aws:ecs:...
//   --context sandboxDomainName=agent.example.com  (used to build the
//     SANDBOX_CALLBACK_URL the sandbox container POSTs back to)
//   --context sandboxGithubAppId=123456  (numeric ID for your GitHub App)
//
// Optional:
//   --context anthropicSecretsArn=arn:aws:secretsmanager:...  (Anthropic
//     API key for the inner Claude agent loop — without it the sandbox
//     still deploys but propose_pr fails at runtime with a clean error)
//
// Skipped silently when sandboxSecretsArn is not set, so existing
// data + services deploys still work standalone.
const sandboxVpcId = app.node.tryGetContext('sandboxVpcId') as string | undefined;
if (sandboxSecretsArn && sandboxVpcId) {
  const sandboxAvailabilityZones = (
    app.node.tryGetContext('sandboxAvailabilityZones') as string | undefined
  )?.split(',').map((s) => s.trim()).filter(Boolean);
  const sandboxPublicSubnetIds = (
    app.node.tryGetContext('sandboxPublicSubnetIds') as string | undefined
  )?.split(',').map((s) => s.trim()).filter(Boolean);
  const sandboxClusterName = app.node.tryGetContext('sandboxClusterName') as string | undefined;
  const sandboxClusterArn = app.node.tryGetContext('sandboxClusterArn') as string | undefined;
  const sandboxDomainName = app.node.tryGetContext('sandboxDomainName') as string | undefined;
  const sandboxGithubAppId = app.node.tryGetContext('sandboxGithubAppId') as
    | string
    | undefined;

  const missing: string[] = [];
  if (!sandboxAvailabilityZones?.length) missing.push('sandboxAvailabilityZones');
  if (!sandboxPublicSubnetIds?.length) missing.push('sandboxPublicSubnetIds');
  if (!sandboxClusterName) missing.push('sandboxClusterName');
  if (!sandboxClusterArn) missing.push('sandboxClusterArn');
  if (!sandboxDomainName) missing.push('sandboxDomainName');
  if (!sandboxGithubAppId) missing.push('sandboxGithubAppId');
  if (missing.length) {
    throw new Error(
      `SandboxStack: missing required context: ${missing.join(', ')}. ` +
        'Run `bash infra/data/scripts/deploy_sandbox.sh` instead of ' +
        '`npm run deploy` directly — the wrapper extracts these values ' +
        'from ServicesStack outputs and threads them in for you.',
    );
  }

  const validatedSandboxSecretsArn = validateRegionalArn(
    'sandboxSecretsArn',
    sandboxSecretsArn,
    'secretsmanager',
    'secret:',
    account,
    region,
  );
  const validatedClusterArn = validateRegionalArn(
    'sandboxClusterArn',
    sandboxClusterArn!,
    'ecs',
    'cluster/',
    account,
    region,
  );
  const anthropicSecretsArn = app.node.tryGetContext('anthropicSecretsArn') as
    | string
    | undefined;
  const validatedAnthropicSecretsArn = anthropicSecretsArn
    ? validateRegionalArn(
        'anthropicSecretsArn',
        anthropicSecretsArn,
        'secretsmanager',
        'secret:',
        account,
        region,
      )
    : undefined;

  new SandboxStack(app, `AgentCore-coreAgent-sandbox-${region}`, {
    env: { account, region },
    description:
      'PR sandbox Fargate task definition + sandbox_jobs DDB + ' +
      'AgentCoreSandboxAccess managed policy for the propose_pr tool.',
    tags: {
      'agentcore:project-name': 'coreAgent',
      'agentcore:stack-type': 'sandbox',
    },
    vpcId: sandboxVpcId,
    availabilityZones: sandboxAvailabilityZones!,
    publicSubnetIds: sandboxPublicSubnetIds!,
    clusterName: sandboxClusterName!,
    clusterArn: validatedClusterArn,
    sandboxSecretsArn: validatedSandboxSecretsArn,
    githubAppId: sandboxGithubAppId!,
    callbackUrl: `https://${sandboxDomainName}/internal/sandbox_complete`,
    anthropicSecretsArn: validatedAnthropicSecretsArn,
  });
}

app.synth();
