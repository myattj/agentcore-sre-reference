"""Idempotency state for the seeder.

A JSON file at ``scripts/testenv/.testenv-state.json`` tracks:

  - ``posted_keys``: map of SeedMessage.key → posted channel_ts. The
    seeder skips keys it's already posted (so re-running is safe) and
    resolves ``parent_key`` references to their posted thread_ts.

  - ``channel_ids``: map of channel_name → Slack channel ID. Populated
    by the channel provisioner on first run, reused on subsequent runs
    to avoid re-listing channels.

  - ``tenant_id``: the tenant this state file was written for. If the
    state file exists but belongs to a different tenant, the seeder
    refuses to run (cross-tenant state would clobber keys).

The state file is gitignored via the existing ``scripts/testenv/``
entry in ``.gitignore`` (see scripts/testenv/README.md). Losing it
just means the next run is a full-replay — Slack will see duplicate
messages but the channels remain usable.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_STATE_FILENAME = ".testenv-state.json"


def _state_path() -> Path:
    return Path(__file__).resolve().parent / _STATE_FILENAME


class SeederState:
    """In-memory view of the state file. Reads on __init__, writes on
    every ``mark_posted`` call (flush-after-every-write so interrupted
    runs don't lose progress)."""

    def __init__(self, tenant_id: str) -> None:
        self.tenant_id = tenant_id
        self.posted_keys: dict[str, str] = {}  # key -> "channel:ts"
        self.channel_ids: dict[str, str] = {}  # name -> id
        self._path = _state_path()
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            log.info("no state file at %s — starting fresh", self._path)
            return
        try:
            data = json.loads(self._path.read_text())
        except json.JSONDecodeError as e:
            raise RuntimeError(
                f"Corrupted state file at {self._path}: {e}. "
                f"Delete it to start fresh."
            ) from e
        existing_tenant = data.get("tenant_id")
        if existing_tenant and existing_tenant != self.tenant_id:
            raise RuntimeError(
                f"State file at {self._path} belongs to tenant "
                f"{existing_tenant!r}, but you're running as "
                f"{self.tenant_id!r}. Delete the file to start over, "
                f"or pass --tenant {existing_tenant}."
            )
        self.posted_keys = dict(data.get("posted_keys") or {})
        self.channel_ids = dict(data.get("channel_ids") or {})
        log.info(
            "loaded state: %d posted keys, %d known channels",
            len(self.posted_keys),
            len(self.channel_ids),
        )

    def _flush(self) -> None:
        data: dict[str, Any] = {
            "tenant_id": self.tenant_id,
            "posted_keys": self.posted_keys,
            "channel_ids": self.channel_ids,
        }
        self._path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")

    # ------------------------------------------------------------------------
    # Posted-key tracking
    # ------------------------------------------------------------------------

    def is_posted(self, key: str) -> bool:
        return key in self.posted_keys

    def get_thread_ts(self, key: str) -> str | None:
        """Return the ``ts`` of a previously-posted message by key, or
        None if it wasn't posted. Used to resolve ``parent_key`` into
        ``thread_ts`` when seeding threaded replies."""
        entry = self.posted_keys.get(key)
        if not entry or ":" not in entry:
            return None
        return entry.split(":", 1)[1]

    def get_channel_of(self, key: str) -> str | None:
        """Channel id (not name) of a previously-posted message by key."""
        entry = self.posted_keys.get(key)
        if not entry or ":" not in entry:
            return None
        return entry.split(":", 1)[0]

    def mark_posted(self, key: str, channel_id: str, ts: str) -> None:
        self.posted_keys[key] = f"{channel_id}:{ts}"
        self._flush()

    # ------------------------------------------------------------------------
    # Channel ID map
    # ------------------------------------------------------------------------

    def get_channel_id(self, name: str) -> str | None:
        return self.channel_ids.get(name)

    def set_channel_id(self, name: str, channel_id: str) -> None:
        self.channel_ids[name] = channel_id
        self._flush()

    def all_channel_ids(self) -> dict[str, str]:
        return dict(self.channel_ids)
