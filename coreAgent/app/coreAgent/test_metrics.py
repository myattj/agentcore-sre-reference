"""Unit tests for metrics.py — the per-tenant EMF emitter.

Run from this directory with:

    uv run python -m unittest test_metrics

The agent has no other unit test infrastructure today (end-to-end coverage
lives in ``scripts/smoke.py``), so this file is deliberately standalone:
plain ``unittest``, no conftest, no fixtures beyond what the module itself
exports. ``reset_emitter_for_tests()`` clears the factory singleton between
tests so env-var wiring can be re-exercised.

What we validate:
  - ``EMFMetricsEmitter`` writes a JSON line with the correct ``_aws`` block,
    dimension-set, metric list, and value fields for both invocations and
    tool calls.
  - Cost cents in the EMF output matches ``pricing.compute_cost_cents`` —
    ensures the metric is the same single source of truth as the spend
    tracker uses for cost caps.
  - Error records set ``InvocationErrors``/``ToolCallErrors`` to 1 when
    ``success=False`` and 0 otherwise.
  - Zero-token invocations (e.g. cost-capped rejections) skip the second
    record instead of planting a 0/0/0 cost series.
  - ``NullMetricsEmitter`` drops silently.
  - ``InMemoryMetricsEmitter`` captures records for smoke-test assertions.
  - ``build_metrics_emitter()`` respects ``LOCAL_AUDIT=memory`` and
    ``AGENT_LOCAL_STORES=1`` env vars.
"""
from __future__ import annotations

import io
import json
import os
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from metrics import (
    DEFAULT_NAMESPACE,
    EMFMetricsEmitter,
    InMemoryMetricsEmitter,
    NullMetricsEmitter,
    build_metrics_emitter,
    reset_emitter_for_tests,
)
from pricing import compute_cost_cents


