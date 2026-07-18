const ACCOUNT_RE = /^[0-9]{12}$/;
const REGION_RE = /^[a-z]{2}(?:-[a-z0-9]+)+-[0-9]+$/;
const GOVCLOUD_REGION_RE = /^us-gov-(?:east|west)-[0-9]+$/;
const COMMERCIAL_REGION_RE = new RegExp(
  '^(?:af-south|ap-(?:east|northeast|south|southeast)|' +
    'ca-(?:central|west)|eu-(?:central|north|south|west)|il-central|' +
    'me-(?:central|south)|mx-central|sa-east|us-(?:east|west))-[0-9]+$',
);
const AGENT_RUNTIME_RESOURCE_RE = new RegExp(
  '^(?:agent/[A-Fa-f0-9]{8}-[A-Fa-f0-9]{4}-[A-Fa-f0-9]{4}-' +
    '[A-Fa-f0-9]{4}-[A-Fa-f0-9]{12}:[1-9][0-9]{0,4}|' +
    'runtime/[A-Za-z][A-Za-z0-9_]{0,99}-[A-Za-z0-9]{10})$',
);

export const SUPPORTED_AWS_PARTITIONS = ['aws', 'aws-us-gov'] as const;
interface ParsedArn {
  readonly partition: string;
  readonly service: string;
  readonly region: string;
  readonly account: string;
  readonly resource: string;
}

export function expectedPartition(region: string): string {
  if (!REGION_RE.test(region)) {
    throw new Error(
      `AWS region must be a valid regional identifier; got ${JSON.stringify(region)}.`,
    );
  }
  if (GOVCLOUD_REGION_RE.test(region)) return 'aws-us-gov';
  if (region.startsWith('cn-')) {
    throw new Error('AWS China is not a supported deployment target for this reference stack.');
  }
  if (COMMERCIAL_REGION_RE.test(region)) return 'aws';
  throw new Error(
    `AWS region ${JSON.stringify(region)} is outside the supported commercial and GovCloud partitions.`,
  );
}

export function validateAwsTarget(account: string, region: string): void {
  if (!ACCOUNT_RE.test(account)) {
    throw new Error(
      `CDK_DEFAULT_ACCOUNT must be a 12-digit AWS account ID; got ${JSON.stringify(account)}.`,
    );
  }
  expectedPartition(region);
}

function parseArn(name: string, value: string): ParsedArn {
  const match = /^arn:([^:]+):([^:]+):([^:]*):([^:]*):(.+)$/.exec(value);
  if (!match) {
    throw new Error(`${name} must be a complete AWS ARN.`);
  }
  return {
    partition: match[1],
    service: match[2],
    region: match[3],
    account: match[4],
    resource: match[5],
  };
}

export function validateRegionalArn(
  name: string,
  value: string,
  service: string,
  resourcePrefix: string,
  account: string,
  region: string,
): string {
  validateAwsTarget(account, region);
  const parsed = parseArn(name, value);
  const partition = expectedPartition(region);
  if (
    parsed.partition !== partition ||
    parsed.service !== service ||
    parsed.region !== region ||
    parsed.account !== account ||
    !parsed.resource.startsWith(resourcePrefix) ||
    parsed.resource === resourcePrefix
  ) {
    throw new Error(
      `${name} must be a ${service} ${resourcePrefix} ARN in ${partition} ` +
        `account ${account}, region ${region}.`,
    );
  }
  return value;
}

export function validateAgentRuntimeArn(
  name: string,
  value: string,
  account: string,
  region: string,
): string {
  validateAwsTarget(account, region);
  const parsed = parseArn(name, value);
  const partition = expectedPartition(region);
  if (
    parsed.partition !== partition ||
    parsed.service !== 'bedrock-agentcore' ||
    parsed.region !== region ||
    parsed.account !== account ||
    !AGENT_RUNTIME_RESOURCE_RE.test(parsed.resource)
  ) {
    throw new Error(
      `${name} must be an AgentCore Runtime ARN in ${partition} account ${account}, ` +
        `region ${region}, using agent/<uuid>:<version> or runtime/<id>.`,
    );
  }
  return value;
}
