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
import { App } from 'aws-cdk-lib';
import { DataStack } from '../lib/data-stack';

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

app.synth();
