"""Shared pytest fixtures.

Defaults every test into LOCAL_DEV mode so the bridge's lazy singletons
(tenant_resolver, dedup, slack_token_store) build their in-memory /
file-based variants instead of trying to reach DynamoDB or Secrets
Manager. Tests that exercise the production paths can monkeypatch the
relevant env vars in their own setup.

Module-level singletons are reset between tests via the explicit
`reset_*_for_tests()` helpers each module exposes — keeps test isolation
honest without needing fancy fixtures.
"""
from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _local_dev_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force LOCAL_DEV=1 for every test unless explicitly overridden.

    Also clears any AWS-related env vars that could accidentally point
    a test at a real account if a developer has them in their shell.
    """
    monkeypatch.setenv("LOCAL_DEV", "1")
    for var in ("AWS_PROFILE",):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def _reset_singletons() -> None:
    """Reset module-level singletons before AND after each test so
    `LOCAL_DEV` toggles, env-var changes, and stub injection are all
    visible to the next test that imports the same module."""
    from bridge import dedup, slack_token_store, tenant_resolver

    dedup.reset_dedup_for_tests()
    slack_token_store.reset_token_store_for_tests()
    tenant_resolver.reset_resolver_for_tests()
    yield
    dedup.reset_dedup_for_tests()
    slack_token_store.reset_token_store_for_tests()
    tenant_resolver.reset_resolver_for_tests()
