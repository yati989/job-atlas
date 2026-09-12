"""
Shared orchestration helpers for the two pipeline entrypoints (`run_all.py`
for the headless tier, `scripts/run_headed_sources.py` for the headed tier).

Overhaul T8 (issue #17): both tiers used to run their own copy of the same
fetch -> relevance-gate -> upsert -> summary loop. This module is the single
place that loop lives now, so `run_all.py` can drive both tiers (headed
included by default) through one code path with:
- API/HTML/feed connectors start immediately in a ThreadPoolExecutor while
  browser connectors run serially beside them. Browser connectors never run
  concurrently with one another (see CLAUDE.md).
- a runner-level retry pass: any connector that raised during fetch() gets
  one retry after every other connector in the run has had its first
  attempt.
- one unified per-connector yield summary across both tiers.
- per-run deduplication: dedupe by (source, external_job_id) across all
  connectors in the run before the relevance gate (T9, issue #18).
- detailed drop observability: per-job drop reason with location-foreign vs
  location-no-signal split (T9, issue #18).
"""
import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit, urlunsplit
from collections.abc import Callable

from sqlalchemy import select

from app.collectors.base import PartialFetchError
from app.models.orm import Job
from app.models.schemas import NormalizedJob
from app.pipeline.relevance import filter_relevant
from app.config.settings import RECENCY_WINDOW_DAYS
from app.pipeline.upsert import (
    upsert_job,
)

logger = logging.getLogger("pipeline")
MAX_RETRYABLE_ATTEMPT_SECONDS = 300.0

_SAFE_DIMENSION_NAMES = ("search", "search_terms", "location_mode", "location", "country")
_URL_RE = re.compile(r"https?://[^\s\]\[()<>\"']+")
_SECRET_VALUE_RE = re.compile(
    r"(?i)\b(token|password|passwd|cookie|secret|api[_-]?key|authorization)"
    r"\s*[=:]\s*[^\s,;]+"
)


@contextmanager
def get_session():
    """Open the legacy database lazily while preserving the test seam."""
    from app.db.session import get_session as open_session

    with open_session() as session:
        yield session


def sanitize_log_text(value: object) -> str:
    """Redact URL query strings and common secret-shaped key/value text."""
    text = str(value)

    def redact_url(match: re.Match) -> str:
        parsed = urlsplit(match.group(0))
        if not parsed.query and not parsed.fragment:
            return match.group(0)
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "<redacted>", ""))

    text = _URL_RE.sub(redact_url, text)
    return _SECRET_VALUE_RE.sub(lambda match: f"{match.group(1)}=<redacted>", text)


def _safe_connector_dimensions(connector: object) -> dict[str, object]:
    dimensions: dict[str, object] = {}
    for name in _SAFE_DIMENSION_NAMES:
        value = getattr(connector, name, None)
        if value is None:
            continue
        if isinstance(value, (str, int, float, bool)):
            dimensions[name] = value
        elif isinstance(value, (list, tuple)) and all(
            isinstance(item, (str, int, float, bool)) for item in value
        ):
            dimensions[name] = list(value)
    return dimensions


def is_browser_connector(connector) -> bool:
    """True for the serial connector lane.

    Browser modules are serial by convention. A connector that graduates to
    direct HTTP may retain serial account-sensitive execution explicitly.
    """
    return type(connector).__module__.startswith(
        "app.collectors.browser"
    ) or bool(getattr(connector, "serial_execution", False))


@dataclass
class FetchAttempt:
    attempt: int
    started_at: datetime
    ended_at: datetime
    duration_s: float
    status: str
    fetched_count: int = 0
    error_type: str | None = None
    error: str | None = None


@dataclass
class FetchResult:
    connector: object
    source: str
    jobs: list[NormalizedJob] | None = None
    error: Exception | None = None
    attempts: int = 1
    retried: bool = False
    retry_succeeded: bool | None = None
    elapsed_s: float = 0.0
    started_at: datetime | None = None
    ended_at: datetime | None = None
    dimensions: dict[str, object] = field(default_factory=dict)
    connector_class: str = ""
    attempt_details: list[FetchAttempt] = field(default_factory=list)
    observations: list["FetchObservation"] = field(default_factory=list)


@dataclass(frozen=True)
class FetchObservation:
    """One raw job occurrence returned by one connector attempt."""

    job: NormalizedJob
    attempt: int
    ordinal: int


