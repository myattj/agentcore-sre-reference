"""`/api/tenants/*` routes consumed by the onboarding UI.

Three endpoints, all tenant-scoped and authenticated via the session
token the OAuth callback mints (`slack_oauth.make_session_token`):

  - `GET  /api/tenants/{tenant_id}`           — return the current config
  - `PATCH /api/tenants/{tenant_id}`          — deep-merge a partial config
  - `GET  /api/tenants/{tenant_id}/channels`  — list Slack channels the bot is in

Authentication flow:
  1. Client sends `Authorization: Bearer <session_token>`
  2. `require_session_token` extracts the token, calls
     `verify_session_token` (HMAC check + TTL), and asserts the token's
     embedded tenant_id matches the URL path param.
  3. 401 on missing/invalid/expired token; 403 on tenant mismatch.

Cross-tenant isolation is enforced here and nowhere else. Every write
path (`update_tenant_row`) runs only after the dependency resolves, so
the bridge will never write to a tenant whose token doesn't match.

**No CORS middleware.** All callers are server-side Next.js code in the
onboarding service. The browser never talks to these routes directly.
Add `CORSMiddleware` only if a client-side caller (admin dashboard,
etc.) ever needs direct access.
"""
from __future__ import annotations

import logging
import os
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Path
from slack_sdk.errors import SlackApiError

from .api_models import (
    ChannelsResponse,
    CodebaseBindingBrief,
    ConfluenceConnectRequest,
    DatadogConnectRequest,
    GitHubAppInstallRequest,
    GitHubAppInstallResponse,
    GitHubConnectRequest,
    IntegrationConnectResponse,
    JiraConnectRequest,
    LinearConnectRequest,
    NotionConnectRequest,
    PagerDutyConnectRequest,
    TenantConfigOut,
    TenantConfigPatch,
)
from .slack_channels import list_channels_for_tenant
from .slack_oauth import verify_session_token
from .tenant_write import deep_merge, get_tenant_row, update_tenant_row

log = logging.getLogger(__name__)

api_router = APIRouter(prefix="/api", tags=["api"])


def _region() -> str:
    return os.getenv("AWS_REGION", "us-west-2")


# ----------------------------------------------------------------------------
# Auth dependency
# ----------------------------------------------------------------------------

def require_session_token(
    tenant_id: Annotated[str, Path()],
    authorization: Annotated[str, Header()] = "",
) -> str:
    """Extract a Bearer token from `Authorization`, verify it, and
    assert its embedded tenant matches the URL path's `tenant_id`.

    Returns the verified tenant_id on success. Raises 401 for any token
    problem (missing, malformed, expired, bad signature) and 403 when
    the token is valid but for a different tenant.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = authorization[len("Bearer "):].strip()
    verified = verify_session_token(token)
    if verified is None:
        raise HTTPException(status_code=401, detail="invalid or expired session")
    if verified != tenant_id:
        # Don't leak the token's tenant in the response — just say 403.
        log.warning(
            "api auth: token tenant mismatch (token=%s, url=%s)",
            verified,
            tenant_id,
        )
        raise HTTPException(status_code=403, detail="tenant mismatch")
    return verified


# ----------------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------------

@api_router.get("/tenants/{tenant_id}", response_model=TenantConfigOut)
async def get_tenant(
    tenant_id: str,
    _verified: Annotated[str, Depends(require_session_token)],
) -> TenantConfigOut:
    """Return the tenant's current config."""
    try:
        row = get_tenant_row(tenant_id, _region())
    except KeyError:
        raise HTTPException(status_code=404, detail="tenant not found")
    return TenantConfigOut.model_validate(row)


@api_router.patch("/tenants/{tenant_id}", response_model=TenantConfigOut)
async def patch_tenant(
    tenant_id: str,
    patch: TenantConfigPatch,
    _verified: Annotated[str, Depends(require_session_token)],
) -> TenantConfigOut:
    """Deep-merge a partial config into the existing row.

    Semantics:
      - Top-level scalars (`model_id`, `system_prompt`) replace
      - `catalog` / `byo` / `memory` / `heartbeat` deep-merge one level
        (so sending `catalog.allowed_tools=[...]` preserves `tool_config`)
      - Missing fields in the patch body leave the existing value alone

    Refuses to create a tenant row that doesn't exist (404). Only the
    OAuth callback can bring a tenant into existence.
    """
    try:
        current = get_tenant_row(tenant_id, _region())
    except KeyError:
        raise HTTPException(status_code=404, detail="tenant not found")

    patch_dict = patch.model_dump(exclude_unset=True, exclude_none=False)
    merged = deep_merge(current, patch_dict)
    # Re-validate the merged result so any invalid combinations surface
    # as 422. This also defaults in any fields the old row was missing.
    validated = TenantConfigOut.model_validate(merged)
    # The canonical dump goes back to DDB.
    try:
        update_tenant_row(tenant_id, _region(), validated.model_dump())
    except KeyError:
        # Race condition — tenant disappeared between GET and UPDATE.
        # Vanishingly unlikely but surface as 404 rather than 500.
        raise HTTPException(status_code=404, detail="tenant not found")
    return validated


