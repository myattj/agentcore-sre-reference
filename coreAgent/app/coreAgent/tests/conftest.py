"""Shared fixtures for coreAgent tests.

AgentCore Runtime loads agent modules as top-level scripts, so the
production code uses absolute imports (``from tenant import ...``).
We add the source directory to ``sys.path`` so the same imports resolve
in the test runner without restructuring the agent code into a package.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Insert the agent source directory onto sys.path so absolute imports
# like ``from tenant import ...`` resolve.
_AGENT_SRC = str(Path(__file__).resolve().parent.parent)
if _AGENT_SRC not in sys.path:
    sys.path.insert(0, _AGENT_SRC)

# Stub out modules that require AWS credentials or external services
# before any agent module is imported. These stubs are only needed so
# that ``import context_assembler`` (which imports ``slack_api`` and
# ``codebase_memory``) doesn't blow up in a test environment.
sys.modules.setdefault("slack_api", MagicMock())
sys.modules.setdefault("codebase_memory", MagicMock())
sys.modules.setdefault("codebase_resolver", MagicMock())


@pytest.fixture()
def sample_ctx() -> dict:
    """Minimal bridge-supplied context dict for tests."""
    return {
        "user_id": "U_TEST",
        "channel_id": "C_TEST",
        "thread_id": "1712345678.123456",
        "workspace_id": "T_TEST",
    }
