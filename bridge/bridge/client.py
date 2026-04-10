"""Client for invoking the coreAgent on AgentCore Runtime.

Two modes:
  - **Production**: boto3 `bedrock-agentcore.invoke_agent_runtime` against
    a real deployed runtime. Requires AGENT_RUNTIME_ARN env var.
  - **Local dev**: HTTP POST to the `agentcore dev` server. Activated when
    LOCAL_AGENT_URL is set. Bypasses AWS entirely.

Both transports return the agent's stream as Server-Sent Events
(`data: <payload>\\n\\n` frames). Strands' `agent.stream_async` yields
strings; the AgentCore runtime JSON-encodes each yielded value into one
SSE `data:` line. We buffer the full stream and concatenate the decoded
payloads before returning, since downstream consumers (Slack
chat.postMessage, the debug adapter) post a complete message rather than
forwarding the SSE stream.

Anything yielded that isn't a string (e.g. a dict telemetry event) is
ignored — only the assistant text is collected.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncGenerator
from typing import Any

import httpx

from .gateway_jwt import mint_token

log = logging.getLogger(__name__)


def _parse_sse_frame(line: str) -> str | None:
    """Parse a single SSE ``data:`` line and return the text payload, or None.

    Returns a string for assistant text chunks. Returns None for non-data
    lines, empty payloads, and telemetry dicts. Falls back to the raw
    payload on JSON decode errors (defensive against format drift).
    """
    if not line.startswith("data:"):
        return None
    payload = line[len("data:"):].lstrip()
    if not payload:
        return None
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError:
        return payload
    if isinstance(decoded, str):
        return decoded
    if isinstance(decoded, dict) and "data" in decoded and isinstance(decoded["data"], str):
        return decoded["data"]
    return None


def _parse_sse_text(text: str) -> str:
    """Parse SSE-framed text from the agent and return the concatenated
    assistant payload.

    The frame format is the standard SSE shape:

        data: <payload>\\n
        \\n
        data: <payload>\\n
        \\n

    Each `<payload>` is JSON-encoded by the runtime. For string yields
    (the common case for `agent.stream_async`), we get back a JSON
    string literal like ``"hello"``. For dict yields (telemetry events),
    we get back a JSON object — those we drop.

    The parser is intentionally lenient:
      - Empty events are skipped
      - Non-string JSON values are dropped
      - Lines that don't parse as JSON are appended raw (defensive against
        format drift between AgentCore SDK versions)
    """
    if not text:
        return ""
    chunks: list[str] = []
    for line in text.split("\n"):
        chunk = _parse_sse_frame(line)
        if chunk is not None:
            chunks.append(chunk)
    return "".join(chunks)


class AgentCoreClient:
    def __init__(
        self,
        runtime_arn: str | None = None,
        local_agent_url: str | None = None,
        region: str = "us-west-2",
    ) -> None:
        self.runtime_arn = runtime_arn or os.getenv("AGENT_RUNTIME_ARN")
        self.local_agent_url = local_agent_url or os.getenv("LOCAL_AGENT_URL")
        self.region = region

        if not self.runtime_arn and not self.local_agent_url:
            raise RuntimeError(
                "AgentCoreClient needs either AGENT_RUNTIME_ARN (production) "
                "or LOCAL_AGENT_URL (local dev) — neither is set."
            )

    def _prepare_payload(
        self,
        tenant_id: str,
        prompt: str,
        ctx: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Build the invocation payload, including the per-invocation Gateway JWT."""
        ctx = dict(ctx or {})
        try:
            ctx["gateway_jwt"] = mint_token(tenant_id)
        except Exception as e:  # noqa: BLE001
            log.warning(
                "invoke: failed to mint Gateway JWT for tenant=%s: %s "
                "(BYO tool calls will be unauthenticated)",
                tenant_id,
                e,
            )
        return {"tenant_id": tenant_id, "prompt": prompt, "ctx": ctx}

    async def invoke(
        self,
        *,
        tenant_id: str,
        prompt: str,
        ctx: dict[str, Any] | None = None,
    ) -> str:
        """Invoke the agent and return the full buffered response."""
        payload = self._prepare_payload(tenant_id, prompt, ctx)
        if self.local_agent_url:
            return await self._invoke_local(payload)
        return await self._invoke_aws(payload)

    async def _invoke_local(self, payload: dict[str, Any]) -> str:
        """Local dev path: POST to agentcore dev's /invocations endpoint.

        The local server streams responses as SSE; we buffer the full body
        and parse the frames. Use a long timeout because long-running tools
        can hold connections for several minutes.
        """
        url = f"{self.local_agent_url.rstrip('/')}/invocations"
        async with httpx.AsyncClient(timeout=600.0) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            return _parse_sse_text(response.text)

    async def _invoke_aws(self, payload: dict[str, Any]) -> str:
        """Production path: boto3 invoke_agent_runtime, wrapped in an
        executor since boto3 is sync.

        boto3 parameter names (verified against bedrock-agentcore service
        model 2025-q4):
          - agentRuntimeArn  (required)  — the runtime ARN
          - payload          (required)  — bytes; JSON-encoded application payload
          - runtimeSessionId (optional, but ≥33 chars when present)
          - runtimeUserId    (optional)  — propagated for audit / tracing
        """
        # Lazy import so local dev doesn't require boto3 to be importable.
        import boto3

        from botocore.config import Config

        client = boto3.client(
            "bedrock-agentcore",
            region_name=self.region,
            config=Config(read_timeout=600),
        )

        # Pull a stable user_id and session_id from the payload's ctx if
        # present, so multi-turn / per-user tracing works once we add it.
        ctx = payload.get("ctx") or {}
        runtime_user_id = ctx.get("user_id") or "default-user"

        def _call() -> str:
            kwargs: dict[str, Any] = {
                "agentRuntimeArn": self.runtime_arn,
                "payload": json.dumps(payload).encode("utf-8"),
                "runtimeUserId": runtime_user_id,
            }
            response = client.invoke_agent_runtime(**kwargs)

            # Response is a StreamingBody / EventStream of SSE-framed bytes
            # (the agent's @app.entrypoint streams chunks via Strands'
            # `stream_async`, which the runtime wraps as
            # `data: <json>\\n\\n`). Collect into one string then parse.
            # Downstream we may switch to incremental forwarding once
            # Slack streaming lands.
            body = response.get("response") or response.get("body")
            if hasattr(body, "read"):
                raw = body.read()
                raw_text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)
            elif hasattr(body, "__iter__"):
                raw_text = "".join(
                    (chunk.decode("utf-8") if isinstance(chunk, (bytes, bytearray)) else str(chunk))
                    for chunk in body
                )
            else:
                raw_text = str(body)
            return _parse_sse_text(raw_text)

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _call)

    # ------------------------------------------------------------------
    # Streaming path — yields chunks as they arrive from the SSE stream
    # ------------------------------------------------------------------

    async def invoke_stream(
        self,
        *,
        tenant_id: str,
        prompt: str,
        ctx: dict[str, Any] | None = None,
    ) -> AsyncGenerator[str, None]:
        """Invoke the agent and yield text chunks as they arrive."""
        payload = self._prepare_payload(tenant_id, prompt, ctx)
        if self.local_agent_url:
            gen = self._invoke_stream_local(payload)
        else:
            gen = self._invoke_stream_aws(payload)
        async for chunk in gen:
            yield chunk

    async def _invoke_stream_local(self, payload: dict[str, Any]) -> AsyncGenerator[str, None]:
        """Local dev streaming: httpx streaming response, parse SSE lines."""
        url = f"{self.local_agent_url.rstrip('/')}/invocations"
        async with httpx.AsyncClient(timeout=600.0) as http:
            async with http.stream("POST", url, json=payload) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    chunk = _parse_sse_frame(line)
                    if chunk is not None:
                        yield chunk

    async def _invoke_stream_aws(self, payload: dict[str, Any]) -> AsyncGenerator[str, None]:
        """Production streaming: boto3 StreamingBody → asyncio.Queue → yield.

        boto3 is synchronous, so we read the stream in an executor thread
        and push parsed chunks through a queue to the async consumer.
        """
        import boto3

        queue: asyncio.Queue[str | Exception | None] = asyncio.Queue()
        loop = asyncio.get_event_loop()

        ctx = payload.get("ctx") or {}
        runtime_user_id = ctx.get("user_id") or "default-user"

        def _read_stream() -> None:
            try:
                from botocore.config import Config

                # Agent may pause for extended thinking or tool execution;
                # the default 60s read timeout is not enough.
                client = boto3.client(
                    "bedrock-agentcore",
                    region_name=self.region,
                    config=Config(read_timeout=600),
                )
                kwargs: dict[str, Any] = {
                    "agentRuntimeArn": self.runtime_arn,
                    "payload": json.dumps(payload).encode("utf-8"),
                    "runtimeUserId": runtime_user_id,
                }
                response = client.invoke_agent_runtime(**kwargs)
                body = response.get("response") or response.get("body")

                if hasattr(body, "iter_lines"):
                    for raw_line in body.iter_lines():
                        line = raw_line.decode("utf-8") if isinstance(raw_line, (bytes, bytearray)) else str(raw_line)
                        if not line:
                            continue
                        chunk = _parse_sse_frame(line)
                        if chunk is not None:
                            loop.call_soon_threadsafe(queue.put_nowait, chunk)
                elif hasattr(body, "__iter__"):
                    leftover = ""
                    for raw_bytes in body:
                        text = raw_bytes.decode("utf-8") if isinstance(raw_bytes, (bytes, bytearray)) else str(raw_bytes)
                        text = leftover + text
                        lines = text.split("\n")
                        leftover = lines.pop()
                        for line in lines:
                            if not line:
                                continue
                            chunk = _parse_sse_frame(line)
                            if chunk is not None:
                                loop.call_soon_threadsafe(queue.put_nowait, chunk)
                    if leftover:
                        chunk = _parse_sse_frame(leftover)
                        if chunk is not None:
                            loop.call_soon_threadsafe(queue.put_nowait, chunk)
                elif hasattr(body, "read"):
                    raw = body.read()
                    text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)
                    for line in text.split("\n"):
                        if not line:
                            continue
                        chunk = _parse_sse_frame(line)
                        if chunk is not None:
                            loop.call_soon_threadsafe(queue.put_nowait, chunk)
            except Exception as exc:
                loop.call_soon_threadsafe(queue.put_nowait, exc)
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        future = loop.run_in_executor(None, _read_stream)

        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                if isinstance(item, Exception):
                    raise item
                yield item
        finally:
            await future
