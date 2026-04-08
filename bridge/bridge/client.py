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
import os
from typing import Any

import httpx


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
    string literal like `\"hello\"`. For dict yields (telemetry events),
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
    for raw_event in text.split("\n\n"):
        for line in raw_event.split("\n"):
            if not line.startswith("data:"):
                continue
            payload = line[len("data:"):].lstrip()
            if not payload:
                continue
            try:
                decoded = json.loads(payload)
            except json.JSONDecodeError:
                # Format drift: append the raw line, minus the "data:" prefix.
                chunks.append(payload)
                continue
            if isinstance(decoded, str):
                chunks.append(decoded)
            elif isinstance(decoded, dict) and "data" in decoded and isinstance(decoded["data"], str):
                # Some runtime versions wrap chunks as {"data": "chunk"}.
                chunks.append(decoded["data"])
            # Anything else (telemetry dicts, lists, numbers) is dropped.
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

    async def invoke(
        self,
        *,
        tenant_id: str,
        prompt: str,
        ctx: dict[str, Any] | None = None,
    ) -> str:
        payload = {"tenant_id": tenant_id, "prompt": prompt, "ctx": ctx or {}}

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

        client = boto3.client("bedrock-agentcore", region_name=self.region)

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