@api_router.get("/tenants/{tenant_id}/channels", response_model=ChannelsResponse)
async def get_channels(
    tenant_id: str,
    _verified: Annotated[str, Depends(require_session_token)],
) -> ChannelsResponse:
    """List Slack channels the bot is a member of for this tenant.

    Uses `users.conversations` — see `bridge/bridge/slack_channels.py`
    docstring. The empty list is a valid response (bot has not been
    invited to any channels yet).

    Graceful degradation:
      - 404 if the tenant has no bot token in Secrets Manager
      - 200 with `needs_reinstall=true` if Slack returns `missing_scope`
        (the bot token was minted under an older scope set and needs
        a re-install to grant the new scopes)
      - 502 for any other Slack API error (rate limit, network, etc.)
    """
    try:
        channels = await list_channels_for_tenant(tenant_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="no bot token for tenant")
    except SlackApiError as e:
        slack_error = (e.response.get("error") if e.response else None) or str(e)
        log.warning(
            "get_channels: slack api error for tenant=%s: %s",
            tenant_id,
            slack_error,
        )
        if slack_error == "missing_scope":
            return ChannelsResponse(channels=[], needs_reinstall=True)
        raise HTTPException(status_code=502, detail="slack api error")
    return ChannelsResponse(channels=channels)


# ----------------------------------------------------------------------------
# Integrations (week 4 chunk F)
# ----------------------------------------------------------------------------

# Minimal Datadog OpenAPI spec covering the three tools from BUILD_PLAN
# week 4: query_metrics, get_recent_alerts (monitors/search), search_logs.
# The Gateway translates OpenAPI paths into MCP tools automatically.
DATADOG_OPENAPI_SPEC = """{
  "openapi": "3.0.0",
  "info": {
    "title": "Datadog API (agent-core subset)",
    "version": "1.0.0"
  },
  "servers": [
    {"url": "https://api.{site}/api/v1", "variables": {"site": {"default": "datadoghq.com"}}}
  ],
  "paths": {
    "/query": {
      "get": {
        "operationId": "query_metrics",
        "summary": "Query timeseries metric data for a given time window.",
        "parameters": [
          {"name": "from", "in": "query", "required": true, "schema": {"type": "integer"}, "description": "Start of the queried time period as a POSIX timestamp (seconds)."},
          {"name": "to", "in": "query", "required": true, "schema": {"type": "integer"}, "description": "End of the queried time period as a POSIX timestamp (seconds)."},
          {"name": "query", "in": "query", "required": true, "schema": {"type": "string"}, "description": "Datadog metrics query string (e.g. 'avg:system.cpu.user{host:web-prod-1}')."}
        ],
        "responses": {"200": {"description": "Metric timeseries data."}}
      }
    },
    "/monitor/search": {
      "get": {
        "operationId": "get_recent_alerts",
        "summary": "Search monitors (alerts). Use to find recently triggered alerts.",
        "parameters": [
          {"name": "query", "in": "query", "schema": {"type": "string"}, "description": "Search query (e.g. 'status:Alert' or 'tag:service:web')."},
          {"name": "page", "in": "query", "schema": {"type": "integer", "default": 0}},
          {"name": "per_page", "in": "query", "schema": {"type": "integer", "default": 10}}
        ],
        "responses": {"200": {"description": "List of matching monitors."}}
      }
    },
    "/logs-queries/list": {
      "post": {
        "operationId": "search_logs",
        "summary": "Search and filter log events.",
        "requestBody": {
          "required": true,
          "content": {
            "application/json": {
              "schema": {
                "type": "object",
                "properties": {
                  "query": {"type": "string", "description": "Log search query string."},
                  "time": {
                    "type": "object",
                    "properties": {
                      "from": {"type": "string", "description": "ISO datetime or relative (e.g. 'now-1h')."},
                      "to": {"type": "string", "description": "ISO datetime or relative (e.g. 'now')."}
                    }
                  },
                  "limit": {"type": "integer", "default": 10}
                }
              }
            }
          }
        },
        "responses": {"200": {"description": "Matching log events."}}
      }
    }
  }
}"""


async def _validate_datadog_key(api_key: str, site: str) -> bool:
    """Hit Datadog's /api/v1/validate to confirm the key works.

    Returns True on success, False on auth failure. Raises on network error.
    """
    import httpx

    url = f"https://api.{site}/api/v1/validate"
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(url, headers={"DD-API-KEY": api_key})
    return resp.status_code == 200


