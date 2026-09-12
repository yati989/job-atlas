"""Print and enforce workload bounds for every active connector instance."""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone

from app.pipeline.registry import ACTIVE_CONNECTORS
from app.pipeline.window import apply_sync_window, parse_since
from app.pipeline.workload import enforce_workload_bounds


def run(*, headless_only: bool = False, since: str | None = None) -> None:
    connectors = list(ACTIVE_CONNECTORS)
    if not headless_only:
        from scripts.run_headed_sources import CONNECTORS
        connectors.extend(CONNECTORS)
    if since:
        window = apply_sync_window(
            connectors,
            since_at=parse_since(since),
            cutoff_at=datetime.now(timezone.utc),
        )
        print(
            f"Effective native window: {window.native_days} day(s); "
            f"exact cutoff: {window.since_at.isoformat()}"
        )
    enforce_workload_bounds(connectors)

    grouped: dict[str, list] = defaultdict(list)
    for connector in connectors:
        grouped[connector.source_name].append(connector)

    print("| Source | Instances | Max results/instance | Max pages | Recency-scaled cap | Detail concurrency | Pacing |")
    print("|---|---:|---:|---:|---|---:|---:|")
    for source, instances in grouped.items():
        result_values = [getattr(item, "max_results", None) for item in instances]
        page_values = [getattr(item, "max_pages", None) for item in instances]
        detail_values = [
            getattr(item, "detail_workers", getattr(item, "detail_batch_size", None))
            for item in instances
        ]
        pacing_values = [getattr(item, "detail_interval_seconds", None) for item in instances]
        scaled_attributes = [
            (
                f"{getattr(item, 'recency_scaled_cap_attribute')}="
                f"{getattr(item, getattr(item, 'recency_scaled_cap_attribute'))}"
            )
            for item in instances
            if getattr(item, "verified_newest_first", False)
        ]

        def display(values: list) -> str:
            present = sorted({value for value in values if value is not None})
            return ",".join(str(value) for value in present) if present else "n/a"

        print(
            f"| {source} | {len(instances)} | {display(result_values)} | "
            f"{display(page_values)} | {display(scaled_attributes)} | "
            f"{display(detail_values)} | {display(pacing_values)} |"
        )
    print(f"\nPASS: {len(connectors)} instances across {len(grouped)} sources")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--headless-only", action="store_true")
    parser.add_argument("--since", help="Show effective caps for this ISO-8601 cutoff.")
    args = parser.parse_args()
    run(headless_only=args.headless_only, since=args.since)
