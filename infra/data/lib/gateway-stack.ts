/**
 * Gateway interceptor stack for the AgentCore multi-tenant agent.
 *
 * Deploys the per-request interceptor Lambda + the IAM roles needed for
 * the shared AgentCore Gateway to invoke it. The Gateway resource itself
 * is NOT created by CDK because aws-cdk-lib does not yet have an L1/L2
 * construct for `bedrock-agentcore-control::Gateway` (CFN doesn't expose
 * it either as of 2026-04). The Gateway is created by
 * `infra/data/scripts/provision_gateway.py` after this stack deploys —
 * that script reads the CFN outputs (Lambda ARN + Gateway role ARN) and
 * calls boto3 `bedrock-agentcore-control:CreateGateway`.
 *
 * **Why this stack lives in `infra/data/`**: same hand-authored CDK app
 * as the data layer, sharing the cdk.json + package.json + node_modules.
 * It is conceptually a separate stack (no resources cross-referenced),
 * just sharing the synth pipeline. CLAUDE.md gotcha #15: do not move
 * either stack into `coreAgent/agentcore/cdk/` — that directory is
 * CLI-regenerated.
 *
 * **Pre-deploy step**: run `infra/data/scripts/build_interceptor_zip.sh`
 * to bundle the Python interceptor + its deps into a Lambda zip. CDK
 * references that zip via `Code.fromAsset()`. If the zip doesn't exist,
 * `cdk synth` fails with a clear error.
 */
import * as path from 'node:path';
import * as fs from 'node:fs';

import {
  CfnOutput,
  Duration,
  Stack,
  type StackProps,
} from 'aws-cdk-lib';
import {
  Effect,
  ManagedPolicy,
  PolicyDocument,
  PolicyStatement,
  Role,
  ServicePrincipal,
} from 'aws-cdk-lib/aws-iam';
import {
  Architecture,
  Code,
  Function as LambdaFunction,
  LoggingFormat,
  Runtime,
} from 'aws-cdk-lib/aws-lambda';
import { LogGroup, RetentionDays } from 'aws-cdk-lib/aws-logs';
import { Construct } from 'constructs';

export interface GatewayStackProps extends StackProps {
  /**
   * Public URL of the bridge service. Used to set the
   * `BRIDGE_JWKS_URL` env var on the interceptor Lambda so it can fetch
   * the JWKS for token verification.
   *
   * In LOCAL_DEV testing this is the ngrok URL; in production it's the
   * bridge's load balancer or CloudFront origin. The Lambda re-fetches
   * JWKS on cold start + on `kid` cache miss, so this URL only needs
   * to be reachable from the Lambda VPC (or, by default, the public
   * internet).
   */
  readonly bridgePublicUrl: string;

  /**
   * Expected `iss` (issuer) claim on JWTs the interceptor verifies.
   * Should match `BRIDGE_PUBLIC_URL` on the bridge side — that's what
   * `bridge/bridge/gateway_jwt.py` puts in the `iss` claim of every
   * minted token.
   */
  readonly gatewayJwtIssuer: string;
}

export class GatewayStack extends Stack {
  public readonly interceptorFunction: LambdaFunction;
  public readonly gatewayRole: Role;

