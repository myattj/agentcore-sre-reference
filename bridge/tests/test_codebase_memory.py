"""Tests for coreAgent/app/coreAgent/codebase_memory.py.

Uses the same sys.path-injection pattern as the other coreAgent
tests. The module uses flat imports, so it loads fine in the bridge
venv. The real boto3 client is never constructed — every test passes
a mock ``memory_client`` object with a ``retrieve_memories`` method,
OR exercises pure helpers directly.

The module reads ``AGENTCORE_MEMORY_ID`` and
``AGENTCORE_SEMANTIC_STRATEGY_ID`` at import time. We monkeypatch
those module attributes in tests that need them set.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

_AGENT_CODE = str(
    Path(__file__).resolve().parents[2] / "coreAgent" / "app" / "coreAgent"
)
if _AGENT_CODE not in sys.path:
    sys.path.insert(0, _AGENT_CODE)

import codebase_memory  # type: ignore[import-not-found]
from codebase_memory import (  # type: ignore[import-not-found]
    _actor_id_for,
    _build_query,
    _extract_content,
    _extract_score,
    _first_known_repo_mentioned,
    _namespace_for,
    retrieve_codebase_affinity_hint,
)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

class TestFirstKnownRepoMentioned:
    def test_full_slug_match(self) -> None:
        assert _first_known_repo_mentioned(
            "we need to fix acme/platform today",
            {"acme/platform", "acme/billing"},
        ) == "acme/platform"

    def test_bare_name_match(self) -> None:
        assert _first_known_repo_mentioned(
            "platform is broken",
            {"acme/platform"},
        ) == "acme/platform"

    def test_word_boundary_prevents_substring_match(self) -> None:
        """A bare-name match on "platform" must not hit "platforms"."""
        assert _first_known_repo_mentioned(
            "platforms are cool",
            {"acme/platform"},
        ) is None

    def test_word_boundary_prevents_embedded_match(self) -> None:
        """"api" must not match "rapid"."""
        assert _first_known_repo_mentioned(
            "rapid response team",
            {"acme/api"},
        ) is None

    def test_case_insensitive(self) -> None:
        assert _first_known_repo_mentioned(
            "PLATFORM is broken",
            {"acme/platform"},
        ) == "acme/platform"

    def test_no_match_returns_none(self) -> None:
        assert _first_known_repo_mentioned(
            "nothing relevant here",
            {"acme/platform", "acme/billing"},
        ) is None

    def test_empty_content_returns_none(self) -> None:
        assert _first_known_repo_mentioned("", {"a/b"}) is None

    def test_empty_known_repos_returns_none(self) -> None:
        assert _first_known_repo_mentioned("platform", set()) is None

    def test_full_slug_preferred_over_bare_name(self) -> None:
        """When content contains a full slug, we return it — not a
        different repo whose bare name happens to appear elsewhere."""
        # Content has both "acme/platform" (full slug) and "billing"
        # (bare name of acme/billing). Full slug should win.
        result = _first_known_repo_mentioned(
            "we need acme/platform for billing reasons",
            {"acme/platform", "acme/billing"},
        )
        assert result == "acme/platform"


class TestExtractScore:
    def test_score_field(self) -> None:
        assert _extract_score({"score": 0.73}) == 0.73

    def test_camel_case_field(self) -> None:
        assert _extract_score({"relevanceScore": 0.85}) == 0.85

    def test_snake_case_field(self) -> None:
        assert _extract_score({"relevance_score": 0.42}) == 0.42

    def test_missing_score_returns_none(self) -> None:
        assert _extract_score({}) is None

    def test_non_numeric_score_returns_none(self) -> None:
        assert _extract_score({"score": "high"}) is None

    def test_integer_score_coerced_to_float(self) -> None:
        result = _extract_score({"score": 1})
        assert result == 1.0
        assert isinstance(result, float)


class TestExtractContent:
    def test_string_content(self) -> None:
        assert _extract_content({"content": "raw text"}) == "raw text"

    def test_nested_text_field(self) -> None:
        assert _extract_content({"content": {"text": "nested"}}) == "nested"

    def test_nested_data_field(self) -> None:
        assert _extract_content({"content": {"data": "other"}}) == "other"

    def test_nested_value_field(self) -> None:
        assert _extract_content({"content": {"value": "v"}}) == "v"

    def test_missing_content_returns_empty_string(self) -> None:
        assert _extract_content({}) == ""

    def test_unrecognized_shape_returns_empty_string(self) -> None:
        assert _extract_content({"content": {"unknown_key": "x"}}) == ""


class TestActorIdFor:
    def test_shared_channel_uses_tenant_id(self) -> None:
        assert _actor_id_for("acme", "C123") == "acme"

    def test_isolated_channel_combines_tenant_and_channel(self) -> None:
        assert _actor_id_for("acme", "C123", isolated=True) == "acme_C123"

    def test_dm_without_channel_uses_user(self) -> None:
        assert _actor_id_for("acme", "", user_id="U999") == "acme_U999"

    def test_dm_without_user_defaults_to_anon(self) -> None:
        assert _actor_id_for("acme", "", user_id="") == "acme_anon"

    def test_isolated_flag_ignored_without_channel(self) -> None:
        """isolated=True with no channel_id shouldn't produce a weird actor."""
        assert _actor_id_for("acme", "", isolated=True, user_id="U1") == "acme_U1"