@dataclass(frozen=True)
class GateOutcome:
    observation: FetchObservation
    source_instance: int
    outcome: str
    reason: str | None = None
    connector_dimensions: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class IngestionGateFacts:
    """Facts emitted by the canonical collection-dedup and relevance pass."""

    raw: tuple[GateOutcome, ...]
    collection: tuple[GateOutcome, ...]
    relevance: tuple[GateOutcome, ...]

    @property
    def kept_jobs(self) -> tuple[NormalizedJob, ...]:
        return tuple(fact.observation.job for fact in self.relevance if fact.outcome == "advanced")


@dataclass
class ConnectorSummary:
    source: str
    fetched: int = 0
    kept: int = 0
    errored: bool = False
    drop_counts: dict = field(default_factory=dict)
    attempts: int = 1
    fetch_time_s: float = 0.0
    source_wall_time_s: float = 0.0
    instances_started: int = 0
    instances_succeeded: int = 0
    instances_failed: int = 0
    instances_retried: int = 0
    raw_count: int = 0
    unique_count: int = 0
    duplicate_count: int = 0
    duplicate_rate_pct: float = 0.0
    location_foreign: int = 0
    location_no_signal: int = 0
    date_quality: dict = field(default_factory=dict)
    field_completeness: dict = field(default_factory=dict)
    upsert_calls_completed: int = 0
    upsert_errors: int = 0


def _upsert_job_batch(
    jobs: list[NormalizedJob], *, source: str, log_prefix: str = ""
) -> tuple[int, int]:
    """Upsert a completed batch without one bad row undoing its siblings."""
    completed = 0
    errors = 0
    with get_session() as session:
        for item in jobs:
            try:
                # PostgreSQL aborts a transaction after any statement error.
                # A savepoint contains that failure so earlier and later jobs
                # in this completed connector result remain committable.
                savepoint = (
                    session.begin_nested()
                    if callable(getattr(session, "begin_nested", None))
                    else nullcontext()
                )
                with savepoint:
                    upsert_job(session, item)
                completed += 1
            except Exception as exc:
                errors += 1
                logger.error(
                    "Failed %supsert for job %s from %s: %s",
                    log_prefix,
                    item.external_job_id,
                    source,
                    exc,
                )
    return completed, errors


class IncrementalRunPersister:
    """Durably store completed connector results while the run continues.

    This is deliberately a small handoff between collection and persistence:
    callers publish each terminal ``FetchResult`` once, and this object owns
    run-wide deduplication plus the immediate relevance-gate/upsert.  The
    normal end-of-run gate still performs source-level hydration and builds
    the authoritative summary; these early writes are the crash-safe copy.
    """

    def __init__(
        self,
        *,
        cutoff_at: datetime,
        relevance_audit_run_id: str | None = None,
    ) -> None:
        self.cutoff_at = cutoff_at
        self.relevance_audit_run_id = relevance_audit_run_id
        self._lock = threading.Lock()
        self._audited_job_keys_by_result: dict[int, set[tuple[str, str]]] = {}
        self._seen_jobs: set[tuple[str, str]] = set()

    def persist(self, result: FetchResult) -> None:
        """Persist available jobs now; a retry may publish additional jobs later."""
        result_identity = id(result)
        with self._lock:
            if self.relevance_audit_run_id is not None:
                from app.pipeline.relevance_audit import record_dropped_observations

                prior_keys = self._audited_job_keys_by_result.get(result_identity)
                if prior_keys is None:
                    audit_jobs = list(result.jobs or [])
                    prior_keys = set()
                    self._audited_job_keys_by_result[result_identity] = prior_keys
                else:
                    audit_jobs = [
                        job for job in result.jobs or []
                        if (job.source, job.external_job_id) not in prior_keys
                    ]
                prior_keys.update(
                    (job.source, job.external_job_id) for job in result.jobs or []
                )
                audit_result = FetchResult(
                    connector=result.connector,
                    source=result.source,
                    jobs=audit_jobs,
                    dimensions=result.dimensions,
                )
                recorded = (
                    record_dropped_observations(
                        run_id=self.relevance_audit_run_id,
                        results=[audit_result],
                        cutoff_at=self.cutoff_at,
                    )
                    if audit_jobs
                    else 0
                )
                if recorded:
                    logger.info(
                        "Incrementally recorded %s dropped observation(s) from %s",
                        recorded,
                        result.source,
                    )

            unique_jobs: list[NormalizedJob] = []
            for job in result.jobs or []:
                key = (job.source, job.external_job_id)
                if key in self._seen_jobs:
                    continue
                self._seen_jobs.add(key)
                unique_jobs.append(job)

            kept, _, _ = filter_relevant(unique_jobs, cutoff_at=self.cutoff_at)
            if not kept:
                return

            # A listing-only write must not erase a complete description from
            # an earlier run.  Reuse it now; genuinely new rows are hydrated
            # by the existing source-level final pass.
            if getattr(result.connector, "reuse_stored_details", False):
                try:
                    _reuse_stored_details(result.source, kept)
                except Exception:
                    logger.exception(
                        "Stored-detail lookup failed during incremental persistence for %s",
                        result.source,
                    )

            completed, _ = _upsert_job_batch(
                kept, source=result.source, log_prefix="incremental "
            )
            logger.info(
                "Incremental persistence complete: source=%s unique=%s kept=%s upserted=%s",
                result.source,
                len(unique_jobs),
                len(kept),
                completed,
            )

    def persist_remaining(self, results: list[FetchResult]) -> None:
        """Publish retry outcomes and partial failures not published earlier."""
        for result in results:
            self.persist(result)


