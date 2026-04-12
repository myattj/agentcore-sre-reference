# Incident kickoff

> **Owner:** SRE · **Last updated:** 2026-03-15

The first 10 minutes of an incident set the tone for the whole thing. Follow this checklist even if it feels like overkill — especially then.

## Step 0 — Declare the incident

In `#incidents`, post:

```
:rotating_light: INCIDENT DECLARED — <short description> :rotating_light:

Severity: SEV-<N>
Impact: <what users see right now, best estimate>
Commander: me
Scribe: <name or "me">
Thread for all updates.
```

**You do not need perfect information to declare.** Declare on suspicion and downgrade later if it turns out to be nothing.

## Severity guidelines

- **SEV-1** — Customer-facing complete outage, data loss, security breach
- **SEV-2** — Customer-facing partial outage or elevated errors (>5% of requests)
- **SEV-3** — Degraded but working, reporting/internal impact, no customer complaints

## Roles

- **Commander** — runs the investigation, makes calls, delegates
- **Scribe** — writes timestamps in the thread for EVERYTHING ("14:03 — tried X, didn't help")
- **Responders** — anyone else pulled in

One person can wear multiple hats on small incidents. SEV-1 should always split commander and scribe.

## First 10 minutes

1. **Declare** (step 0)
2. **Identify impact** — user-visible vs internal-only? bounded or growing?
3. **Check the obvious things first** — recent deploys, recent config changes, upstream provider status pages
4. **Post status externally** — if customer-facing, update our status page at https://status.acmedata.co
5. **Page the secondary** if you're alone on SEV-1 or SEV-2
6. **Don't apply fixes yet.** Investigate for at least 5 minutes. Most "obvious" fixes in the first minute make things worse.

## Thread discipline

Every decision goes in the thread with a timestamp. Every hypothesis, every tool result, every failed attempt. The scribe owns this but everyone contributes.

## When it's resolved

Post the resolution in the thread. Format:

```
**INCIDENT RESOLVED**

Duration: <N> min
Impact: <quantified>
Root cause: <one sentence>

Action items:
1. <thing>
2. <thing>

Postmortem: <scheduled time>
```

Then take a walk.

## Related

- `incidents/comms-template.md` — for customer-facing updates
- `oncall/handoff.md` — note the incident in your handoff thread
- `deploys/deploy-rollback.md` — if rollback is a viable fix