@api_router.post(
    "/tenants/{tenant_id}/integrations/datadog",
    response_model=IntegrationConnectResponse,
)
async def connect_datadog(
    tenant_id: str,
    body: DatadogConnectRequest,
    _verified: Annotated[str, Depends(require_session_token)],
) -> IntegrationConnectResponse:
    """Connect a Datadog account to this tenant.

    Flow:
      1. Validate the API key against Datadog's /api/v1/validate
      2. Provision a credential provider + Gateway target via gateway_provisioner
      3. Enable BYO on the tenant row with the shared Gateway URL
      4. Return the connection status
    """
    # 1. Validate
    try:
        valid = await _validate_datadog_key(body.api_key, body.site)
    except Exception as e:
        log.warning("connect_datadog: validation failed for tenant=%s: %s", tenant_id, e)
        return IntegrationConnectResponse(
            ok=False, integration="datadog", error="could not reach Datadog API"
        )
    if not valid:
        return IntegrationConnectResponse(
            ok=False, integration="datadog", error="invalid Datadog API key"
        )

    # 2. Provision
    from .gateway_provisioner import provision_integration

    region = _region()
    # Inject the tenant's Datadog site into the OpenAPI spec's server URL.
    spec = DATADOG_OPENAPI_SPEC.replace("datadoghq.com", body.site)
    try:
        result = provision_integration(
            tenant_id,
            "datadog",
            api_key=body.api_key,
            app_key=body.app_key,
            openapi_spec=spec,
            credential_header_name="DD-API-KEY",
            app_key_header_name="DD-APPLICATION-KEY",
            region=region,
        )
    except Exception as e:
        log.exception("connect_datadog: provisioning failed for tenant=%s", tenant_id)
        return IntegrationConnectResponse(
            ok=False, integration="datadog", error=f"provisioning failed: {e}"
        )

    # 3. Enable BYO on the tenant row + track connection
    _enable_byo_for_integration(tenant_id, "datadog", result, region)

    return IntegrationConnectResponse(
        ok=True,
        integration="datadog",
        target_name=result["target_name"],
        gateway_url=result["gateway_url"],
    )


# ---------------------------------------------------------------------------
# Helper: enable BYO + track connected integration on tenant row
# ---------------------------------------------------------------------------

def _enable_byo_for_integration(
    tenant_id: str,
    integration: str,
    result: dict[str, Any],
    region: str,
) -> None:
    """Shared post-provisioning step: enable BYO, record the integration."""
    current = get_tenant_row(tenant_id, region)
    byo_patch: dict[str, Any] = {
        "enabled": True,
        "gateway_endpoint": result["gateway_url"],
    }
    extra = result.get("extra_headers")
    if extra:
        existing_headers: dict[str, str] = {}
        existing_auth = current.get("byo", {}).get("gateway_auth")
        if isinstance(existing_auth, dict):
            h = existing_auth.get("headers")
            if isinstance(h, dict):
                existing_headers.update(h)
        existing_headers.update(extra)
        byo_patch["gateway_auth"] = {"headers": existing_headers}

    # Track which integrations are connected
    connected = list(current.get("byo", {}).get("connected_integrations", []))
    if integration not in connected:
        connected.append(integration)
    byo_patch["connected_integrations"] = connected

    merged = deep_merge(current, {"byo": byo_patch})
    update_tenant_row(tenant_id, region, merged)


# ---------------------------------------------------------------------------
# Confluence
# ---------------------------------------------------------------------------

CONFLUENCE_OPENAPI_SPEC = """{
  "openapi": "3.0.0",
  "info": {"title": "Confluence Cloud API (agent-core subset)", "version": "1.0.0"},
  "servers": [{"url": "https://DOMAIN.atlassian.net/wiki/rest/api"}],
  "paths": {
    "/content/search": {
      "get": {
        "operationId": "search_content",
        "summary": "Search Confluence content using CQL.",
        "parameters": [
          {"name": "cql", "in": "query", "required": true, "schema": {"type": "string"}, "description": "Confluence Query Language expression (e.g. 'text ~ runbook')."},
          {"name": "limit", "in": "query", "schema": {"type": "integer", "default": 10}}
        ],
        "responses": {"200": {"description": "Search results."}}
      }
    },
    "/content/{id}": {
      "get": {
        "operationId": "get_page",
        "summary": "Get a Confluence page by ID.",
        "parameters": [
          {"name": "id", "in": "path", "required": true, "schema": {"type": "string"}},
          {"name": "expand", "in": "query", "schema": {"type": "string", "default": "body.storage"}}
        ],
        "responses": {"200": {"description": "Page content."}}
      }
    }
  }
}"""


async def _validate_confluence(email: str, api_token: str, domain: str) -> bool:
    import httpx
    import base64

    creds = base64.b64encode(f"{email}:{api_token}".encode()).decode()
    url = f"https://{domain}.atlassian.net/wiki/rest/api/content?limit=1"
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(url, headers={"Authorization": f"Basic {creds}"})
    return resp.status_code == 200


