"""
Pipeline entrypoint: fetch from every active connector, normalize, and
upsert into Postgres. By default drives BOTH tiers (headless + headed) —
attended, opens visible browser windows for the headed tier.

Run with:
    python -m app.pipeline.run_all                 # both tiers (attended)
    python -m app.pipeline.run_all --headless-only  # unattended-safe: headless tier only
    python -m app.pipeline.run_all --headless-only --tier=parallel  # non-browser connectors only
    python -m app.pipeline.run_all --headless-only --tier=browser   # browser connectors only
    python -m app.pipeline.run_all --headless-only --tier=browser --shard=1/3  # 1st third of the browser tier only
    python -m app.pipeline.run_all --headless-only --tier=browser --index-range=33:36  # exact index slice

Issue #30: this entrypoint previously ran its own separate, serial,
no-dedup fetch/upsert loop instead of the shared orchestration in
runner.py (fetch_all/retry_failed/gate_and_upsert), so none of #17/#18's
overhaul (concurrent fetch, per-run cross-connector dedup, unified retry
pass) or #21/#31's latency work (render-wait fixes, pooled httpx client)
were actually reachable from this entrypoint. This rewires it onto
runner.py, which is the single place that loop now lives.

--tier/--shard/--index-range are scaffolding added for #30's own
verification: this session's background-task execution model has a hard
ceiling somewhere around 55-65 minutes (confirmed by killing both an
unrelated memory-poll loop and run_all itself at the same wall-clock
moment despite doing unrelated work — an execution-environment constraint,
not a code bug), well short of a full run's multi-hour wall time. Even the
browser tier alone (142 connectors, serial) didn't finish inside that
ceiling, so --shard=N/M splits the (already tier-filtered) connector list
into M contiguous, roughly-equal-size pieces; --index-range=A:B takes an
exact slice instead, for isolating a specific known-slow connector into
its own small shard rather than letting it consume a big chunk of a shard
shared with 40+ unrelated connectors. Sum
wall times and combine per-connector kept-counts across shards for the
full picture. Left in as real flags (not deleted after #30) since
splitting is generically useful for future same-constraint verification,
not just this one ticket.
"""
import argparse
import copy
import logging
from datetime import datetime, timedelta, timezone

