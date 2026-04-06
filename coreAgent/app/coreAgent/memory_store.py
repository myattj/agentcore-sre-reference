"""Memory storage abstraction for the self-managed memory contract.

The product uses AgentCore Memory's **self-managed strategy**: AgentCore is
just storage and retrieval; we own the extraction pipeline. The same
`extract_records` function will eventually run inside a Lambda triggered by
AgentCore Memory's SNS notifications, but for now it runs in-process and
writes to `InMemoryStore` so the local dev loop has zero infra cost.

Day-N migration:
  - Provision AgentCore Memory resource with selfManagedConfiguration
    pointing at SNS + S3
  - Move `extract_records` into a Lambda handler that reads SNS notifications,
    fetches the S3 payload, and calls BatchCreateMemoryRecords
  - Swap `_memory = InMemoryStore()` to `BatchCreateMemoryRecordsStore` in main.py
  - main.py stops calling extract_records inline; AgentCore triggers fire it
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
        # Real impl in BatchCreateMemoryRecordsStore uses AgentCore's semantic retrieval.
        return list(self._data[namespace][-limit:])


class BatchCreateMemoryRecordsStore:
    """Production implementation backed by AgentCore Memory's BatchCreateMemoryRecords API.

    Stub: raises until the AgentCore Memory resource is provisioned. See
    Phase 8 in /path/to/project
    """

    def __init__(self, memory_id: str, region: str = "us-west-2") -> None:
        self.memory_id = memory_id
        self.region = region

    def write_records(self, namespace: str, records: list[dict[str, Any]]) -> None:
        raise NotImplementedError(
            "Self-managed memory infrastructure not provisioned yet. "
            "Requires AgentCore Memory resource + SNS topic + S3 bucket + IAM. "
            "See Phase 8 in the plan."
        )

    def query(self, namespace: str, query: str, limit: int = 10) -> list[dict[str, Any]]:
        raise NotImplementedError(
            "Self-managed memory infrastructure not provisioned yet."
        )


# ----------------------------------------------------------------------------
# Extraction
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

    return records
