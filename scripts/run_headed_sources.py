"""Run active connectors that require a visible browser session.

Indeed is kept outside the unattended registry because its current collection
mechanism needs a real display. The full pipeline imports ``CONNECTORS`` and
runs it alongside the unattended connector set when ``--headless-only`` is not
requested.
"""

import argparse
import logging
from datetime import datetime, timedelta, timezone

from app.collectors.browser.indeed import IndeedConnector
from app.config.categories import SEARCH_TERMS
from app.config.settings import RECENCY_WINDOW_DAYS
from app.pipeline.completion_email import send_completion_email
from app.pipeline.progress import ConnectorProgress
from app.pipeline.runner import (
    IncrementalRunPersister,
    fetch_all,
    gate_and_upsert,
    log_summary,
    retry_failed,
)
from app.pipeline.window import apply_sync_window, parse_since
from app.pipeline.workload import enforce_workload_bounds


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("run_headed_sources")


CONNECTORS = [
    IndeedConnector(search=term, location_mode=mode)
    for term in SEARCH_TERMS
    for mode in ("bengaluru", "remote_india")
]


def run(*, since: str | None = None) -> None:
    """Run the attended tier through the shared ingestion orchestration."""
    started_at = datetime.now(timezone.utc)
    connectors = list(CONNECTORS)
    since_at = (
        parse_since(since)
        if since
        else started_at - timedelta(days=RECENCY_WINDOW_DAYS)
    )
    window = apply_sync_window(
        connectors,
        since_at=since_at,
        cutoff_at=started_at,
    )
    enforce_workload_bounds(connectors)
    logger.info(
        "Sync window: since=%s run_start=%s native_filter_days=%s",
        window.since_at.isoformat(),
        window.cutoff_at.isoformat(),
        window.native_days,
    )

    progress = ConnectorProgress(
        connectors,
        run_id=started_at.strftime("%Y%m%dT%H%M%SZ"),
        command="scripts.run_headed_sources",
    )
    persister = IncrementalRunPersister(cutoff_at=window.since_at)
    results = fetch_all(
        connectors,
        progress=progress,
        on_result=persister.persist,
    )
    retry_failed(results, progress=progress)
    persister.persist_remaining(results)
    summary, total_upserted, total_errors = gate_and_upsert(
        results,
        cutoff_at=window.since_at,
    )
    progress.complete(summary)
    logger.info("Run complete. Upserted=%s Errors=%s", total_upserted, total_errors)
    log_summary(summary)
    if send_completion_email(progress.snapshot):
        logger.info("Connector completion email sent to self")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--since",
        default=None,
        help=(
            "Exact ISO-8601 posting-time lower bound. Native whole-day "
            "filters round upward; the central gate enforces it exactly."
        ),
    )
    return parser.parse_args()


if __name__ == "__main__":
    run(since=_parse_args().since)