from app.config.categories import SEARCH_TERMS
from app.config.settings import RECENCY_WINDOW_DAYS
from app.pipeline.progress import ConnectorProgress
from app.pipeline.completion_email import send_completion_email
from app.pipeline.window import apply_sync_window, parse_since
from app.pipeline.workload import enforce_workload_bounds
from app.pipeline.registry import ACTIVE_CONNECTORS
from app.pipeline.runner import (
    IncrementalRunPersister,
    fetch_all,
    retry_failed,
    gate_and_upsert,
    log_summary,
    is_browser_connector,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("pipeline")


_SEARCH_ALIASES = {
    "data science": "data scientist",
    "machine learning": "machine learning engineer",
    "ml": "machine learning engineer",
    "ml engineer": "machine learning engineer",
    "artificial intelligence": "ai engineer",
    "ai": "ai engineer",
}
_LOCATION_ALIASES = {
    "remote": "remote",
    "remote india": "remote",
    "remote_india": "remote",
    "india remote": "remote",
    "bengaluru": "bengaluru",
    "bangalore": "bengaluru",
}


def normalize_sample_search(value: str) -> str:
    """Resolve a user-facing role choice to one registered search term."""
    normalized = value.strip().lower().replace("_", " ")
    normalized = _SEARCH_ALIASES.get(normalized, normalized)
    if normalized not in SEARCH_TERMS:
        choices = ", ".join(SEARCH_TERMS)
        raise ValueError(f"unsupported collection search {value!r}; choose one of: {choices}")
    return normalized


def normalize_sample_location(value: str) -> str:
    """Resolve board-specific remote/Bengaluru spellings to one scope value."""
    normalized = value.strip().lower().replace("-", " ")
    try:
        return _LOCATION_ALIASES[normalized]
    except KeyError as exc:
        raise ValueError(
            f"unsupported collection location {value!r}; choose remote or Bengaluru"
        ) from exc


def select_connectors(
    connectors: list,
    *,
    tier: str = "all",
    source: str | None = None,
    exclude_sources: set[str] | None = None,
    per_source_index_range: str | None = None,
    shard: str | None = None,
    index_range: str | None = None,
    one_per_source: bool = False,
    sample_search: str = "data scientist",
    sample_location: str | None = None,
    location_mode: str | None = None,
) -> list:
    """Select an exact reproducible connector slice without changing activation."""
    selected = list(connectors)
    if tier == "parallel":
        selected = [c for c in selected if not is_browser_connector(c)]
    elif tier == "browser":
        selected = [c for c in selected if is_browser_connector(c)]

    if source:
        selected = [c for c in selected if c.source_name == source]
    if location_mode:
        selected = [
            connector for connector in selected
            if getattr(connector, "location_mode", None) == location_mode
        ]
    if exclude_sources:
        excluded = {name.strip().lower() for name in exclude_sources}
        selected = [
            connector
            for connector in selected
            if str(connector.source_name).lower() not in excluded
        ]

    if per_source_index_range:
        a_str, b_str = per_source_index_range.split(":")
        start, end = int(a_str), int(b_str)
        grouped: dict[str, list] = {}
        for connector in selected:
            grouped.setdefault(str(connector.source_name), []).append(connector)
        selected_ids = {
            id(connector)
            for instances in grouped.values()
            for connector in instances[start:end]
        }
        selected = [connector for connector in selected if id(connector) in selected_ids]

    if one_per_source:
        target = normalize_sample_search(sample_search)

        def preference(connector) -> tuple[int, int, int]:
            search = str(getattr(connector, "search", "")).strip().lower()
            search_terms = [
                str(value).strip().lower()
                for value in getattr(connector, "search_terms", [])
            ]
            search_rank = (
                0 if search == target
                else 1 if search.startswith(f"{target} ") or target in search_terms
                else 2
            )
            mode = str(getattr(connector, "location_mode", "")).lower()
            if sample_location:
                requested_location = normalize_sample_location(sample_location)
                location_aliases = {
                    "bengaluru": {"bengaluru", "bangalore"},
                    "remote": {"remote", "remote_india"},
                }.get(requested_location, {requested_location})
                if mode in location_aliases or any(
                    alias in search for alias in location_aliases
                ):
                    location_rank = 0
                elif not mode or mode == "none":
                    location_rank = 1
                else:
                    location_rank = 2
            elif "remote" in mode or search.endswith(" remote"):
                location_rank = 0
            elif mode == "worldwide":
                location_rank = 1
            elif not mode or mode == "none":
                location_rank = 2
            else:
                location_rank = 3
            return search_rank + location_rank, search_rank, location_rank

        grouped: dict[str, list] = {}
        for connector in selected:
            grouped.setdefault(str(connector.source_name), []).append(connector)
        representatives = []
        for instances in grouped.values():
            representative = copy.copy(min(instances, key=preference))
            if target in [
                str(value).strip().lower()
                for value in getattr(representative, "search_terms", [])
            ]:
                representative.search_terms = [sample_search]
            representatives.append(representative)
        selected = representatives

    if shard:
        n_str, m_str = shard.split("/")
        n, m = int(n_str), int(m_str)
        total = len(selected)
        start = (n - 1) * total // m
        end = n * total // m
        selected = selected[start:end]

    if index_range:
        a_str, b_str = index_range.split(":")
        selected = selected[int(a_str):int(b_str)]
    return selected


def run(
    headless_only: bool = False,
    tier: str = "all",
    source: str | None = None,
    exclude_sources: set[str] | None = None,
    per_source_index_range: str | None = None,
    shard: str | None = None,
    index_range: str | None = None,
    since: str | None = None,
    one_per_source: bool = False,
    sample_search: str = "data scientist",
    sample_location: str | None = None,
    location_mode: str | None = None,
    record_dropped_observations: bool = False,
) -> None:
    started_at = datetime.now(timezone.utc)
    connectors = list(ACTIVE_CONNECTORS)
    if headless_only:
        logger.info("--headless-only: skipping the headed tier (needs a real display)")
    else:
        # Headed tier import deferred to here (not module-level) since it
        # instantiates every headed connector, some of which launch a real
        # browser session on construction — no reason to pay that cost for
        # a --headless-only run.
        from scripts.run_headed_sources import CONNECTORS as HEADED_CONNECTORS
        connectors = connectors + list(HEADED_CONNECTORS)

    connectors = select_connectors(
        connectors,
        tier=tier,
        source=source,
        exclude_sources=exclude_sources,
        per_source_index_range=per_source_index_range,
        shard=shard,
        index_range=index_range,
        one_per_source=one_per_source,
        sample_search=sample_search,
        sample_location=sample_location,
        location_mode=location_mode,
    )
    logger.info(
        "Selected %s connector instance(s) (tier=%s source=%s excluded=%s per_source_range=%s shard=%s range=%s sample_location=%s location_mode=%s)",
        len(connectors), tier, source or "all", sorted(exclude_sources or []),
        per_source_index_range or "all", shard or "all", index_range or "all",
        sample_location or "default", location_mode or "all",
    )
    since_at = (
        parse_since(since)
        if since
        else started_at - timedelta(days=RECENCY_WINDOW_DAYS)
    )
    window = apply_sync_window(
        connectors, since_at=since_at, cutoff_at=started_at
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
        command="app.pipeline.run_all",
    )
    audit_run_id = None
    if record_dropped_observations:
        from app.pipeline.relevance_audit import start_audit_run
        audit_run_id = f"relevance-audit-{started_at.strftime('%Y%m%dT%H%M%SZ')}"
        start_audit_run(
            run_id=audit_run_id,
            since_at=window.since_at,
            command="app.pipeline.run_all",
            instance_count=len(connectors),
        )
        logger.info("Recording raw dropped observations in relevance audit %s", audit_run_id)

    try:
        persister = IncrementalRunPersister(
            cutoff_at=window.since_at,
            relevance_audit_run_id=audit_run_id,
        )
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
            # Raw drops were already recorded per completed instance above.
            relevance_audit_run_id=None,
        )
        progress.complete(summary)

        logger.info("Pipeline run complete. Upserted=%s Errors=%s", total_upserted, total_errors)
        log_summary(summary)
        if send_completion_email(progress.snapshot):
            logger.info("Connector completion email sent to self")
        if audit_run_id is not None:
            from app.pipeline.relevance_audit import finish_audit_run
            finish_audit_run(
                run_id=audit_run_id,
                state="completed" if total_errors == 0 else "partial",
            )
    except BaseException as exc:
        if audit_run_id is not None:
            from app.pipeline.relevance_audit import finish_audit_run
            finish_audit_run(run_id=audit_run_id, state="failed", failure_detail=str(exc))
        raise


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--headless-only",
        action="store_true",
        help="Skip the headed/anti-bot tier (unattended-safe, no display required).",
    )
    parser.add_argument(
        "--record-dropped-observations",
        action="store_true",
        help=("Persist every raw relevance-gate rejection in the dedicated "
              "relevance audit tables; relevant jobs still upsert normally."),
    )
    parser.add_argument(
        "--tier",
        choices=["all", "parallel", "browser"],
        default="all",
        help="Run only the non-browser ('parallel') or browser ('browser') connectors, "
             "or 'all' (default) for both — see module docstring for why this exists.",
    )
    parser.add_argument(
        "--source",
        default=None,
        help="Run only connector instances for one active source, e.g. indeed.",
    )
    parser.add_argument(
        "--exclude-source",
        action="append",
        default=[],
        help="Exclude an active source; repeat this option for multiple sources.",
    )
    parser.add_argument(
        "--per-source-index-range",
        default=None,
        metavar="A:B",
        help="Run instances[A:B] independently within every active source, "
             "preserving registry order.",
    )
    parser.add_argument(
        "--shard",
        default=None,
        metavar="N/M",
        help="Run only shard N of M contiguous, roughly-equal pieces of the "
             "(tier-filtered) connector list, e.g. --shard=1/3. 1-indexed.",
    )
    parser.add_argument(
        "--index-range",
        default=None,
        metavar="A:B",
        help="Run only connectors[A:B] (0-indexed, exclusive end) of the "
             "(tier-filtered) connector list, e.g. --index-range=33:36. "
             "Applied after --shard if both are given.",
    )
    parser.add_argument(
        "--since",
        default=None,
        help=("Exact ISO-8601 posting-time lower bound. Native whole-day "
              "filters round upward; the central gate enforces this timestamp exactly."),
    )
    parser.add_argument(
        "--one-per-source",
        action="store_true",
        help="Run one representative instance per active source.",
    )
    parser.add_argument(
        "--sample-search",
        default="data scientist",
        help="Preferred search term for --one-per-source (default: data scientist).",
    )
    parser.add_argument(
        "--sample-location",
        default=None,
        help="Preferred location for --one-per-source, e.g. bengaluru.",
    )
    parser.add_argument(
        "--location-mode",
        default=None,
        help="Run only connector instances with this exact location mode.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    run(
        headless_only=args.headless_only,
        tier=args.tier,
        source=args.source,
        exclude_sources=set(args.exclude_source),
        per_source_index_range=args.per_source_index_range,
        shard=args.shard,
        index_range=args.index_range,
        since=args.since,
        one_per_source=args.one_per_source,
        sample_search=args.sample_search,
        sample_location=args.sample_location,
        location_mode=args.location_mode,
        record_dropped_observations=args.record_dropped_observations,
    )