@api_router.post(
    "/tenants/{tenant_id}/integrations/confluence",
    response_model=IntegrationConnectResponse,
)
async def connect_confluence(
    tenant_id: str,
    body: ConfluenceConnectRequest,
    _verified: Annotated[str, Depends(require_session_token)],
) -> IntegrationConnectResponse:
    import base64

    try:
        valid = await _validate_confluence(body.email, body.api_token, body.domain)
    except Exception as e:
        log.warning("connect_confluence: validation failed for tenant=%s: %s", tenant_id, e)
        return IntegrationConnectResponse(ok=False, integration="confluence", error="could not reach Confluence API")
    if not valid:
        return IntegrationConnectResponse(ok=False, integration="confluence", error="invalid Confluence credentials")

    from .gateway_provisioner import provision_integration

    region = _region()
    creds = base64.b64encode(f"{body.email}:{body.api_token}".encode()).decode()
    spec = CONFLUENCE_OPENAPI_SPEC.replace("DOMAIN", body.domain)
    try:
        result = provision_integration(
            tenant_id, "confluence",
            api_key=f"Basic {creds}",
            openapi_spec=spec,
            credential_header_name="Authorization",
            region=region,
        )
    except Exception as e:
        log.exception("connect_confluence: provisioning failed for tenant=%s", tenant_id)
        return IntegrationConnectResponse(ok=False, integration="confluence", error=f"provisioning failed: {e}")

    _enable_byo_for_integration(tenant_id, "confluence", result, region)
    return IntegrationConnectResponse(ok=True, integration="confluence", target_name=result["target_name"], gateway_url=result["gateway_url"])


# ---------------------------------------------------------------------------
# Notion
# ---------------------------------------------------------------------------

NOTION_OPENAPI_SPEC = """{
  "openapi": "3.0.0",
  "info": {"title": "Notion API (agent-core subset)", "version": "1.0.0"},
  "servers": [{"url": "https://api.notion.com/v1"}],
  "paths": {
    "/search": {
      "post": {
        "operationId": "search",
        "summary": "Search across all Notion pages and databases.",
        "requestBody": {
          "required": true,
          "content": {
            "application/json": {
              "schema": {
                "type": "object",
                "properties": {
                  "query": {"type": "string", "description": "Search query text."},
                  "page_size": {"type": "integer", "default": 10}
                }
              }
            }
          }
        },
        "responses": {"200": {"description": "Search results."}}
      }
    },
    "/pages/{page_id}": {
      "get": {
        "operationId": "get_page",
        "summary": "Get a Notion page by ID.",
        "parameters": [
          {"name": "page_id", "in": "path", "required": true, "schema": {"type": "string"}}
        ],
        "responses": {"200": {"description": "Page properties."}}
      }
    }
  }
}"""


async def _validate_notion(token: str) -> bool:
    import httpx

    url = "https://api.notion.com/v1/users/me"
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(url, headers={
            "Authorization": f"Bearer {token}",
            "Notion-Version": "2022-06-28",
        })
    return resp.status_code == 200


@api_router.post(
    "/tenants/{tenant_id}/integrations/notion",
    response_model=IntegrationConnectResponse,
)
async def connect_notion(
    tenant_id: str,
    body: NotionConnectRequest,
    _verified: Annotated[str, Depends(require_session_token)],
) -> IntegrationConnectResponse:
    try:
        valid = await _validate_notion(body.integration_token)
    except Exception as e:
        log.warning("connect_notion: validation failed for tenant=%s: %s", tenant_id, e)
        return IntegrationConnectResponse(ok=False, integration="notion", error="could not reach Notion API")
    if not valid:
        return IntegrationConnectResponse(ok=False, integration="notion", error="invalid Notion integration token")

    from .gateway_provisioner import provision_integration

    region = _region()
    try:
        # Notion-Version is required on every request. We use app_key +
        # app_key_header_name so provision_integration forwards it as a
        # secondary header (same pattern as Datadog's DD-APPLICATION-KEY).
        result = provision_integration(
            tenant_id, "notion",
            api_key=f"Bearer {body.integration_token}",
            app_key="2022-06-28",
            openapi_spec=NOTION_OPENAPI_SPEC,
            credential_header_name="Authorization",
            app_key_header_name="Notion-Version",
            region=region,
        )
    except Exception as e:
        log.exception("connect_notion: provisioning failed for tenant=%s", tenant_id)
        return IntegrationConnectResponse(ok=False, integration="notion", error=f"provisioning failed: {e}")

    _enable_byo_for_integration(tenant_id, "notion", result, region)
    return IntegrationConnectResponse(ok=True, integration="notion", target_name=result["target_name"], gateway_url=result["gateway_url"])


# ---------------------------------------------------------------------------
# Jira
# ---------------------------------------------------------------------------