def _fetch_one(connector, attempt_number: int = 1) -> FetchResult:
    source = connector.source_name
    started_at = datetime.now(timezone.utc)
    start = time.monotonic()
    dimensions = _safe_connector_dimensions(connector)
    connector_class = type(connector).__name__
    try:
        jobs = connector.fetch()
        elapsed_s = time.monotonic() - start
        ended_at = datetime.now(timezone.utc)
        attempt = FetchAttempt(
            attempt=attempt_number,
            started_at=started_at,
            ended_at=ended_at,
            duration_s=elapsed_s,
            status="succeeded",
            fetched_count=len(jobs),
        )
        logger.info(
            "Instance complete: source=%s connector=%s dimensions=%s attempt=%s "
            "start=%s end=%s duration=%.3fs fetched=%s status=succeeded",
            source,
            connector_class,
            json.dumps(dimensions, sort_keys=True),
            attempt_number,
            started_at.isoformat(),
            ended_at.isoformat(),
            elapsed_s,
            len(jobs),
        )
        return FetchResult(
            connector=connector,
            source=source,
            jobs=jobs,
            elapsed_s=elapsed_s,
            started_at=started_at,
            ended_at=ended_at,
            dimensions=dimensions,
            connector_class=connector_class,
            attempt_details=[attempt],
            observations=[FetchObservation(job, attempt_number, ordinal)
                          for ordinal, job in enumerate(jobs, start=1)],
        )
    except Exception as exc:
        elapsed_s = time.monotonic() - start
        ended_at = datetime.now(timezone.utc)
        partial_jobs = exc.jobs if isinstance(exc, PartialFetchError) else None
        safe_error = sanitize_log_text(exc)
        attempt = FetchAttempt(
            attempt=attempt_number,
            started_at=started_at,
            ended_at=ended_at,
            duration_s=elapsed_s,
            status="partial-error" if partial_jobs else "error",
            fetched_count=len(partial_jobs or []),
            error_type=type(exc).__name__,
            error=safe_error,
        )
        logger.error(
            "Instance complete: source=%s connector=%s dimensions=%s attempt=%s "
            "start=%s end=%s duration=%.3fs fetched=%s status=%s error=%s: %s",
            source,
            connector_class,
            json.dumps(dimensions, sort_keys=True),
            attempt_number,
            started_at.isoformat(),
            ended_at.isoformat(),
            elapsed_s,
            len(partial_jobs or []),
            "partial-error" if partial_jobs else "error",
            type(exc).__name__,
            safe_error,
        )
        return FetchResult(
            connector=connector,
            source=source,
            jobs=partial_jobs,
            error=exc,
            elapsed_s=elapsed_s,
            started_at=started_at,
            ended_at=ended_at,
            dimensions=dimensions,
            connector_class=connector_class,
            attempt_details=[attempt],
            observations=[FetchObservation(job, attempt_number, ordinal)
                          for ordinal, job in enumerate(partial_jobs or [], start=1)],
        )


def _fetch_with_progress(connector, progress=None, attempt_number: int = 1) -> FetchResult:
    if progress is not None:
        progress.instance_started(connector, attempt=attempt_number)
    result = _fetch_one(connector, attempt_number=attempt_number)
    if progress is not None:
        progress.instance_finished(result)
    return result


