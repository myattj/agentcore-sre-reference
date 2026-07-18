# Pull-request sandbox worker

> [!CAUTION]
> This worker is disabled by default and is not a safe production sandbox. In
> the current reference design, model-generated shell commands run in the same
> ECS task that receives an Anthropic API key, callback secret, and a
> repository-scoped GitHub installation credential. Use it only in a
> disposable account with throwaway credentials. A real deployment needs a
> trusted credential broker/control plane, short-lived least-privilege tokens,
> stronger process or VM isolation, restricted egress, and human approval.

This directory contains the experimental one-shot Fargate worker used by the
<code>propose_pr</code> tool. It clones one allowed repository, runs a bounded
Anthropic-powered edit loop, pushes an isolated branch through authenticated
Git transport, opens a pull request, reports progress to the bridge, and exits.

Treat the worker as a hostile-code boundary. Model-generated commands execute
inside its checkout. Review its IAM policy, network access, repository allowlist,
token budget, timeouts, callback authentication, and human-approval path even
for disposable experiments.

## Reproducible Python environment

Python 3.13 runtime and test dependencies are defined in
[<code>pyproject.toml</code>](./pyproject.toml) and resolved in the checked-in
<code>uv.lock</code>. The service owns this environment; do not import packages
from the bridge or core agent environments.

~~~bash
cd infra/sandbox
uv sync --frozen --extra test
uv run --frozen python -m pytest
~~~

The Dockerfile installs runtime dependencies from the same frozen lock without
the test extra. The build context excludes local virtual environments, caches,
and tests. No credential belongs in this directory or image; ECS injects runtime
secrets from Secrets Manager.

## Deployment

The hand-authored CDK application builds this directory as a
<code>DockerImageAsset</code>. Follow the sandbox section in
[<code>infra/data/README.md</code>](../data/README.md) and inspect
<code>scripts/deploy_sandbox.sh</code> before deploying. A deployment can create
billable ECR, ECS/Fargate, CloudWatch, DynamoDB, and network resources.
