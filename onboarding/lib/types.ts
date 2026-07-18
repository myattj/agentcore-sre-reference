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
  shared_across_channels: boolean;
  isolated_channels: string[];
};

export type HeartbeatConfig = {
  busy_threshold: number;
  max_background_seconds: number;
};

export type CostCapConfig = {
  monthly_limit_dollars: number;
  enabled: boolean;
};

export type ChannelPersona = {
  system_prompt?: string | null;
  allowed_tools?: string[] | null;
  memory_rules?: string[] | null;
};

export type BotPolicyConfig = {
  allow_all_bots: boolean;
  trusted_bot_ids: string[];
  open_channels: string[];
};

export type ContextAssemblyConfig = {
  resolve_permalinks: boolean;
  inject_thread_history: boolean;
  thread_history_depth: number;
  max_permalinks: number;
};

export type SkillDef = {
  trigger: string;
  name: string;
  prompt_template: string;
  required_tools: string[];
  channels: string[];
};

export type EscalationRoute = {
  team_name: string;
  channel_id: string;
  description: string;
  contacts: string[];
};

export type EscalationConfig = {
  routes: EscalationRoute[];
};

/**
 * A single repo binding. Mirrors `coreAgent.tenant.CodebaseBinding`.
 * `aliases` are informal names users might call this repo; `channels`
 * are Slack channel IDs where this binding is the confirmed default.
 */
export type CodebaseBinding = {
  repo: string;
  default_branch: string;
  aliases: string[];
  channels: string[];
};

/**
 * Per-tenant code access layer. Mirrors `coreAgent.tenant.CodebasesConfig`.
 * Drives the GitHub-App-backed code tools and the discovery layer that
 * picks which repo a Slack message refers to.
 */
export type CodebasesConfig = {
  enabled: boolean;
  github_installation_id: string | null;
  default_repo: string | null;
  bindings: CodebaseBinding[];
  allow_learning: boolean;
};

export type TenantConfig = {
  tenant_id: string;
  model_id: string;
  system_prompt: string;
  catalog: CatalogConfig;
  byo: ByoConfig;
  memory: MemoryConfig;
  heartbeat: HeartbeatConfig;
  cost_cap: CostCapConfig;
  channels: Record<string, ChannelPersona>;
  admin_user_ids: string[];
  bot_policy: BotPolicyConfig;
  context_assembly: ContextAssemblyConfig;
  skills: SkillDef[];
  escalation: EscalationConfig;
  codebases: CodebasesConfig;
  /**
   * Marks a tenant as an internal test/demo environment. The ops
   * dashboard filters these out of cross-tenant leaderboards by
   * default so they don't pollute real-customer metrics. Purely a
   * presentation flag — the agent itself ignores it.
   */
  is_internal_testenv: boolean;
};

/** Sparse partial used as the body of `PATCH /api/tenants/{id}`. */
export type TenantConfigPatch = Partial<{
  model_id: string;
  system_prompt: string;
  catalog: Partial<CatalogConfig>;
  // BYO Gateway trust configuration is operator/connector-managed and is
  // intentionally absent from the tenant-session PATCH surface.
  // Memory namespace is a platform isolation boundary, not a tenant setting.
  memory: Partial<Omit<MemoryConfig, "namespace">>;
  heartbeat: Partial<HeartbeatConfig>;
  // Cost caps are platform enforcement policy and are operator-managed.
  channels: Record<string, ChannelPersona>;
  bot_policy: Partial<BotPolicyConfig>;
  context_assembly: Partial<ContextAssemblyConfig>;
  skills: SkillDef[];
  escalation: Partial<EscalationConfig>;
  codebases: Partial<Omit<CodebasesConfig, "github_installation_id">>;
  // is_internal_testenv is an operator accounting/visibility marker.
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
 * Compact binding shape returned from the GitHub App install endpoint.
 * Mirrors `bridge/bridge/api_models.py:CodebaseBindingBrief`.
 */
export type CodebaseBindingBrief = {
  repo: string;
  default_branch: string;
};

/**
 * Response from POST /api/tenants/{id}/codebases/github/install.
 * Mirrors `bridge/bridge/api_models.py:GitHubAppInstallResponse`.
 */
export type GitHubAppInstallResponse = {
  ok: boolean;
  installation_id: string;
  default_repo?: string | null;
  bindings: CodebaseBindingBrief[];
  total_repos_available: number;
  pending_approval: boolean;
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
    id: "post_to_channel",
    label: "Post to channel",
    description:
      "Post a message to any Slack channel the bot is a member of (cross-channel actions).",
  },
  {
    id: "escalate",
    label: "Escalate",
    description:
      "Escalate an issue to a specific team using the configured routing table.",
  },
  {
    id: "ask_codebase_choice",
    label: "Ask codebase choice",
    description:
      "Post Slack Block Kit buttons asking the user to pick a repo. A UX affordance the agent uses when it genuinely can't tell which connected repo is meant.",
  },
  {
    id: "inspect_codebase_context",
    label: "Inspect codebase context",
    description:
      "Gather extra signals (channel name/topic, user profile, channel-pinned bindings, memory hint) when the agent needs more context before picking a codebase. Returns hints, not a decision.",
  },
  {
    id: "code_search",
    label: "Code search",
    description:
      "Search code across the tenant's connected GitHub repos. Requires the GitHub App to be installed.",
  },
  {
    id: "code_read_file",
    label: "Code read file",
    description:
      "Read a specific file from a connected repo. Requires the GitHub App to be installed.",
  },
  {
    id: "code_find_symbol",
    label: "Code find symbol",
    description:
      "Find files mentioning a specific symbol (function, class, constant). Requires the GitHub App to be installed.",
  },
  {
    id: "code_list_commits",
    label: "Code list commits",
    description:
      "List recent commits on a connected repo, optionally filtered by branch or path. Requires the GitHub App to be installed.",
  },
  {
    id: "propose_pr",
    label: "Experimental — Propose PR",
    description:
      "Unsafe credential boundary; disabled by default. Explicit opt-in runs model-authored code in a sandbox, pushes a branch, and opens a pull request. Review the sandbox and GitHub write permissions before enabling.",
  },
  {
    id: "render_dashboard",
    label: "Render dashboard",
    description:
      "Create a validated, seven-day dashboard link with charts, tables, metrics, text, and lists.",
  },
];
