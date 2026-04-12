"""Investigation eval scenarios.

Each scenario represents a realistic incident investigation situation.
The ``expected`` dict describes the known-good outcome so a scoring
function can compare the agent's actual behavior against it.

Scenario design principles:
  - Each tests a distinct failure mode or investigation path.
  - Thread context simulates what Slack would actually contain.
  - Expected outcomes are specific enough to score but flexible enough
    that minor wording differences don't cause false failures.
  - Tool availability varies to test graceful degradation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class EvalScenario:
    """A single investigation evaluation scenario."""
    id: str
    name: str
    description: str
    # The user message that triggers the investigation
    user_message: str
    # Simulated thread context (what read_thread_context would return)
    thread_context: str
    # Which tools should be available (determines graceful degradation)
    available_tools: list[str] = field(default_factory=list)
    # Simulated tool responses keyed by tool name
    mock_tool_responses: dict[str, Any] = field(default_factory=dict)
    # Expected outcomes for scoring
    expected: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Scenario 1: CPU spike + recent deploy → find the commit
# ---------------------------------------------------------------------------
SCENARIO_CPU_SPIKE_DEPLOY = EvalScenario(
    id="cpu-spike-deploy",
    name="CPU spike correlates with recent deploy",
    description=(
        "CPU usage spiked to 95% on the payments service at 2:15 PM. "
        "A deploy happened at 2:13 PM. The agent should find the deploy "
        "commit and identify the code change that caused the spike."
    ),
    user_message="alert fired for high CPU on payments — 95% and climbing",
    thread_context=(
        "**U_SRE1**: alert fired for high CPU on payments — 95% and climbing\n"
        "**B_DATADOG**: [ALERT] payments-service CPU > 90% in us-east-1 "
        "(triggered at 14:15 UTC, current: 95.2%)\n"
        "**U_SRE1**: started spiking ~2 mins ago, right after the deploy"
    ),
    available_tools=[
        "read_thread_context", "search_team_history", "search_docs",
        "escalate", "query_metrics", "get_recent_alerts",
        "code_search", "code_read_file", "code_list_commits",
        "code_find_symbol",
    ],
    mock_tool_responses={
        "query_metrics": {
            "series": [
                {"metric": "system.cpu.user", "values": [
                    {"timestamp": "14:10", "value": 22.1},
                    {"timestamp": "14:12", "value": 23.5},
                    {"timestamp": "14:14", "value": 78.3},
                    {"timestamp": "14:15", "value": 95.2},
                ]},
            ],
        },
        "get_recent_alerts": {
            "alerts": [
                {
                    "id": "alert-123",
                    "title": "payments-service CPU > 90%",
                    "status": "triggered",
                    "triggered_at": "2026-04-12T14:15:00Z",
                },
            ],
        },
        "code_list_commits": {
            "commits": [
                {
                    "sha": "abc123f",
                    "author": "jdoe",
                    "date": "2026-04-12T14:13:00Z",
                    "message": "Add batch processing to payment reconciliation",
                    "files_changed": [
                        "src/services/payments/reconciliation.py",
                        "src/services/payments/batch_processor.py",
                    ],
                },
                {
                    "sha": "def456a",
                    "author": "msmith",
                    "date": "2026-04-12T10:30:00Z",
                    "message": "Update payment gateway timeout config",
                    "files_changed": ["src/config/payments.yaml"],
                },
            ],
        },
        "code_read_file": {
            "content": (
                "class BatchProcessor:\n"
                "    def process_all(self, transactions):\n"
                "        # N+1: processes each transaction with a DB round-trip\n"
                "        for txn in transactions:\n"
                "            result = self.db.query(\n"
                "                'SELECT * FROM ledger WHERE txn_id = %s', txn.id\n"
                "            )\n"
                "            self._reconcile(txn, result)\n"
            ),
        },
    },
    expected={
        "found_repo": True,
        "found_file": "src/services/payments/batch_processor.py",
        "found_root_cause": "N+1 query in batch processor",
        "identified_commit": "abc123f",
        "quality_keywords": [
            "batch", "reconciliation", "N+1", "query", "CPU",
            "abc123f", "14:13",
        ],
    },
)

# ---------------------------------------------------------------------------
# Scenario 2: Latency increase + no recent deploy → infrastructure cause
# ---------------------------------------------------------------------------
SCENARIO_LATENCY_NO_DEPLOY = EvalScenario(
    id="latency-no-deploy",
    name="Latency increase with no recent deploy",
    description=(
        "API latency p99 jumped from 200ms to 2s. No deploys in the last "
        "24 hours. The agent should investigate infrastructure causes "
        "(database, network, upstream dependencies)."
    ),
    user_message="p99 latency on the API is through the roof — 2 seconds",
    thread_context=(
        "**U_SRE2**: p99 latency on the API is through the roof — 2 seconds\n"
        "**U_SRE2**: no deploys today, last one was yesterday at 4pm\n"
        "**U_SRE1**: seeing high connection pool usage on the primary DB too"
    ),
    available_tools=[
        "read_thread_context", "search_team_history", "search_docs",
        "escalate", "query_metrics", "search_logs",
        "code_search", "code_read_file", "code_list_commits",
        "code_find_symbol",
    ],
    mock_tool_responses={
        "query_metrics": {
            "series": [
                {"metric": "http.request.duration.p99", "values": [
                    {"timestamp": "10:00", "value": 0.21},
                    {"timestamp": "11:00", "value": 0.19},
                    {"timestamp": "12:00", "value": 0.85},
                    {"timestamp": "13:00", "value": 2.1},
                ]},
                {"metric": "db.pool.active_connections", "values": [
                    {"timestamp": "10:00", "value": 12},
                    {"timestamp": "11:00", "value": 15},
                    {"timestamp": "12:00", "value": 48},
                    {"timestamp": "13:00", "value": 50},
                ]},
            ],
        },
        "search_logs": {
            "entries": [
                {
                    "timestamp": "12:05",
                    "level": "WARN",
                    "message": "Connection pool exhausted, waiting for available connection",
                    "service": "api-gateway",
                },
                {
                    "timestamp": "12:30",
                    "level": "ERROR",
                    "message": "Query timeout after 5000ms on primary replica",
                    "service": "api-gateway",
                },
            ],
        },
        "code_list_commits": {
            "commits": [
                {
                    "sha": "xyz789b",
                    "author": "deploy-bot",
                    "date": "2026-04-11T16:00:00Z",
                    "message": "Release v2.14.0",
                    "files_changed": ["src/api/handlers.py"],
                },
            ],
        },
    },
    expected={
        "found_repo": True,
        "found_file": None,  # No specific file — infrastructure issue
        "found_root_cause": "database connection pool exhaustion",
        "identified_commit": None,  # Not deploy-related
        "quality_keywords": [
            "connection pool", "database", "exhausted", "no recent deploy",
            "infrastructure", "p99",
        ],
    },
)

# ---------------------------------------------------------------------------
# Scenario 3: Error rate increase + multiple repos → ask clarifying question
# ---------------------------------------------------------------------------
SCENARIO_AMBIGUOUS_REPO = EvalScenario(
    id="ambiguous-repo",
    name="Error rate increase with multiple possible repos",
    description=(
        "500 error rate is up across two services. The agent has multiple "
        "repos configured and should ask which service to investigate first "
        "or use ask_codebase_choice."
    ),
    user_message="sev-2 — 500 errors spiking across checkout and inventory",
    thread_context=(
        "**U_SRE1**: sev-2 — 500 errors spiking across checkout and inventory\n"
        "**U_SRE1**: both services started erroring at the same time, ~11:30"
    ),
    available_tools=[
        "read_thread_context", "search_team_history", "search_docs",
        "escalate", "query_metrics", "get_recent_alerts",
        "code_search", "code_read_file", "code_list_commits",
        "code_find_symbol", "ask_codebase_choice",
    ],
    mock_tool_responses={
        "query_metrics": {
            "series": [
                {"metric": "http.5xx.count", "tags": {"service": "checkout"}, "values": [
                    {"timestamp": "11:00", "value": 2},
                    {"timestamp": "11:30", "value": 145},
                    {"timestamp": "12:00", "value": 312},
                ]},
                {"metric": "http.5xx.count", "tags": {"service": "inventory"}, "values": [
                    {"timestamp": "11:00", "value": 1},
                    {"timestamp": "11:30", "value": 98},
                    {"timestamp": "12:00", "value": 201},
                ]},
            ],
        },
    },
    expected={
        "found_repo": False,  # Should ask for clarification
        "found_file": None,
        "found_root_cause": None,  # Can't determine without knowing which repo
        "identified_commit": None,
        "should_ask_clarification": True,
        "quality_keywords": [
            "checkout", "inventory", "both", "which", "service",
        ],
    },
)

# ---------------------------------------------------------------------------
# Scenario 4: Alert with no useful thread context → ask targeted questions
# ---------------------------------------------------------------------------
SCENARIO_SPARSE_CONTEXT = EvalScenario(
    id="sparse-context",
    name="Alert with minimal thread context",
    description=(
        "A terse alert fires with almost no context. The agent should ask "
        "targeted questions rather than guessing."
    ),
    user_message="p1",
    thread_context=(
        "**U_SRE3**: p1\n"
    ),
    available_tools=[
        "read_thread_context", "search_team_history", "search_docs",
        "escalate",
    ],
    mock_tool_responses={
        "search_team_history": {"messages": []},
    },
    expected={
        "found_repo": False,
        "found_file": None,
        "found_root_cause": None,
        "identified_commit": None,
        "should_ask_questions": True,
        "quality_keywords": [
            "which", "service", "what", "happening", "details",
        ],
    },
)

# ---------------------------------------------------------------------------
# Scenario 5: False positive alert → identify normal behavior
# ---------------------------------------------------------------------------
SCENARIO_FALSE_POSITIVE = EvalScenario(
    id="false-positive",
    name="False positive alert — normal traffic pattern",
    description=(
        "Memory usage alert fires during a known batch processing window. "
        "The agent should identify this as expected behavior."
    ),
    user_message="alert firing for high memory on data-pipeline",
    thread_context=(
        "**B_DATADOG**: [ALERT] data-pipeline memory > 80% in us-west-2 "
        "(triggered at 02:05 UTC, current: 84.1%)\n"
        "**U_SRE1**: alert firing for high memory on data-pipeline\n"
        "**U_SRE2**: isn't this the nightly ETL window?"
    ),
    available_tools=[
        "read_thread_context", "search_team_history", "search_docs",
        "escalate", "query_metrics", "get_recent_alerts",
        "code_search", "code_read_file", "code_list_commits",
    ],
    mock_tool_responses={
        "query_metrics": {
            "series": [
                {"metric": "system.mem.used_pct", "values": [
                    {"timestamp": "01:00", "value": 45.2},
                    {"timestamp": "02:00", "value": 82.1},
                    {"timestamp": "02:05", "value": 84.1},
                    {"timestamp": "03:00", "value": 83.5},
                    {"timestamp": "04:00", "value": 46.0},
                ]},
            ],
        },
        "search_team_history": {
            "messages": [
                {
                    "user": "U_SRE2",
                    "text": "the nightly ETL runs 2-4am UTC, always spikes memory to ~85%",
                    "ts": "2026-04-10T15:30:00Z",
                },
                {
                    "user": "U_SRE1",
                    "text": "we should tune the alert threshold for data-pipeline, it fires every night",
                    "ts": "2026-04-08T09:15:00Z",
                },
            ],
        },
    },
    expected={
        "found_repo": True,
        "found_file": None,
        "found_root_cause": "expected nightly ETL batch processing",
        "identified_commit": None,
        "is_false_positive": True,
        "quality_keywords": [
            "nightly", "ETL", "batch", "expected", "normal", "threshold",
            "false positive", "tune",
        ],
    },
)

# All scenarios for iteration
ALL_SCENARIOS = [
    SCENARIO_CPU_SPIKE_DEPLOY,
    SCENARIO_LATENCY_NO_DEPLOY,
    SCENARIO_AMBIGUOUS_REPO,
    SCENARIO_SPARSE_CONTEXT,
    SCENARIO_FALSE_POSITIVE,
]
