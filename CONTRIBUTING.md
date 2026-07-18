# Contributing

Thank you for helping make Agent a better reference implementation.

Agent is archived: there is no active product roadmap, support SLA, or guarantee that a pull request will be reviewed or released. Contributions are still welcome when they make the repository safer, clearer, easier to run, or more useful as an example.

## Good contributions

- Fix a reproducible bug or broken test.
- Remove a secret, unsafe default, stale deployment artifact, or misleading claim.
- Improve local setup, examples, architecture explanations, or failure messages.
- Add focused tests around tenant boundaries, authentication, async behavior, or tool auditing.
- Update a dependency with a clear compatibility reason and verification notes.
- Generalize a reusable pattern without turning the archive into a new product roadmap.

Large feature proposals should start as an issue. Explain the use case, security boundary, operational cost, and why it belongs in an archived reference rather than a fork.

## Development setup

Use Python 3.13, Node.js 22, uv, and npm. From the repository root:

~~~bash
make doctor  # required vs optional tools, with fixes
make setup   # locked dependencies + private matching local env files
make demo    # bridge + web UI + synthetic dashboard, no cloud required
make check   # full local validation suite
~~~

The root [README](./README.md#try-it-locally--no-cloud-account-required)
explains what each command does. The main setup, doctor, demo, and check
entrypoints also support <code>--help</code>.
Make is a convenience wrapper; <code>./scripts/setup.sh</code> and its sibling
entrypoints work directly when Make is unavailable.

Each service owns its environment:

- <code>bridge/</code> is a FastAPI package.
- <code>coreAgent/app/coreAgent/</code> is the agent package.
- <code>workers/gateway_interceptor/</code> is an independent Lambda package.
- <code>infra/sandbox/</code> is an independent Python worker package.
- <code>onboarding/</code> is a Next.js package.
- <code>infra/data/</code> is a TypeScript CDK package.

Do not cross-import between the bridge and agent packages merely to avoid duplicating a small data shape. They deploy independently.

## Architecture guardrails

Please preserve these invariants:

1. **The bridge is transport-only.** Agent tools and reasoning belong in the agent, not the Slack request path.
2. **Never block the AgentCore entrypoint.** Long-running work must use the async-task lifecycle so health checks can report busy state.
3. **Acknowledge Slack quickly.** Agent invocation must remain outside the webhook handler.
4. **Do not hardcode a model in feature code.** Load the model from tenant configuration; the default belongs in <code>coreAgent/app/coreAgent/model/load.py</code>.
5. **Use audited tools.** New catalog tools use <code>@audited_tool("name")</code>, and audit failures must never fail the user request.
6. **Keep local flags distinct.** The agent uses <code>AGENT_LOCAL_STORES=1</code>; the bridge uses <code>LOCAL_DEV=1</code>.
7. **Treat generated AgentCore CDK as generated.** Change <code>coreAgent/agentcore/agentcore.json</code>, not <code>coreAgent/agentcore/cdk/</code>, unless the AgentCore tooling explicitly requires otherwise.
8. **Keep tenant schemas synchronized.** A new editable field generally touches the authoritative agent schema, bridge API/default writer, onboarding TypeScript types, and relevant UI.

The root <code>AGENTS.md</code> has the deeper project-specific rules.

## Tests

The preferred full-repository check is <code>make check</code>. Run focused checks
while iterating on one area:

| Area | Command from its directory |
|---|---|
| Bridge | <code>uv sync --frozen --extra dev && uv run --frozen pytest</code> |
| Agent | <code>uv sync --frozen --extra test && uv run --frozen pytest</code> |
| Gateway interceptor | <code>uv sync --frozen --extra dev && uv run --frozen pytest</code> |
| Pull-request sandbox | <code>uv sync --frozen --extra test && uv run --frozen python -m pytest</code> |
| Synthetic incident seed | <code>uv run --python 3.13 --with requests==2.32.5 python -m unittest discover -s seed/tests -v</code> |
| Onboarding | <code>npm ci && npm test && NEXT_PUBLIC_BRIDGE_INSTALL_URL=https://ci.test/slack/install npm run build</code> |
| CDK | <code>npm ci && npm test && npx cdk synth --quiet --app 'env CDK_DEFAULT_ACCOUNT=000000000000 CDK_DEFAULT_REGION=us-west-2 node dist/bin/data.js' -c region=us-west-2</code> |

Tests should be deterministic and should not contact AWS, Slack, GitHub, Datadog, or another external service unless they are explicitly marked as integration tests.

## Pull requests

A useful pull request:

- Describes the behavior before and after the change.
- Links a reproducer or explains the design constraint.
- Lists the exact verification commands run.
- Calls out AWS cost, IAM, data-retention, or migration impact.
- Adds or updates tests for behavioral changes.
- Updates public documentation when setup or behavior changes.
- Contains no credentials, customer data, private URLs, deployment state, or generated build output.

Keep changes focused. Preserve unrelated work already present in the tree.

> [!WARNING]
> Normal pushes run validation only. Deployment requires a manual workflow dispatch with an explicit production opt-in. Review the jobs and IAM scope before attaching cloud credentials to a fork.

## Security

Do not use a public issue for an undisclosed vulnerability, leaked secret, or customer data. Follow [SECURITY.md](./SECURITY.md).

By contributing, you agree that your contribution is licensed under the repository's [MIT License](./LICENSE) and that you will follow the [Code of Conduct](./CODE_OF_CONDUCT.md).
