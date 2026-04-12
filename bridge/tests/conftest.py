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

# Set BEFORE any test module imports `bridge.main`. The bridge instantiates
# its `AgentCoreClient` at module load and explodes if neither
# `AGENT_RUNTIME_ARN` nor `LOCAL_AGENT_URL` is set. We use AGENT_RUNTIME_ARN
# (not LOCAL_AGENT_URL) so test_client.py's local-dev tests don't see a
# spurious LOCAL_AGENT_URL leaking into their explicitly-constructed clients
# via `AgentCoreClient.__init__`'s `or os.getenv(...)` fallback. Tests
# never actually hit this ARN — it just satisfies the constructor's "have
# at least one transport" check.
os.environ.setdefault(
    "AGENT_RUNTIME_ARN",
    "arn:aws:bedrock-agentcore:us-west-2:000000000000:runtime/test-fixture",
)
os.environ.setdefault("LOCAL_DEV", "1")
os.environ.setdefault("BRIDGE_OAUTH_STATE_SECRET", "test-state-secret")
# Phase B: sandbox callback shared secret. Tests for the
# /internal/sandbox_complete route compare against this constant.
os.environ.setdefault("SANDBOX_CALLBACK_SECRET", "test-sandbox-secret")


@pytest.fixture(autouse=True)
def _local_dev_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force LOCAL_DEV=1 for every test unless explicitly overridden.

    Also clears any AWS-related env vars that could accidentally point
    a test at a real account if a developer has them in their shell.
    Sets a deterministic `BRIDGE_OAUTH_STATE_SECRET` so state and
    session token tests don't need per-test setup.
    """
    monkeypatch.setenv("LOCAL_DEV", "1")
    monkeypatch.setenv("BRIDGE_OAUTH_STATE_SECRET", "test-state-secret")
    monkeypatch.setenv("SANDBOX_CALLBACK_SECRET", "test-sandbox-secret")
    for var in ("AWS_PROFILE",):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def _reset_singletons() -> None:
    """Reset module-level singletons before AND after each test so
    `LOCAL_DEV` toggles, env-var changes, and stub injection are all
    visible to the next test that imports the same module."""
    from bridge import (
        dedup,
        gateway_jwt,
        gateway_provisioner,
        slack_token_store,
        tenant_resolver,
        tenant_write,
    )

    dedup.reset_dedup_for_tests()
    slack_token_store.reset_token_store_for_tests()
    tenant_resolver.reset_resolver_for_tests()
    tenant_write.reset_tenant_write_for_tests()
    gateway_jwt._reset_key_cache()
    gateway_provisioner.reset_provisioner_for_tests()
    yield
    dedup.reset_dedup_for_tests()
    slack_token_store.reset_token_store_for_tests()
    tenant_resolver.reset_resolver_for_tests()
    tenant_write.reset_tenant_write_for_tests()
    gateway_jwt._reset_key_cache()
    gateway_provisioner.reset_provisioner_for_tests()
