#!/usr/bin/env python3
"""
Seed Datadog with synthetic metrics for an N+1 query / connection pool incident.

Scenario: A deploy introduced an N+1 query on the items export endpoint,
causing latency to spike. Then a connection pool config change made it worse.

Timeline (relative to "now"):
  T-4h to T-2h : Normal baseline
  T-2h         : "Deploy" of N+1 export endpoint
  T-2h to T-1h : Gradual latency increase on /api/v1/items/export
  T-1h         : Latency crosses 2000ms threshold
  T-1h to now  : Error rate climbs as connection pool saturates

Usage:
  uv run --with requests python seed/seed_datadog_metrics.py
  DD_API_KEY=... DD_APP_KEY=... uv run --with requests \
    python seed/seed_datadog_metrics.py --apply
"""

import argparse
import os
import random
import sys
import time

import requests

DEFAULT_SEED = 20260412
DATADOG_SITES = frozenset(
    {
        "datadoghq.com",
        "us3.datadoghq.com",
        "us5.datadoghq.com",
        "datadoghq.eu",
        "ap1.datadoghq.com",
        "ap2.datadoghq.com",
        "ddog-gov.com",
    }
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ts(offset_seconds: float, now: float) -> int:
    """Return a Unix timestamp at *offset_seconds* before *now*."""
    return int(now + offset_seconds)


def validate_datadog_site(site: str) -> str:
    """Return a supported Datadog site hostname or raise ``ValueError``.

    Keeping this to an explicit allowlist prevents a typo (or a URL containing a
    path, port, or credentials) from turning the seeder into an arbitrary HTTP
    client.
    """
    normalized = site.strip().lower()
    if normalized not in DATADOG_SITES:
        supported = ", ".join(sorted(DATADOG_SITES))
        raise ValueError(
            f"unsupported Datadog site {site!r}; choose one of: {supported}"
        )
    return normalized


def _noise(base: float, rng: random.Random, pct: float = 0.15) -> float:
    """Add +/- *pct* jitter around *base*."""
    return base * (1 + rng.uniform(-pct, pct))


# ---------------------------------------------------------------------------
# Metric generators
# ---------------------------------------------------------------------------


def generate_export_latency(now: float, rng: random.Random) -> list[dict]:
    """acme.api.request.duration for /api/v1/items/export."""
    points = []
    for offset in range(-4 * 3600, 1, 60):
        ts = _ts(offset, now)
        t_rel = offset + 4 * 3600  # seconds since T-4h (0..14400)

        if t_rel < 7200:
            # T-4h to T-2h: baseline 150-250ms
            value = _noise(200, rng, 0.25)
        elif t_rel < 10800:
            # T-2h to T-1h: ramp 250 -> 5000ms
            progress = (t_rel - 7200) / 3600
            value = _noise(250 + 4750 * progress, rng, 0.10)
        else:
            # T-1h to now: fluctuate 3000-8000ms
            value = _noise(5500, rng, 0.45)

        points.append({"timestamp": ts, "value": round(value, 1)})

    return [
        {
            "metric": "acme.api.request.duration",
            "type": 3,  # gauge
            "points": points,
            "tags": [
                "service:acme-data-api",
                "env:production",
                "endpoint:/api/v1/items/export",
            ],
            "unit": "millisecond",
        }
    ]


def generate_items_latency(now: float, rng: random.Random) -> list[dict]:
    """acme.api.request.duration for /api/v1/items (healthy contrast)."""
    points = []
    for offset in range(-4 * 3600, 1, 60):
        ts = _ts(offset, now)
        value = _noise(85, rng, 0.40)  # 50-120ms
        points.append({"timestamp": ts, "value": round(value, 1)})

    return [
        {
            "metric": "acme.api.request.duration",
            "type": 3,  # gauge
            "points": points,
            "tags": [
                "service:acme-data-api",
                "env:production",
                "endpoint:/api/v1/items",
            ],
            "unit": "millisecond",
        }
    ]


def generate_request_count(now: float, rng: random.Random) -> list[dict]:
    """acme.api.request.count for /api/v1/items/export."""
    points = []
    for offset in range(-4 * 3600, 1, 60):
        ts = _ts(offset, now)
        value = _noise(7.5, rng, 0.35)  # 5-10 req/min
        points.append({"timestamp": ts, "value": round(value, 1)})

    return [
        {
            "metric": "acme.api.request.count",
            "type": 1,  # count
            "points": points,
            "tags": [
                "service:acme-data-api",
                "env:production",
                "endpoint:/api/v1/items/export",
            ],
        }
    ]


def generate_error_count(now: float, rng: random.Random) -> list[dict]:
    """acme.api.error.count — timeouts after T-1h."""
    points = []
    for offset in range(-4 * 3600, 1, 60):
        ts = _ts(offset, now)
        t_rel = offset + 4 * 3600

        if t_rel < 10800:
            value = 0.0
        else:
            value = float(rng.randint(1, 3))

        points.append({"timestamp": ts, "value": value})

    return [
        {
            "metric": "acme.api.error.count",
            "type": 1,  # count
            "points": points,
            "tags": [
                "service:acme-data-api",
                "env:production",
                "error_type:timeout",
            ],
        }
    ]


def generate_db_query_duration(now: float, rng: random.Random) -> list[dict]:
    """acme.db.query.duration — per-query time stays fast (N+1 signal)."""
    points = []
    for offset in range(-4 * 3600, 1, 60):
        ts = _ts(offset, now)
        value = _noise(3.5, rng, 0.40)  # 2-5ms always
        points.append({"timestamp": ts, "value": round(value, 2)})

    return [
        {
            "metric": "acme.db.query.duration",
            "type": 3,  # gauge
            "points": points,
            "tags": [
                "service:acme-data-api",
                "env:production",
                "query:select_user_by_id",
            ],
            "unit": "millisecond",
        }
    ]


def generate_connection_pool(now: float, rng: random.Random) -> list[dict]:
    """acme.db.connection_pool.active — ramps to saturation."""
    points = []
    for offset in range(-4 * 3600, 1, 60):
        ts = _ts(offset, now)
        t_rel = offset + 4 * 3600

        if t_rel < 7200:
            # baseline: 2-3 active
            value = _noise(2.5, rng, 0.20)
        elif t_rel < 10800:
            # ramp from 3 to 8
            progress = (t_rel - 7200) / 3600
            value = _noise(3 + 5 * progress, rng, 0.08)
        else:
            # saturated at 8
            value = _noise(8.0, rng, 0.03)

        points.append({"timestamp": ts, "value": round(max(1, value), 1)})

    return [
        {
            "metric": "acme.db.connection_pool.active",
            "type": 3,  # gauge
            "points": points,
            "tags": [
                "service:acme-data-api",
                "env:production",
            ],
        }
    ]


# ---------------------------------------------------------------------------
# Submission
# ---------------------------------------------------------------------------

GENERATORS = [
    ("export endpoint latency (/api/v1/items/export)", generate_export_latency),
    ("items endpoint latency (/api/v1/items — healthy)", generate_items_latency),
    ("request count (/api/v1/items/export)", generate_request_count),
    ("error count (timeouts)", generate_error_count),
    ("DB query duration (select_user_by_id)", generate_db_query_duration),
    ("connection pool active count", generate_connection_pool),
]


def generate_metric_batches(now: float, seed: int) -> list[tuple[str, list[dict]]]:
    """Generate every metric batch deterministically for ``now`` and ``seed``."""
    rng = random.Random(seed)
    return [(label, generator(now, rng)) for label, generator in GENERATORS]


def submit_series(series: list[dict], api_key: str, app_key: str, site: str) -> None:
    """POST a batch of series to the Datadog v2 metrics API."""
    site = validate_datadog_site(site)
    url = f"https://api.{site}/api/v2/series"
    headers = {
        "DD-API-KEY": api_key,
        "DD-APPLICATION-KEY": app_key,
        "Content-Type": "application/json",
    }

    # Datadog v2 expects { "series": [ { ... resources/points ... } ] }
    # Convert our flat points list into the v2 envelope.
    v2_series = []
    for s in series:
        v2_points = [
            {"timestamp": p["timestamp"], "value": p["value"]} for p in s["points"]
        ]
        v2_series.append(
            {
                "metric": s["metric"],
                "type": s["type"],
                "points": v2_points,
                "tags": s.get("tags", []),
                **({"unit": s["unit"]} if "unit" in s else {}),
            }
        )

    payload = {"series": v2_series}

    resp = requests.post(url, json=payload, headers=headers, timeout=30)
    if resp.status_code not in (200, 202):
        print(f"  ERROR: HTTP {resp.status_code} — {resp.text[:300]}", file=sys.stderr)
        raise requests.HTTPError(
            f"unexpected Datadog response status {resp.status_code}",
            response=resp,
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Seed Datadog with synthetic metrics for an N+1 / connection pool incident."
    )
    parser.add_argument(
        "--site",
        default=os.environ.get("DD_SITE", "datadoghq.com"),
        help="Datadog site, e.g. datadoghq.com or datadoghq.eu (default: datadoghq.com)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=DEFAULT_SEED,
        help=f"random seed for reproducible values (default: {DEFAULT_SEED})",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="submit metrics; without this flag the script only generates a preview",
    )
    args = parser.parse_args()

    try:
        site = validate_datadog_site(args.site)
    except ValueError as exc:
        parser.error(str(exc))

    api_key = os.environ.get("DD_API_KEY")
    app_key = os.environ.get("DD_APP_KEY")
    if args.apply and (not api_key or not app_key):
        parser.error("--apply requires DD_API_KEY and DD_APP_KEY environment variables")

    now = time.time()
    total_points = 0
    submitted_metrics: list[str] = []
    failed_batches: list[str] = []

    action = "Seeding" if args.apply else "Previewing"
    print(f"{action} Datadog metrics on {site} (seed={args.seed})")
    print(
        f"Time window: {time.strftime('%H:%M:%S', time.localtime(now - 4 * 3600))} to {time.strftime('%H:%M:%S', time.localtime(now))}"
    )
    print(f"{'=' * 60}")

    for label, series in generate_metric_batches(now, args.seed):
        verb = "Submitting" if args.apply else "Generated"
        print(f"\n  {verb}: {label} ...", end=" ", flush=True)
        n_points = sum(len(s["points"]) for s in series)
        if not args.apply:
            print(f"{n_points} points (dry run)")
            total_points += n_points
            submitted_metrics.append(series[0]["metric"])
            continue
        try:
            submit_series(series, api_key, app_key, site)
            print(f"{n_points} points OK")
            total_points += n_points
            submitted_metrics.append(series[0]["metric"])
        except requests.RequestException as exc:
            print(f"FAILED ({exc})", file=sys.stderr)

            failed_batches.append(label)

    if failed_batches:
        print(f"\n{'=' * 60}", file=sys.stderr)
        print(
            "Datadog seed failed. "
            f"Submitted {total_points} data points; "
            f"{len(failed_batches)} batch(es) failed:",
            file=sys.stderr,
        )
        for label in failed_batches:
            print(f"  - {label}", file=sys.stderr)
        return 1

    print(f"\n{'=' * 60}")
    result = "Submitted" if args.apply else "Generated"
    print(
        f"Done. {result} {total_points} data points across {len(submitted_metrics)} metric series."
    )
    if not args.apply:
        print(
            "No network requests were made. Re-run with --apply to submit this dataset."
        )
    print("\nMetrics created:")
    for m in submitted_metrics:
        print(f"  - {m}")
    print("\nTimeline anchors:")
    print(
        f"  Deploy (T-2h) : {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now - 2 * 3600))}"
    )
    print(
        f"  Threshold hit  : {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now - 1 * 3600))}"
    )
    print(
        f"  Now            : {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now))}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
