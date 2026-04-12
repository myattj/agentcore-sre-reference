# On-call handoff

> **Owner:** SRE · **Last updated:** 2026-04-01

## Monday morning checklist

Run through this BEFORE your first coffee:

1. **Read last week's oncall thread** in `#oncall`. Skim the full week. Note any unresolved pages, in-progress investigations, or notes the outgoing person left for you.
2. **Check Datadog dashboards** for currently-firing alerts:
   - [Infra overview](https://app.datadoghq.com/dashboard/infra)
   - [Service SLOs](https://app.datadoghq.com/dashboard/slo)
3. **Ack your PagerDuty assignment.** Primary rotation auto-assigns, but ack so the outgoing person knows you're on it.
4. **Post the handoff message** in `#oncall` using this template:

```
oncall handoff from <name>: taking it this week. open threads: <links>. notes: <anything>. ping me for any infra escalations.
```

5. **Know who's secondary.** Pull it up in PagerDuty. You will page them at some point; don't hesitate.

## During the week

- **You are the default for all `#alerts-*` pages.** Ack fast. "Looking" in thread counts.
- **If an alert self-resolves, still note it in the oncall thread.** Future-you will thank you.
- **Page the secondary** when: you need to sleep, you need a break, the incident is beyond one person, or you just want a second opinion. That's what they're there for.
- **Sync with Data Eng on page sensitivity for `#alerts-data`.** Data pages are less time-critical than infra pages but still need same-day action.

## Friday handoff

- Write the handoff thread. Include: pages this week, unresolved items, things the incoming person should know (in-progress upgrades, recent config changes, etc.).
- Mention anyone who pulled up to help.
- Sign off with something human. It's been a week.

## Related

- `incidents/kickoff.md`
- `incidents/comms-template.md`
