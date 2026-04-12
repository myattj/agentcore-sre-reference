# dbt model failure

> **Owner:** Data Eng · **Last updated:** 2026-04-08

## tl;dr

1. Check the dbt-cloud run page for the actual error message
2. Classify the error: permissions / resource / data
3. If data, use `check_upstream_nulls` macro to find upstream problems
4. Fix forward — don't just re-run and hope

## Step 1 — Get the real error

dbt-cloud UI shows the failing model's compiled SQL and the Snowflake error. The slack alert usually cuts off at 200 chars; always go to the dbt-cloud page for the full text.

## Step 2 — Classify the error

**Permissions**: `SQL access control error`, `insufficient privileges`. → Someone changed a Snowflake role. Check `acme-runbooks/security/iam-key-rotation.md` for the IAM→Snowflake role mapping.

**Resource**: `warehouse suspended`, `query cancelled`, `statement timeout`. → See `data/snowflake-warehouse-suspended.md`.

**Data**: anything mentioning column types, null violations, duplicate keys. → Continue below.

## Step 3 — Check upstream nulls

We have a macro `check_upstream_nulls` in `acme-data-api/dbt/macros/debug_helpers.sql` that runs a row count and null count for every column in a ref'd model. Example:

```bash
dbt run-operation check_upstream_nulls --args '{"model": "stg_orders"}'
```

Output tells you which columns are suddenly null or sparse. Common findings:

- A previously-never-null column now has nulls → upstream schema change or ingest bug
- Row count way down → upstream ingest dropping rows, see `data/ingest-pipeline-lag.md`
- Row count way up → upstream deduplication broken

## Step 4 — Fix forward

If the root cause is upstream data quality, you have two choices:

- **Fix the upstream** (preferred) — push the fix to the ingest or source
- **Harden the dbt model** — add a `not_null` test, fail louder, stop silently dropping bad rows

**Do not "just re-run" a failed model** hoping the issue was transient. Transient Snowflake errors are a real thing but they're rare; 9 times out of 10 a "transient" error is a deterministic upstream problem that will recur.

## Step 5 — Related incident

The March `fct_orders_daily` regression (see `incidents/2026-04-09-orders-data-regression.md`) was caused by a silent null-drop pattern we've since banned. See that postmortem for why `not_null` tests matter more than you think.

## Related

- `data/snowflake-warehouse-suspended.md`
- `data/ingest-pipeline-lag.md`
- `data/dbt-conventions.md`
