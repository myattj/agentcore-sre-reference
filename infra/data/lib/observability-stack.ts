/**
 * Observability stack for the AgentCore multi-tenant agent.
 *
 * Consumes metrics the agent emits via CloudWatch Embedded Metric Format
 * (see `coreAgent/app/coreAgent/metrics.py`). Metrics are auto-published
 * by CloudWatch Logs under the `Agent/Runtime` namespace with dimensions
 * `tenant_id`, `model_id`, and `tool_name`.
 *
 * Resources:
 *   - CloudWatch Dashboard with per-tenant and platform-wide widgets
 *   - SNS topic for operator alarms (subscribe ops email out-of-band)
 *   - Two platform canary alarms:
 *       1. HighPlatformErrorRate — errors > 10% of invocations over 5m
 *       2. NoInvocationsIn1Hour  — canary for "is anything happening?"
 *
 * **Why a separate stack** (not folded into data-stack.ts):
 *   - Zero dependencies on the data stack — it just queries metric names.
 *   - Can be destroyed/recreated without touching DDB tables.
 *   - Keeps the data stack focused on stateful primitives.
 *
 * **Metric namespace** is fixed at `Agent/Runtime` to match the agent code.
 * Changing it requires an agent redeploy, so a CfnParameter would be
 * misleading; it's hardcoded here and in `metrics.py`'s `DEFAULT_NAMESPACE`.
 *
 * **Alarm email** is read from CDK context (`--context alarmEmail=...`).
 * When omitted, the topic is still created but has no subscription — the
 * operator can subscribe via the console later. We deliberately do NOT
 * pass it as a required param so a deploy with no subscriber still works.
 */
import {
  CfnMapping,
  CfnOutput,
  Duration,
  Stack,
  type StackProps,
} from 'aws-cdk-lib';
import {
  Alarm,
  ComparisonOperator,
  Dashboard,
  GraphWidget,
  LegendPosition,
  MathExpression,
  Metric,
  PeriodOverride,
  Stats,
  TextWidget,
  TreatMissingData,
} from 'aws-cdk-lib/aws-cloudwatch';
import { SnsAction } from 'aws-cdk-lib/aws-cloudwatch-actions';
import { Topic } from 'aws-cdk-lib/aws-sns';
import { EmailSubscription } from 'aws-cdk-lib/aws-sns-subscriptions';
import { Construct } from 'constructs';

/**
 * Must match `DEFAULT_NAMESPACE` in
 * `coreAgent/app/coreAgent/metrics.py`. Changing it here without changing
 * the agent will produce an empty dashboard.
 */
const METRIC_NAMESPACE = 'Agent/Runtime';

export interface ObservabilityStackProps extends StackProps {
  /**
   * Optional email address to subscribe to the operator alarms SNS topic.
   * When omitted, the topic is created without subscribers — the operator
   * can add one via the console or a follow-up deploy.
   */
  readonly alarmEmail?: string;
}

export class ObservabilityStack extends Stack {
  public readonly dashboard: Dashboard;
  public readonly alarmTopic: Topic;

