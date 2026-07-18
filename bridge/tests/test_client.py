"""Tests for `bridge.client._parse_sse_text` and `_invoke_aws` SSE handling.

Covers the AgentCore streaming contract: the agent returns responses as
`data: ...\\n\\n` SSE frames, which the bridge parses into clean
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
    # ctx now carries a per-invocation Gateway JWT minted by gateway_jwt.
    # Asserting on its presence + tenant_id claim is
    # done in test_invoke_injects_gateway_jwt below; here we just check
    # that the rest of the payload shape is unchanged.
    assert payload["tenant_id"] == "demo"
    assert payload["prompt"] == "hi"
    assert "gateway_jwt" in payload["ctx"]


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
    # gateway_jwt is added but the original ctx fields are preserved.
    assert "gateway_jwt" in payload["ctx"]
    # runtimeUserId should be propagated from ctx.user_id
    assert fake_client.invoke_agent_runtime.call_args.kwargs["runtimeUserId"] == "u1"


# ---------------------------------------------------------------------------
# Gateway JWT injection
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_invoke_injects_gateway_jwt_with_correct_tenant_claim():
    """Every invoke() call must mint a fresh JWT carrying the call's
    tenant_id, so the AgentCore Gateway interceptor can route to the
    right target."""
    import jwt as pyjwt

    from bridge.gateway_jwt import JWT_AUDIENCE, get_jwks

    fake_client = MagicMock()
    fake_client.invoke_agent_runtime.return_value = {"response": iter([b'data: "ok"\n\n'])}
    client = AgentCoreClient(runtime_arn="arn:aws:bedrock-agentcore:us-west-2:0:runtime/test")

    with patch("boto3.client", return_value=fake_client):
        await client.invoke(tenant_id="slack-acme", prompt="hi", ctx={})

    payload = json.loads(fake_client.invoke_agent_runtime.call_args.kwargs["payload"].decode("utf-8"))
    token = payload["ctx"]["gateway_jwt"]
    assert token.count(".") == 2

    # Verify against the published JWKS — the same path the Gateway uses.
    jwks = get_jwks()
    public_key = pyjwt.PyJWK(jwks["keys"][0]).key
    claims = pyjwt.decode(
        token,
        public_key,
        algorithms=["RS256"],
        audience=JWT_AUDIENCE,
        issuer="http://localhost:8000",
    )
    assert claims["tenant_id"] == "slack-acme"
    assert claims["sub"] == "slack-acme"


@pytest.mark.asyncio
async def test_invoke_jwt_failure_does_not_block_call(monkeypatch):
    """If JWT minting blows up (misconfigured prod), invoke() must still
    deliver the request — BYO calls will fail at the Gateway with 401,
    which is louder than silently dropping the user's prompt."""
    from bridge import client as client_module

    def boom(*_args, **_kwargs):
        raise RuntimeError("missing key")

    monkeypatch.setattr(client_module, "mint_token", boom)

    fake_client = MagicMock()
    fake_client.invoke_agent_runtime.return_value = {"response": iter([b'data: "still here"\n\n'])}
    client = AgentCoreClient(runtime_arn="arn:aws:bedrock-agentcore:us-west-2:0:runtime/test")

    with patch("boto3.client", return_value=fake_client):
        result = await client.invoke(tenant_id="slack-acme", prompt="hi", ctx={})

    assert result == "still here"
    payload = json.loads(fake_client.invoke_agent_runtime.call_args.kwargs["payload"].decode("utf-8"))
    assert "gateway_jwt" not in payload["ctx"]


@pytest.mark.asyncio
async def test_invoke_does_not_mutate_caller_ctx():
    """The caller's ctx dict must not be modified in place — invoke()
    builds its own copy so background tasks dispatching multiple calls
    don't see cross-contamination."""
    fake_client = MagicMock()
    fake_client.invoke_agent_runtime.return_value = {"response": iter([b'data: "ok"\n\n'])}
    client = AgentCoreClient(runtime_arn="arn:aws:bedrock-agentcore:us-west-2:0:runtime/test")

    caller_ctx = {"user_id": "u1"}
    with patch("boto3.client", return_value=fake_client):
        await client.invoke(tenant_id="slack-acme", prompt="hi", ctx=caller_ctx)

    assert "gateway_jwt" not in caller_ctx
    assert caller_ctx == {"user_id": "u1"}