JIRA_OPENAPI_SPEC = """{
  "openapi": "3.0.0",
  "info": {"title": "Jira Cloud API (agent-core subset)", "version": "1.0.0"},
  "servers": [{"url": "https://DOMAIN.atlassian.net/rest/api/3"}],
  "paths": {
    "/search": {
      "get": {
        "operationId": "search_issues",
        "summary": "Search for Jira issues using JQL.",
        "parameters": [
          {"name": "jql", "in": "query", "required": true, "schema": {"type": "string"}, "description": "JQL query (e.g. 'project = OPS AND status = Open')."},
          {"name": "maxResults", "in": "query", "schema": {"type": "integer", "default": 10}},
          {"name": "fields", "in": "query", "schema": {"type": "string", "default": "summary,status,assignee,priority"}}
        ],
        "responses": {"200": {"description": "Issue search results."}}
      }
    },
    "/issue/{issueKey}": {
      "get": {
        "operationId": "get_issue",
        "summary": "Get a Jira issue by key (e.g. OPS-123).",
        "parameters": [
          {"name": "issueKey", "in": "path", "required": true, "schema": {"type": "string"}}
        ],
        "responses": {"200": {"description": "Issue details."}}
      }
    },
    "/issue": {
      "post": {
        "operationId": "create_issue",
        "summary": "Create a new Jira issue.",
        "requestBody": {
          "required": true,
          "content": {
            "application/json": {
              "schema": {
                "type": "object",
                "properties": {
                  "fields": {
                    "type": "object",
                    "properties": {
                      "project": {"type": "object", "properties": {"key": {"type": "string"}}},
                      "summary": {"type": "string"},
                      "description": {"type": "object"},
                      "issuetype": {"type": "object", "properties": {"name": {"type": "string", "default": "Task"}}}
                    },
                    "required": ["project", "summary", "issuetype"]
                  }
                }
              }
            }
          }
        },
        "responses": {"201": {"description": "Created issue."}}
      }
    }
  }
}"""


async def _validate_jira(email: str, api_token: str, domain: str) -> bool:
    import httpx
    import base64

    creds = base64.b64encode(f"{email}:{api_token}".encode()).decode()
    url = f"https://{domain}.atlassian.net/rest/api/3/myself"
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(url, headers={"Authorization": f"Basic {creds}"})
    return resp.status_code == 200


@api_router.post(
    "/tenants/{tenant_id}/integrations/jira",
    response_model=IntegrationConnectResponse,
)
async def connect_jira(
    tenant_id: str,
    body: JiraConnectRequest,
    _verified: Annotated[str, Depends(require_session_token)],
) -> IntegrationConnectResponse:
    import base64

    try:
        valid = await _validate_jira(body.email, body.api_token, body.domain)
    except Exception as e:
        log.warning("connect_jira: validation failed for tenant=%s: %s", tenant_id, e)
        return IntegrationConnectResponse(ok=False, integration="jira", error="could not reach Jira API")
    if not valid:
        return IntegrationConnectResponse(ok=False, integration="jira", error="invalid Jira credentials")

    from .gateway_provisioner import provision_integration

    region = _region()
    creds = base64.b64encode(f"{body.email}:{body.api_token}".encode()).decode()
    spec = JIRA_OPENAPI_SPEC.replace("DOMAIN", body.domain)
    try:
        result = provision_integration(
            tenant_id, "jira",
            api_key=f"Basic {creds}",
            openapi_spec=spec,
            credential_header_name="Authorization",
            region=region,
        )
    except Exception as e:
        log.exception("connect_jira: provisioning failed for tenant=%s", tenant_id)
        return IntegrationConnectResponse(ok=False, integration="jira", error=f"provisioning failed: {e}")

    _enable_byo_for_integration(tenant_id, "jira", result, region)
    return IntegrationConnectResponse(ok=True, integration="jira", target_name=result["target_name"], gateway_url=result["gateway_url"])


# ---------------------------------------------------------------------------
# Linear
# ---------------------------------------------------------------------------

LINEAR_OPENAPI_SPEC = """{
  "openapi": "3.0.0",
  "info": {"title": "Linear GraphQL API (agent-core subset)", "version": "1.0.0"},
  "servers": [{"url": "https://api.linear.app"}],
  "paths": {
    "/graphql": {
      "post": {
        "operationId": "graphql_query",
        "summary": "Execute a GraphQL query or mutation against the Linear API. Use this for searching issues, getting issue details, and creating issues.",
        "requestBody": {
          "required": true,
          "content": {
            "application/json": {
              "schema": {
                "type": "object",
                "properties": {
                  "query": {"type": "string", "description": "GraphQL query or mutation string."},
                  "variables": {"type": "object", "description": "Query variables (optional)."}
                },
                "required": ["query"]
              }
            }
          }
        },
        "responses": {"200": {"description": "GraphQL response."}}
      }
    }
  }
}"""


async def _validate_linear(api_key: str) -> bool:
    import httpx

    url = "https://api.linear.app/graphql"
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            url,
            json={"query": "{ viewer { id } }"},
            headers={"Authorization": api_key, "Content-Type": "application/json"},
        )
    return resp.status_code == 200 and "errors" not in (resp.json() or {})