class TestNamespaceFor:
    def test_uses_semantic_strategy_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(codebase_memory, "_SEMANTIC_STRATEGY_ID", "strat_abc")
        ns = _namespace_for("acme")
        assert ns == "/strategies/strat_abc/actors/acme/"

    def test_trailing_slash_required(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(codebase_memory, "_SEMANTIC_STRATEGY_ID", "s")
        assert _namespace_for("a").endswith("/")


class TestBuildQuery:
    def test_channel_query_mentions_channel_id(self) -> None:
        q = _build_query("C123", "")
        assert "C123" in q
        assert "channel" in q.lower()

    def test_user_query_fallback(self) -> None:
        q = _build_query("", "U999")
        assert "U999" in q

    def test_anonymous_fallback(self) -> None:
        q = _build_query("", "")
        assert q  # non-empty
        assert "channel" not in q.lower() or "conversation" in q.lower()


# ---------------------------------------------------------------------------
# retrieve_codebase_affinity_hint — integration-ish with a mock client
# ---------------------------------------------------------------------------

class _MockMemoryClient:
    """Stub ``MemoryClient`` with a ``retrieve_memories`` method.

    ``results`` is the canned response returned on every call. Captures
    the last call args for assertions. Set ``raise_on_call`` to an
    Exception to simulate boto3 errors.
    """

    def __init__(
        self,
        results: list[dict[str, Any]] | None = None,
        raise_on_call: Exception | None = None,
    ) -> None:
        self.results = results or []
        self.raise_on_call = raise_on_call
        self.last_call: dict[str, Any] | None = None

    def retrieve_memories(
        self,
        *,
        memory_id: str,
        namespace: str,
        query: str,
        top_k: int,
    ) -> list[dict[str, Any]]:
        self.last_call = {
            "memory_id": memory_id,
            "namespace": namespace,
            "query": query,
            "top_k": top_k,
        }
        if self.raise_on_call is not None:
            raise self.raise_on_call
        return list(self.results)


@pytest.fixture
def env_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the module-level memory env vars for tests that need them."""
    monkeypatch.setattr(codebase_memory, "_MEMORY_ID", "mem_test")
    monkeypatch.setattr(
        codebase_memory, "_SEMANTIC_STRATEGY_ID", "strat_test"
    )


class TestRetrieveCodebaseAffinityHint:
    def test_returns_none_when_memory_id_unset(self) -> None:
        """No AGENTCORE_MEMORY_ID → no retrieval attempt at all."""
        # env defaults — no monkeypatch
        result = retrieve_codebase_affinity_hint(
            tenant_id="acme",
            channel_id="C123",
            known_repos=["acme/platform"],
            memory_client=_MockMemoryClient(),
        )
        assert result is None

    def test_returns_none_when_known_repos_empty(
        self, env_configured: None
    ) -> None:
        """Empty binding list → skip retrieval entirely."""
        mock = _MockMemoryClient(results=[{"content": "acme/platform", "score": 1.0}])
        result = retrieve_codebase_affinity_hint(
            tenant_id="acme",
            channel_id="C123",
            known_repos=[],
            memory_client=mock,
        )
        assert result is None
        # Must not have even called the client
        assert mock.last_call is None

    def test_returns_known_repo_on_high_score_match(
        self, env_configured: None
    ) -> None:
        mock = _MockMemoryClient(
            results=[
                {
                    "content": "I'll use acme/platform in this channel going forward",
                    "score": 0.82,
                }
            ]
        )
        result = retrieve_codebase_affinity_hint(
            tenant_id="acme",
            channel_id="C123",
            known_repos=["acme/platform", "acme/billing"],
            memory_client=mock,
        )
        assert result == "acme/platform"

    def test_rejects_low_score_match(
        self, env_configured: None
    ) -> None:
        """A record below min_score should NOT be trusted."""
        mock = _MockMemoryClient(
            results=[
                {
                    "content": "I'll use acme/platform in this channel",
                    "score": 0.30,  # below the 0.55 default
                }
            ]
        )
        result = retrieve_codebase_affinity_hint(
            tenant_id="acme",
            channel_id="C123",
            known_repos=["acme/platform"],
            memory_client=mock,
        )
        assert result is None

    def test_rejects_unknown_repo_even_at_high_score(
        self, env_configured: None
    ) -> None:
        """Bounded-answer filter: semantic returns something confident
        but it's not a real tenant repo → drop it."""
        mock = _MockMemoryClient(
            results=[
                {
                    "content": "we always use evil/unknown in this channel",
                    "score": 0.95,
                }
            ]
        )
        result = retrieve_codebase_affinity_hint(
            tenant_id="acme",
            channel_id="C123",
            known_repos=["acme/platform", "acme/billing"],
            memory_client=mock,
        )
        assert result is None

    def test_walks_past_first_miss_to_find_known_repo(
        self, env_configured: None
    ) -> None:
        """First result is confident but unknown; second is also confident
        and matches a known repo. Should return the second."""
        mock = _MockMemoryClient(
            results=[
                {"content": "unrelated ghost/repo discussion", "score": 0.9},
                {
                    "content": "confirmed acme/billing for #billing-oncall",
                    "score": 0.7,
                },
            ]
        )
        result = retrieve_codebase_affinity_hint(
            tenant_id="acme",
            channel_id="C123",
            known_repos=["acme/platform", "acme/billing"],
            memory_client=mock,
        )
        assert result == "acme/billing"

    def test_swallows_client_exception(
        self, env_configured: None
    ) -> None:
        """Any boto3/network error is logged and returns None —
        never blocks the invocation."""
        mock = _MockMemoryClient(raise_on_call=RuntimeError("boto went boom"))
        result = retrieve_codebase_affinity_hint(
            tenant_id="acme",
            channel_id="C123",
            known_repos=["acme/platform"],
            memory_client=mock,
        )
        assert result is None

    def test_empty_results_returns_none(
        self, env_configured: None
    ) -> None:
        mock = _MockMemoryClient(results=[])
        result = retrieve_codebase_affinity_hint(
            tenant_id="acme",
            channel_id="C123",
            known_repos=["acme/platform"],
            memory_client=mock,
        )
        assert result is None

    def test_record_without_score_still_considered(
        self, env_configured: None
    ) -> None:
        """Records missing a score field fall through the score filter
        (the SDK may not always populate it). Content check still applies."""
        mock = _MockMemoryClient(
            results=[{"content": "use acme/platform for this channel"}]
        )
        result = retrieve_codebase_affinity_hint(
            tenant_id="acme",
            channel_id="C123",
            known_repos=["acme/platform"],
            memory_client=mock,
        )
        assert result == "acme/platform"

    def test_namespace_passed_to_client_matches_actor_format(
        self, env_configured: None
    ) -> None:
        """Shared channel → actor_id == tenant_id → namespace uses tenant_id."""
        mock = _MockMemoryClient(results=[])
        retrieve_codebase_affinity_hint(
            tenant_id="acme",
            channel_id="C123",
            known_repos=["a/b"],
            memory_client=mock,
        )
        assert mock.last_call is not None
        assert mock.last_call["namespace"] == "/strategies/strat_test/actors/acme/"
        assert mock.last_call["memory_id"] == "mem_test"

    def test_isolated_channel_namespace_includes_channel_id(
        self, env_configured: None
    ) -> None:
        mock = _MockMemoryClient(results=[])
        retrieve_codebase_affinity_hint(
            tenant_id="acme",
            channel_id="C_SECRET",
            known_repos=["a/b"],
            isolated=True,
            memory_client=mock,
        )
        assert mock.last_call is not None
        assert "acme_C_SECRET" in mock.last_call["namespace"]

    def test_dm_without_channel_uses_user_in_namespace(
        self, env_configured: None
    ) -> None:
        mock = _MockMemoryClient(results=[])
        retrieve_codebase_affinity_hint(
            tenant_id="acme",
            channel_id="",
            known_repos=["a/b"],
            user_id="U42",
            memory_client=mock,
        )
        assert mock.last_call is not None
        assert "acme_U42" in mock.last_call["namespace"]

    def test_query_string_includes_channel_id_when_present(
        self, env_configured: None
    ) -> None:
        mock = _MockMemoryClient(results=[])
        retrieve_codebase_affinity_hint(
            tenant_id="acme",
            channel_id="C_PLATFORM_ONCALL",
            known_repos=["a/b"],
            memory_client=mock,
        )
        assert mock.last_call is not None
        assert "C_PLATFORM_ONCALL" in mock.last_call["query"]
