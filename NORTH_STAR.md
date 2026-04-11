# North star

> Where Novari is going, beyond the "first paying customer onboards in <5 min" KPI in [`BUILD_PLAN.md`](./BUILD_PLAN.md). This is direction, not a delivery schedule — initiatives are grouped by pillar, not sequenced. Dependencies are called out inline.

## The feeling we're chasing

**Magic, pliable, learning.** The product should feel like it bends to the shape of each team, gets smarter the more they use it, and makes things that previously required a ticket feel like a sentence in Slack. Every initiative below should be judged by whether it moves us toward that feeling — not just whether it ships a feature.

Three shorthand tests:
- **Magic** — does a first-time user see the bot do something they didn't know was possible?
- **Pliable** — can a non-engineer shape the bot's behavior without writing code or filing a ticket to us?
- **Learning** — does the bot get meaningfully better for this tenant by week 4 than it was on day 1, without us touching it?

---

## Pillar 1 — Extensibility: skills, MCPs, and customer integrations

**Why this pillar matters:** the bot is only as useful as the surface area it can reach into. Today that surface is a fixed catalog + one generic BYO Gateway. To feel pliable we need customers shaping their own surface area, and to feel magic we need a much richer default one.

### Initiatives
- **Skills portal with AI-assisted authoring.** A first-class UI where a tenant describes a skill in plain language (trigger, steps, required tools) and the portal drafts a `skills[]` entry — prompt template, trigger phrases, tool whitelist. AI co-authors with the user; user approves. Lives in the admin portal (Pillar 2).
- **Richer built-in skill + MCP library.** Ship a curated set of default skills and MCPs every tenant inherits (incident triage, PR review, standup summary, doc Q&A, oncall handoff, etc.). Depth matters more than breadth — each one should be a reason to install the bot.
- **Customer-owned MCPs / integrations with their own OAuth.** Customers bring their own systems (internal tools, private SaaS) by registering an MCP endpoint and supplying OAuth credentials that live under *their* identity, not ours. Requires: a per-tenant credential vault, an OAuth broker flow in the onboarding/admin UI, and Gateway target provisioning keyed to the tenant's credentials.
- **Skill sharing & templates.** Once the authoring flow works, let tenants fork skills from a public template gallery. Reinforces the "magic out of the box" feeling for new tenants and creates a long-tail library without us authoring every skill.

**Dependencies:** the customer-OAuth story blocks on a clean credential model — today's single-credential-provider Gateway gotcha (see `reference_agentcore_gateway_gotchas.md`) needs an answer before this scales. Skills portal can ship against the existing `skills[]` schema without waiting.

---

## Pillar 2 — Tenant admin portal

**Why this pillar matters:** today tenants get an onboarding UI and nothing else. For them to trust the bot with real work — and pay for it — they need a home where they can see what it's doing, steer it, and manage the commercial relationship.

