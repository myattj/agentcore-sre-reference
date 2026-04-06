"""Client for invoking the coreAgent on AgentCore Runtime.

Two modes:
  - **Production**: boto3 `bedrock-agentcore.invoke_agent_runtime` against
    a real deployed runtime. Requires AGENT_RUNTIME_ARN env var.
  - **Local dev**: HTTP POST to the `agentcore dev` server. Activated when
    LOCAL_AGENT_URL is set. Bypasses AWS entirely.

The agent streams its response. We buffer the full text into a single
string before returning, since downstream consumers (Slack chat.postMessage,
the debug adapter) post a complete message rather than streaming.
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import httpx


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

        The local server streams responses; we collect chunks into a single
        string. Use a long timeout because long-running tools can hold
        connections for several minutes.
        """
        url = f"{self.local_agent_url.rstrip('/')}/invocations"
        async with httpx.AsyncClient(timeout=600.0) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            return response.text

    async def _invoke_aws(self, payload: dict[str, Any]) -> str:
        """Production path: boto3 invoke_agent_runtime, wrapped in an
        executor since boto3 is sync."""
        # Lazy import so local dev doesn't require boto3 to be importable.
        import boto3

        client = boto3.client("bedrock-agentcore", region_name=self.region)

        def _call() -> str:
            response = client.invoke_agent_runtime(
                runtimeArn=self.runtime_arn,
                payload=json.dumps(payload),
            )
            # Response includes a streaming body; collect into one string.
            body = response.get("response") or response.get("body")
            if hasattr(body, "read"):
                return body.read().decode("utf-8")
            return str(body)

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _call)