def _merge_jobs(
    first: list[NormalizedJob] | None,
    second: list[NormalizedJob] | None,
) -> list[NormalizedJob]:
    merged: list[NormalizedJob] = []
    seen: set[tuple[str, str]] = set()
    for job in [*(first or []), *(second or [])]:
        key = (job.source, job.external_job_id)
        if key in seen:
            continue
        seen.add(key)
        merged.append(job)
    return merged


def _fetch_priority_browser_batches(
    browser: list, *, progress=None, on_result: Callable[[FetchResult], None] | None = None
) -> tuple[list[FetchResult], float]:
    """Run explicitly prioritized sources before the remaining browser lane."""
    results: list[FetchResult] = []
    started = time.monotonic()
    index = 0
    while index < len(browser):
        first = browser[index]
        batch = [first]
        index += 1
        while (
            index < len(browser)
            and type(browser[index]) is type(first)
            and browser[index].source_name == first.source_name
        ):
            batch.append(browser[index])
            index += 1

        batch_scope = getattr(first, "batch_scope", None)
        scope = batch_scope(batch) if callable(batch_scope) else nullcontext()
        with scope:
            previous_result = None
            for connector in batch:
                cooldown_s = float(
                    getattr(connector, "inter_instance_cooldown_s", 0.0)
                )
                if (
                    previous_result is not None
                    and previous_result.error is None
                    and cooldown_s > 0
                ):
                    logger.info(
                        "Inter-instance cooldown: source=%s wait=%.1fs",
                        connector.source_name,
                        cooldown_s,
                    )
                    time.sleep(cooldown_s)
                previous_result = _fetch_with_progress(connector, progress)
                results.append(previous_result)
                if on_result is not None:
                    on_result(previous_result)
    return results, time.monotonic() - started if browser else 0.0


def fetch_all(
    connectors: list,
    *,
    phase_timings: dict[str, float] | None = None,
    progress=None,
    on_result: Callable[[FetchResult], None] | None = None,
) -> list[FetchResult]:
    """Fetch every connector in `connectors`: non-browser connectors run
    concurrently through a thread pool alongside the serial browser lane.
    Returns one FetchResult per connector, first-attempt only — retries are
    handled separately by `retry_failed` so the retry pass can run after
    ALL connectors (both tiers) have had their first attempt."""
    browser = [c for c in connectors if is_browser_connector(c)]
    non_browser = [c for c in connectors if not is_browser_connector(c)]

    priority_browser = sorted(
        [c for c in browser if getattr(c, "pipeline_priority", 0) < 0],
        key=lambda c: getattr(c, "pipeline_priority", 0),
    )
    browser = [c for c in browser if getattr(c, "pipeline_priority", 0) >= 0]

    parallel_pool: ThreadPoolExecutor | None = None
    parallel_futures = {}
    parallel_start = 0.0
    parallel_wall_s = 0.0
    if non_browser:
        parallel_start = time.monotonic()
        parallel_pool = ThreadPoolExecutor(max_workers=min(32, len(non_browser)))

        def fetch_parallel(connector):
            result = _fetch_with_progress(connector, progress)
            if on_result is not None:
                on_result(result)
            return result, time.monotonic()

        parallel_futures = {
            parallel_pool.submit(fetch_parallel, connector): connector
            for connector in non_browser
        }

    priority_results: list[FetchResult] = []
    browser_results: list[FetchResult] = []
    priority_browser_wall_s = 0.0
    regular_browser_start = 0.0
    regular_browser_wall_s = 0.0
    try:
        priority_results, priority_browser_wall_s = _fetch_priority_browser_batches(
            priority_browser, progress=progress, on_result=on_result
        )

        regular_browser_start = time.monotonic()
        index = 0
        while index < len(browser):
            first = browser[index]
            batch = [first]
            index += 1
            while (
                index < len(browser)
                and type(browser[index]) is type(first)
                and browser[index].source_name == first.source_name
            ):
                batch.append(browser[index])
                index += 1

            batch_scope = getattr(first, "batch_scope", None)
            scope = batch_scope(batch) if callable(batch_scope) else nullcontext()
            with scope:
                previous_result = None
                for connector in batch:
                    cooldown_s = float(
                        getattr(connector, "inter_instance_cooldown_s", 0.0)
                    )
                    if (
                        previous_result is not None
                        and previous_result.error is None
                        and cooldown_s > 0
                    ):
                        logger.info(
                            "Inter-instance cooldown: source=%s wait=%.1fs",
                            connector.source_name,
                            cooldown_s,
                        )
                        time.sleep(cooldown_s)
                    previous_result = _fetch_with_progress(connector, progress)
                    browser_results.append(previous_result)
                    if on_result is not None:
                        on_result(previous_result)

        if browser:
            regular_browser_wall_s = time.monotonic() - regular_browser_start

        parallel_results: list[FetchResult] = []
        parallel_finished_at: list[float] = []
        for future in as_completed(parallel_futures):
            result, finished_at = future.result()
            parallel_results.append(result)
            parallel_finished_at.append(finished_at)
    finally:
        if parallel_pool is not None:
            parallel_pool.shutdown(wait=True)

    if non_browser:
        parallel_wall_s = max(parallel_finished_at) - parallel_start
        logger.info(
            "Parallel fetch of %s API/HTML/feed connectors took %.1fs (thread pool)",
            len(non_browser), parallel_wall_s,
        )

    results = [*priority_results, *parallel_results, *browser_results]
    browser_wall_s = priority_browser_wall_s + regular_browser_wall_s

    if phase_timings is not None:
        phase_timings["parallel_fetch_wall_s"] = parallel_wall_s
        phase_timings["browser_fetch_wall_s"] = browser_wall_s

    return results


