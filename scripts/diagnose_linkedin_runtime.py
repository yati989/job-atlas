"""Source-scoped live diagnostics for LinkedIn collection runtime.

Fetch-only: does not touch the database, progress snapshot, email, registry,
or production connector defaults.
"""
from __future__ import annotations

import argparse
import json
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from bs4 import BeautifulSoup
import httpx

from app.collectors.html.linkedin import (
    LINKEDIN_INDIA_CONTINUATION_URL,
    LINKEDIN_INDIA_DETAIL_URL,
    LINKEDIN_INDIA_SEARCH_URL,
    LinkedInConnector,
    _parse_listings,
)
from app.pipeline.window import parse_since
from app.pipeline.relevance import filter_relevant


STRUCTURED_MARKERS = (
    "datePosted",
    "description",
    "employmentType",
    "hiringOrganization",
    "jobPosting",
    "seniority",
)


def _structured_summary(html: str, known_job_ids: set[str]) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    scripts = soup.find_all("script")
    json_scripts = 0
    parsed_json_scripts = 0
    scripts_with_known_job_id = 0
    marker_counts: Counter[str] = Counter()
    script_types: Counter[str] = Counter()
    json_object_types: Counter[str] = Counter()
    json_job_posting_objects = 0

    def inspect_json(value: Any) -> None:
        nonlocal json_job_posting_objects
        if isinstance(value, dict):
            object_type = value.get("@type")
            if isinstance(object_type, str):
                json_object_types[object_type] += 1
                if object_type.lower() == "jobposting":
                    json_job_posting_objects += 1
            for child in value.values():
                inspect_json(child)
        elif isinstance(value, list):
            for child in value:
                inspect_json(child)

    for script in scripts:
        script_type = str(script.get("type") or "untyped")
        script_types[script_type] += 1
        text = script.string or script.get_text() or ""
        if any(job_id in text for job_id in known_job_ids):
            scripts_with_known_job_id += 1
        for marker in STRUCTURED_MARKERS:
            if marker in text:
                marker_counts[marker] += 1
        if "json" not in script_type.lower() and not text.lstrip().startswith(("{", "[")):
            continue
        json_scripts += 1
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            continue
        parsed_json_scripts += 1
        inspect_json(parsed)
    return {
        "bytes": len(html.encode("utf-8")),
        "script_count": len(scripts),
        "script_types": dict(script_types),
        "json_like_scripts": json_scripts,
        "parsed_json_scripts": parsed_json_scripts,
        "json_object_types": dict(json_object_types),
        "json_job_posting_objects": json_job_posting_objects,
        "scripts_with_known_job_id": scripts_with_known_job_id,
        "structured_marker_counts": dict(marker_counts),
    }


