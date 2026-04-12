"""Direct AgentCore Memory retrieval for codebase affinity hints.

Called from ``context_assembler`` before ``resolve_codebase_context``
runs. Queries the tenant's SEMANTIC memory namespace for a "which
codebase does this channel prefer" signal and returns a repo slug
the resolver can treat as a hint.

## Design: hint, not authority

The resolver treats the returned repo as a **candidate** — it's only
promoted to CONFIRMED when the hint matches a repo that's already in
``codebases.bindings``. That bounded-answer-set filter kills the
silent-wrong failure mode: if the semantic layer returns a repo the
tenant doesn't actually have, we drop the hint and fall back to
SHORTLIST (ask the user).

## Extraction path

No custom extraction rule. AgentCore's SEMANTIC strategy extracts
from every assistant/user turn automatically. The SHORTLIST prompt
block (see ``codebase_resolver._shortlist``) teaches the model to
acknowledge the user's answer in a scoped, indexable form — e.g.,
"I'll use acme/platform in this channel going forward" — so there's
a clear utterance for the strategy to pick up and index.

## Local dev / production parity

When ``AGENTCORE_MEMORY_ID`` is unset (local dev) this module is a
no-op: the retrieval function returns None and the resolver falls
through to its non-hint behavior. Production paths only activate
when memory is actually provisioned.

## Latency cost

One synchronous ``retrieve_memory_records`` call per invocation in
production when ``codebases.enabled`` and ``allow_learning`` are
both True. ~200-500ms added to the pre-LLM hot path. We accept this
trade-off because it's the difference between "ask every new
conversation" and "ask once, remember."
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any, Iterable

log = logging.getLogger(__name__)

# Env-sourced at import time — matches main.py's convention. Both values
# must be set for semantic retrieval to activate.
_MEMORY_ID = os.getenv("AGENTCORE_MEMORY_ID", "")
_SEMANTIC_STRATEGY_ID = os.getenv("AGENTCORE_SEMANTIC_STRATEGY_ID", "")

# Minimum relevance score before a semantic hit is trusted. AgentCore's
# scores are roughly on a 0-1 embedding-similarity scale; 0.55 picks up
# reasonably-related content without admitting weak/random matches.
# Tunable later once we have real retrieval logs.
_MIN_RELEVANCE_SCORE = 0.55

# Top-K to fetch. We walk results in score order until we find one that
# mentions a known repo. K=5 balances "enough headroom for the bounded
# filter to find something" against wasted retrieval.
_DEFAULT_TOP_K = 5


# ---------------------------------------------------------------------------
# Client provisioning
# ---------------------------------------------------------------------------

_memory_client: Any | None = None


def _get_memory_client() -> Any:
    """Lazily build and cache a ``MemoryClient``.

    Separated from the retrieval function so tests can patch the client
    without touching module state, and so the import cost is only paid
    when semantic retrieval is actually used.
    """
    global _memory_client
    if _memory_client is None:
        from bedrock_agentcore.memory.client import MemoryClient

        _memory_client = MemoryClient(
            region_name=os.getenv("AWS_REGION", "us-west-2"),
        )
    return _memory_client


def reset_memory_client_for_tests() -> None:
    """Test helper: drop the cached client so the next call re-builds."""
    global _memory_client
    _memory_client = None


# ---------------------------------------------------------------------------
# Actor/namespace construction (mirrors main.py:_build_memory_session_manager)
# ---------------------------------------------------------------------------

def _actor_id_for(
    tenant_id: str,
    channel_id: str,
    *,
    isolated: bool = False,
    user_id: str = "",
) -> str:
    """Build the actor_id that main.py's session_manager uses for this scope.

    Must stay in sync with ``_build_memory_session_manager``:

      - isolated channels: ``{tenant_id}_{channel_id}``
      - shared (channel present, not isolated): ``{tenant_id}``
      - DMs (no channel): ``{tenant_id}_{user_id or "anon"}``

    If this diverges from main.py we'd be querying a different namespace
    than the session manager is writing to, which would silently return
    empty results.
    """
    if isolated and channel_id:
        return f"{tenant_id}_{channel_id}"
    if channel_id:
        return tenant_id
    return f"{tenant_id}_{user_id or 'anon'}"


def _namespace_for(actor_id: str) -> str:
    """Build the SEMANTIC strategy namespace for this actor.

    Format matches the path in ``main.py:_build_memory_session_manager``:
    ``/strategies/{strategyId}/actors/{actorId}/``. Trailing slash is
    required by AgentCore Memory.
    """
    return f"/strategies/{_SEMANTIC_STRATEGY_ID}/actors/{actor_id}/"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def retrieve_codebase_affinity_hint(
    tenant_id: str,
    channel_id: str,
    known_repos: Iterable[str],
    *,
    isolated: bool = False,
    user_id: str = "",
    memory_client: Any | None = None,
    min_score: float = _MIN_RELEVANCE_SCORE,
    top_k: int = _DEFAULT_TOP_K,
) -> str | None:
    """Query AgentCore Memory for the preferred codebase in this scope.

    Returns the best-match repo slug from ``known_repos`` when:
      - AgentCore Memory is provisioned (``AGENTCORE_MEMORY_ID`` set)
      - A SEMANTIC strategy ID is configured
      - The top semantic hit is above ``min_score``
      - The hit's content mentions a repo that's in ``known_repos``
        (bounded-answer filter — drops hallucinations and unknown repos)

    Returns ``None`` otherwise. The resolver treats ``None`` as "no
    hint — fall back to explicit bindings / SHORTLIST / UNKNOWN".

    Never raises. Any boto3 error (ResourceNotFound, Validation,
    network) is logged and swallowed as ``None`` so a broken memory
    resource never blocks the invocation.

    ``memory_client`` is accepted for tests — pass a mock with a
    ``retrieve_memories`` method. Omit in production to use the
    cached module-level client.
    """
    if not _MEMORY_ID or not _SEMANTIC_STRATEGY_ID:
        return None

    known = {r for r in known_repos if r}
    if not known:
        return None

    actor_id = _actor_id_for(
        tenant_id, channel_id, isolated=isolated, user_id=user_id
    )
    namespace = _namespace_for(actor_id)
    query = _build_query(channel_id, user_id)

    try:
        client = memory_client or _get_memory_client()
        results = client.retrieve_memories(
            memory_id=_MEMORY_ID,
            namespace=namespace,
            query=query,
            top_k=top_k,
        )
    except Exception as e:  # noqa: BLE001 — never block the invocation
        log.warning(
            "codebase_memory: retrieve failed for tenant=%s channel=%s: %s",
            tenant_id,
            channel_id,
            e,
        )
        return None

    if not results:
        return None

    # Walk results in rank order. Return the first one that's above the
    # score threshold AND mentions a known repo.
    for record in results:
        score = _extract_score(record)
        if score is not None and score < min_score:
            continue
        content = _extract_content(record)
        if not content:
            continue
        hit = _first_known_repo_mentioned(content, known)
        if hit is not None:
            log.info(
                "codebase_memory: hint for tenant=%s channel=%s → %s "
                "(score=%s)",
                tenant_id,
                channel_id,
                hit,
                f"{score:.2f}" if score is not None else "n/a",
            )
            return hit

    return None


# ---------------------------------------------------------------------------
# Result parsing (defensive against SDK field-name drift)
# ---------------------------------------------------------------------------

def _extract_score(record: dict[str, Any]) -> float | None:
    """Pull the relevance score from a memory record summary.

    The SDK docstring warns of field-name asymmetry between input and
    output shapes, so we check a few candidate keys. Returns None when
    no score is present — callers treat that as "trust the rank order,
    don't filter by score".
    """
    for key in ("score", "relevanceScore", "relevance_score"):
        val = record.get(key)
        if isinstance(val, (int, float)):
            return float(val)
    return None


def _extract_content(record: dict[str, Any]) -> str:
    """Pull the textual content from a memory record summary.

    AgentCore records commonly nest content under ``content.text`` or
    return it directly as ``content``. Empty string when no content
    is available so the caller can cleanly skip the record.
    """
    content = record.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        # Common shapes: {"text": "..."} or {"data": "..."}
        for key in ("text", "data", "value"):
            val = content.get(key)
            if isinstance(val, str):
                return val
    # Some SDK versions expose the body differently.
    for key in ("memoryRecordSummary", "summary", "text"):
        val = record.get(key)
        if isinstance(val, str):
            return val
    return ""


def _first_known_repo_mentioned(
    content: str, known_repos: set[str]
) -> str | None:
    """Return the first repo from ``known_repos`` that appears in ``content``.

    Matching is case-insensitive and prefers the full ``owner/name``
    slug over the bare name — "acme/platform" is a stronger signal
    than just "platform" (which could appear in unrelated context).

    The bare-name fallback uses a word boundary to avoid matching
    substrings: "platform" won't match "platforms" or "cross-platform".

    Pure function — no IO, safe to unit-test directly.
    """
    if not content:
        return None
    lower = content.lower()

    # First pass: full slugs. Strongest signal.
    for repo in known_repos:
        if repo.lower() in lower:
            return repo

    # Second pass: bare names (part after the slash). Word-boundary match
    # so "api" doesn't match "apis" or "rapid".
    for repo in known_repos:
        if "/" not in repo:
            continue
        name = repo.split("/", 1)[1]
        if not name:
            continue
        pattern = rf"\b{re.escape(name.lower())}\b"
        if re.search(pattern, lower):
            return repo

    return None


def _build_query(channel_id: str, user_id: str) -> str:
    """Build the semantic query string.

    Phrased to match the kind of sentence the model is coached to emit
    after a user confirms a codebase: "I'll use acme/platform in this
    channel going forward." AgentCore's SEMANTIC extractor indexes the
    assistant utterance; a similarly-shaped query should retrieve it.

    Including the raw channel_id or user_id in the query is a weak but
    non-zero signal — embeddings don't understand Slack ID formatting
    but they'll still cluster tokens together.
    """
    if channel_id:
        return (
            f"preferred codebase repository to use for Slack channel "
            f"{channel_id} going forward"
        )
    return (
        f"preferred codebase repository to use for user "
        f"{user_id or 'this conversation'} going forward"
    )


__all__ = [
    "retrieve_codebase_affinity_hint",
    "reset_memory_client_for_tests",
]
