# Incident comms template

> **Owner:** Product · **Last updated:** 2026-03-20

External communication during an incident. Use these as starting points — edit for tone and specifics.

## Initial page (within 5 minutes of declaring)

```
We're investigating reports of <symptom>. Affected: <what / who>. No customer action is needed right now. Next update in 15 minutes.
```

Post to:

- https://status.acmedata.co (status page)
- Customer success Slack channel (they'll handle inbound from customers)

## Status update (every 15 minutes during an active incident)

```
Update on <symptom>: we've identified <what we've found>. Working on <what we're trying>. Next update at <time>.
```

If there's nothing new to say, say that:

```
Update on <symptom>: continuing to investigate. No new information since the last update. Next update at <time>.
```

"No news" updates are better than silence.

## Resolution

```
Resolved: <symptom> is no longer occurring as of <time>. Duration: <N> min. We're writing up a postmortem and will share the root cause in the next 48 hours.
```

## Postmortem announcement (within 48 hours of resolution)

A short forum post or email linking the full postmortem doc. Focus on:

- What happened
- Why it happened
- What we're doing so it doesn't happen again

**No blame. No "human error". Every incident is a system-design problem.**

## Related

- `incidents/kickoff.md`