@api_router.post(
    "/tenants/{tenant_id}/integrations/linear",
    response_model=IntegrationConnectResponse,
)
async def connect_linear(
    tenant_id: str,
    body: LinearConnectRequest,
    _verified: Annotated[str, Depends(require_session_token)],
) -> IntegrationConnectResponse:
    try:
        valid = await _validate_linear(body.api_key)
    except Exception as e:
        log.warning("connect_linear: validation failed for tenant=%s: %s", tenant_id, e)
        return IntegrationConnectResponse(ok=False, integration="linear", error="could not reach Linear API")
    if not valid:
        return IntegrationConnectResponse(ok=False, integration="linear", error="invalid Linear API key")

    from .gateway_provisioner import provision_integration

    region = _region()
    try:
        result = provision_integration(
            tenant_id, "linear",
            api_key=body.api_key,
            openapi_spec=LINEAR_OPENAPI_SPEC,
            credential_header_name="Authorization",
            region=region,
        )
    except Exception as e:
        log.exception("connect_linear: provisioning failed for tenant=%s", tenant_id)
        return IntegrationConnectResponse(ok=False, integration="linear", error=f"provisioning failed: {e}")

    _enable_byo_for_integration(tenant_id, "linear", result, region)
    return IntegrationConnectResponse(ok=True, integration="linear", target_name=result["target_name"], gateway_url=result["gateway_url"])


# ---------------------------------------------------------------------------
# PagerDuty
# ---------------------------------------------------------------------------

PAGERDUTY_OPENAPI_SPEC = """{
  "openapi": "3.0.0",
  "info": {"title": "PagerDuty API (agent-core subset)", "version": "1.0.0"},
  "servers": [{"url": "https://api.pagerduty.com"}],
  "paths": {
    "/incidents/{id}": {
      "get": {
        "operationId": "get_incident",
        "summary": "Get a PagerDuty incident by ID.",
        "parameters": [
          {"name": "id", "in": "path", "required": true, "schema": {"type": "string"}}
        ],
        "responses": {"200": {"description": "Incident details."}}
      }
    },
    "/oncalls": {
      "get": {
        "operationId": "get_oncall",
        "summary": "Get the current on-call user(s).",
        "parameters": [
          {"name": "escalation_policy_ids[]", "in": "query", "schema": {"type": "string"}, "description": "Filter by escalation policy ID."}
        ],
        "responses": {"200": {"description": "On-call list."}}
      }
    },
    "/incidents": {
      "get": {
        "operationId": "recent_incidents",
        "summary": "List recent PagerDuty incidents.",
        "parameters": [
          {"name": "since", "in": "query", "schema": {"type": "string"}, "description": "ISO8601 start date (e.g. '2024-01-01T00:00:00Z')."},
          {"name": "until", "in": "query", "schema": {"type": "string"}, "description": "ISO8601 end date."},
          {"name": "statuses[]", "in": "query", "schema": {"type": "string"}, "description": "Filter by status: triggered, acknowledged, resolved."},
          {"name": "limit", "in": "query", "schema": {"type": "integer", "default": 10}}
        ],
        "responses": {"200": {"description": "Incident list."}}
      }
    }
  }
}"""


async def _validate_pagerduty(api_key: str) -> bool:
    import httpx

    url = "https://api.pagerduty.com/abilities"
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(url, headers={
            "Authorization": f"Token token={api_key}",
            "Content-Type": "application/json",
        })
    return resp.status_code == 200


@api_router.post(
    "/tenants/{tenant_id}/integrations/pagerduty",
    response_model=IntegrationConnectResponse,
)
async def connect_pagerduty(
    tenant_id: str,
    body: PagerDutyConnectRequest,
    _verified: Annotated[str, Depends(require_session_token)],
) -> IntegrationConnectResponse:
    try:
        valid = await _validate_pagerduty(body.api_key)
    except Exception as e:
        log.warning("connect_pagerduty: validation failed for tenant=%s: %s", tenant_id, e)
        return IntegrationConnectResponse(ok=False, integration="pagerduty", error="could not reach PagerDuty API")
    if not valid:
        return IntegrationConnectResponse(ok=False, integration="pagerduty", error="invalid PagerDuty API key")

    from .gateway_provisioner import provision_integration

    region = _region()
    try:
        result = provision_integration(
            tenant_id, "pagerduty",
            api_key=f"Token token={body.api_key}",
            openapi_spec=PAGERDUTY_OPENAPI_SPEC,
            credential_header_name="Authorization",
            region=region,
        )
    except Exception as e:
        log.exception("connect_pagerduty: provisioning failed for tenant=%s", tenant_id)
        return IntegrationConnectResponse(ok=False, integration="pagerduty", error=f"provisioning failed: {e}")

    _enable_byo_for_integration(tenant_id, "pagerduty", result, region)
    return IntegrationConnectResponse(ok=True, integration="pagerduty", target_name=result["target_name"], gateway_url=result["gateway_url"])


# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------