def retry_failed(
    results: list[FetchResult],
    *,
    phase_timings: dict[str, float] | None = None,
    progress=None,
) -> None:
    """Runner-level retry: any connector whose first attempt raised gets
    exactly one more try, after everything else has run once. Mutates the
    FetchResult in place. Logs both attempts either way."""
    retry_start = time.monotonic()
    failed = [
        result
        for result in results
        if result.error is not None
        and getattr(result.error, "retryable", True)
        and getattr(result.connector, "max_attempts", 2) > 1
        and result.elapsed_s <= MAX_RETRYABLE_ATTEMPT_SECONDS
    ]
    skipped_slow = [
        result
        for result in results
        if result.error is not None
        and getattr(result.error, "retryable", True)
        and result.elapsed_s > MAX_RETRYABLE_ATTEMPT_SECONDS
    ]
    for result in skipped_slow:
        logger.warning(
            "Retry skipped for %s: first attempt already consumed %.1fs "
            "(limit %.1fs)",
            result.source,
            result.elapsed_s,
            MAX_RETRYABLE_ATTEMPT_SECONDS,
        )
    if not failed:
        if phase_timings is not None:
            phase_timings["retry_pass_wall_s"] = 0.0
        return

    logger.info("Retry pass: %s connector(s) failed on first attempt, retrying once: %s",
                len(failed), [r.source for r in failed])

    for result in failed:
        result.retried = True
        result.attempts = 2
        retry = _fetch_with_progress(
            result.connector, progress, attempt_number=2
        )
        merged_jobs = _merge_jobs(result.jobs, retry.jobs)
        result.elapsed_s += retry.elapsed_s
        result.ended_at = retry.ended_at
        result.attempt_details.extend(retry.attempt_details)
        result.observations.extend(retry.observations)
        if retry.error is None:
            logger.info("Retry succeeded for %s (%s jobs)", result.source, len(retry.jobs or []))
            result.jobs = merged_jobs
            result.error = None
            result.retry_succeeded = True
        else:
            logger.error(
                "Retry failed for %s: %s",
                result.source,
                sanitize_log_text(retry.error),
            )
            result.jobs = merged_jobs
            result.error = retry.error
            result.retry_succeeded = False

        if progress is not None:
            progress.instance_finished(result)

    if phase_timings is not None:
        phase_timings["retry_pass_wall_s"] = time.monotonic() - retry_start


def _source_wall_time_s(results: list[FetchResult]) -> float:
    starts = [result.started_at for result in results if result.started_at is not None]
    ends = [result.ended_at for result in results if result.ended_at is not None]
    if not starts or not ends:
        return 0.0
    return round((max(ends) - min(starts)).total_seconds(), 6)


def deduplicate_normalized_jobs(
    jobs: list[NormalizedJob],
) -> tuple[list[NormalizedJob], int]:
    """Keep the first observation for each canonical collection identity."""
    seen: set[tuple[str, str]] = set()
    unique: list[NormalizedJob] = []
    duplicate_count = 0
    for job in jobs:
        identity = (job.source, job.external_job_id)
        if identity in seen:
            duplicate_count += 1
            continue
        seen.add(identity)
        unique.append(job)
    return unique, duplicate_count


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _date_quality(
    jobs: list[NormalizedJob],
    kept: list[NormalizedJob],
    cutoff_at: datetime,
) -> dict[str, int]:
    cutoff_at = _as_utc(cutoff_at)

    def is_known(job: NormalizedJob) -> bool:
        return isinstance(job.posted_at, datetime)

    def is_within(job: NormalizedJob) -> bool:
        return is_known(job) and _as_utc(job.posted_at) >= cutoff_at

    known = sum(1 for job in jobs if is_known(job))
    within = sum(1 for job in jobs if is_within(job))
    return {
        "known": known,
        "unknown": len(jobs) - known,
        "within_cutoff": within,
        "stale": known - within,
        "kept_within_cutoff": sum(1 for job in kept if is_within(job)),
        "kept_unknown": sum(1 for job in kept if not is_known(job)),
    }


