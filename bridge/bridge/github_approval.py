"""Operator-only approval for GitHub App installation trust bindings."""

from __future__ import annotations

import hmac
from dataclasses import dataclass

from .github_app import get_installation_metadata, normalize_installation_id
from .tenant_write import approve_github_installation_binding


class GitHubAccountMismatch(RuntimeError):
    """The verified installation owner did not match operator intent."""


@dataclass(frozen=True)
class GitHubApprovalResult:
    tenant_id: str
    installation_id: str
    account_login: str


def approve_github_installation(
    tenant_id: str,
    installation_id: str | int,
    expected_account_login: str,
    region: str,
) -> GitHubApprovalResult:
    """Verify GitHub-owned metadata, then persist an exclusive binding."""

    canonical_id = normalize_installation_id(installation_id)
    expected = expected_account_login.strip().lower()
    metadata = get_installation_metadata(canonical_id)
    actual = metadata.account_login.strip().lower()
    if not expected or not hmac.compare_digest(actual, expected):
        raise GitHubAccountMismatch(
            "GitHub installation account does not match operator approval"
        )

    approve_github_installation_binding(tenant_id, canonical_id, region)
    return GitHubApprovalResult(
        tenant_id=tenant_id,
        installation_id=canonical_id,
        account_login=metadata.account_login,
    )