### Initiatives
- **Unified admin portal per tenant.** One surface that absorbs the current onboarding flow and extends it with: configuration editing, tool/skill management, memory inspection, user/permission management, audit log search, usage & cost, and billing.
- **Self-serve config for everything in `TenantConfig`.** Anything that today requires editing a DynamoDB row should be editable in the portal. The three-place tenant-config rule (CLAUDE.md gotcha #14) becomes load-bearing — need a strategy that doesn't make every new field a three-way merge exercise.
- **Pricing and billing.** Plans, metering (Bedrock spend + tool calls + skills invoked), invoices, payment method, usage alerts. Ties directly into the cost-cap work already in flight (see `BUILD_PLAN.md` post-week-7).
- **Tenant admin observability.** A dashboard the *customer's* admin team uses to see their own bot's health: conversations per channel, top skills fired, tool success rates, memory growth, cost burn vs. cap, recent errors. This is the customer-side half of Pillar 4.
- **Role-based access within a tenant.** At minimum: admins (edit config, see billing) vs. viewers (see usage, can't change anything). Critical before billing is exposed.

**Dependencies:** billing and RBAC both want a real identity model. Today the onboarding UI auths via session tokens tied to Slack OAuth install — that's enough for one admin per tenant, not enough for a team.

---

## Pillar 3 — Dynamic dashboards hosted by the bot

**Why this pillar matters:** this is one of the biggest "magic" levers. Instead of posting a table in Slack, the bot can say *"here's a live dashboard: novari.dev/d/abc123"* and the user gets an interactive visualization of whatever they asked about. This is the single feature most likely to produce "wait, it can do that?" moments.

### Initiatives
- **Ephemeral dashboard hosting.** The bot can generate a small web page (chart, table, timeline, map) from a tool result and host it at a short-lived per-tenant URL. Think "render this query result as a chart I can share."
- **Dashboard skill primitives.** Expose dashboard-building as a tool the agent can call: `render_chart`, `render_table`, `render_timeline`, `render_kanban`. Implementation leans on a small set of React components the bot parameterizes — not a general-purpose code-exec sandbox (too big).
- **Saveable + shareable.** Users can promote an ephemeral dashboard to a persistent one that lives in the admin portal. Creates a natural path from conversation to durable internal tool.
- **Sandboxing & permissions.** Dashboards render only data the requesting user already has access to. This is a security design problem as much as a product one — needs to be designed early, not bolted on.

**Dependencies:** needs the admin portal (Pillar 2) as the "home" for persistent dashboards. Sandboxing design should happen before we let the bot render arbitrary content.

---

## Pillar 4 — Observability (ours and theirs)

**Why this pillar matters:** today we cannot answer basic questions like "which tenants are actually using the bot?" or "why did this tenant's response quality regress?" Customers can't answer them either. Both sides need a real window in.

### Initiatives
- **Internal operator dashboard.** For us: tenant roster, per-tenant health, error rates, Bedrock spend leaderboard, memory storage burn, stuck/failed invocations, recent deploys' impact. Should answer "is any tenant having a bad day right now?" at a glance.
- **Tenant-side observability.** The customer-facing half lives inside the admin portal (Pillar 2). Same spine, scoped views.
- **Structured audit trail with UI.** The audit log tables already exist (`coreAgent/app/coreAgent/audit.py`). Need a query UI, not just the CLI helper.
- **Eval + regression tracking.** Once customers depend on skills, we need to know when a skill silently got worse. Tie evals to the skill definitions from Pillar 1 so customers can see their own skill quality over time.

**Dependencies:** builds on the audit log spine that already exists. Cleaner once we have a real identity model (Pillar 2) so dashboards can scope by user as well as tenant.

---

## Pillar 5 — Brand, marketing, and UX polish

**Why this pillar matters:** the product currently looks like an internal tool. For a company called Novari at `novari.dev`, the surface needs to match the ambition. First impressions determine whether anyone even reaches the onboarding flow.

### Initiatives
- **Landing page at novari.dev.** Marketing surface: what it does, the three core patterns (alert triage / team Q&A / workflow automation), pricing, social proof, install CTA. Currently `novari.dev` serves the app.
- **New brand system.** Logo, color palette, typography, voice. Applied across marketing site, onboarding, admin portal, Slack messages. This is a precondition for the UX cleanup — don't redesign onboarding twice.
- **UX cleanup pass.** Across onboarding (`onboarding/`) and the eventual admin portal. Focus: fewer steps, clearer defaults, less "configure everything before you can try it" and more "try it, then configure what's wrong." The zero-config onboarding work (commit `df13d3a`) is the right direction; extend it.
- **In-product moments of delight.** Small magic: the bot introduces itself contextually, first-run surfaces the most-likely-useful skill, empty states teach instead of blocking.

**Dependencies:** brand system is upstream of both the landing page and the UX pass. Picking a brand system can happen in parallel with everything else, but should land before we invest heavily in admin portal visual design.

---

## Pillar 6 — The "magic, pliable, learning" substrate

**Why this pillar matters:** pillars 1–5 are surfaces. This pillar is the underlying capability that makes those surfaces feel alive. Without it, we have a well-designed configuration tool instead of a product that learns.

### Initiatives
- **Active learning from feedback.** Every message the bot sends should be implicitly rateable (thumbs in Slack, or inferred from user follow-ups). Feedback flows into memory + future skill selection + eval signal. This is the mechanism by which the bot gets better for a tenant over time.
- **Proactive suggestions.** The bot notices patterns — "I see you're asked about deploy status every morning, want me to post it automatically?" — and suggests skills the tenant should install. Turns Pillar 1's skill library into something the bot sells itself.
- **Per-user personalization on top of per-tenant config.** Memory already has a user namespace (built-in `USER_PREFERENCE` strategy, week 6). Extend that to shape tone, default tools, and which skills fire per-user, not just per-channel.
- **Self-updating context.** The bot keeps its understanding of the tenant's systems current (what channels matter, who's on-call, what's deploying) without anyone telling it to refresh. Ambient awareness rather than static config.

**Dependencies:** this pillar is where the eval + observability work (Pillar 4) pays off. You can't tell if the bot is learning without measurement. Also depends on the skill library (Pillar 1) having enough substance for "suggest a skill" to mean something.

---

## Cross-cutting concerns to resolve early

These don't belong to one pillar but block several:

1. **Identity model.** Today: one admin per tenant via Slack OAuth session token. Needed: multi-user tenants with roles. Blocks billing, RBAC, per-user personalization, audit-by-user.
2. **Credential vault for customer-owned OAuth.** Needed for Pillar 1's custom MCPs. Should be designed once, reused by any future integration that needs per-tenant credentials.
3. **Tenant-config schema management.** Three-place edit rule (CLAUDE.md #14) is already friction at today's size. Pillars 1 and 2 will make it worse. Decide whether to accept it, codegen from one source, or unify the schema.
4. **Brand system.** Upstream of Pillar 5. Should be chosen before any significant visual design work in Pillars 2–3.
5. **Sandboxing model for hosted dashboards.** Upstream of Pillar 3. Security design problem, not a product one.

---

## What this document is not

- Not a commitment — items here are direction, not scope.
- Not ordered — pillars are parallel tracks with their own dependency chains.
- Not a replacement for `BUILD_PLAN.md`, which covers the near-term path to the first-customer KPI. That KPI still comes first.
- Not exhaustive — it captures the shape of the direction, not every feature we'll build along the way.

---

## TODO list (flat view of the dump)

A checklist form of the original brain-dump, grouped by pillar. Use this as a working surface — the pillars above are the *why*, this is the *what*. Nothing here is scheduled; check items off as they land.

### Skills & extensibility
- [ ] Skills portal UI — plain-language skill authoring, lives in admin portal
- [ ] AI co-author flow — portal drafts `skills[]` entry (trigger, template, required tools) from a description
- [ ] Skill approval + edit + versioning before activation
- [ ] Curated built-in skill library — ship N "reason to install" skills (incident triage, PR review, standup, oncall handoff, doc Q&A, …)
- [ ] Curated built-in MCP library — expand default catalog beyond current scaffold
- [ ] Skill template gallery — fork-from-template, public entries
- [ ] Customer-owned MCP registration UI
- [ ] Per-tenant OAuth broker flow — customer connects their own system with their own credentials
- [ ] Per-tenant credential vault — scoped to the tenant's identity, not ours
- [ ] Gateway target provisioning keyed to tenant credentials (resolve single-credential-provider gotcha first)

### Dynamic dashboards (bot-hosted)
- [ ] `render_chart` / `render_table` / `render_timeline` / `render_kanban` tools the agent can call
- [ ] Ephemeral dashboard hosting — short-lived per-tenant URLs
- [ ] Persistent dashboards saved into the admin portal
- [ ] Shareable dashboard links with access scoping
- [ ] Sandboxing + permissions model — dashboards only show data the requesting user can access
- [ ] Dashboard-from-conversation UX — "turn this reply into a dashboard"

### Tenant admin portal
- [ ] Unified admin portal absorbing today's onboarding flow
- [ ] Config editing surface for every field in `TenantConfig`
- [ ] Memory inspector — browse/edit records, see what was stored and why
- [ ] Audit log query UI (replaces `audit_query.py` CLI for tenants)
- [ ] Usage dashboard — conversations, top skills, tool success rates, memory growth
- [ ] Cost dashboard — Bedrock spend, tool calls, burn vs. cap
- [ ] Pricing & plans page
- [ ] Metering pipeline — Bedrock + tool + skill invocation accounting
- [ ] Invoicing + payment method
- [ ] Usage alerts (approaching cap, anomalies)
- [ ] RBAC inside a tenant — at minimum admin vs. viewer
- [ ] Multi-user identity model (prerequisite for RBAC + billing)

### Observability
- [ ] Internal operator dashboard — tenant roster, health, error rates, Bedrock leaderboard
- [ ] "Is any tenant having a bad day right now?" at-a-glance view
- [ ] Tenant-side observability (scoped twin inside admin portal)
- [ ] Audit log query UI (shared with admin portal)
- [ ] Eval harness wired to skills — catch silent skill regressions
- [ ] Per-tenant eval trend visible in their admin portal

### Brand, marketing, UX
- [ ] New logo
- [ ] New color palette
- [ ] Typography + voice guidelines
- [ ] Brand system applied across marketing, onboarding, admin portal, Slack messages
- [ ] Landing page at `novari.dev` — what it does, three patterns, pricing, install CTA
- [ ] Move app off `novari.dev` root (currently serves the app, not marketing)
- [ ] UX cleanup pass on onboarding
- [ ] UX cleanup pass on admin portal (once it exists)
- [ ] First-run "magic moment" — surface most-likely-useful skill immediately
- [ ] Teaching empty states across the product

### "Magic, pliable, learning" substrate
- [ ] Implicit + explicit feedback capture on every bot reply (Slack thumbs + inferred signals)
- [ ] Feedback → memory → future skill selection loop
- [ ] Proactive skill suggestions ("I see you ask this every morning, want me to automate it?")
- [ ] Per-user personalization on top of per-tenant config (tone, default tools, active skills)
- [ ] Self-updating context — bot tracks on-call, active deploys, recent incidents without being told to refresh
- [ ] Learning signal visible in observability (week-over-week quality per tenant)

### Cross-cutting unblockers
- [ ] Multi-user identity model (blocks billing, RBAC, per-user personalization, audit-by-user)
- [ ] Per-tenant credential vault design (blocks customer-owned MCPs)
- [ ] Tenant-config schema management — resolve the three-place edit rule before Pillars 1 & 2 make it worse
- [ ] Dashboard sandboxing + permissions model (blocks Pillar 3 launch)
- [ ] Brand system locked in (blocks meaningful visual design work in Pillars 2, 3, 5)