def _field_completeness(jobs: list[NormalizedJob]) -> dict[str, dict[str, float | int]]:
    total = len(jobs)

    def present(value: object) -> bool:
        return value is not None and (not isinstance(value, str) or bool(value.strip()))

    checks = {
        "description": lambda job: present(job.description_raw),
        "company": lambda job: (
            present(job.company_name_raw)
            and job.company_name_raw.strip().lower() != "unknown"
        ),
        "location": lambda job: present(job.location_raw),
        "salary": lambda job: present(job.salary_raw),
        "employment_type": lambda job: present(job.employment_type),
        "seniority": lambda job: present(job.seniority),
        "posted_date": lambda job: isinstance(job.posted_at, datetime),
        "apply_url": lambda job: present(job.apply_url),
    }
    completeness: dict[str, dict[str, float | int]] = {}
    for name, check in checks.items():
        count = sum(1 for job in jobs if check(job))
        completeness[name] = {
            "count": count,
            "pct": round(100 * count / total, 1) if total else 0.0,
        }
    return completeness


def _reuse_stored_details(
    source: str, jobs: list[NormalizedJob]
) -> tuple[list[NormalizedJob], int]:
    """Populate complete stored details and return only jobs needing hydration."""
    candidates = [job for job in jobs if not job.description_raw]
    if not candidates:
        return [], 0

    external_ids = [job.external_job_id for job in candidates]
    with get_session() as session:
        rows = session.execute(
            select(
                Job.external_job_id,
                Job.description_raw,
                Job.seniority,
                Job.employment_type,
                Job.raw_payload,
            ).where(
                Job.source == source,
                Job.external_job_id.in_(external_ids),
            )
        ).all()

    stored_by_id = {
        external_id: {
            "description_raw": description_raw,
            "seniority": seniority,
            "employment_type": employment_type,
            "raw_payload": raw_payload or {},
        }
        for (
            external_id,
            description_raw,
            seniority,
            employment_type,
            raw_payload,
        ) in rows
        if description_raw
    }

    pending: list[NormalizedJob] = []
    reused = 0
    for job in candidates:
        stored = stored_by_id.get(job.external_job_id)
        if stored is None:
            pending.append(job)
            continue
        job.description_raw = stored["description_raw"]
        if not job.seniority:
            job.seniority = stored["seniority"]
        if not job.employment_type:
            job.employment_type = stored["employment_type"]
        stored_payload = stored["raw_payload"]
        for field in ("industry_scraped", "job_function"):
            if field not in job.raw_payload and stored_payload.get(field) is not None:
                job.raw_payload[field] = stored_payload[field]
        reused += 1
    return pending, reused


