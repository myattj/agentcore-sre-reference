## Summary

Describe the behavior before and after this change. Keep the pull request focused
on making the self-hosted product safer, clearer, easier to install, or easier to
operate.

## Verification

- [ ] `make check`
- [ ] New or changed behavior has focused tests
- [ ] Public documentation matches the implementation

List any other commands or manual checks:

## Risk and operations

- [ ] I called out changes to IAM, authentication, tenant isolation, data retention, AWS cost, or migration behavior
- [ ] This change contains no credentials, customer data, private URLs, deployment state, or generated build output
- [ ] Experimental or unsafe behavior remains disabled by default and clearly documented

## Scope

- [ ] This belongs in the shared self-hosted product rather than one deployment-specific fork
- [ ] I read `CONTRIBUTING.md`, `SECURITY.md`, and `CODE_OF_CONDUCT.md`
