/**
 * TypeScript mirror of `coreAgent/app/coreAgent/tenant.py:TenantConfig`.
 *
 * **KEEP IN SYNC** with:
 *   - `coreAgent/app/coreAgent/tenant.py:41-93`           (authoritative Pydantic)
 *   - `bridge/bridge/api_models.py:TenantConfigOut`        (validation boundary)
 *   - `bridge/bridge/tenant_write.py:build_default_config_dict` (default shape)
 *
 * Used only for form state typing on the onboarding UI. The bridge's
 * Pydantic models are the runtime validation layer, so a divergence here
 * surfaces as a 422 from `PATCH /api/tenants/{id}` rather than as a
 * silent corruption. See CLAUDE.md gotcha #21.
 */

export type CatalogConfig = {
  allowed_tools: string[];
  tool_config: Record<string, Record<string, unknown>>;
};

export type ByoConfig = {
  enabled: boolean;
  gateway_endpoint: string | null;
  gateway_auth: Record<string, unknown> | null;
  connected_integrations: string[];
};

export type MemoryTriggers = {
  message_count: number;
  token_count: number;
  idle_timeout_seconds: number;
};

export type MemoryExtraction = {
  enabled: boolean;
  rules: string[];
};

export type MemoryConfig = {
  triggers: MemoryTriggers;
  namespace: string;
  extraction: MemoryExtraction;
};

export type HeartbeatConfig = {
  busy_threshold: number;
  max_background_seconds: number;
};

export type ChannelPersona = {
  system_prompt?: string | null;
  allowed_tools?: string[] | null;
  memory_rules?: string[] | null;
};

export type TenantConfig = {
  tenant_id: string;
  model_id: string;
  system_prompt: string;
  catalog: CatalogConfig;
  byo: ByoConfig;
  memory: MemoryConfig;
  heartbeat: HeartbeatConfig;
  channels: Record<string, ChannelPersona>;
};

/** Sparse partial used as the body of `PATCH /api/tenants/{id}`. */
export type TenantConfigPatch = Partial<{
  model_id: string;
  system_prompt: string;
  catalog: Partial<CatalogConfig>;
  byo: Partial<ByoConfig>;
  memory: Partial<MemoryConfig>;
  heartbeat: Partial<HeartbeatConfig>;
  channels: Record<string, ChannelPersona>;
}>;

export type ChannelInfo = {
  id: string;
  name: string;
  is_private: boolean;
};

export type ChannelsResponse = {
  channels: ChannelInfo[];
  /**
   * True when the bot token is valid but missing one of the scopes
   * needed to list channels. UI should show a "re-install for new
   * scopes" hint instead of an error banner. Mirrors the bridge field
   * in `bridge/bridge/api_models.py:ChannelsResponse`.
   */
  needs_reinstall?: boolean;
};

/** Response from POST /api/tenants/{id}/integrations/{integration}. */
export type IntegrationConnectResponse = {
  ok: boolean;
  integration: string;
  target_name?: string | null;
  gateway_url?: string | null;
  error?: string | null;
};

/**
 * Catalog tools the bridge ships today (mirrors
 * `coreAgent/app/coreAgent/tools.py:CATALOG`). When new catalog tools
 * land, add them here so the config form's checkbox list stays in sync.
 */
export const KNOWN_CATALOG_TOOLS: { id: string; label: string; description: string }[] = [
  {
    id: "echo",
    label: "Echo",
    description: "Repeats text back. Sanity-check tool — confirms the agent can call tools.",
  },
  {
    id: "start_background_task",
    label: "Start background task",
    description:
      "Demonstrates the long-running background task lifecycle (HealthyBusy heartbeat).",
  },
  {
    id: "search_team_history",
    label: "Search team history",
    description: "Search past Slack messages in a channel by keyword.",
  },
  {
    id: "read_thread_context",
    label: "Read thread context",
    description: "Fetch the full Slack thread the bot was tagged in.",
  },
  {
    id: "search_docs",
    label: "Search docs",
    description:
      "Fan out a search across all configured doc sources (Confluence, Notion).",
  },
];
