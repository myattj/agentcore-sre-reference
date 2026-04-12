"""Ambient Acme Data Co team chatter.

Standups, PR mentions, deploy announcements, off-topic banter. No
threading in this pack — these are standalone posts that establish
the workspace feels alive. They also seed keywords the Q&A packs
reference (service names, past events, team preferences) so
``search_team_history`` has something to surface.
"""
from __future__ import annotations

from .._common import SeedMessage


def build() -> list[SeedMessage]:
    m: list[SeedMessage] = []

    # ---- Morning standup / daily sync in eng-general ------------------------
    m.append(SeedMessage("ch_001", "eng-general", "morgan",
        "morning all :coffee: heads up — I'll be in interviews 10–12 today so ping Taylor for any infra escalations"))
    m.append(SeedMessage("ch_002", "eng-general", "priya",
        "morning! data team update: the `fct_orders_daily` dbt model is live in prod. if anything looks off in reporting dashboards lmk"))
    m.append(SeedMessage("ch_003", "eng-general", "sam",
        "FYI checkout-api v2.18.3 going out this afternoon — just the retry-backoff fix we talked about in yesterday's incident thread, low risk"))
    m.append(SeedMessage("ch_004", "eng-general", "riley",
        "reminder: EKS 1.29 upgrade is next Tuesday 2pm. acme-infra PR is up, I'll post the full runbook in #oncall later today"))
    m.append(SeedMessage("ch_005", "eng-general", "alex",
        "security reminder :lock: — the quarterly access review goes out Friday. please actually do it this time, I had to chase 7 people last quarter"))
    m.append(SeedMessage("ch_006", "eng-general", "jamie",
        "heads up from product side: the Q2 roadmap doc is in the usual place. three themes: checkout reliability, reporting self-serve, and data catalog mvp"))

    # ---- PR + deploy traffic (GitHub bot in eng-general) --------------------
    m.append(SeedMessage("ch_007", "eng-general", "github",
        ":large_green_circle: Merged `acme-data-api` PR #412 — bump fastapi to 0.115.4 (sam)"))
    m.append(SeedMessage("ch_008", "eng-general", "github",
        ":large_green_circle: Merged `acme-infra` PR #88 — rds-prod: bump instance class to db.r6g.xlarge (morgan)"))
    m.append(SeedMessage("ch_009", "eng-general", "github",
        ":large_green_circle: Merged `acme-runbooks` PR #31 — new runbook: snowflake-warehouse-suspended (priya)"))
    m.append(SeedMessage("ch_010", "eng-general", "github",
        ":arrows_counterclockwise: Deploy started for checkout-api v2.18.3 to production"))
    m.append(SeedMessage("ch_011", "eng-general", "github",
        ":white_check_mark: Deploy finished for checkout-api v2.18.3 to production (6m12s, no alerts)"))

    # ---- #eng-random banter -------------------------------------------------
    m.append(SeedMessage("ch_012", "eng-random", "taylor",
        "anyone else noticing the office coffee machine has been weirdly aggressive this week, or is that just me"))
    m.append(SeedMessage("ch_013", "eng-random", "jordan",
        "i've been using the portafilter one since last Thursday, the main one is making weird noises"))
    m.append(SeedMessage("ch_014", "eng-random", "morgan",
        "ok unrelated but if anyone wants to nerd out on distributed tracing there's a talk tomorrow at the SF observability meetup, lmk if you want to carpool"))
    m.append(SeedMessage("ch_015", "eng-random", "priya",
        "wait is that the Honeycomb one? I had to bail last month because of the pipeline drama, definitely interested"))
    m.append(SeedMessage("ch_016", "eng-random", "sam",
        "btw the rice place next to the office finally opened back up :partying_face:"))
    m.append(SeedMessage("ch_017", "eng-random", "jamie",
        "does anyone have a good template for Q2 OKR setting? our last one was kind of a mess"))
    m.append(SeedMessage("ch_018", "eng-random", "riley",
        "I have one from the last platform team cycle, will dm you"))

    # ---- Oncall handoffs in #oncall ----------------------------------------
    m.append(SeedMessage("ch_019", "oncall", "taylor",
        "oncall handoff from last week: only real thing was the 03:14 PagerDuty page on checkout-api — 504s from the payment provider, self-resolved in ~6 min. logged in the incident thread. nothing else firing."))
    m.append(SeedMessage("ch_020", "oncall", "morgan",
        "thanks, taking it this week. sre runbook refresh still in-flight for the RDS rotation flow, will try to land that before next handoff"))
    m.append(SeedMessage("ch_021", "oncall", "taylor",
        "also quick note: the datadog monitor for `ingest-pipeline lag` is still flaky. priya has the repro, see thread in alerts-data from last thursday"))
    m.append(SeedMessage("ch_022", "oncall", "morgan",
        "ack, will look at it today"))

    # ---- Cross-references and reminders ------------------------------------
    m.append(SeedMessage("ch_023", "eng-general", "priya",
        "oh one more data thing — we are officially off the old airflow dags for orders_etl as of this morning. full cutover to dbt, the old dags are paused. if you see anything referencing `orders_etl_hourly` in a runbook, it's stale"))
    m.append(SeedMessage("ch_024", "eng-general", "riley",
        "side note: the Q1 Snowflake cost spike is done draining. April so far is ~34% cheaper vs March. mostly from the warehouse autosuspend change"))
    m.append(SeedMessage("ch_025", "eng-general", "jordan",
        "ok who's going to own writing the postmortem for the Feb checkout incident, it's still in draft"))
    m.append(SeedMessage("ch_026", "eng-general", "morgan",
        "I'll finish it this week. it's been sitting in my tabs for a month, sorry team"))

    # ---- Quick questions in ask-* channels (standalone, not threaded) -------
    m.append(SeedMessage("ch_027", "ask-platform", "jamie",
        "dumb q — when we say 'deploy window' in the runbooks, is that meant to be a hard constraint or a suggestion?"))
    m.append(SeedMessage("ch_028", "ask-data", "sam",
        "how do i get the current row count for `fct_orders_daily` without burning a full warehouse query"))
    m.append(SeedMessage("ch_029", "ask-security", "jordan",
        "is Okta the right place to request access to the new reporting dashboard, or is that a Confluence thing"))

    # ---- Late-night / weekend-ish traffic ----------------------------------
    m.append(SeedMessage("ch_030", "eng-random", "taylor",
        "just fixed the coffee machine. it was a bean hopper issue, not the grinder. thread of fixes in case it happens again :arrow_right: check bean moisture, then clean the hopper sensor, then reset the hopper lid sensor, THEN call the vendor"))
    m.append(SeedMessage("ch_031", "eng-general", "morgan",
        "mini reminder — if you're setting up new `acme-data-api` locally, the default-branch rebase flow changed last week. pin to `main`, not `master` (we renamed it finally)"))
    m.append(SeedMessage("ch_032", "eng-general", "alex",
        "security note: we're rotating the prod database password next Monday morning. acme-data-api will need a deploy right after to pick up the new secret. Morgan and I synced on the sequencing, it's in the runbooks repo as `rds-password-rotation.md`"))

    return m
