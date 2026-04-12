"""Memory storage abstraction — local dev fallback.

Production path: main.py uses ``AgentCoreMemorySessionManager`` (from the
``bedrock_agentcore.memory`` SDK) which handles event creation and retrieval
automatically via Strands hooks. AgentCore's built-in SEMANTIC and
USER_PREFERENCE strategies handle extraction — no custom Lambda pipeline.

Local dev path (AGENT_LOCAL_STORES=1): ``InMemoryStore`` + inline
``extract_records()`` run in-process so the dev loop has zero AWS cost.
Records are not persisted across process restarts.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Protocol


class MemoryStore(Protocol):
    """The storage contract. Two implementations: in-memory (local) and
    AgentCore-backed (production)."""

    def write_records(self, namespace: str, records: list[dict[str, Any]]) -> None: ...

    def query(self, namespace: str, query: str, limit: int = 10) -> list[dict[str, Any]]: ...


class InMemoryStore:
    """Dict-backed implementation. Used by `agentcore dev` and tests.
    Records are not persisted across process restarts."""

    def __init__(self) -> None:
        self._data: dict[str, list[dict[str, Any]]] = defaultdict(list)

    def write_records(self, namespace: str, records: list[dict[str, Any]]) -> None:
        self._data[namespace].extend(records)

    def query(self, namespace: str, query: str, limit: int = 10) -> list[dict[str, Any]]:
        # Naive: return the most recent N records in the namespace, no semantic search.
        return list(self._data[namespace][-limit:])


# Module-level singleton so main.py and tools.py share ONE InMemoryStore.
# Same pattern as ``audit.build_audit_store()``.
_cached_store: InMemoryStore | None = None


def build_memory_store() -> InMemoryStore:
    """Return the module-level InMemoryStore singleton.

    Used for local dev (AGENT_LOCAL_STORES=1) memory writes. Both main.py
    (post-stream extraction) and tools.py (record_feedback) call this so
    they share the same in-process dict.

    Production memory goes through AgentCoreMemorySessionManager and never
    touches this store.
    """
    global _cached_store
    if _cached_store is None:
        _cached_store = InMemoryStore()
    return _cached_store



# ----------------------------------------------------------------------------
# Extraction (local dev only — production uses built-in strategies)
# ----------------------------------------------------------------------------

def extract_records(
    turn: dict[str, str],
    rules: list[str],
) -> list[dict[str, Any]]:
    """Apply extraction rules to a single conversation turn.

    Returns a list of memory records suitable for `MemoryStore.write_records`.
    The shape of each record is the same shape AgentCore Memory's
    BatchCreateMemoryRecords API expects, so the day this moves into a Lambda
    the records can be ingested without translation.

    v0 rules are intentionally trivial — the point is the contract, not
    the extraction quality. Real extraction will use an LLM call against
    a small/fast model.
    """
    user_text = turn.get("user", "")
    assistant_text = turn.get("assistant", "")
    records: list[dict[str, Any]] = []

    if "user_preferences" in rules:
        # Trivial heuristic: any sentence starting with "I prefer" or "I like"
        # gets stored as a user_preference record.
        for marker in ("I prefer", "I like"):
            if marker in user_text:
                records.append({
                    "type": "user_preference",
                    "content": user_text,
                    "extracted_via": "heuristic_v0",
                })
                break

    if "facts" in rules:
        # Trivial heuristic: any sentence with "is a" or "are" in the user
        # message gets logged as a fact candidate.
        if " is a " in user_text or " are " in user_text:
            records.append({
                "type": "fact_candidate",
                "content": user_text,
                "extracted_via": "heuristic_v0",
            })

    if "faq_in_channel" in rules:
        # Store every Q+A pair as an FAQ record. The caller (main.py)
        # handles writing these to a channel-scoped namespace
        # (tenants/{id}/channels/{channel_id}/faq).
        if user_text and assistant_text:
            records.append({
                "type": "faq",
                "question": user_text,
                "answer": assistant_text,
                "extracted_via": "faq_in_channel_v0",
            })

    if "reaction_feedback" in rules:
        # Feedback records are written directly by the reaction_feedback
        # handler in main.py, not via the extraction pipeline. This rule
        # entry exists for documentation and for future inline extraction
        # of implicit signals (e.g. user re-asks the same question →
        # negative implicit feedback).
        pass

    return records