def _capture_emf(fn, *args, **kwargs) -> list[dict]:
    """Run `fn` and return every JSON line it wrote to stdout, parsed."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        fn(*args, **kwargs)
    lines = [line for line in buf.getvalue().splitlines() if line.strip()]
    return [json.loads(line) for line in lines]


class EMFEmitterInvocationTest(unittest.TestCase):
    """Validate the EMF shape for invocation records."""

    def setUp(self) -> None:
        self.emitter = EMFMetricsEmitter(namespace="Test/Agent")

    def test_happy_path_emits_two_records(self) -> None:
        records = _capture_emf(
            self.emitter.emit_invocation,
            tenant_id="acme",
            model_id="global.anthropic.claude-sonnet-4-6",
            input_tokens=1000,
            output_tokens=500,
            duration_ms=1234,
            success=True,
            invocation_id="inv-abc",
            channel_id="C123",
            workspace_id="T123",
        )
        # Two records: tenant-only (counts/duration) + (tenant, model) (tokens/cost).
        self.assertEqual(len(records), 2)

        r1, r2 = records

        # Record 1 — tenant-only dimension set.
        aws1 = r1["_aws"]
        self.assertEqual(aws1["CloudWatchMetrics"][0]["Namespace"], "Test/Agent")
        self.assertEqual(aws1["CloudWatchMetrics"][0]["Dimensions"], [["tenant_id"]])
        names1 = [m["Name"] for m in aws1["CloudWatchMetrics"][0]["Metrics"]]
        self.assertEqual(
            sorted(names1),
            ["InvocationDurationMs", "InvocationErrors", "Invocations"],
        )
        self.assertEqual(r1["tenant_id"], "acme")
        self.assertEqual(r1["Invocations"], 1)
        self.assertEqual(r1["InvocationErrors"], 0)  # success=True
        self.assertEqual(r1["InvocationDurationMs"], 1234)
        # Non-dimension properties for Logs Insights:
        self.assertEqual(r1["invocation_id"], "inv-abc")
        self.assertEqual(r1["channel_id"], "C123")
        self.assertEqual(r1["workspace_id"], "T123")
        self.assertEqual(r1["model_id"], "global.anthropic.claude-sonnet-4-6")
        self.assertTrue(r1["success"])

        # Record 2 — (tenant, model) dimension set for tokens + cost.
        aws2 = r2["_aws"]
        self.assertEqual(aws2["CloudWatchMetrics"][0]["Dimensions"], [["tenant_id", "model_id"]])
        names2 = [m["Name"] for m in aws2["CloudWatchMetrics"][0]["Metrics"]]
        self.assertEqual(
            sorted(names2),
            ["EstimatedCostCents", "InputTokens", "OutputTokens"],
        )
        self.assertEqual(r2["tenant_id"], "acme")
        self.assertEqual(r2["model_id"], "global.anthropic.claude-sonnet-4-6")
        self.assertEqual(r2["InputTokens"], 1000)
        self.assertEqual(r2["OutputTokens"], 500)
        # Cost must match the shared pricing module exactly.
        expected_cost = compute_cost_cents(
            "global.anthropic.claude-sonnet-4-6", 1000, 500
        )
        self.assertEqual(r2["EstimatedCostCents"], expected_cost)

    def test_error_sets_error_count_to_one(self) -> None:
        records = _capture_emf(
            self.emitter.emit_invocation,
            tenant_id="acme",
            model_id="global.anthropic.claude-sonnet-4-6",
            input_tokens=100,
            output_tokens=50,
            duration_ms=500,
            success=False,
        )
        self.assertEqual(records[0]["InvocationErrors"], 1)
        self.assertEqual(records[0]["Invocations"], 1)
        self.assertFalse(records[0]["success"])

    def test_zero_tokens_skips_cost_record(self) -> None:
        """Cost-capped rejections have 0 tokens and must not plant a 0/0/0 series."""
        records = _capture_emf(
            self.emitter.emit_invocation,
            tenant_id="acme",
            model_id="global.anthropic.claude-sonnet-4-6",
            input_tokens=0,
            output_tokens=0,
            duration_ms=5,
            success=True,
        )
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["Invocations"], 1)

    def test_default_namespace(self) -> None:
        emitter = EMFMetricsEmitter()
        self.assertEqual(emitter.namespace, DEFAULT_NAMESPACE)

    def test_env_var_cannot_disconnect_namespace_contract(self) -> None:
        with patch.dict(os.environ, {"METRICS_NAMESPACE": "Override/NS"}):
            emitter = EMFMetricsEmitter()
            self.assertEqual(emitter.namespace, DEFAULT_NAMESPACE)


class EMFEmitterToolCallTest(unittest.TestCase):
    """Validate the EMF shape for tool-call records."""

    def setUp(self) -> None:
        self.emitter = EMFMetricsEmitter(namespace="Test/Agent")

    def test_tool_call_happy_path(self) -> None:
        records = _capture_emf(
            self.emitter.emit_tool_call,
            tenant_id="acme",
            tool_name="search_team_history",
            duration_ms=42,
            success=True,
            invocation_id="inv-abc",
        )
        self.assertEqual(len(records), 1)
        r = records[0]
        self.assertEqual(
            r["_aws"]["CloudWatchMetrics"][0]["Dimensions"],
            [["tenant_id", "tool_name"]],
        )
        names = [m["Name"] for m in r["_aws"]["CloudWatchMetrics"][0]["Metrics"]]
        self.assertEqual(
            sorted(names),
            ["ToolCallDurationMs", "ToolCallErrors", "ToolCalls"],
        )
        self.assertEqual(r["tenant_id"], "acme")
        self.assertEqual(r["tool_name"], "search_team_history")
        self.assertEqual(r["ToolCalls"], 1)
        self.assertEqual(r["ToolCallErrors"], 0)
        self.assertEqual(r["ToolCallDurationMs"], 42)
        self.assertEqual(r["invocation_id"], "inv-abc")

    def test_tool_call_error_sets_error_count(self) -> None:
        records = _capture_emf(
            self.emitter.emit_tool_call,
            tenant_id="acme",
            tool_name="search_team_history",
            duration_ms=42,
            success=False,
        )
        self.assertEqual(records[0]["ToolCallErrors"], 1)
        self.assertFalse(records[0]["success"])


class NullEmitterTest(unittest.TestCase):
    def test_drops_silently(self) -> None:
        emitter = NullMetricsEmitter()
        records = _capture_emf(
            emitter.emit_invocation,
            tenant_id="acme",
            model_id="x",
            input_tokens=10,
            output_tokens=5,
            duration_ms=1,
            success=True,
        )
        self.assertEqual(records, [])

        records = _capture_emf(
            emitter.emit_tool_call,
            tenant_id="acme",
            tool_name="t",
            duration_ms=1,
            success=True,
        )
        self.assertEqual(records, [])


class InMemoryEmitterTest(unittest.TestCase):
    def test_captures_invocation_and_tool_records(self) -> None:
        emitter = InMemoryMetricsEmitter()
        emitter.emit_invocation(
            tenant_id="acme",
            model_id="global.anthropic.claude-sonnet-4-6",
            input_tokens=100,
            output_tokens=200,
            duration_ms=50,
            success=True,
        )
        emitter.emit_tool_call(
            tenant_id="acme",
            tool_name="echo",
            duration_ms=5,
            success=True,
        )
        records = emitter.records_for("acme")
        self.assertEqual(len(records), 2)
        inv = records[0]
        self.assertEqual(inv["kind"], "invocation")
        expected = compute_cost_cents(
            "global.anthropic.claude-sonnet-4-6", 100, 200
        )
        self.assertEqual(inv["estimated_cost_cents"], expected)
        tool = records[1]
        self.assertEqual(tool["kind"], "tool_call")
        self.assertEqual(tool["tool_name"], "echo")

    def test_tenant_isolation(self) -> None:
        emitter = InMemoryMetricsEmitter()
        emitter.emit_invocation(
            tenant_id="a", model_id="x",
            input_tokens=1, output_tokens=1,
            duration_ms=1, success=True,
        )
        emitter.emit_invocation(
            tenant_id="b", model_id="x",
            input_tokens=1, output_tokens=1,
            duration_ms=1, success=True,
        )
        self.assertEqual(len(emitter.records_for("a")), 1)
        self.assertEqual(len(emitter.records_for("b")), 1)
        self.assertEqual(len(emitter.all_records()), 2)


class FactoryEnvVarTest(unittest.TestCase):
    """The factory's env-var routing must match audit.py / spend_tracker.py."""

    def setUp(self) -> None:
        reset_emitter_for_tests()

    def tearDown(self) -> None:
        reset_emitter_for_tests()

    def test_local_audit_memory_returns_in_memory(self) -> None:
        with patch.dict(os.environ, {"LOCAL_AUDIT": "memory"}, clear=False):
            # Ensure AGENT_LOCAL_STORES is unset so it doesn't win.
            os.environ.pop("AGENT_LOCAL_STORES", None)
            emitter = build_metrics_emitter()
            self.assertIsInstance(emitter, InMemoryMetricsEmitter)

    def test_agent_local_stores_returns_null(self) -> None:
        with patch.dict(os.environ, {"AGENT_LOCAL_STORES": "1"}, clear=False):
            os.environ.pop("LOCAL_AUDIT", None)
            emitter = build_metrics_emitter()
            self.assertIsInstance(emitter, NullMetricsEmitter)

    def test_default_returns_emf(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LOCAL_AUDIT", None)
            os.environ.pop("AGENT_LOCAL_STORES", None)
            emitter = build_metrics_emitter()
            self.assertIsInstance(emitter, EMFMetricsEmitter)

    def test_singleton_caching(self) -> None:
        reset_emitter_for_tests()
        with patch.dict(os.environ, {"AGENT_LOCAL_STORES": "1"}, clear=False):
            os.environ.pop("LOCAL_AUDIT", None)
            e1 = build_metrics_emitter()
            e2 = build_metrics_emitter()
            self.assertIs(e1, e2)


if __name__ == "__main__":
    unittest.main()