GITHUB_OPENAPI_SPEC = """{
  "openapi": "3.0.0",
  "info": {"title": "GitHub API (agent-core subset)", "version": "1.0.0"},
  "servers": [{"url": "https://api.github.com"}],
  "paths": {
    "/repos/{owner}/{repo}/deployments": {
      "get": {
        "operationId": "recent_deploys",
        "summary": "List recent deployments for a repository.",
        "parameters": [
          {"name": "owner", "in": "path", "required": true, "schema": {"type": "string"}},
          {"name": "repo", "in": "path", "required": true, "schema": {"type": "string"}},
          {"name": "per_page", "in": "query", "schema": {"type": "integer", "default": 10}}
        ],
        "responses": {"200": {"description": "Deployment list."}}
      }
    },
    "/repos/{owner}/{repo}/commits": {
      "get": {
        "operationId": "recent_commits",
        "summary": "List recent commits for a repository.",
        "parameters": [
          {"name": "owner", "in": "path", "required": true, "schema": {"type": "string"}},
          {"name": "repo", "in": "path", "required": true, "schema": {"type": "string"}},
          {"name": "per_page", "in": "query", "schema": {"type": "integer", "default": 10}},
          {"name": "sha", "in": "query", "schema": {"type": "string"}, "description": "Branch name or commit SHA to list from."}
        ],
        "responses": {"200": {"description": "Commit list."}}
      }
    },
    "/repos/{owner}/{repo}/pulls/{pull_number}": {
      "get": {
        "operationId": "get_pr",
        "summary": "Get a pull request by number.",
        "parameters": [
          {"name": "owner", "in": "path", "required": true, "schema": {"type": "string"}},
          {"name": "repo", "in": "path", "required": true, "schema": {"type": "string"}},
          {"name": "pull_number", "in": "path", "required": true, "schema": {"type": "integer"}}
        ],
        "responses": {"200": {"description": "Pull request details."}}
      }
    },
    "/repos/{owner}/{repo}/pulls": {
      "get": {
        "operationId": "list_open_prs",
        "summary": "List open pull requests for a repository.",
        "parameters": [
          {"name": "owner", "in": "path", "required": true, "schema": {"type": "string"}},
          {"name": "repo", "in": "path", "required": true, "schema": {"type": "string"}},
          {"name": "state", "in": "query", "schema": {"type": "string", "default": "open"}},
          {"name": "per_page", "in": "query", "schema": {"type": "integer", "default": 10}}
        ],
        "responses": {"200": {"description": "Pull request list."}}
      }
    }
  }
}"""


async def _validate_github(token: str) -> bool:
    import httpx

    url = "https://api.github.com/user"
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(url, headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        })
    return resp.status_code == 200


@api_router.post(
    "/tenants/{tenant_id}/integrations/github",
    response_model=IntegrationConnectResponse,
)
async def connect_github(
    tenant_id: str,
    body: GitHubConnectRequest,
    _verified: Annotated[str, Depends(require_session_token)],
) -> IntegrationConnectResponse:
    try:
        valid = await _validate_github(body.personal_access_token)
    except Exception as e:
        log.warning("connect_github: validation failed for tenant=%s: %s", tenant_id, e)
        return IntegrationConnectResponse(ok=False, integration="github", error="could not reach GitHub API")
    if not valid:
        return IntegrationConnectResponse(ok=False, integration="github", error="invalid GitHub personal access token")

    from .gateway_provisioner import provision_integration

    region = _region()
    try:
        result = provision_integration(
            tenant_id, "github",
            api_key=f"Bearer {body.personal_access_token}",
            openapi_spec=GITHUB_OPENAPI_SPEC,
            credential_header_name="Authorization",
            region=region,
        )
    except Exception as e:
        log.exception("connect_github: provisioning failed for tenant=%s", tenant_id)
        return IntegrationConnectResponse(ok=False, integration="github", error=f"provisioning failed: {e}")

    _enable_byo_for_integration(tenant_id, "github", result, region)
    return IntegrationConnectResponse(ok=True, integration="github", target_name=result["target_name"], gateway_url=result["gateway_url"])


# ----------------------------------------------------------------------------
# Codebase routes — GitHub App install + warm-start
# ----------------------------------------------------------------------------
#
# The /integrations/github endpoint above is the BYO PAT flow — it
# provisions a Gateway target so the agent can call the GitHub API as a
# BYO tool. The /codebases/github/install endpoint below is a completely
# different flow: the tenant installs the AgentCore Reference GitHub App on their org
# (OAuth redirect on the onboarding page), and we seed the ``codebases``
# config block so the first Slack message already has a ranked shortlist.
#
# The two flows can coexist — a tenant could use both PAT-backed tooling
# AND the App-backed codebase access layer at the same time. They touch
# different parts of the tenant config (``byo`` vs ``codebases``).

