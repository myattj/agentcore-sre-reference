import * as assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  expectedPartition,
  validateAgentRuntimeArn,
  validateAwsTarget,
  validateRegionalArn,
} from '../lib/aws-target';

const ACCOUNT = '000000000000';
const VALID_VERSIONED_RUNTIME =
  'arn:aws:bedrock-agentcore:eu-west-1:000000000000:agent/00000000-0000-0000-0000-000000000001:1';
const VALID_LEGACY_RUNTIME =
  'arn:aws:bedrock-agentcore:eu-west-1:000000000000:runtime/coreAgent-ABCDEFGHIJ';

test('commercial and GovCloud targets select their matching partition', () => {
  assert.equal(expectedPartition('eu-west-1'), 'aws');
  assert.equal(expectedPartition('eu-west-2'), 'aws');
  assert.equal(expectedPartition('us-gov-west-1'), 'aws-us-gov');
  assert.doesNotThrow(() => validateAwsTarget(ACCOUNT, 'eu-west-1'));
  assert.doesNotThrow(() => validateAwsTarget(ACCOUNT, 'us-gov-west-1'));
});

test('unsupported or malformed targets fail closed', () => {
  assert.throws(() => validateAwsTarget('123', 'eu-west-1'), /12-digit/);
  assert.throws(() => validateAwsTarget(ACCOUNT, 'not-a-region'), /valid regional/);
  assert.throws(() => validateAwsTarget(ACCOUNT, 'cn-north-1'), /China/);
  assert.throws(() => validateAwsTarget(ACCOUNT, 'us-iso-east-1'), /outside the supported/);
});

test('current and legacy AgentCore Runtime ARNs are returned unchanged', () => {
  for (const runtimeArn of [VALID_VERSIONED_RUNTIME, VALID_LEGACY_RUNTIME]) {
    assert.equal(
      validateAgentRuntimeArn('agentRuntimeArn', runtimeArn, ACCOUNT, 'eu-west-1'),
      runtimeArn,
    );
  }
});

test('valid regional ARNs are returned unchanged', () => {
  const govArn =
    'arn:aws-us-gov:secretsmanager:us-gov-west-1:000000000000:secret:bridge-abc123';
  assert.equal(
    validateRegionalArn(
      'bridgeSecretsArn',
      govArn,
      'secretsmanager',
      'secret:',
      ACCOUNT,
      'us-gov-west-1',
    ),
    govArn,
  );
});

test('malformed and empty-resource ARNs are rejected', () => {
  assert.throws(
    () =>
      validateAgentRuntimeArn('agentRuntimeArn', 'not-an-arn', ACCOUNT, 'eu-west-1'),
    /complete AWS ARN/,
  );
  for (const resource of [
    'runtime/',
    'runtime/example',
    'agent/not-a-uuid:1',
    'agent/00000000-0000-0000-0000-000000000001:0',
  ]) {
    assert.throws(
      () =>
        validateAgentRuntimeArn(
          'agentRuntimeArn',
          `arn:aws:bedrock-agentcore:eu-west-1:000000000000:${resource}`,
          ACCOUNT,
          'eu-west-1',
        ),
      /must be an AgentCore Runtime ARN/,
    );
  }
});

for (const [label, arn] of [
  [
    'partition',
    'arn:aws-us-gov:bedrock-agentcore:eu-west-1:000000000000:agent/00000000-0000-0000-0000-000000000001:1',
  ],
  [
    'account',
    'arn:aws:bedrock-agentcore:eu-west-1:111111111111:agent/00000000-0000-0000-0000-000000000001:1',
  ],
  [
    'region',
    'arn:aws:bedrock-agentcore:us-east-1:000000000000:agent/00000000-0000-0000-0000-000000000001:1',
  ],
  [
    'service',
    'arn:aws:lambda:eu-west-1:000000000000:agent/00000000-0000-0000-0000-000000000001:1',
  ],
  [
    'resource shape',
    'arn:aws:bedrock-agentcore:eu-west-1:000000000000:gateway/example',
  ],
] as const) {
  test(`a mismatched ${label} is rejected`, () => {
    assert.throws(
      () =>
        validateAgentRuntimeArn(
          'agentRuntimeArn',
          arn,
          ACCOUNT,
          'eu-west-1',
        ),
      /must be an AgentCore Runtime ARN/,
    );
  });
}