def gate_and_upsert(
    results: list[FetchResult],
    *,
    cutoff_at: datetime | None = None,
    relevance_audit_run_id: str | None = None,
    outcome_sink: Callable[[IngestionGateFacts], None] | None = None,
) -> tuple[list[ConnectorSummary], int, int]:
    """Central relevance gate + upsert for every successfully-fetched
    connector's jobs. Returns (per-connector summary, total_upserted,
    total_errors).

    Includes per-run deduplication across all connectors before the
    relevance gate (T9/issue #18): dedupe by (source, external_job_id) so
    that the same job fetched from multiple connectors/location modes in the
    same run doesn't multiply.
    """
    summary: list[ConnectorSummary] = []
    total_upserted = 0
    total_errors = 0
    cutoff_at = cutoff_at or (
        datetime.now(timezone.utc) - timedelta(days=RECENCY_WINDOW_DAYS)
    )

    if relevance_audit_run_id is not None:
        # Audit raw occurrences before the normal production dedupe.  The
        # relevant-job path below remains unchanged: it only stores the
        # deduplicated postings which pass the gate.
        from app.pipeline.relevance_audit import record_dropped_observations
        recorded = record_dropped_observations(
            run_id=relevance_audit_run_id,
            results=results,
            cutoff_at=cutoff_at,
        )
        logger.info(
            "Recorded %s dropped raw observation(s) for relevance audit %s",
            recorded,
            relevance_audit_run_id,
        )

    # --- Per-run deduplication across all connectors ---
    # Collect all jobs from all successful results, dedupe by (source, external_job_id)
    all_jobs: list[NormalizedJob] = []
    raw_count_by_source: dict[str, int] = {}
    for result in results:
        if result.jobs:
            all_jobs.extend(result.jobs)
            raw_count_by_source[result.source] = (
                raw_count_by_source.get(result.source, 0) + len(result.jobs)
            )

    raw_facts: list[GateOutcome] = []
    for source_instance, result in enumerate(results, start=1):
        observations = result.observations or [
            FetchObservation(job, 1, ordinal)
            for ordinal, job in enumerate(result.jobs or [], start=1)
        ]
        raw_facts.extend(GateOutcome(
            observation, source_instance, "advanced",
            connector_dimensions=dict(result.dimensions),
        )
                         for observation in observations)

    deduped_jobs, duplicate_count = deduplicate_normalized_jobs(
        [fact.observation.job for fact in raw_facts]
    )
    seen: set[tuple[str, str]] = set()
    collection_facts: list[GateOutcome] = []
    for fact in raw_facts:
        job = fact.observation.job
        key = (job.source, job.external_job_id)
        if key in seen:
            collection_facts.append(GateOutcome(
                fact.observation, fact.source_instance, "dropped", "collection_duplicate",
                fact.connector_dimensions,
            ))
            continue
        seen.add(key)
        collection_facts.append(fact)

    if all_jobs:
        hit_rate = round(100 * duplicate_count / len(all_jobs), 1)
        logger.info(
            "Per-run dedupe: %s/%s fetched job(s) were duplicates across connectors "
            "(hit rate %s%%), %s unique jobs proceed to the gate",
            duplicate_count, len(all_jobs), hit_rate, len(deduped_jobs),
        )

    # --- Relevance gate on deduped jobs ---
    # Group deduped jobs by source for per-source logging/upsert. Each source
    # is gated/upserted exactly ONCE here, even if multiple connector
    # instances share that source_name (e.g. 26 LinkedIn instances for
    # different search-term/location-mode combos) — the per-run dedup above
    # already merged their output into one job list per source.
    jobs_by_source: dict[str, list[NormalizedJob]] = {}
    for job in deduped_jobs:
        jobs_by_source.setdefault(job.source, []).append(job)

    # One aggregate row per source. Per-instance failures remain available in
    # FetchResult/attempt_details and are counted here without duplicating the
    # source in the final report.
    results_by_source: dict[str, list[FetchResult]] = {}
    source_order: list[str] = []
    for result in results:
        if result.source not in results_by_source:
            source_order.append(result.source)
            results_by_source[result.source] = []
        results_by_source[result.source].append(result)

    relevance_facts: list[GateOutcome] = []
    first_observation_by_key = {
        (fact.observation.job.source, fact.observation.job.external_job_id): fact
        for fact in collection_facts if fact.outcome == "advanced"
    }
    for source in source_order:
        source_results = results_by_source[source]
        instances_succeeded = sum(1 for result in source_results if result.error is None)
        instances_failed = len(source_results) - instances_succeeded
        total_errors += instances_failed

        jobs = jobs_by_source.get(source, [])
        kept, drop_counts, drop_details = filter_relevant(
            jobs, cutoff_at=cutoff_at
        )
        kept_keys = {(job.source, job.external_job_id) for job in kept}
        detail_iter = iter(drop_details)
        for job in jobs:
            base = first_observation_by_key[(job.source, job.external_job_id)]
            if (job.source, job.external_job_id) in kept_keys:
                relevance_facts.append(base)
            else:
                detail = next(detail_iter)
                relevance_facts.append(GateOutcome(
                    base.observation, base.source_instance, "dropped", str(detail["axis"]),
                    base.connector_dimensions,
                ))

        # Some sources deliberately expose a fast listing feed plus a much
        # scarcer detail endpoint. Let the connector enrich only rows the
        # central gate kept, rather than making fetch time and rate limits pay
        # for rows that are about to be discarded. Hydration is best-effort:
        # a source-specific circuit breaker must never prevent an otherwise
        # valid listing from being stored.
        hydration_connector = next(
            (
                result.connector
                for result in source_results
                if callable(
                    getattr(result.connector, "hydrate_relevant_jobs", None)
                )
            ),
            None,
        )
        if hydration_connector is not None and kept:
            hydrator = hydration_connector.hydrate_relevant_jobs
            hydration_candidates = kept
            reused = 0
            try:
                if getattr(hydration_connector, "reuse_stored_details", False):
                    try:
                        hydration_candidates, reused = _reuse_stored_details(
                            source, kept
                        )
                    except Exception:
                        logger.exception(
                            "Stored-detail lookup failed for %s; hydrating all eligible jobs",
                            source,
                        )
                        hydration_candidates = kept
                hydration = (
                    hydrator(hydration_candidates)
                    if hydration_candidates
                    else {"eligible": 0, "hydrated": 0, "failed": 0}
                )
                logger.info(
                    "Post-gate hydration for %s: eligible=%s reused=%s "
                    "requested=%s hydrated=%s",
                    source,
                    len(kept),
                    reused,
                    len(hydration_candidates),
                    hydration.get("hydrated", 0),
                )
            except Exception:
                logger.exception(
                    "Post-gate hydration failed for %s; storing kept listings without it",
                    source,
                )

        # Detailed drop logging (T9/issue #18): location-foreign vs location-no-signal split
        location_foreign = sum(1 for d in drop_details if d.get("axis") == "location" and d.get("location_reason") == "foreign")
        location_no_signal = sum(1 for d in drop_details if d.get("axis") == "location" and d.get("location_reason") == "no_signal")

        logger.info(
            "Fetched %s jobs from %s, kept %s (dropped: role=%s seniority=%s "
            "location=%s recency=%s; location_detail: foreign=%s no_signal=%s)%s",
            len(jobs), source, len(kept),
            drop_counts["role"], drop_counts["seniority"],
            drop_counts["location"], drop_counts["recency"],
            location_foreign, location_no_signal,
            " [retry succeeded]" if any(r.retry_succeeded for r in source_results) else "",
        )

        upserted, upsert_errors = _upsert_job_batch(kept, source=source)
        total_errors += upsert_errors

        total_upserted += upserted
        raw_count = raw_count_by_source.get(source, 0)
        duplicate_count = raw_count - len(jobs)
        summary.append(ConnectorSummary(
            source=source,
            fetched=len(jobs),
            kept=len(kept),
            errored=not jobs and instances_succeeded == 0 and instances_failed > 0,
            drop_counts=drop_counts,
            attempts=max((result.attempts for result in source_results), default=1),
            fetch_time_s=sum(result.elapsed_s for result in source_results),
            source_wall_time_s=_source_wall_time_s(source_results),
            instances_started=len(source_results),
            instances_succeeded=instances_succeeded,
            instances_failed=instances_failed,
            instances_retried=sum(1 for result in source_results if result.retried),
            raw_count=raw_count,
            unique_count=len(jobs),
            duplicate_count=duplicate_count,
            duplicate_rate_pct=(
                round(100 * duplicate_count / raw_count, 1) if raw_count else 0.0
            ),
            location_foreign=location_foreign,
            location_no_signal=location_no_signal,
            date_quality=_date_quality(jobs, kept, cutoff_at),
            field_completeness=_field_completeness(jobs),
            upsert_calls_completed=upserted,
            upsert_errors=len(kept) - upserted,
        ))

    if outcome_sink is not None:
        outcome_sink(IngestionGateFacts(
            raw=tuple(raw_facts), collection=tuple(collection_facts),
            relevance=tuple(relevance_facts),
        ))
    return summary, total_upserted, total_errors


def log_summary(summary: list[ConnectorSummary]) -> None:
    logger.info("--- Per-connector yield summary (both tiers) ---")
    for s in summary:
        retry_note = " (needed retry)" if s.attempts > 1 else ""
        if s.errored:
            logger.warning(
                "  %s: ERRORED (zero yield); source wall %.1fs; "
                "cumulative worker %.1fs%s",
                s.source, s.source_wall_time_s, s.fetch_time_s, retry_note,
            )
        elif s.kept == 0:
            logger.warning(
                "  %s: 0 jobs kept (zero yield); source wall %.1fs; "
                "cumulative worker %.1fs%s",
                s.source, s.source_wall_time_s, s.fetch_time_s, retry_note,
            )
        else:
            logger.info(
                "  %s: %s jobs kept; source wall %.1fs; cumulative worker %.1fs%s",
                s.source, s.kept, s.source_wall_time_s, s.fetch_time_s, retry_note,
            )
