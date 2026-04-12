"""Q&A history — the bulk of content `search_team_history` surfaces.

20 Q&A threads across #ask-data, #ask-platform, #ask-security,
#eng-general, and #oncall. Each thread is a parent question followed
by 1–3 threaded replies, mimicking the typical "teammate asks,
teammate answers, OP acks" flow.

When the user later asks the agent something like "what does the team
think about dbt vs Airflow?", the agent runs ``search_team_history``
and these threads show up. They cover:

  - Stack-specific how-tos (Snowflake, dbt, Airflow, EKS, Terraform)
  - Ownership questions (who owns service X)
  - Past decisions (why we migrated from X to Y)
  - Style/process questions (deploy windows, code review norms)
  - Debug recipes (how to investigate X error)
"""
from __future__ import annotations

from .._common import SeedMessage


def build() -> list[SeedMessage]:
    m: list[SeedMessage] = []

    # Thread 1: dbt vs Airflow -----------------------------------------------
    m.append(SeedMessage("qa_001", "ask-data", "jamie",
        "genuine question — why did we move the orders ETL from Airflow to dbt? was Airflow actually broken or was it more of a preference thing"))
    m.append(SeedMessage("qa_001a", "ask-data", "priya", 
        "both honestly. the Airflow dag had ~40 tasks with brittle dependencies and retries that would silently double-insert on retry. dbt is declarative so we just define the models and let it figure out the DAG. the deduping issue alone was worth the cutover", parent_key="qa_001"))
    m.append(SeedMessage("qa_001b", "ask-data", "jordan", 
        "+1 — also the monitoring story is way better with dbt-cloud + our custom slack alerts. Airflow's UI for debugging a failed task was a nightmare", parent_key="qa_001"))
    m.append(SeedMessage("qa_001c", "ask-data", "jamie", 
        "got it, that makes sense. is the migration fully done or are there still Airflow dags running anything", parent_key="qa_001"))
    m.append(SeedMessage("qa_001d", "ask-data", "priya", 
        "fully done for orders. we still have airflow running a handful of legacy dags for finance reporting — those are on the deprecation list for Q3", parent_key="qa_001"))

    # Thread 2: RDS connection pooling ---------------------------------------
    m.append(SeedMessage("qa_002", "ask-platform", "sam",
        "stupid question but is there a recommended max pool size for rds connections from checkout-api pods? I'm seeing 'remaining connection slots reserved' errors intermittently"))
    m.append(SeedMessage("qa_002a", "ask-platform", "morgan", 
        "not stupid at all — we've been burned by this. the instance has max_connections=500 and we run 20 pods, so pool_size=20 per pod is the ceiling. currently set to 15 in the checkout-api config to leave headroom. what's yours at", parent_key="qa_002"))
    m.append(SeedMessage("qa_002b", "ask-platform", "sam", 
        "I have it at 25 :see_no_evil: that's probably my answer. will ship a fix", parent_key="qa_002"))
    m.append(SeedMessage("qa_002c", "ask-platform", "morgan", 
        "yep. also there's a runbook `rds-connection-exhaustion.md` that has the full math, worth bookmarking", parent_key="qa_002"))

    # Thread 3: who owns reporting-worker ------------------------------------
    m.append(SeedMessage("qa_003", "eng-general", "jamie",
        "who owns reporting-worker these days? I need to request a new metric added"))
    m.append(SeedMessage("qa_003a", "eng-general", "priya", 
        "data-eng owns it but riley has been doing most of the platform-side work on it lately. riley or me for anything scope-ish", parent_key="qa_003"))
    m.append(SeedMessage("qa_003b", "eng-general", "riley", 
        "yep. jamie file an issue on acme-data-api with label `reporting-worker` and I'll pick it up", parent_key="qa_003"))

    # Thread 4: checkout-api 504s past incident ------------------------------
    m.append(SeedMessage("qa_004", "ask-platform", "jordan",
        "how did we end up resolving the checkout-api 504s from last month? I remember it being a payment provider thing but the details are fuzzy"))
    m.append(SeedMessage("qa_004a", "ask-platform", "morgan", 
        "two-part fix. (1) payment provider had their own upstream issue for ~8 min, self-resolved. (2) our retry policy was aggressive enough that when they came back, we sent 3x normal traffic and tipped them over again. we fixed the retry backoff in checkout-api v2.18.3 — exponential with jitter now, starts at 500ms", parent_key="qa_004"))
    m.append(SeedMessage("qa_004b", "ask-platform", "morgan", 
        "full postmortem is still in draft (my bad) but the thread from that day in #alerts-sre has the timeline", parent_key="qa_004"))

    # Thread 5: Snowflake cost control ---------------------------------------
    m.append(SeedMessage("qa_005", "ask-data", "jamie",
        "the Q1 snowflake bill was brutal. what did we do to fix it? I want to explain this to finance"))
    m.append(SeedMessage("qa_005a", "ask-data", "priya", 
        "three things: (1) auto-suspend on all warehouses set to 60s instead of the default 10min — biggest single save, (2) split the reporting warehouse by audience so `ANALYTICS_WH` isn't carrying ad-hoc queries AND scheduled reports, (3) moved the heaviest dbt models to an XL warehouse that only spins up during the 2am run window", parent_key="qa_005"))
    m.append(SeedMessage("qa_005b", "ask-data", "priya", 
        "result: April MTD is ~34% under March despite more data volume. more details in acme-runbooks under `snowflake-cost-optimization.md`", parent_key="qa_005"))

    # Thread 6: deploy window interpretation ---------------------------------
    m.append(SeedMessage("qa_006", "ask-platform", "jamie",
        "when the runbooks say 'deploy window 10am-4pm PT', is that a hard rule or a suggestion? asking because I want to ship a small onboarding config change at 5pm"))
    m.append(SeedMessage("qa_006a", "ask-platform", "morgan", 
        "hard rule for checkout-api, orders-api, user-service. suggestion for everything else (including onboarding). the reason is oncall coverage — if something breaks outside the window, you're the oncall", parent_key="qa_006"))
    m.append(SeedMessage("qa_006b", "ask-platform", "taylor", 
        "+1. also if it's after hours and you're deploying, please post in #oncall first so we know to watch", parent_key="qa_006"))

    # Thread 7: where is the User model --------------------------------------
    m.append(SeedMessage("qa_007", "ask-platform", "sam",
        "where is the User model defined these days? I can't find it in the obvious place. acme-data-api/models/user.py doesn't exist anymore"))
    m.append(SeedMessage("qa_007a", "ask-platform", "riley", 
        "we refactored it into `acme-data-api/app/models/user.py` last sprint — the `app/` namespace is now the canonical entry point. the old `models/` path was a pre-refactor leftover", parent_key="qa_007"))
    m.append(SeedMessage("qa_007b", "ask-platform", "sam", 
        "found it, thanks. is that documented anywhere", parent_key="qa_007"))
    m.append(SeedMessage("qa_007c", "ask-platform", "riley", 
        "it is now — just added it to the acme-data-api README", parent_key="qa_007"))

    # Thread 8: ingest-pipeline lag alert ------------------------------------
    m.append(SeedMessage("qa_008", "ask-data", "jordan",
        "the ingest-pipeline lag datadog monitor has been flapping all week. is that a real signal or is the monitor broken"))
    m.append(SeedMessage("qa_008a", "ask-data", "priya", 
        "both. the monitor threshold was set too tight (p95 > 30s) and it doesn't account for the Sunday batch window. I'm adjusting to p95 > 60s excluding Sunday 01-04 UTC. however there IS a real intermittent slowdown related to snowflake COPY INTO contention, still digging", parent_key="qa_008"))
    m.append(SeedMessage("qa_008b", "ask-data", "priya", 
        "I have a thread in #alerts-data from last thursday with the investigation notes if anyone wants to join", parent_key="qa_008"))

    # Thread 9: security — SSO access request --------------------------------
    m.append(SeedMessage("qa_009", "ask-security", "sam",
        "how do I request access to the prod snowflake account? I got the 'not in acme-data-readers group' error"))
    m.append(SeedMessage("qa_009a", "ask-security", "alex", 
        "prod snowflake is gated by the `acme-data-readers` group in Okta. file a ticket in ServiceNow → 'Access Request' → select group → I auto-approve for anyone on eng or data-eng rosters. usually takes <1 business day", parent_key="qa_009"))
    m.append(SeedMessage("qa_009b", "ask-security", "alex", 
        "note: production TABLES are further gated at the snowflake role level. you get read-only on `PROD_REPORTING` by default; anything else needs a manager's approval in the same ticket", parent_key="qa_009"))

    # Thread 10: terraform plan weirdness ------------------------------------
    m.append(SeedMessage("qa_010", "ask-platform", "sam",
        "my terraform plan on acme-infra is showing a diff on the EKS cluster that nobody touched. every time I run plan it wants to re-apply the same tags. is this a known thing"))
    m.append(SeedMessage("qa_010a", "ask-platform", "riley", 
        "yes, ugh. it's the aws provider plugin not respecting tag casing consistently. there's a known workaround: add `ignore_changes = [tags[\"auto-generated\"]]` to the lifecycle block. I keep meaning to fix it properly with a null_resource hack", parent_key="qa_010"))
    m.append(SeedMessage("qa_010b", "ask-platform", "sam", 
        "ok, will add the ignore and move on", parent_key="qa_010"))

    # Thread 11: dbt model naming convention ---------------------------------
    m.append(SeedMessage("qa_011", "ask-data", "jordan",
        "naming convention question — when I add a new mart-level dbt model, is it `fct_` for facts, `dim_` for dimensions, or something else we're doing? I've seen both"))
    m.append(SeedMessage("qa_011a", "ask-data", "priya", 
        "`fct_` for facts, `dim_` for dimensions, `stg_` for staging, `int_` for intermediate. convention doc is in `acme-runbooks/data/dbt-conventions.md`. if you see a model that doesn't follow this, it's probably pre-migration and needs renaming", parent_key="qa_011"))

    # Thread 12: incident comms template -------------------------------------
    m.append(SeedMessage("qa_012", "eng-general", "jamie",
        "is there a template for incident comms? the one we used for the Feb incident was good"))
    m.append(SeedMessage("qa_012a", "eng-general", "morgan", 
        "yep, `acme-runbooks/incidents/comms-template.md`. it has sections for: initial page, status updates every 15min, resolution, and postmortem kickoff. the bot knows about it too if you ask", parent_key="qa_012"))

    # Thread 13: why are we on EKS vs self-managed ---------------------------
    m.append(SeedMessage("qa_013", "ask-platform", "jordan",
        "kind of a philosophical question but why did we go EKS instead of self-managed k8s on ec2? I know we discussed it before my time"))
    m.append(SeedMessage("qa_013a", "ask-platform", "morgan", 
        "three reasons: (1) we have 4 SREs for ~60 eng, managing our own control plane wasn't a good use of that headcount, (2) we wanted IAM integration for IRSA which EKS does cleanly, (3) upgrade cadence — EKS does the hard part for us. the cost premium vs self-managed is real but we pay it happily", parent_key="qa_013"))
    m.append(SeedMessage("qa_013b", "ask-platform", "riley", 
        "and the next upgrade (1.29) is the first one where we're not nervous about it. the runbook is in acme-runbooks under eks-version-upgrade.md", parent_key="qa_013"))

    # Thread 14: search for a past config discussion --------------------------
    m.append(SeedMessage("qa_014", "ask-data", "jamie",
        "didn't we have a discussion last month about moving reporting off the main snowflake warehouse? I can't find it"))
    m.append(SeedMessage("qa_014a", "ask-data", "priya", 
        "yes — we split it into ANALYTICS_WH (scheduled) and ANALYTICS_ADHOC_WH (human queries) in week 2 of April. saved us the ad-hoc-query blast radius problem", parent_key="qa_014"))

    # Thread 15: ops — how do I rotate an IAM key ----------------------------
    m.append(SeedMessage("qa_015", "ask-security", "sam",
        "I need to rotate an IAM access key for a deployment service account. is there a runbook or do I DIY"))
    m.append(SeedMessage("qa_015a", "ask-security", "alex", 
        "runbook exists: `acme-runbooks/security/iam-key-rotation.md`. high-level: create new key → deploy service with new key → verify → disable old → wait 24h → delete old. do NOT skip the wait step, we've tripped cloudtrail alarms doing that", parent_key="qa_015"))

    # Thread 16: what does Jordan own ----------------------------------------
    m.append(SeedMessage("qa_016", "eng-general", "sam",
        "quick who-owns-what — jordan, you own the reporting-worker infra side right? I need someone to review an acme-infra PR"))
    m.append(SeedMessage("qa_016a", "eng-general", "jordan", 
        "yep. drop the PR link, I'll review today", parent_key="qa_016"))

    # Thread 17: python version ----------------------------------------------
    m.append(SeedMessage("qa_017", "ask-platform", "sam",
        "what python version is acme-data-api on? I'm setting up a local branch"))
    m.append(SeedMessage("qa_017a", "ask-platform", "riley", 
        "3.13. uv pins it in pyproject.toml. if you're on 3.12 locally, `uv venv --python 3.13` to get the right one. we had to jump to 3.13 because 3.14 broke a couple SDK deps (agentcore CLI expects 3.13)", parent_key="qa_017"))

    # Thread 18: how to debug a failing dbt model ----------------------------
    m.append(SeedMessage("qa_018", "ask-data", "jordan",
        "dbt model `fct_orders_daily` failed this morning with a weird snowflake error. where do I even start debugging"))
    m.append(SeedMessage("qa_018a", "ask-data", "priya", 
        "runbook: `acme-runbooks/data/dbt-model-failure.md`. tl;dr: (1) check dbt-cloud run page for the actual error, (2) if it's a snowflake error, check if it's perm/resource/data, (3) if data, `select count(*) from {{ ref('upstream_model') }}` to check for upstream nulls/dupes", parent_key="qa_018"))
    m.append(SeedMessage("qa_018b", "ask-data", "priya", 
        "also the bot knows about this runbook — try `/runbook fct_orders_daily` in this channel and it'll pull up the runbook plus any past incidents that touched this model", parent_key="qa_018"))

    # Thread 19: git branch naming -------------------------------------------
    m.append(SeedMessage("qa_019", "eng-general", "jamie",
        "do we have a branch naming convention? I'm seeing `feat/`, `feature/`, `fix/`, `jm/`, just wondering what's canonical"))
    m.append(SeedMessage("qa_019a", "eng-general", "riley", 
        "there's no hard rule but the team preference is `{initials}/{short-description}` — e.g. `jm/reporting-metric-add`. CI doesn't care. there's no convention enforcement on branch names so people use whatever", parent_key="qa_019"))

    # Thread 20: confluence → notion migration -------------------------------
    m.append(SeedMessage("qa_020", "eng-general", "jordan",
        "are we actually migrating from confluence to notion or was that abandoned? my docs links are half-and-half"))
    m.append(SeedMessage("qa_020a", "eng-general", "jamie", 
        "migration is on pause. we moved the eng-wide runbooks to the acme-runbooks git repo (which is way better than either). confluence is still the canonical place for non-eng docs until product & marketing decide", parent_key="qa_020"))
    m.append(SeedMessage("qa_020b", "eng-general", "jordan", 
        "so tldr for eng stuff: check acme-runbooks first, fall back to confluence?", parent_key="qa_020"))
    m.append(SeedMessage("qa_020c", "eng-general", "jamie", 
        "exactly", parent_key="qa_020"))

    return m