  constructor(scope: Construct, id: string, props: GatewayStackProps) {
    super(scope, id, props);

    // ------------------------------------------------------------------
    // Pre-deploy zip check
    // ------------------------------------------------------------------
    // The interceptor Lambda is bundled by `build_interceptor_zip.sh`
    // BEFORE `cdk synth`. If the zip is missing, fail loud here rather
    // than letting CDK upload an empty asset.
    //
    // __dirname at runtime is `infra/data/dist/lib/` (the compiled JS),
    // so we walk up two levels to reach the infra/data project root.
    const zipPath = path.resolve(__dirname, '..', '..', 'build', 'gateway_interceptor.zip');
    if (!fs.existsSync(zipPath)) {
      throw new Error(
        `Gateway interceptor zip not found at ${zipPath}. ` +
          'Run `bash scripts/build_interceptor_zip.sh` from infra/data/ first.',
      );
    }

    // ------------------------------------------------------------------
    // Interceptor Lambda
    // ------------------------------------------------------------------
    // Execution role: basic Lambda + outbound HTTPS for fetching JWKS
    // from the bridge. We don't put the Lambda in a VPC — JWKS is a
    // public endpoint and putting Lambda in a VPC adds cold-start cost
    // and requires NAT for internet egress. Reconsider if the bridge
    // moves behind a private endpoint.
    const lambdaRole = new Role(this, 'InterceptorLambdaRole', {
      assumedBy: new ServicePrincipal('lambda.amazonaws.com'),
      description:
        'Execution role for the AgentCore Gateway interceptor Lambda. ' +
        'Basic Lambda perms + CloudWatch Logs. No DDB, Secrets, or S3 access - ' +
        'the interceptor only needs to verify JWTs against a public JWKS URL.',
    });
    lambdaRole.addManagedPolicy(
      // Equivalent to AWSLambdaBasicExecutionRole — CloudWatch Logs only.
      ManagedPolicy.fromAwsManagedPolicyName('service-role/AWSLambdaBasicExecutionRole'),
    );

    // CloudWatch log group with explicit retention. Default is "never expire"
    // which silently grows costs.
    const logGroup = new LogGroup(this, 'InterceptorLogGroup', {
      logGroupName: '/aws/lambda/agentcore-gateway-interceptor',
      retention: RetentionDays.ONE_MONTH,
    });

    this.interceptorFunction = new LambdaFunction(this, 'InterceptorFunction', {
      functionName: 'agentcore-gateway-interceptor',
      description:
        'Per-request interceptor for the shared AgentCore Gateway. ' +
        'Verifies bridge-minted JWTs and enforces per-target tenant isolation. ' +
        'See workers/gateway_interceptor/ for source.',
      runtime: Runtime.PYTHON_3_13,
      // Linux wheels in the bundled zip are amd64. If we move to arm64
      // here, also pass --python-platform aarch64-manylinux2014 in the
      // build script.
      architecture: Architecture.X86_64,
      handler: 'gateway_interceptor.handler.lambda_handler',
      code: Code.fromAsset(zipPath),
      role: lambdaRole,
      timeout: Duration.seconds(10),
      memorySize: 256,
      logGroup,
      loggingFormat: LoggingFormat.JSON,
      environment: {
        BRIDGE_JWKS_URL: `${stripTrailingSlash(props.bridgePublicUrl)}/jwks.json`,
        GATEWAY_JWT_AUDIENCE: 'agentcore-gateway',
        GATEWAY_JWT_ISSUER: stripTrailingSlash(props.gatewayJwtIssuer),
        // Default delimiter used by AgentCore Gateway to namespace tools
        // per target.
        // Override here without redeploying the Lambda code.
        INTERCEPTOR_TARGET_DELIMITER: '___',
      },
    });

    // ------------------------------------------------------------------
    // Gateway-side IAM role
    // ------------------------------------------------------------------
    // The shared AgentCore Gateway assumes this role to invoke the
    // interceptor Lambda on every request. Scoped narrowly: only
    // lambda:InvokeFunction on this exact function ARN. CLAUDE.md /
    // AWS docs both warn against wildcard lambda:InvokeFunction on
    // gateway execution roles.
    //
    // We don't ALSO grant credentialProvider read perms here — those go
    // on a SEPARATE role attached when each target is created,
    // because credential providers are per-tenant and we want a tight
    // blast radius if one is misconfigured.
    this.gatewayRole = new Role(this, 'AgentCoreGatewayRole', {
      roleName: 'AgentCoreGatewayRole',
      assumedBy: new ServicePrincipal('bedrock-agentcore.amazonaws.com'),
      description:
        'Assumed by the shared AgentCore Gateway. Grants lambda:InvokeFunction ' +
        'on the interceptor only. Per-target credential provider access is ' +
        'attached separately for each target.',
      inlinePolicies: {
        InvokeInterceptor: new PolicyDocument({
          statements: [
            new PolicyStatement({
              effect: Effect.ALLOW,
              actions: ['lambda:InvokeFunction'],
              resources: [this.interceptorFunction.functionArn],
            }),
          ],
        }),
        // The Gateway needs these permissions to authenticate with API-key
        // credential providers and read only their AgentCore Identity-managed
        // backing secrets. Never widen the Secrets Manager resource to
        // `secret:*`: that would expose tenant Slack tokens and platform keys.
        AgentCoreAndSecretsAccess: new PolicyDocument({
          statements: [
            new PolicyStatement({
              effect: Effect.ALLOW,
              actions: ['bedrock-agentcore:GetWorkloadAccessToken'],
              resources: [
                `arn:aws:bedrock-agentcore:${this.region}:${this.account}:` +
                  'workload-identity-directory/default',
                `arn:aws:bedrock-agentcore:${this.region}:${this.account}:` +
                  'workload-identity-directory/default/workload-identity/*',
              ],
            }),
            new PolicyStatement({
              effect: Effect.ALLOW,
              actions: ['bedrock-agentcore:GetResourceApiKey'],
              // AgentCore's current documentation uses `api-key`, while some
              // control-plane responses use `apikeycredentialprovider`. Scope
              // both shapes to this account's default token vault rather than
              // falling back to an account-wide `*` resource.
              resources: [
                `arn:aws:bedrock-agentcore:${this.region}:${this.account}:` +
                  'workload-identity-directory/default',
                `arn:aws:bedrock-agentcore:${this.region}:${this.account}:` +
                  'workload-identity-directory/default/workload-identity/*',
                `arn:aws:bedrock-agentcore:${this.region}:${this.account}:` +
                  'token-vault/default',
                `arn:aws:bedrock-agentcore:${this.region}:${this.account}:` +
                  'token-vault/default/api-key/*',
                `arn:aws:bedrock-agentcore:${this.region}:${this.account}:` +
                  'token-vault/default/apikeycredentialprovider/*',
              ],
            }),
            new PolicyStatement({
              effect: Effect.ALLOW,
              actions: ['secretsmanager:GetSecretValue'],
              // Prefix documented by AgentCore Identity for API-key providers.
              // The suffix wildcard accounts for Secrets Manager's generated
              // six-character ARN suffix.
              resources: [
                `arn:aws:secretsmanager:${this.region}:${this.account}:` +
                  'secret:bedrock-agentcore-identity!default/apikey/*-*',
              ],
            }),
          ],
        }),
      },
    });

    // The Gateway needs explicit permission to invoke the Lambda from
    // the gateway service principal. addPermission() creates a Lambda
    // resource policy entry — separate from the IAM role above.
    this.interceptorFunction.addPermission('AllowAgentCoreGatewayInvoke', {
      principal: new ServicePrincipal('bedrock-agentcore.amazonaws.com'),
      action: 'lambda:InvokeFunction',
    });

    // ------------------------------------------------------------------
    // Outputs (consumed by provision_gateway.py)
    // ------------------------------------------------------------------
    new CfnOutput(this, 'InterceptorLambdaArn', {
      value: this.interceptorFunction.functionArn,
      exportName: 'AgentCoreGatewayInterceptorLambdaArn',
      description: 'ARN of the request interceptor Lambda for CreateGateway.',
    });
    new CfnOutput(this, 'GatewayRoleArn', {
      value: this.gatewayRole.roleArn,
      exportName: 'AgentCoreGatewayRoleArn',
      description: 'ARN of the IAM role the Gateway assumes for tool invocation.',
    });
    new CfnOutput(this, 'BridgeJwksUrl', {
      value: `${stripTrailingSlash(props.bridgePublicUrl)}/jwks.json`,
      description:
        'JWKS URL the interceptor fetches from. Verify this is reachable ' +
        'from outside the AWS account before running provision_gateway.py.',
    });
    new CfnOutput(this, 'BridgeOidcDiscoveryUrl', {
      value: `${stripTrailingSlash(props.bridgePublicUrl)}/.well-known/openid-configuration`,
      description:
        'OIDC discovery URL passed to CreateGateway as the CUSTOM_JWT ' +
        'authorizer discoveryUrl. The Gateway calls this once at create time.',
    });
  }
}

function stripTrailingSlash(url: string): string {
  return url.endsWith('/') ? url.slice(0, -1) : url;
}
