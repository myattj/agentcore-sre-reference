# Security policy

Agent is an archived reference implementation. It is not a maintained security product, has not received an independent security audit, and carries no response-time or remediation SLA.

## Supported versions

| Version | Security support |
|---|---|
| Current <code>main</code> branch | Best effort only |
| Tags, forks, historical commits, deployed copies | Not supported |

Assume that dependencies, cloud APIs, model behavior, and platform defaults may have changed since the archive snapshot.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting flow:

1. Open the repository's **Security** tab.
2. Choose **Report a vulnerability**.
3. Include the affected component, impact, reproduction, and a suggested mitigation if you have one.

Please do not include real credentials, customer data, or exploit payloads that target systems you do not own. If private reporting is unavailable, open a minimal public issue asking for a private contact channel; do not publish the vulnerability details.

Because this project is archived, a report may not receive a timely response. If you operate a deployment, you remain responsible for containment, patching, credential rotation, and incident response.

## Known high-risk boundaries

These are design warnings, not a complete threat model:

- **Tenant isolation:** multiple tenants share application services and tables. Tenant scoping is enforced in application, IAM, Gateway, memory, Slack, and GitHub paths; a missed check can become cross-tenant access.
- **Agent tools:** the model can call tools that read communication history, operational systems, and source code. Tool whitelists are necessary but not sufficient authorization.
- **PR sandbox:** disabled by default. The reference Fargate worker executes model-generated shell commands in the same task that receives the model key, callback secret, and GitHub installation credential. Containerization does not protect those in-process credentials from the model-driven shell. Use only disposable credentials and accounts; a production design needs a trusted credential broker, short-lived repository-scoped tokens, stronger isolation, restricted egress, output scanning, spend caps, and human approval.
- **Dashboard links:** ephemeral dashboards are unauthenticated bearer URLs. Anyone with the URL can read the rendered data until the application-enforced expiry.
- **Slack:** signing-secret verification, replay protection, OAuth state, token storage, scopes, bot loops, and the three-second acknowledgement deadline are security-relevant.
- **GitHub App binding:** a numeric installation ID is an authorization binding between one GitHub installation and one tenant. Only a privileged operator may set <code>codebases.github_installation_id</code>; the tenant session API intentionally cannot edit it.
- **Gateway integrations:** a shared Gateway or credential provider can enlarge blast radius if target and tenant claims are not checked on every call. Versioned target names carry a lossless encoded owner, and the request interceptor decodes and compares that complete owner for every <code>tools/call</code>. The shared Gateway's <code>tools/list</code> response can still expose other tenants' target names and tool schemas because a request interceptor cannot filter response bodies. Use per-tenant Gateways when even connector metadata is sensitive.
- **Memory:** tenant and channel isolation depends on correct namespace construction and authorization around the backing service.
- **Onboarding identity:** the reference session-token flow is not a finished multi-user RBAC system. The bridge sets a host-scoped HttpOnly cookie and redirects to a clean URL, so production deployments must route the bridge callback and onboarding UI through one HTTPS public origin. Never move the session bearer into a URL.
- **Operator identity:** the <code>/ops</code> surface is protected by a deployment-wide shared secret and a derived, signed browser session, not SSO or role-based access control. Keep it private or replace it with your identity provider before production use.
- **CI/CD:** deployment is manual-only, but a dispatch with configured AWS variables can still create or mutate billable cloud resources.
- **Retained data:** some DynamoDB resources use retention policies, and TTL deletion is asynchronous.
- **Model output:** prompts and retrieved data are untrusted. Defend against prompt injection, data exfiltration, unsafe commands, and misleading conclusions.

## Secrets and accidental disclosure

Never commit Slack tokens, signing secrets, OAuth credentials, GitHub App private keys, third-party API keys, AWS keys, session secrets, <code>.env</code> files, deployed-state files, or customer data.

If a secret is exposed:

1. Revoke or rotate it at the provider immediately.
2. Stop affected automation and investigate use of the old credential.
3. Remove it from the current tree and, when necessary, rewrite repository history.
4. Invalidate caches, sessions, installation tokens, and derived credentials.
5. Record the incident without copying the secret into issues, logs, or pull requests.

Deleting a secret from the latest commit does not make it safe again.

## Deployment checklist

Before exposing a fork to real users:

- Run a threat model for tenant boundaries, tool authorization, data flow, and failure behavior.
- Replace all example values and generate independent high-entropy secrets.
- Store secrets in a managed secret store and restrict access per workload.
- Use least-privilege IAM and GitHub App permissions.
- Disable local/debug routes and verbose secret-bearing logs.
- Verify Slack signatures and OAuth redirect allowlists.
- Route Slack OAuth callbacks and onboarding through one HTTPS public origin; keep session bearers out of URLs, logs, and browser history, and preserve the browser-bound state cookie on the callback route.
- Approve and bind every GitHub App installation ID to exactly one tenant through a privileged operator path.
- Require TLS for every public and service-to-service endpoint.
- Add authenticated access or strict data classification for dashboards.
- Keep the bridge's dashboard rate/concurrency limits enabled and add a distributed edge throttle such as AWS WAF for internet-facing production deployments.
- Add human approval, stronger isolation, and output scanning around code-writing tools.
- Configure budgets, alarms, log retention, backups, and a deletion procedure.
- Review dependency and container vulnerabilities.
- Test cross-tenant negative paths, not only happy paths.
- Review the GitHub Actions workflow before adding AWS credentials.

## Scope

This policy covers the source in this repository. AWS, Slack, GitHub, Anthropic, Datadog, and other connected services have their own security programs and reporting channels.
