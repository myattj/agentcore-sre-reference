# Changelog

This changelog begins with the public archive snapshot. The public repository preserves meaningful development history after removing private planning artifacts, deployment fingerprints, and personal commit metadata.

## Unreleased

### AWS portability

- Connect named profiles, SSO, environment credentials, or workload roles through a read-only STS and AgentCore preflight that writes an ignored, mode-0600 deployment target.
- Carry one selected region through AgentCore, the bridge, CDK, sandbox helpers, and manual GitHub Actions deployments, with fail-closed account, partition, region, and ARN checks.
- Synthesize partition-aware IAM policies for commercial AWS and GovCloud instead of baking in commercial ARN literals.
- Deploy through the pinned AgentCore CLI's nine-region schema while still validating existing commercial and GovCloud Runtime ARNs in the bridge, resolver, and CDK.
- Run validation and deployment through an atomic wrapper that injects non-secret runtime settings, then restores the tracked manifest on every trappable exit.
- Use current versioned AgentCore Runtime ARNs or legacy runtime IDs during bridge startup, CDK synthesis, and deployment-time discovery.
- Verify downloaded source archives without scanning generated local secrets or build output, while Git clones retain full-history secret scanning.

## 0.1.0 — 2026-07-17

This is an open-source archive milestone, not a promise of a hosted service, active roadmap, or SemVer support policy.

### Included

- Multi-tenant Slack bridge with OAuth, signature verification, retry deduplication, and asynchronous AgentCore dispatch.
- Strands agent runtime with tenant-scoped configuration, memory, skills, audited catalog tools, cost tracking, and AgentCore Gateway support.
- Built-in SRE workflows for incident response, runbooks, on-call handoff, deploy review, status updates, and post-mortem drafting.
- GitHub App code search, file and symbol inspection, commit correlation, and an experimental Fargate PR sandbox.
- Next.js onboarding, workspace settings, operator views, and experimental ephemeral dashboards.
- CDK reference stacks for data, IAM, services, observability, Gateway, and sandbox resources.
- Synthetic incident and persistent test-environment tooling.

### Open-source preparation

- Reframed the project honestly as an archived reference implementation.
- Added a three-command no-cloud demo that boots the real bridge and Next.js dashboard renderer with a synthetic incident.
- Added setup, verification, architecture, cost, security, and cleanup guidance.
- Added MIT licensing, contribution guidance, a security policy, and a code of conduct.
- Preserved meaningful development commits while removing private planning artifacts from public history.
- Removed deployment fingerprints from the current tree and public history, and made cloud/GitHub identities portable.
- Made production deployment manual-only while expanding read-only CI coverage.
- Updated locked dependencies and cleared known npm/pip audit findings.
- Hardened dashboard validation, expiry, storage errors, public metadata, and local development.
- Added fail-closed Slack verification, channel-aware authorization, repository allowlists, and operator-approved GitHub installation binding.
- Bound OAuth state to the initiating browser, attributed reaction feedback to the exact Slack app, constrained skill triggers to a bounded linear-time regex engine, and guarded public dashboard reads.
- Replaced ambiguous Gateway target-prefix authorization with versioned, exactly decoded tenant ownership; legacy targets require explicit reprovisioning.
- Scoped memory to tenant and channel by default, narrowed cloud credential access, and removed browser-visible administrative secrets.

### Known limitations

- The archived cloud deployment is not provided or supported.
- Production security, compliance, cost, and reliability have not been independently revalidated.
- Dashboard URLs are bearer links without per-user authorization.
- The PR sandbox is disabled by default. Its current in-task credential boundary is unsafe for production and requires a trusted broker plus stronger isolation before real use.
- Multi-user RBAC, billing, additional transports, and several roadmap concepts remain incomplete.
