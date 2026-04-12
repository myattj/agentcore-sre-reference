# Snowflake warehouse suspended

> **Owner:** Data Eng · **Last updated:** 2026-04-05

## tl;dr

Most common cause: someone flipped `AUTO_RESUME` off on a warehouse for debugging and forgot to turn it back on. Fix:

```sql
ALTER WAREHOUSE <name> SET AUTO_RESUME = TRUE;
```

If that doesn't fix it, see the "credits exhausted" section below — that's the worse case.

## How to tell which is which

```sql
SHOW WAREHOUSES LIKE '<name>';
```

Look at the `state` column:

- `SUSPENDED` + `auto_resume = true` → the warehouse is fine, it will auto-resume on next query. Just query it.
- `SUSPENDED` + `auto_resume = false` → run the ALTER above.
- `SUSPENDED_FOR_RESOURCE_MONITOR` → the resource monitor suspended it. See next section.

## Credits exhausted (resource monitor suspended)

We set monthly credit limits per warehouse via resource monitors. When a warehouse hits 100% of its quota, the monitor suspends it and a page fires.

1. **Don't just increase the limit.** Investigate first. A 100% month before month-end usually means something unexpected is running.
2. Query `SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY` for the last 24h of credits consumed by the warehouse:

```sql
SELECT start_time, credits_used, query_history.query_text
FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY wmh
JOIN SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY qh USING(query_id)
WHERE warehouse_name = '<NAME>'
  AND start_time > dateadd(hour, -24, current_timestamp())
ORDER BY credits_used DESC
LIMIT 20;
```

3. Identify the offender. Usual suspects: a new dbt model scanning more than expected, an ad-hoc query that ran in a loop, a dashboard that auto-refreshes too aggressively.
4. **Fix the offender before bumping the limit.** If the limit needs to go up legitimately, file an approval ticket with finance.
5. Resume the warehouse:

```sql
ALTER WAREHOUSE <name> RESUME;
```

## Related

- `data/snowflake-cost-optimization.md`
- `data/dbt-model-failure.md`
