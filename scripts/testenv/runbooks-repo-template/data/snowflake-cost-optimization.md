# Snowflake cost optimization

> **Owner:** Data Eng · **Last updated:** 2026-04-06

Our Q1 bill was brutal. What we did to cut ~34% off March:

## 1. Auto-suspend timeout

Default Snowflake auto-suspend is 10 minutes. We dropped it to 60 seconds on every prod warehouse:

```sql
ALTER WAREHOUSE ANALYTICS_WH SET AUTO_SUSPEND = 60;
ALTER WAREHOUSE INGEST_WH    SET AUTO_SUSPEND = 60;
ALTER WAREHOUSE REPORTING_WH SET AUTO_SUSPEND = 60;
```

**Single biggest save.** ~50% of the cost reduction was from this alone. The 60s threshold is aggressive but not problematic — Snowflake's resume is fast enough that users don't notice.

## 2. Workload-specific warehouses

Before: one `ANALYTICS_WH` handled scheduled dbt runs AND ad-hoc queries AND dashboard refreshes. An ad-hoc query that scanned too much data could starve the scheduled jobs.

After:

- `ANALYTICS_WH` → scheduled dbt runs only
- `ANALYTICS_ADHOC_WH` → human queries, Python notebooks
- `REPORTING_WH` → BI tool refreshes (Looker, etc.)
- `INGEST_WH` → COPY INTO operations

Isolated blast radius, smaller individual warehouses, clearer cost attribution.

## 3. Time-windowed XL warehouses for heavy models

The heaviest dbt models used to run on the default `ANALYTICS_WH` (L) all day. We moved them to an XL warehouse that only spins up during the 02:00 UTC nightly run window:

```sql
CREATE WAREHOUSE ANALYTICS_NIGHTLY_XL WITH
  WAREHOUSE_SIZE = 'XLARGE'
  AUTO_SUSPEND = 60
  AUTO_RESUME = TRUE
  SCALING_POLICY = 'ECONOMY';
```

Larger warehouse for the heavy work (faster runs = fewer credits despite higher $/min), off most of the day.

## 4. Resource monitors as guardrails

Every warehouse has a monthly credit limit enforced by a resource monitor. When a warehouse hits 100% of quota, Snowflake suspends it and pages us. This is a guardrail, not a limit — if we're hitting 100% legitimately we raise it. But it stops runaway costs from landing in a bill at month-end.

## 5. Query profiling as a habit

We profile the top 10 queries by credits every Monday in the data-eng team review. Usual suspects:

- A dbt model that added a bad JOIN
- A dashboard that auto-refreshes on a cron
- A Python notebook someone left running

## What we didn't do

- **Didn't add fancy caching layers.** Snowflake's result cache is free and good.
- **Didn't move to a different warehouse tech.** The migration cost would swamp any savings.
- **Didn't cap user queries.** Humans are 10% of spend; we optimize the 90%.

## Related

- `data/snowflake-warehouse-suspended.md`
- `data/dbt-conventions.md`
- `data/dbt-model-failure.md`
