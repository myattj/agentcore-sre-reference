"""Three full incident timelines.

Each incident is a single parent post in ``#incidents`` with 20–30
threaded replies showing the real flow: initial kickoff, triage
updates, fix attempts, resolution, postmortem handoff. These are the
longest, richest threads in the seed set — they're what the agent
uses when the user asks "catch me up on the X incident" via
``read_thread_context``.

The incidents intentionally reference the same services, runbooks,
and teammates mentioned in casual_chatter and qa_history so
cross-channel correlation (the "remember when Priya said Y last
week" pattern) actually works.

    Incident 1: Feb checkout-api 504 flood (resolved, postmortem done)
    Incident 2: Ingest pipeline Snowflake contention (ongoing, unresolved)
    Incident 3: Orders dbt data regression (recent, resolved same-day)
"""
from __future__ import annotations

from .._common import SeedMessage


def build() -> list[SeedMessage]:
    m: list[SeedMessage] = []

    # ========================================================================
    # INCIDENT 1 — Feb checkout-api 504 flood (resolved, has postmortem)
    # ========================================================================

    m.append(SeedMessage("in_001", "incidents", "morgan",
        ":rotating_light: **INCIDENT DECLARED — checkout-api** :rotating_light:\n\n"
        "Severity: SEV-2\n"
        "Impact: ~12% of checkout requests returning 504 since 22:47 PT\n"
        "Commander: me\n"
        "Scribe: taylor\n"
        "Duration so far: 6 min\n\n"
        "Thread for all updates. Ops bridge: not open yet, escalating if not resolved in 10 min."))

    m.append(SeedMessage("in_001_01", "incidents", "morgan", 
        "22:47 — pagerduty fired on checkout-api 5xx > 2%, taylor and I started investigating", parent_key="in_001"))
    m.append(SeedMessage("in_001_02", "incidents", "taylor", 
        "22:49 — confirmed it's the /checkout/confirm endpoint specifically. /checkout/init is fine. that narrows it to the payment provider call path", parent_key="in_001"))
    m.append(SeedMessage("in_001_03", "incidents", "morgan", 
        "22:51 — payment provider status page shows their gateway at 'degraded'. they acknowledged 1 min ago. our 5xx is likely downstream propagation", parent_key="in_001"))
    m.append(SeedMessage("in_001_04", "incidents", "taylor", 
        "22:52 — confirming: datadog shows payment provider latency p99 jumped from 180ms → 4.2s at 22:46. circuit breaker in checkout-api is NOT tripping because we set the threshold at 5s and they're at 4.2s :cry:", parent_key="in_001"))
    m.append(SeedMessage("in_001_05", "incidents", "morgan", 
        "22:54 — I'm manually lowering the circuit breaker timeout from 5s → 2s to shed load. if that works, good; if not, we drain checkout traffic to the secondary payment path", parent_key="in_001"))
    m.append(SeedMessage("in_001_06", "incidents", "sam", 
        "22:55 — pulled up. what can I help with", parent_key="in_001"))
    m.append(SeedMessage("in_001_07", "incidents", "morgan", 
        "sam: watch the queue depth on orders-api, it's going to back up if we can't drain checkout. if it exceeds 5k, page me immediately", parent_key="in_001"))
    m.append(SeedMessage("in_001_08", "incidents", "taylor", 
        "22:57 — circuit breaker change deployed via helm. 5xx rate dropping. payment provider latency still at 3.8s, their side is not improving", parent_key="in_001"))
    m.append(SeedMessage("in_001_09", "incidents", "morgan", 
        "22:58 — status update posted to #eng-general. will post another in 10 min.", parent_key="in_001"))
    m.append(SeedMessage("in_001_10", "incidents", "morgan", 
        "23:02 — payment provider latency back to 200ms. they posted an 'all clear' on their status page. our 5xx rate back to 0.05%", parent_key="in_001"))
    m.append(SeedMessage("in_001_11", "incidents", "taylor", 
        "23:04 — BUT — our own retry policy is now flooding them as soon as they're back. they paged us back saying 'please back off'. we sent 3x normal traffic in the first 30s of recovery", parent_key="in_001"))
    m.append(SeedMessage("in_001_12", "incidents", "morgan", 
        "23:05 — oh :expletive:. rolling back the circuit breaker change. the retry storm is now the primary issue", parent_key="in_001"))
    m.append(SeedMessage("in_001_13", "incidents", "morgan", 
        "23:08 — circuit breaker back to normal. retry queue draining. payment provider is holding. we're below normal 5xx now. monitoring.", parent_key="in_001"))
    m.append(SeedMessage("in_001_14", "incidents", "taylor", 
        "23:12 — all metrics green for 4 minutes. pagerduty alert resolved. I think we're out of the woods", parent_key="in_001"))
    m.append(SeedMessage("in_001_15", "incidents", "morgan",
        "23:14 — **INCIDENT RESOLVED**\n\n"
        "Duration: ~27 min\n"
        "Impact: ~8,400 failed checkout attempts, ~2% of daily checkout volume\n"
        "Root cause: payment provider outage amplified by our retry storm on recovery\n\n"
        "Action items:\n"
        ":one: fix retry policy — exponential backoff with jitter (tracking: PR on acme-data-api)\n"
        ":two: lower circuit breaker timeout to 3s default (tracking: runbook update)\n"
        ":three: add a 'payment provider recovery' chaos test to the quarterly game day\n"
        ":four: postmortem meeting scheduled for tomorrow 10am, I'll prep the doc tonight\n\n"
        "Thanks to everyone who pulled up. Back to bed :sleeping:",
        parent_key="in_001"))
    m.append(SeedMessage("in_001_16", "incidents", "priya", 
        "23:16 — thanks team. big save, nobody outside really noticed this beyond the inevitable support tickets tomorrow", parent_key="in_001"))
    m.append(SeedMessage("in_001_17", "incidents", "jamie", 
        "23:18 — customer success knows. we're drafting a 'brief partial outage' note for the status page. will post before morning EU traffic", parent_key="in_001"))
    m.append(SeedMessage("in_001_18", "incidents", "morgan", 
        "(next day) 11:04 — postmortem doc posted in acme-runbooks → `incidents/2026-02-14-checkout-api-504-retry-storm.md`. the key takeaway: our retry policy was 'fixed retry interval with no jitter', which is a classic thundering herd. switching to exponential with jitter as the default for all outbound HTTP clients", parent_key="in_001"))
    m.append(SeedMessage("in_001_19", "incidents", "morgan", 
        "checkout-api v2.18.3 (the retry fix) is the direct output of this incident. see PR #412 if you want the diff", parent_key="in_001"))

    # ========================================================================
    # INCIDENT 2 — Ingest pipeline Snowflake contention (ongoing, unresolved)
    # ========================================================================

    m.append(SeedMessage("in_002", "incidents", "priya",
        ":rotating_light: **INCIDENT DECLARED — ingest-pipeline latency** :rotating_light:\n\n"
        "Severity: SEV-3 (no customer impact, reporting only)\n"
        "Impact: ingest-pipeline lag p95 elevated from ~8s → 45s intermittently\n"
        "Commander: me\n"
        "Scribe: jordan\n"
        "Duration: 2h and counting\n\n"
        "Opening this as an incident to get focused attention. This has been grumbling for a week, snowflake support is engaged but we don't have a fix yet. Thread for investigation notes."))

    m.append(SeedMessage("in_002_01", "incidents", "priya", 
        "symptoms: ingest pipeline COPY INTO operations from S3 to raw_events table are queueing up intermittently. p95 normally 8s, now 45s+ in bursts. happens ~3x per day, each burst 10-20 min. no obvious pattern by hour or day", parent_key="in_002"))
    m.append(SeedMessage("in_002_02", "incidents", "priya",
        "what we've tried:\n"
        ":one: increased warehouse size from M → L — didn't help, not a compute issue\n"
        ":two: added `MAX_FILE_SIZE_HINT` to COPY INTO — minor improvement, not a fix\n"
        ":three: enabled snowflake query acceleration service on the warehouse — no effect on COPY\n"
        ":four: looked for lock contention via `QUERY_HISTORY_BY_WAREHOUSE` — some correlation with big MERGE operations but not 1:1",
        parent_key="in_002"))
    m.append(SeedMessage("in_002_03", "incidents", "jordan", 
        "pulling the query history for the last 7 days filtered by warehouse, grouping by time bucket. I'll post the csv in this thread", parent_key="in_002"))
    m.append(SeedMessage("in_002_04", "incidents", "priya", 
        "I opened a support case with snowflake 3 days ago (case #1234567). they responded yesterday saying 'pipe queue contention during concurrent ingestion' but didn't give us an actionable fix. I pinged them again this morning", parent_key="in_002"))
    m.append(SeedMessage("in_002_05", "incidents", "priya", 
        "working theory: we have two separate ingest workloads running on the same warehouse — the event stream (high frequency, small files) and the finance extract (low frequency, big files). when the finance extract runs, it monopolizes the warehouse's pipe slots and the event stream backs up", parent_key="in_002"))
    m.append(SeedMessage("in_002_06", "incidents", "jordan", 
        "query history csv: [would be an attachment]. I see 3 clear bursts in the last 24h that line up with finance extract runs. theory is probably right", parent_key="in_002"))
    m.append(SeedMessage("in_002_07", "incidents", "priya", 
        "ok, new plan: we split the workloads. event stream gets its own warehouse (`INGEST_STREAM_WH`), finance extract stays on the current one. cost is marginal (+$50/mo), isolation is clean. I'll write the terraform and the dbt profile changes", parent_key="in_002"))
    m.append(SeedMessage("in_002_08", "incidents", "morgan", 
        "+1 from an sre perspective. workload isolation > shared resource at this scale", parent_key="in_002"))
    m.append(SeedMessage("in_002_09", "incidents", "priya", 
        "acme-infra PR #92 is up — new warehouse. reviewing and merging once CI passes", parent_key="in_002"))
    m.append(SeedMessage("in_002_10", "incidents", "priya", 
        "heads up: the cutover will require a ~2min pause on the event stream while we move the dbt profile. I'll schedule for tomorrow 10am PT, low-traffic window. this incident stays open until the cutover is validated", parent_key="in_002"))
    m.append(SeedMessage("in_002_11", "incidents", "jordan", 
        "I'll run the 'before/after' comparison once the cutover is done. expect p95 back in the 10s range", parent_key="in_002"))
    m.append(SeedMessage("in_002_12", "incidents", "priya",
        "action items tracked:\n"
        ":one: acme-infra PR #92 — new warehouse (awaiting review)\n"
        ":two: dbt profile change (draft)\n"
        ":three: cutover schedule (tomorrow 10am PT)\n"
        ":four: monitor tuning after cutover\n"
        ":five: add runbook `ingest-pipeline-lag-contention.md` once we prove the fix\n"
        "will keep updating this thread as we execute",
        parent_key="in_002"))

    # ========================================================================
    # INCIDENT 3 — Orders dbt data regression (recent, resolved same-day)
    # ========================================================================

    m.append(SeedMessage("in_003", "incidents", "priya",
        ":rotating_light: **INCIDENT DECLARED — fct_orders_daily row count regression** :rotating_light:\n\n"
        "Severity: SEV-2\n"
        "Impact: fct_orders_daily showing -40% rows vs 7-day avg. reporting dashboards will be wrong until we fix.\n"
        "Commander: me\n"
        "Scribe: jordan\n\n"
        "Datadog sentry caught the regression at 07:12 PT. Thread for updates."))

    m.append(SeedMessage("in_003_01", "incidents", "priya", 
        "07:14 — confirming the regression is real, not a query artifact. `select count(*) from fct_orders_daily where dt = current_date - 1` returns 12,847 vs 7-day avg of 21,500", parent_key="in_003"))
    m.append(SeedMessage("in_003_02", "incidents", "jordan", 
        "07:17 — I have the dbt run log from last night. the run succeeded (no errors), but the upstream model `stg_orders` also shows fewer rows. so the regression is upstream of fct_orders", parent_key="in_003"))
    m.append(SeedMessage("in_003_03", "incidents", "priya", 
        "07:20 — checking raw_orders — same thing, fewer rows than expected. so the regression is all the way upstream at ingest. priority shift: this is an ingest issue, not a dbt issue", parent_key="in_003"))
    m.append(SeedMessage("in_003_04", "incidents", "priya", 
        "07:22 — THE FINANCE ETL MIGRATION YESTERDAY. the finance team pushed a change that renamed a column in the upstream `orders_source` schema. our ingest job was not updated so it's dropping rows silently where the column is null", parent_key="in_003"))
    m.append(SeedMessage("in_003_05", "incidents", "priya", 
        "07:23 — Sam, can you check the finance etl migration notes for the column rename? I want to confirm", parent_key="in_003"))
    m.append(SeedMessage("in_003_06", "incidents", "sam", 
        "07:28 — confirmed. yesterday's migration renamed `order_total_cents` → `order_total_amount_cents` in the source. our ingest has a SELECT on the old column and dbt is silently nulling it, which triggers the data quality filter in `stg_orders` that drops null rows", parent_key="in_003"))
    m.append(SeedMessage("in_003_07", "incidents", "priya", 
        "07:30 — root cause confirmed. fixing now. two changes: (1) update the ingest to use the new column name, (2) remove the silent-drop in stg_orders (we should ERROR on null, not drop). backfilling the missing rows will take ~45 min", parent_key="in_003"))
    m.append(SeedMessage("in_003_08", "incidents", "priya", 
        "07:34 — acme-data-api PR #434 is up — the ingest and stg_orders fix. tests passing, merging", parent_key="in_003"))
    m.append(SeedMessage("in_003_09", "incidents", "jordan", 
        "07:37 — I'll handle the backfill. running `dbt run --select stg_orders+ --full-refresh` scoped to the last 7 days of data to catch anything that was silently dropped", parent_key="in_003"))
    m.append(SeedMessage("in_003_10", "incidents", "priya", 
        "07:40 — posted status to #eng-general: 'reporting dashboards will look weird until ~08:30 while we backfill'", parent_key="in_003"))
    m.append(SeedMessage("in_003_11", "incidents", "jordan", 
        "08:21 — backfill complete. `fct_orders_daily` row counts now back to expected range", parent_key="in_003"))
    m.append(SeedMessage("in_003_12", "incidents", "priya",
        "08:24 — **INCIDENT RESOLVED**\n\n"
        "Duration: ~72 min\n"
        "Impact: 7 days of reporting data under-counted until backfill. no customer-visible impact.\n"
        "Root cause: finance etl migration yesterday renamed an upstream column; our ingest silently nulled it; stg_orders filter dropped null rows\n\n"
        "Action items:\n"
        ":one: remove silent-null-drop from stg_orders — DONE (part of the fix)\n"
        ":two: add a dbt test for `not_null` on `order_total_amount_cents` — TODO today\n"
        ":three: cross-team change management — finance needs to notify us BEFORE schema migrations\n"
        ":four: postmortem (shorter than usual since fix was clean) — I'll draft today\n\n"
        "Lesson: 'silently drop bad rows' is the opposite of what you want when the bad rows are a canary for upstream changes. we're auditing all dbt filters for similar patterns",
        parent_key="in_003"))
    m.append(SeedMessage("in_003_13", "incidents", "alex", 
        "08:26 — re: cross-team change mgmt — want me to loop finance in on the postmortem? happy to facilitate", parent_key="in_003"))
    m.append(SeedMessage("in_003_14", "incidents", "priya", 
        "08:27 — yes please, that would be great. thanks alex", parent_key="in_003"))

    return m
