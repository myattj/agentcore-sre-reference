# RDS connection exhaustion

> **Owner:** SRE · **Last updated:** 2026-04-07

## tl;dr

When you see `FATAL: remaining connection slots are reserved for non-replication superuser connections`, someone's pool is too big. The math is simple: `pool_size_per_pod * pod_count < max_connections - reserved_buffer`.

## Current prod numbers

- RDS `max_connections` = **500**
- Reserved for superuser/replication = **20**
- Usable by app pods = **480**
- Prod pod count (steady state) = **~30 across all services**
- Therefore max sustainable `pool_size_per_pod` = **~15**

We set `pool_size = 15` as the default in `acme-data-api/app/db.py`. Any service that overrides it is a bug.

## Investigation

### Who's using connections?

```sql
SELECT application_name,
       state,
       count(*) AS conn_count
FROM pg_stat_activity
WHERE datname = 'acme_prod'
GROUP BY application_name, state
ORDER BY conn_count DESC;
```

Look for services with more connections than `pod_count * 15`. That's your offender.

### Idle in transaction

The other common culprit is a stuck `idle in transaction` connection holding a slot forever:

```sql
SELECT pid,
       application_name,
       now() - state_change AS idle_duration,
       left(query, 200) AS last_query
FROM pg_stat_activity
WHERE state = 'idle in transaction'
ORDER BY idle_duration DESC;
```

Anything idle for >5 minutes is a bug. Kill it:

```sql
SELECT pg_terminate_backend(<pid>);
```

Then file a bug against the owning service — idle-in-transaction is almost always a missing COMMIT or ROLLBACK in a try/finally.

## Mitigation vs fix

**Fix** (always prefer): reduce the pool size in the offending service, deploy, verify connection count drops.

**Mitigation** (emergency only): raise RDS `max_connections` via parameter group. This requires an instance restart (~2 minutes of 5xx) and we've done it twice — it buys you time but doesn't solve the underlying issue.

## Related

- `infra/rds-slow-query.md`
- `security/rds-password-rotation.md`
