"""Probe anonymous LinkedIn guest-detail pacing without running a connector.

Fetch-only: one initial listing request plus a small, fixed set of guest-detail
requests. No database, progress, email, registry, or production default changes.
Trials stop after the first interval that receives HTTP 429.
"""
from __future__ import annotations

import argparse
import json
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import httpx

from app.collectors.html.linkedin import (
    LINKEDIN_INDIA_DETAIL_URL,
    LinkedInConnector,
    _parse_detail,
    _parse_listings,
)
from app.pipeline.relevance import first_failing_axis
from app.pipeline.window import parse_since


def _trial(
    job_ids: list[str], *, interval_seconds: float, workers: int
) -> dict:
    start_lock = threading.Lock()
    next_start = 0.0
    statuses: Counter[int] = Counter()
    latencies: list[float] = []
    response_bytes = 0
    descriptions = 0

    def fetch_one(client: httpx.Client, job_id: str) -> None:
        nonlocal next_start, response_bytes, descriptions
        with start_lock:
            now = time.monotonic()
            if next_start > now:
                time.sleep(next_start - now)
            next_start = time.monotonic() + interval_seconds
        started = time.monotonic()
        response = client.get(LINKEDIN_INDIA_DETAIL_URL.format(job_id=job_id))
        latency = time.monotonic() - started
        with start_lock:
            statuses[response.status_code] += 1
            latencies.append(latency)
            response_bytes += len(response.content)
            if response.status_code == 200 and _parse_detail(response.text).get(
                "description_raw"
            ):
                descriptions += 1

    started = time.monotonic()
    connector = LinkedInConnector("data scientist", "bengaluru")
    with connector._client() as client:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(fetch_one, client, job_id) for job_id in job_ids]
            for future in as_completed(futures):
                future.result()
    elapsed = time.monotonic() - started
    return {
        "interval_seconds": interval_seconds,
        "workers": workers,
        "requests": len(job_ids),
        "elapsed_seconds": round(elapsed, 3),
        "status_counts": {str(key): value for key, value in sorted(statuses.items())},
        "descriptions": descriptions,
        "response_bytes": response_bytes,
        "mean_latency_seconds": (
            round(sum(latencies) / len(latencies), 3) if latencies else None
        ),
    }


def run(
    *, since: str, sample_size: int, intervals: list[float], cooldown_seconds: float
) -> dict:
    since_at = parse_since(since)
    connector = LinkedInConnector("data scientist", "bengaluru")
    connector.date_filter_days = max(
        1,
        int((datetime.now(timezone.utc) - since_at).total_seconds() // 86_400) + 1,
    )
    connector.exact_recency_cutoff = since_at
    with connector._client() as client:
        response = client.get(connector._build_search_url())
        response.raise_for_status()
    listing_rows = _parse_listings(response.text, 1_000)
    relevant_rows = [
        row
        for row in listing_rows
        if first_failing_axis(
            connector._normalize(row), cutoff_at=since_at
        ) is None
    ]
    job_ids = [row["job_id"] for row in relevant_rows[:sample_size]]
    if len(job_ids) < sample_size:
        raise RuntimeError(
            f"Only {len(job_ids)} relevant initial-card jobs available; "
            f"requested {sample_size}"
        )

    trials = []
    for index, interval in enumerate(intervals):
        if index and cooldown_seconds:
            time.sleep(cooldown_seconds)
        trial = _trial(job_ids, interval_seconds=interval, workers=4)
        trials.append(trial)
        if trial["status_counts"].get("429", 0):
            break
    return {
        "scope": {
            "since_at": since_at.isoformat(),
            "sample_size": sample_size,
            "same_job_ids_each_trial": True,
            "cooldown_seconds": cooldown_seconds,
        },
        "trials": trials,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", required=True)
    parser.add_argument("--sample-size", type=int, default=20)
    parser.add_argument("--interval", type=float, action="append", dest="intervals")
    parser.add_argument("--cooldown-seconds", type=float, default=10.0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = run(
        since=args.since,
        sample_size=args.sample_size,
        intervals=args.intervals or [1.2, 1.0, 0.8],
        cooldown_seconds=args.cooldown_seconds,
    )
    rendered = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
        print(f"LinkedIn detail-rate probe saved to {args.output}")
    else:
        print(rendered)


if __name__ == "__main__":
    main()