  constructor(scope: Construct, id: string, props: ObservabilityStackProps) {
    super(scope, id, props);

    const consoleDomains = new CfnMapping(this, 'ConsoleDomains', {
      mapping: {
        aws: { domain: 'console.aws.amazon.com' },
        'aws-us-gov': { domain: 'console.amazonaws-us-gov.com' },
      },
    });

    // --------------------------------------------------------------------
    // SNS topic for alarms. Optional email subscription.
    // --------------------------------------------------------------------
    this.alarmTopic = new Topic(this, 'OperatorAlarmsTopic', {
      topicName: 'agentcore-operator-alarms',
      displayName: 'Agent Runtime operator alarms',
    });

    if (props.alarmEmail) {
      this.alarmTopic.addSubscription(new EmailSubscription(props.alarmEmail));
    }

    // --------------------------------------------------------------------
    // Metric builders. One helper per metric so widgets read consistently
    // and any dimension change is a one-line edit.
    // --------------------------------------------------------------------

    /** Total platform-wide invocations (no dimensions — rolls up across tenants). */
    const totalInvocations = (stat: string = Stats.SUM) =>
      new Metric({
        namespace: METRIC_NAMESPACE,
        metricName: 'Invocations',
        statistic: stat,
        period: Duration.minutes(5),
      });

    const totalErrors = (stat: string = Stats.SUM) =>
      new Metric({
        namespace: METRIC_NAMESPACE,
        metricName: 'InvocationErrors',
        statistic: stat,
        period: Duration.minutes(5),
      });

    const invocationDuration = (stat: string) =>
      new Metric({
        namespace: METRIC_NAMESPACE,
        metricName: 'InvocationDurationMs',
        statistic: stat,
        period: Duration.minutes(5),
      });

    // --------------------------------------------------------------------
    // Platform canary alarms. Both emit to the same SNS topic so operator
    // routing is a single subscription point.
    // --------------------------------------------------------------------

    // Alarm 1: error rate > 10% over a 5-minute window.
    // Uses a math expression so we alarm on a ratio, not a raw error count
    // (a 10-invocation burst with 3 errors is a real signal; a 10k-invocation
    // burst with 50 errors is not).
    //
    // FILL(m1,0) + FILL(m2,0) handle gaps where no errors were reported
    // at all (CloudWatch otherwise treats the dividend as MISSING and the
    // alarm stays in INSUFFICIENT_DATA forever).
    const errorRateExpression = new MathExpression({
      expression: '100 * (FILL(m2, 0) / IF(m1 > 0, m1, 1))',
      label: 'Platform error rate (%)',
      usingMetrics: {
        m1: totalInvocations(),
        m2: totalErrors(),
      },
      period: Duration.minutes(5),
    });

    new Alarm(this, 'HighPlatformErrorRateAlarm', {
      alarmName: 'AgentCore-HighPlatformErrorRate',
      alarmDescription:
        'Fires when platform-wide agent error rate exceeds 10% over a ' +
        '5-minute window. Investigate via the CloudWatch Logs Insights ' +
        'query pinned on the ObservabilityDashboard.',
      metric: errorRateExpression,
      evaluationPeriods: 2,
      threshold: 10,
      comparisonOperator: ComparisonOperator.GREATER_THAN_THRESHOLD,
      // NOT_BREACHING: if the math expression returns MISSING (e.g. no
      // invocations at all), treat it as healthy. The separate "no
      // invocations" alarm below catches the dead-silence case.
      treatMissingData: TreatMissingData.NOT_BREACHING,
    }).addAlarmAction(new SnsAction(this.alarmTopic));

    // Alarm 2: no invocations for an hour. Canary for "did something break
    // upstream of the agent entirely?" (bridge down, Slack OAuth broken,
    // DNS misconfigured, etc.).
    //
    // Uses BREACHING on missing data so a completely silent period trips
    // the alarm — that's the exact condition we're looking for.
    // Alarm 2 uses a 1-hour period metric — the alarm window is set on the
    // metric itself via `.with({ period })`, not as a top-level Alarm prop.
    const hourlyInvocationsMetric = new Metric({
      namespace: METRIC_NAMESPACE,
      metricName: 'Invocations',
      statistic: Stats.SUM,
      period: Duration.hours(1),
    });

    new Alarm(this, 'NoInvocationsCanaryAlarm', {
      alarmName: 'AgentCore-NoInvocationsIn1Hour',
      alarmDescription:
        'Fires when the platform has received zero invocations for a full ' +
        'hour. Sanity check that the bridge → agent path is alive — not ' +
        'per-tenant. Noisy in pre-launch; disable if fewer than ~10 ' +
        'invocations/hour is the norm.',
      metric: hourlyInvocationsMetric,
      evaluationPeriods: 1,
      threshold: 1,
      comparisonOperator: ComparisonOperator.LESS_THAN_THRESHOLD,
      treatMissingData: TreatMissingData.BREACHING,
    }).addAlarmAction(new SnsAction(this.alarmTopic));

    // --------------------------------------------------------------------
    // CloudWatch Dashboard
    // --------------------------------------------------------------------
    // Layout (top to bottom):
    //   Row 1: headline — total invocations + error rate (full-width graphs)
    //   Row 2: cost attribution — stacked cost by tenant
    //   Row 3: token consumption by tenant/model
    //   Row 4: latency — p50/p95/p99
    //   Row 5: tool-call volume + errors (by tool_name)
    //   Row 6: alarms summary text + link to Logs Insights query
    //
    // Per-tenant breakouts use the SEARCH expression so tenants are picked
    // up automatically as they onboard — no hand-edit required when the
    // roster changes.

    const searchExpression = (
      metricName: string,
      stat: string,
      dims: string[],
    ) =>
      new MathExpression({
        expression:
          `SEARCH('{${METRIC_NAMESPACE},${dims.join(',')}} MetricName="${metricName}"', '${stat}', 300)`,
        label: '',
        usingMetrics: {},
        period: Duration.minutes(5),
      });

    this.dashboard = new Dashboard(this, 'ObservabilityDashboard', {
      dashboardName: 'AgentCore-Observability',
      periodOverride: PeriodOverride.AUTO,
    });

    // Row 1: headline
    this.dashboard.addWidgets(
      new GraphWidget({
        title: 'Invocations (platform total)',
        width: 12,
        height: 6,
        left: [totalInvocations(Stats.SUM).with({ label: 'Invocations' })],
        leftYAxis: { min: 0 },
        legendPosition: LegendPosition.HIDDEN,
      }),
      new GraphWidget({
        title: 'Error rate (%) — platform',
        width: 12,
        height: 6,
        left: [
          new MathExpression({
            expression: '100 * (FILL(m2, 0) / IF(m1 > 0, m1, 1))',
            label: 'Error %',
            usingMetrics: {
              m1: totalInvocations(),
              m2: totalErrors(),
            },
            period: Duration.minutes(5),
          }),
        ],
        leftYAxis: { min: 0, max: 100 },
      }),
    );

    // Row 2: cost attribution by tenant
    this.dashboard.addWidgets(
      new GraphWidget({
        title: 'Estimated cost (cents) — by tenant',
        width: 12,
        height: 6,
        stacked: true,
        left: [
          searchExpression('EstimatedCostCents', 'Sum', [
            'tenant_id',
            'model_id',
          ]),
        ],
        leftYAxis: { min: 0, label: 'Cents' },
      }),
      new GraphWidget({
        title: 'Invocations — by tenant',
        width: 12,
        height: 6,
        stacked: true,
        left: [searchExpression('Invocations', 'Sum', ['tenant_id'])],
        leftYAxis: { min: 0 },
      }),
    );

    // Row 3: tokens by tenant/model
    this.dashboard.addWidgets(
      new GraphWidget({
        title: 'Input tokens — by tenant/model',
        width: 12,
        height: 6,
        stacked: true,
        left: [
          searchExpression('InputTokens', 'Sum', ['tenant_id', 'model_id']),
        ],
        leftYAxis: { min: 0 },
      }),
      new GraphWidget({
        title: 'Output tokens — by tenant/model',
        width: 12,
        height: 6,
        stacked: true,
        left: [
          searchExpression('OutputTokens', 'Sum', ['tenant_id', 'model_id']),
        ],
        leftYAxis: { min: 0 },
      }),
    );

    // Row 4: latency percentiles
    this.dashboard.addWidgets(
      new GraphWidget({
        title: 'Invocation duration — platform percentiles (ms)',
        width: 24,
        height: 6,
        left: [
          invocationDuration('p50').with({ label: 'p50' }),
          invocationDuration('p95').with({ label: 'p95' }),
          invocationDuration('p99').with({ label: 'p99' }),
        ],
        leftYAxis: { min: 0, label: 'ms' },
      }),
    );

    // Row 5: tool-call volume + errors
    this.dashboard.addWidgets(
      new GraphWidget({
        title: 'Tool calls — by tool_name',
        width: 12,
        height: 6,
        stacked: true,
        left: [
          searchExpression('ToolCalls', 'Sum', ['tenant_id', 'tool_name']),
        ],
        leftYAxis: { min: 0 },
      }),
      new GraphWidget({
        title: 'Tool call errors — by tool_name',
        width: 12,
        height: 6,
        stacked: true,
        left: [
          searchExpression('ToolCallErrors', 'Sum', [
            'tenant_id',
            'tool_name',
          ]),
        ],
        leftYAxis: { min: 0 },
      }),
    );

    // Row 6: operator hints
    this.dashboard.addWidgets(
      new TextWidget({
        markdown: [
          '## Operator notes',
          '',
          '**Namespace:** `Agent/Runtime` — populated via EMF from the agent (see `coreAgent/app/coreAgent/metrics.py`).',
          '',
          '**Dimensions:** `tenant_id`, `model_id`, `tool_name`. Non-dimension properties (`invocation_id`, `channel_id`, `workspace_id`) are queryable in CloudWatch Logs Insights.',
          '',
          '**Drilling into a specific invocation:**',
          '```',
          'fields @timestamp, tenant_id, invocation_id, model_id, Invocations, InvocationErrors, InvocationDurationMs',
          '| filter invocation_id = "<paste-here>"',
          '| sort @timestamp desc',
          '```',
          '',
          '**Alarms:** `AgentCore-HighPlatformErrorRate`, `AgentCore-NoInvocationsIn1Hour` → SNS topic `agentcore-operator-alarms`.',
        ].join('\n'),
        width: 24,
        height: 6,
      }),
    );

    // --------------------------------------------------------------------
    // Outputs
    // --------------------------------------------------------------------
    new CfnOutput(this, 'DashboardName', {
      value: this.dashboard.dashboardName,
      description: 'CloudWatch dashboard name',
      exportName: `${this.stackName}-DashboardName`,
    });

    new CfnOutput(this, 'DashboardUrl', {
      value:
        `https://${consoleDomains.findInMap(this.partition, 'domain')}` +
        `/cloudwatch/home?region=${this.region}` +
        `#dashboards:name=${this.dashboard.dashboardName}`,
      description: 'Direct URL to the CloudWatch dashboard',
    });

    new CfnOutput(this, 'AlarmTopicArn', {
      value: this.alarmTopic.topicArn,
      description: 'SNS topic ARN for operator alarms',
      exportName: `${this.stackName}-AlarmTopicArn`,
    });
  }
}