@api_router.post(
    "/tenants/{tenant_id}/codebases/github/install",
    response_model=GitHubAppInstallResponse,
)
async def install_github_app(
    tenant_id: str,
    body: GitHubAppInstallRequest,
    _verified: Annotated[str, Depends(require_session_token)],
) -> GitHubAppInstallResponse:
    """Run the install-time warm-start for a GitHub App installation.

    The onboarding UI calls this after the user completes the GitHub
    App install flow and is redirected back with ``installation_id`` in
    the URL. We:

      1. Mint an installation token
      2. List the installation's repos
      3. Rank by ``pushed_at`` / stars
      4. Write a ``codebases`` block to the tenant row with
         ``enabled=True``, the ranked bindings, and the top repo as
         ``default_repo``

    Never raises on GitHub/network errors — the warm-start orchestrator
    wraps those into ``WarmStartResult(ok=False, error=...)``, which we
    pass through as ``ok=False`` in the response body. The UI decides
    whether to surface that as an error toast or a retry prompt.

    Re-running this endpoint with the same ``installation_id`` is safe
    and idempotent — it re-fetches the repo list, re-ranks, and
    re-writes. Useful if the tenant adds repos to the installation
    after onboarding and wants to refresh their shortlist.
    """
    from .github_install import run_install_warm_start

    result = run_install_warm_start(tenant_id, body.installation_id, _region())

    return GitHubAppInstallResponse(
        ok=result.ok,
        installation_id=result.installation_id,
        default_repo=result.default_repo,
        bindings=[
            CodebaseBindingBrief(
                repo=b["repo"],
                default_branch=b["default_branch"],
            )
            for b in result.bindings
        ],
        total_repos_available=result.total_repos_available,
        error=result.error,
    )


# ----------------------------------------------------------------------------
# Metrics routes — powered by bridge/metrics_reader.py (CloudWatch EMF data)
# ----------------------------------------------------------------------------
#
# Two surfaces:
#   - GET /api/tenants/{tenant_id}/metrics?window=7d
#       Session-authenticated, tenant_id forced by the token. Used by
#       the onboarding workspace metrics page.
#
#   - GET /api/ops/metrics/roster?window=7d
#     GET /api/ops/metrics/tenants/{tenant_id}?window=7d
#       Admin-secret-authenticated, cross-tenant. Temporary auth shim
#       until the real identity model lands; secret is set via
#       ADMIN_SECRET env var on the bridge service.
#
# See metrics_reader.py for the CloudWatch query shapes and windowing.

def _ops_guard(x_admin_token: Annotated[str, Header()] = "") -> None:
    """Require a header matching the ``ADMIN_SECRET`` env var.

    Temporary shared-secret gate for the ``/ops`` pages. Fails closed
    when ``ADMIN_SECRET`` is unset so a forgotten env var can't silently
    expose cross-tenant data.
    """
    expected = os.getenv("ADMIN_SECRET", "")
    if not expected:
        log.warning("_ops_guard: ADMIN_SECRET unset — rejecting all ops traffic")
        raise HTTPException(status_code=503, detail="ops dashboard disabled")
    if not x_admin_token or x_admin_token != expected:
        raise HTTPException(status_code=401, detail="invalid admin token")


@api_router.get("/tenants/{tenant_id}/metrics")
async def get_tenant_metrics_route(
    tenant_id: str,
    _verified: Annotated[str, Depends(require_session_token)],
    window: str = "7d",
) -> dict[str, Any]:
    """Return a per-tenant CloudWatch metrics snapshot.

    ``tenant_id`` is validated against the session token by
    ``require_session_token`` — a caller can't swap it for another
    tenant by editing the URL. The ``window`` query string accepts
    ``1h | 24h | 7d | 30d`` (anything else falls back to 7d).
    """
    from .metrics_reader import get_tenant_metrics

    snapshot = get_tenant_metrics(tenant_id, window)
    return snapshot.to_dict()


@api_router.get("/ops/metrics/roster")
async def get_ops_roster_route(
    _: Annotated[None, Depends(_ops_guard)],
    window: str = "7d",
    include_testenv: bool = False,
) -> dict[str, Any]:
    """Cross-tenant roster for the operator dashboard.

    Returns every tenant that has invocation metrics in the window,
    sorted by invocation count descending, with error rate and cost
    attached. Dead tenants (no metrics) don't appear here.

    ``include_testenv=false`` (default) hides tenants with
    ``config.is_internal_testenv=true``, which is how the manual-test
    rig keeps itself out of real-customer ops views.
    """
    from .metrics_reader import get_ops_roster

    rows = get_ops_roster(window, include_testenv=include_testenv)
    return {
        "window": window,
        "tenants": [r.to_dict() for r in rows],
        "include_testenv": include_testenv,
    }


@api_router.get("/ops/metrics/tenants/{tenant_id}")
async def get_ops_tenant_metrics_route(
    tenant_id: str,
    _: Annotated[None, Depends(_ops_guard)],
    window: str = "7d",
) -> dict[str, Any]:
    """Operator drill-down on a single tenant's metrics.

    Same shape as the tenant-scoped ``/metrics`` route above, but gated
    by the admin secret instead of the tenant session. Lets an operator
    inspect any tenant without holding a session token for it.
    """
    from .metrics_reader import get_tenant_metrics

    snapshot = get_tenant_metrics(tenant_id, window)
    return snapshot.to_dict()
