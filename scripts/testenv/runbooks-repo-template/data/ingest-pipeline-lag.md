# Ingest pipeline lag

> **Owner:** Data Eng · **Last updated:** 2026-04-09

## tl;dr

When `ingest-pipeline lag p95 > 60s` fires, check in this order:

1. **Snowflake COPY INTO queue depth** — most common cause (throttled copies)
2. **Kafka consumer lag** — second most common
3. **S3 raw bucket write rate** — rarely the issue but worth checking

## Step 1 — Snowflake COPY queue

```sql
SELECT COUNT(*) AS pending_copies,
       MIN(scheduled_time) AS oldest
FROM SNOWFLAKE.ACCOUNT_USAGE.COPY_HISTORY
WHERE pipe_name IN (
  'RAW_EVENTS_PIPE',
  'ORDERS_PIPE',
  'USERS_PIPE'
)
  AND status = 'PENDING';
```

If `pending_copies > 50`, the pipe is contended. Check what else is running on the warehouse:

```sql
SELECT query_id, query_text, warehouse_name, start_time, execution_status
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
WHERE start_time > dateadd(minute, -15, current_timestamp())
  AND warehouse_name = 'INGEST_WH'
ORDER BY start_time DESC;
```

Known culprit: finance extract (big MERGE operations) monopolizing the warehouse. Fix: move finance extract to its own warehouse.

## Step 2 — Kafka consumer lag

```bash
kubectl -n prod exec -it $(kubectl -n prod get pod -l app=ingest-pipeline -o jsonpath='{.items[0].metadata.name}') -- \
  kafka-consumer-groups.sh \
    --bootstrap-server kafka.prod.acmedata.co:9092 \
    --describe \
    --group ingest-pipeline-prod
```

Look at the `LAG` column. Normal: <1000. Investigate: 1000-10000. Urgent: >10000.

## Step 3 — S3 write rate

```bash
aws s3api list-objects-v2 \
  --bucket acme-raw-events \
  --prefix "year=$(date +%Y)/month=$(date +%m)/day=$(date +%d)/hour=$(date +%H)" \
  --query "length(Contents)"
```

Normal: 200 objects/min. Investigate if: <50.

## Historical context

See the open incident thread in `#incidents` (incident-2: "ingest-pipeline Snowflake contention"). We've been chasing a workload-isolation issue for ~2 weeks. Current plan: split `INGEST_WH` into `INGEST_STREAM_WH` (event stream) and `INGEST_BATCH_WH` (finance extract). PR #92 on `acme-infra`.

## Related

- `data/snowflake-warehouse-suspended.md`
- `data/dbt-model-failure.md`
- `data/snowflake-cost-optimization.md`
