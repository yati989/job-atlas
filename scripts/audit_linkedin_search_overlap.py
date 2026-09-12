"""Source-scoped LinkedIn listing overlap audit.

Fetch-only: no detail requests, database writes, progress snapshot, email, or
registry changes. The report measures raw and centrally relevant job-ID overlap
per search term and location mode.
"""
from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path

from app.collectors.html.linkedin import LinkedInConnector
from app.config.categories import SEARCH_TERMS
from app.pipeline.relevance import filter_relevant
from app.pipeline.window import apply_sync_window, parse_since


def summarize_sets(grouped: dict[str, set[str]]) -> dict:
    union = set().union(*grouped.values()) if grouped else set()
    raw_occurrences = sum(len(ids) for ids in grouped.values())
    rows = []
    for name, ids in grouped.items():
        other_ids = set().union(
            *(other for other_name, other in grouped.items() if other_name != name)
        ) if len(grouped) > 1 else set()
        exclusive = ids - other_ids
        rows.append(
            {
                "name": name,
                "jobs": len(ids),
                "exclusive_jobs": len(exclusive),
                "union_coverage_pct": (
                    round(100 * len(ids) / len(union), 1) if union else 0.0
                ),
                "coverage_without_pct": (
                    round(100 * len(union - exclusive) / len(union), 1)
                    if union
                    else 0.0
                ),
            }
        )
    pairs = []
    for (left_name, left), (right_name, right) in combinations(grouped.items(), 2):
        pair_union = left | right
        intersection = left & right
        pairs.append(
            {
                "left": left_name,
                "right": right_name,
                "shared_jobs": len(intersection),
                "jaccard_pct": (
                    round(100 * len(intersection) / len(pair_union), 1)
                    if pair_union
                    else 0.0
                ),
            }
        )
    pairs.sort(key=lambda row: (row["shared_jobs"], row["jaccard_pct"]), reverse=True)
    return {
        "raw_occurrences": raw_occurrences,
        "unique_jobs": len(union),
        "duplicate_occurrences": raw_occurrences - len(union),
        "duplicate_rate_pct": (
            round(100 * (raw_occurrences - len(union)) / raw_occurrences, 1)
            if raw_occurrences
            else 0.0
        ),
        "groups": rows,
        "top_pairwise_overlaps": pairs[:10],
    }


def run(*, since: str, max_results: int) -> dict:
    since_at = parse_since(since)
    cutoff_at = datetime.now(timezone.utc)
    connectors = [
        LinkedInConnector(
            search=term,
            location_mode=mode,
            max_results=max_results,
            fetch_descriptions=False,
        )
        for term in SEARCH_TERMS
        for mode in ("remote_india", "bengaluru")
    ]
    apply_sync_window(connectors, since_at=since_at, cutoff_at=cutoff_at)
    with LinkedInConnector._request_rate_lock:
        LinkedInConnector._last_request_started = 0.0
        LinkedInConnector._request_cooldown_until = 0.0

    jobs_by_instance = {}
    errors = {}
    with ThreadPoolExecutor(max_workers=len(connectors)) as pool:
        futures = {pool.submit(connector.fetch): connector for connector in connectors}
        for future in as_completed(futures):
            connector = futures[future]
            name = f"{connector.search}|{connector.location_mode}"
            try:
                jobs_by_instance[name] = future.result()
            except Exception as exc:
                errors[name] = f"{type(exc).__name__}: {exc}"

    raw_instance_sets = {
        name: {job.external_job_id for job in jobs}
        for name, jobs in jobs_by_instance.items()
    }
    relevant_instance_sets = {}
    for name, jobs in jobs_by_instance.items():
        relevant, _counts, _details = filter_relevant(jobs, cutoff_at=since_at)
        relevant_instance_sets[name] = {job.external_job_id for job in relevant}

    def by_term(instance_sets: dict[str, set[str]]) -> dict[str, set[str]]:
        return {
            term: set().union(
                *(
                    ids
                    for name, ids in instance_sets.items()
                    if name.startswith(f"{term}|")
                )
            )
            for term in SEARCH_TERMS
        }

    return {
        "scope": {
            "since_at": since_at.isoformat(),
            "cutoff_at": cutoff_at.isoformat(),
            "max_results_per_instance": max_results,
            "detail_requests": 0,
            "instances_requested": len(connectors),
            "instances_succeeded": len(jobs_by_instance),
        },
        "errors": errors,
        "raw_by_instance": summarize_sets(raw_instance_sets),
        "raw_by_term": summarize_sets(by_term(raw_instance_sets)),
        "relevant_by_instance": summarize_sets(relevant_instance_sets),
        "relevant_by_term": summarize_sets(by_term(relevant_instance_sets)),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", required=True)
    parser.add_argument("--max-results", type=int, default=100)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = run(since=args.since, max_results=args.max_results)
    rendered = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
        print(f"LinkedIn overlap audit saved to {args.output}")
    else:
        print(rendered)


if __name__ == "__main__":
    main()
