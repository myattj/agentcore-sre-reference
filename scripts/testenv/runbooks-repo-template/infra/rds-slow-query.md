# RDS slow query investigation

> **Owner:** SRE · **Last updated:** 2026-03-22

## tl;dr

When the APM slow-query alert fires, start with `pg_stat_activity` to find the running query, then `pg_locks` to see if it's blocked, then `EXPLAIN` if neither of those explain it. 90% of the time it's a missing index or a bad query plan from stale ANALYZE stats.

## Step 1 — Who's running queries right now?

```sql
SELECT pid,
       now() - query_start AS duration,
       state,
       wait_event_type,
       wait_event,
       left(query, 200) AS query
FROM pg_stat_activity
WHERE state != 'idle'
  AND datname = 'acme_prod'
ORDER BY duration DESC
LIMIT 20;
```

Look for long-running queries with a `wait_event`. If `wait_event_type = Lock`, go to step 2.

## Step 2 — Who's blocking whom?

```sql
SELECT blocked.pid AS blocked_pid,
       blocked.query AS blocked_query,
       blocker.pid AS blocker_pid,
       blocker.query AS blocker_query
FROM pg_stat_activity blocked
JOIN pg_locks blocked_locks ON blocked_locks.pid = blocked.pid
JOIN pg_locks blocker_locks
  ON blocker_locks.locktype = blocked_locks.locktype
  AND blocker_locks.database IS NOT DISTINCT FROM blocked_locks.database
  AND blocker_locks.relation IS NOT DISTINCT FROM blocked_locks.relation
  AND blocker_locks.granted
JOIN pg_stat_activity blocker ON blocker.pid = blocker_locks.pid
WHERE NOT blocked_locks.granted;
```

If there's a blocker, you have a choice:

- **Kill the blocker** (`SELECT pg_terminate_backend(<pid>);`) if it's a runaway. Usually safe.
- **Wait** if the blocker is a legit long-running migration or report.

## Step 3 — EXPLAIN the slow query

```sql
EXPLAIN (ANALYZE, BUFFERS) <query>;
```

Look for:

- **Seq Scan on large table** — missing index, usually
- **Nested Loop** on large rowcounts — stats are stale, try `ANALYZE <table>`
- **High Rows Removed by Filter** — the planner is not pushing predicates down

## Step 4 — Top offenders from pg_stat_statements

When the slow query isn't running right now, pull the historical top 10:

```sql
SELECT substring(query, 1, 100) AS query,
       calls,
       round(total_exec_time::numeric, 2) AS total_ms,
       round(mean_exec_time::numeric, 2) AS mean_ms
FROM pg_stat_statements
WHERE dbid = (SELECT oid FROM pg_database WHERE datname = 'acme_prod')
ORDER BY total_exec_time DESC
LIMIT 10;
```

## Related

- `infra/rds-connection-exhaustion.md`
- `security/rds-password-rotation.md`