def _timed_fetch(
    *,
    search: str,
    location_mode: str,
    max_results: int,
    since_at: datetime,
    fetch_descriptions: bool,
    interval_seconds: float,
) -> dict[str, Any]:
    connector = LinkedInConnector(
        search=search,
        location_mode=location_mode,
        max_results=max_results,
        date_filter_days=max(
            1,
            int((datetime.now(timezone.utc) - since_at).total_seconds() // 86_400) + 1,
        ),
        fetch_descriptions=fetch_descriptions,
        listing_interval_seconds=interval_seconds,
        detail_interval_seconds=interval_seconds,
    )
    connector.exact_recency_cutoff = since_at
    connector._detail_cache.clear()
    connector._detail_inflight.clear()
    counters: Counter[str] = Counter()
    durations: Counter[str] = Counter()
    response_statuses: Counter[str] = Counter()
    lock = threading.Lock()

    original_client = connector._client

    def instrumented_client() -> httpx.Client:
        client = original_client()

        def record_response(response: httpx.Response) -> None:
            with lock:
                response_statuses[str(response.status_code)] += 1

        client.event_hooks.setdefault("response", []).append(record_response)
        return client

    connector._client = instrumented_client

    def instrument(name: str, original: Callable) -> Callable:
        def wrapped(*args, **kwargs):
            started = time.monotonic()
            try:
                return original(*args, **kwargs)
            finally:
                with lock:
                    counters[name] += 1
                    durations[name] += time.monotonic() - started
        return wrapped

    connector._fetch_initial = instrument("initial", connector._fetch_initial)
    connector._fetch_continuation = instrument(
        "continuation", connector._fetch_continuation
    )
    connector._request_detail = instrument("detail", connector._request_detail)

    started = time.monotonic()
    jobs = connector.fetch()
    if fetch_descriptions:
        relevant, _drop_counts, _drop_details = filter_relevant(
            jobs, cutoff_at=since_at
        )
        connector.hydrate_relevant_jobs(relevant)
    elapsed = time.monotonic() - started
    return {
        "fetch_descriptions": fetch_descriptions,
        "elapsed_seconds": round(elapsed, 3),
        "jobs": len(jobs),
        "unique_job_ids": len({job.external_job_id for job in jobs}),
        "job_ids": [job.external_job_id for job in jobs],
        "request_counts": dict(counters),
        "response_status_counts": dict(response_statuses),
        "cumulative_request_seconds": {
            key: round(value, 3) for key, value in durations.items()
        },
        "description_count": sum(bool(job.description_raw) for job in jobs),
        "seniority_count": sum(bool(job.seniority) for job in jobs),
    }


def _live_response_probe(
    *, search: str, location_mode: str, since_at: datetime
) -> dict[str, Any]:
    connector = LinkedInConnector(
        search=search,
        location_mode=location_mode,
        date_filter_days=max(
            1,
            int((datetime.now(timezone.utc) - since_at).total_seconds() // 86_400) + 1,
        ),
        fetch_descriptions=False,
    )
    connector.exact_recency_cutoff = since_at
    result: dict[str, Any] = {}
    with connector._client() as client:
        initial = client.get(connector._build_search_url())
        initial.raise_for_status()
        initial_rows = _parse_listings(initial.text, 1_000)
        initial_job_ids = [row["job_id"] for row in initial_rows]
        initial_ids = set(initial_job_ids)
        result["initial"] = {
            "listing_count": len(initial_rows),
            "job_ids": initial_job_ids,
            **_structured_summary(initial.text, initial_ids),
        }

        search_offset_variants: dict[str, Any] = {}
        search_offset_ids: dict[str, list[str]] = {}
        for start in (60, 120):
            connector._wait_for_slot(
                connector._request_rate_lock,
                "_last_request_started",
                "_request_cooldown_until",
                connector.request_interval_seconds,
            )
            response = client.get(
                LINKEDIN_INDIA_SEARCH_URL,
                params={**connector._search_params(), "start": start},
            )
            response.raise_for_status()
            rows = _parse_listings(response.text, 1_000)
            ids = [row["job_id"] for row in rows]
            search_offset_ids[f"start_{start}"] = ids
            search_offset_variants[f"start_{start}"] = {
                "listing_count": len(rows),
                "overlap_with_initial": len(set(ids) & initial_ids),
                **_structured_summary(response.text, set(ids)),
            }
        result["search_offset_pages"] = search_offset_variants

        continuation_variants: dict[str, Any] = {}
        for label, extra in (
            ("default", {}),
            ("count_25", {"count": 25}),
            ("count_100", {"count": 100}),
        ):
            connector._wait_for_slot(
                connector._request_rate_lock,
                "_last_request_started",
                "_request_cooldown_until",
                connector.request_interval_seconds,
            )
            response = client.get(
                LINKEDIN_INDIA_CONTINUATION_URL,
                params={**connector._search_params(), "start": 60, **extra},
            )
            response.raise_for_status()
            rows = _parse_listings(response.text, 1_000)
            ids = [row["job_id"] for row in rows]
            continuation_variants[label] = {
                "listing_count": len(rows),
                "job_ids": ids,
                **_structured_summary(response.text, set(ids)),
            }
        default_ids = continuation_variants["default"].pop("job_ids")
        for value in continuation_variants.values():
            ids = value.pop("job_ids", default_ids)
            value["same_ids_as_default"] = ids == default_ids
        result["continuation"] = continuation_variants
        start_60_ids = search_offset_ids["start_60"]
        result["search_offset_pages"]["start_60"][
            "first_ten_same_as_continuation"
        ] = start_60_ids[:10] == default_ids
        result["search_offset_pages"]["start_60"][
            "unique_union_with_initial"
        ] = len(set(initial_job_ids + start_60_ids))
        result["search_offset_pages"]["start_120"][
            "overlap_with_start_60"
        ] = len(set(search_offset_ids["start_120"]) & set(start_60_ids))

        first_id = next(iter(initial_ids), None)
        if first_id:
            connector._wait_for_slot(
                connector._request_rate_lock,
                "_last_request_started",
                "_request_cooldown_until",
                connector.request_interval_seconds,
            )
            detail = client.get(LINKEDIN_INDIA_DETAIL_URL.format(job_id=first_id))
            detail.raise_for_status()
            result["guest_detail"] = _structured_summary(detail.text, {first_id})
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--search", default="data scientist")
    parser.add_argument(
        "--location-mode",
        choices=["bengaluru", "remote_india"],
        default="bengaluru",
    )
    parser.add_argument("--max-results", type=int, default=100)
    parser.add_argument(
        "--since", default="2026-08-16T21:00:00+05:30"
    )
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument(
        "--probe-only",
        action="store_true",
        help="Inspect response shapes without running timed connector fetches.",
    )
    parser.add_argument(
        "--timing-only",
        action="store_true",
        help="Run timed connector fetches without the response-shape probes.",
    )
    parser.add_argument(
        "--listing-only",
        action="store_true",
        help="Skip the hydrated timing pass.",
    )
    parser.add_argument(
        "--interval-seconds",
        type=float,
        default=1.2,
        help="Diagnostic-only shared request-start interval.",
    )
    args = parser.parse_args()
    if args.probe_only and args.timing_only:
        parser.error("--probe-only and --timing-only cannot be combined")
    since_at = parse_since(args.since)

    report: dict[str, Any] = {
        "query": {
            "search": args.search,
            "location_mode": args.location_mode,
            "max_results": args.max_results,
            "since_at": since_at.isoformat(),
        },
    }
    if not args.timing_only:
        report["response_probe"] = _live_response_probe(
            search=args.search,
            location_mode=args.location_mode,
            since_at=since_at,
        )
    if not args.probe_only:
        report["listing_only"] = _timed_fetch(
            search=args.search,
            location_mode=args.location_mode,
            max_results=args.max_results,
            since_at=since_at,
            fetch_descriptions=False,
            interval_seconds=args.interval_seconds,
        )
    if not args.probe_only and not args.listing_only:
        report["hydrated"] = _timed_fetch(
            search=args.search,
            location_mode=args.location_mode,
            max_results=args.max_results,
            since_at=since_at,
            fetch_descriptions=True,
            interval_seconds=args.interval_seconds,
        )
    rendered = json.dumps(report, indent=2, sort_keys=True)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
        print(f"LinkedIn diagnostic saved to {args.output}")
    else:
        print(rendered)


if __name__ == "__main__":
    main()
