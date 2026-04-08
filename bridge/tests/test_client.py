"""Tests for `bridge.client._parse_sse_text` and `_invoke_aws` SSE handling.

Covers the BUILD_PLAN week-2 carryover fix: the agent streams responses
as `data: ...\\n\\n` SSE frames; the bridge must parse them into clean
text for downstream consumers.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from bridge.client import AgentCoreClient, _parse_sse_text


# ---------------------------------------------------------------------------
# _parse_sse_text — pure parser
# ---------------------------------------------------------------------------

def test_parse_empty_string_returns_empty():
    assert _parse_sse_text("") == ""


def test_parse_single_text_frame():
    raw = 'data: "hello world"\n\n'
    assert _parse_sse_text(raw) == "hello world"


def test_parse_multiple_text_frames_concatenates():
    raw = 'data: "hello "\n\ndata: "world"\n\n'
    assert _parse_sse_text(raw) == "hello world"


def test_parse_mixed_text_and_telemetry_drops_telemetry():
    # Strands occasionally yields dict telemetry events alongside text
    # chunks. Telemetry dicts are JSON objects, not strings, and should
    # be dropped (only the assistant text matters for the buffered reply).
    raw = (
        'data: "hello"\n\n'
        'data: {"usage": {"inputTokens": 10}}\n\n'
        'data: " world"\n\n'
    )
    assert _parse_sse_text(raw) == "hello world"


def test_parse_wrapped_data_dict_form():
    # Some runtime versions wrap chunks as {"data": "chunk"} instead of
    # raw JSON strings. The parser handles both shapes.
    raw = 'data: {"data": "hello"}\n\ndata: {"data": " world"}\n\n'
    assert _parse_sse_text(raw) == "hello world"


def test_parse_drops_non_data_lines():
    # SSE supports event:, id:, retry:, and comments. We only consume `data:`.
    raw = (
        ':comment line\n'
        'event: chunk\n'
        'data: "real"\n'
        '\n'
        'id: 1\n'
        'data: " text"\n'
        '\n'
    )
    assert _parse_sse_text(raw) == "real text"


def test_parse_empty_data_lines_skipped():
    raw = 'data: \n\ndata: "ok"\n\n'
    assert _parse_sse_text(raw) == "ok"


def test_parse_invalid_json_falls_through_as_raw():
    # Defensive: if a frame isn't valid JSON, append it raw rather than
    # dropping data.
    raw = 'data: not json here\n\n'
    assert _parse_sse_text(raw) == "not json here"


def test_parse_unicode_string():
    raw = 'data: "héllo \\u00e9"\n\n'
    assert _parse_sse_text(raw) == "héllo é"


# ---------------------------------------------------------------------------
# _invoke_aws — boto3 streaming-body integration
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_invoke_aws_parses_streaming_body_iterator():
    """A boto3 EventStream-style iterator yielding bytes chunks should be
    decoded and SSE-parsed."""
    fake_stream = [
        b'data: "hello"\n\n',
        b'data: " world"\n\n',
    ]

    fake_response = {"response": iter(fake_stream)}
    fake_client = MagicMock()
    fake_client.invoke_agent_runtime.return_value = fake_response

    client = AgentCoreClient(runtime_arn="arn:aws:bedrock-agentcore:us-west-2:0:runtime/test")

    with patch("boto3.client", return_value=fake_client):
        result = await client.invoke(tenant_id="demo", prompt="hi", ctx={})

    assert result == "hello world"
    fake_client.invoke_agent_runtime.assert_called_once()
    call_kwargs = fake_client.invoke_agent_runtime.call_args.kwargs
    assert call_kwargs["agentRuntimeArn"] == "arn:aws:bedrock-agentcore:us-west-2:0:runtime/test"
    payload = json.loads(call_kwargs["payload"].decode("utf-8"))
    assert payload == {"tenant_id": "demo", "prompt": "hi", "ctx": {}}


@pytest.mark.asyncio
async def test_invoke_aws_parses_streaming_body_read():
    """A StreamingBody-style object with .read() should also work."""
    class FakeBody:
        def read(self) -> bytes:
            return b'data: "single"\n\ndata: " chunk"\n\n'

    fake_client = MagicMock()
    fake_client.invoke_agent_runtime.return_value = {"response": FakeBody()}

    client = AgentCoreClient(runtime_arn="arn:aws:bedrock-agentcore:us-west-2:0:runtime/test")

    with patch("boto3.client", return_value=fake_client):
        result = await client.invoke(tenant_id="demo", prompt="hi", ctx={"user_id": "u1"})

    assert result == "single chunk"
    payload = json.loads(fake_client.invoke_agent_runtime.call_args.kwargs["payload"].decode("utf-8"))
    assert payload["ctx"]["user_id"] == "u1"
    # runtimeUserId should be propagated from ctx.user_id
    assert fake_client.invoke_agent_runtime.call_args.kwargs["runtimeUserId"] == "u1"
