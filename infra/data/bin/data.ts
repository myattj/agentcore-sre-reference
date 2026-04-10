#!/usr/bin/env node
/**
 * CDK app entrypoint for the AgentCore data layer.
 *
 * Deploys one DataStack to us-west-2 using the account from the user's
 * default AWS profile (CDK_DEFAULT_ACCOUNT). The stack name mirrors the
 * naming convention of the CLI-managed agent stack:
 *   AgentCore-coreAgent-data-<region>
 *
 * Override region via CDK_DEFAULT_REGION or by passing --context region=... .
 */
import { App, Fn } from 'aws-cdk-lib';
import { DataStack } from '../lib/data-stack';
import { GatewayStack } from '../lib/gateway-stack';
import { ServicesStack } from '../lib/services-stack';

const app = new App();

// The project memory / BUILD_PLAN / CLAUDE.md all lock the region to
// us-west-2 (widest AgentCore + Bedrock Memory availability). We do NOT
// respect CDK_DEFAULT_REGION from the shell because developer shells often
// have it set to a different region for unrelated work — we'd silently
// deploy to the wrong region.
//
// Override via: npx cdk deploy --context region=eu-west-1
// (only do this if you know what you're doing; AgentCore Runtime may not
// be available in that region yet.)
const account = process.env.CDK_DEFAULT_ACCOUNT;
const region = (app.node.tryGetContext('region') as string | undefined) ?? 'us-west-2';

if (!account) {
  throw new Error(
    'CDK_DEFAULT_ACCOUNT is not set. Run `aws sts get-caller-identity` to confirm ' +
      'your AWS credentials are active, then retry (CDK picks this up automatically ' +
      'from your default profile).',
  );
}

new DataStack(app, `AgentCore-coreAgent-data-${region}`, {
  env: { account, region },
  description: 'DynamoDB tables + IAM managed policy for the AgentCore multi-tenant agent',
  tags: {
    'agentcore:project-name': 'coreAgent',
    'agentcore:stack-type': 'data',
  },
});

// GatewayStack — week 4 chunk C. Deploys the request interceptor Lambda
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

// ServicesStack — week 7. Deploys VPC + ECS cluster + ALB + two Fargate
// services (bridge + onboarding) with path-based routing.
//
// Required context:
//   --context agentRuntimeArn=arn:aws:bedrock-agentcore:...
//   --context slackSecretsArn=arn:aws:secretsmanager:...
//   --context bridgeSecretsArn=arn:aws:secretsmanager:...
//
// Optional context:
//   --context certificateArn=arn:aws:acm:... (HTTPS; omit for HTTP-only testing)
//   --context domainName=app.agentcore.dev  (custom domain; omit to use ALB DNS)
//
// Skipped silently when agentRuntimeArn is not set.
const agentRuntimeArn = app.node.tryGetContext('agentRuntimeArn') as string | undefined;
if (agentRuntimeArn) {
  const dataStackName = `AgentCore-coreAgent-data-${region}`;

  new ServicesStack(app, `AgentCore-coreAgent-services-${region}`, {
    env: { account, region },
    description:
      'VPC + ECS + ALB + Fargate services for the AgentCore bridge and onboarding UI.',
    tags: {
      'agentcore:project-name': 'coreAgent',
      'agentcore:stack-type': 'services',
    },
    agentRuntimeArn,
    certificateArn: app.node.tryGetContext('certificateArn') as string | undefined,
    domainName: app.node.tryGetContext('domainName') as string | undefined,
    slackSecretsArn: app.node.tryGetContext('slackSecretsArn') as string,
    bridgeSecretsArn: app.node.tryGetContext('bridgeSecretsArn') as string,
    bridgeDataAccessPolicyArn: Fn.importValue(`${dataStackName}-BridgeDataAccessPolicyArn`),
    onboardingDataAccessPolicyArn: Fn.importValue(`${dataStackName}-OnboardingDataAccessPolicyArn`),
  });
}

app.synth();
