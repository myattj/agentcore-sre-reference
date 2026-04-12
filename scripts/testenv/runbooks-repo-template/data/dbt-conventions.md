# dbt conventions

> **Owner:** Data Eng · **Last updated:** 2026-02-25

## Naming

| Prefix | Purpose | Example |
|---|---|---|
| `stg_` | Staging — 1:1 with a source, lightly typed | `stg_orders` |
| `int_` | Intermediate — composed from staging, reused across marts | `int_order_line_items` |
| `fct_` | Fact table (mart layer) | `fct_orders_daily` |
| `dim_` | Dimension table (mart layer) | `dim_customers` |
| `rpt_` | Reporting / BI-facing view | `rpt_daily_revenue` |

If you see a model that doesn't follow this, it's probably pre-migration and needs renaming. File it under "data hygiene" tasks.

## File layout

```
acme-data-api/dbt/
├── models/
│   ├── staging/      # stg_*.sql
│   ├── intermediate/ # int_*.sql
│   ├── marts/        # fct_*.sql, dim_*.sql
│   └── reporting/    # rpt_*.sql
├── macros/
│   └── debug_helpers.sql
├── tests/            # singular tests
└── snapshots/
```

## Materializations

- Staging: `view` (cheap, always fresh)
- Intermediate: `view` for small, `incremental` for large
- Marts: `table` or `incremental`
- Reporting: `table` (BI tools prefer stable physical tables)

## Tests (enforced by CI)

Every model in `marts/` must have at least:

- `unique` + `not_null` on the primary key
- `not_null` on every column marked as required in the schema.yml description
- One business-logic test (custom test or `dbt_utils.expression_is_true`)

Staging/intermediate tests are encouraged but not enforced.

## Debug helpers

`check_upstream_nulls(model)` — row count and null distribution per column. See `data/dbt-model-failure.md` for usage.

`warehouse_cost(query_id)` — credits consumed by a specific query. Useful for optimization passes.

## Related

- `data/dbt-model-failure.md`
- `data/snowflake-cost-optimization.md`
