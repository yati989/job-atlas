import logging
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.schemas import NormalizedJob
from app.models.orm import Base
from app.collectors.base import PartialFetchError
from app.pipeline import runner
from app.pipeline.runner import (
    ConnectorSummary,
    FetchResult,
    IncrementalRunPersister,
    fetch_all,
    gate_and_upsert,
    log_summary,
    retry_failed,
)
from app.pipeline.relevance_audit import dropped_observation_mappings


def test_runner_summary_prints_per_source_fetch_time(caplog):
    caplog.set_level(logging.INFO, logger="pipeline")

    log_summary([
        ConnectorSummary(
            source="test_source",
            kept=2,
            fetch_time_s=3.75,
            source_wall_time_s=1.25,
        )
    ])

    assert (
        "test_source: 2 jobs kept; source wall 1.2s; cumulative worker 3.8s"
        in caplog.text
    )


def _job(
    external_id: str,
    title: str,
    posted_at: datetime | None,
    *,
    complete: bool = False,
) -> NormalizedJob:
    return NormalizedJob(
        source="example",
        external_job_id=external_id,
        title=title,
        company_name_raw="Acme" if complete else "Unknown",
        location_raw="Bengaluru" if complete else None,
        is_remote=False if complete else True,
        employment_type="full-time" if complete else None,
        seniority="mid" if complete else None,
        description_raw="Build models" if complete else None,
        salary_raw="INR 20L" if complete else None,
        apply_url="https://example.test/apply" if complete else None,
        posted_at=posted_at,
    )


def test_gate_summary_aggregates_instances_dedup_date_and_completeness(monkeypatch):
    now = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
    first_start = now - timedelta(seconds=7)
    first_end = now - timedelta(seconds=2)
    second_start = now - timedelta(seconds=4)
    second_end = now
    current = now - timedelta(days=2)
    stale = now - timedelta(days=30)

    results = [
        FetchResult(
            connector=object(),
            source="example",
            jobs=[
                _job("1", "Data Scientist", current, complete=True),
                _job("2", "Account Executive", stale),
            ],
            elapsed_s=5.0,
            started_at=first_start,
            ended_at=first_end,
        ),
        FetchResult(
            connector=object(),
            source="example",
            jobs=[
                _job("1", "Data Scientist", current, complete=True),
                _job("3", "Data Analyst", None),
            ],
            elapsed_s=4.0,
            started_at=second_start,
            ended_at=second_end,
        ),
    ]

    @contextmanager
    def no_database():
        yield object()

    monkeypatch.setattr(runner, "get_session", no_database)
    monkeypatch.setattr(runner, "upsert_job", lambda _session, _item: None)

    summary, successful_upserts, errors = gate_and_upsert(
        results,
        cutoff_at=now - timedelta(days=15),
    )

    assert successful_upserts == 2
    assert errors == 0
    assert len(summary) == 1
    source = summary[0]
    assert source.instances_started == 2
    assert source.instances_succeeded == 2
    assert source.instances_failed == 0
    assert source.raw_count == 4
    assert source.unique_count == 3
    assert source.duplicate_count == 1
    assert source.duplicate_rate_pct == 25.0
    assert source.kept == 2
    assert source.drop_counts == {
        "role": 1,
        "seniority": 0,
        "location": 0,
        "recency": 0,
    }
    assert source.source_wall_time_s == 7.0
    assert source.fetch_time_s == 9.0
    assert source.date_quality == {
        "known": 2,
        "unknown": 1,
        "within_cutoff": 1,
        "stale": 1,
        "kept_within_cutoff": 1,
        "kept_unknown": 1,
    }
    assert source.field_completeness["description"] == {"count": 1, "pct": 33.3}
    assert source.field_completeness["posted_date"] == {"count": 2, "pct": 66.7}


def test_relevance_audit_preserves_raw_duplicate_drops_with_provenance():
    now = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
    rejected = _job("dropped", "Account Executive", now)
    results = [
        FetchResult(
            connector=object(), source="example", jobs=[rejected],
            dimensions={"search": "data scientist", "location_mode": "bengaluru"},
        ),
        FetchResult(
            connector=object(), source="example", jobs=[rejected],
            dimensions={"search": "data analyst", "location_mode": "remote_india"},
        ),
    ]

    observations = dropped_observation_mappings(
        results, audit_run_id="audit-1", cutoff_at=now - timedelta(days=15)
    )

    assert len(observations) == 2
    assert all(row["first_failed_axis"] == "role" for row in observations)
    assert {row["dimensions"]["search"] for row in observations} == {
        "data scientist", "data analyst",
    }
    assert observations[0]["job_snapshot"]["title"] == "Account Executive"


def test_fetch_result_records_safe_dimensions_attempts_and_redacted_retry_error():
    class FlakyConnector:
        source_name = "example"

        def __init__(self):
            self.search = "data scientist"
            self.location_mode = "remote_india"
            self.session_token = "must-not-appear"
            self.calls = 0

        def fetch(self):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError(
                    "blocked https://example.test/jobs?token=must-not-appear"
                )
            return [_job("4", "Data Scientist", datetime.now(timezone.utc), complete=True)]

    phase_timings = {}
    results = fetch_all([FlakyConnector()], phase_timings=phase_timings)
    retry_failed(results, phase_timings=phase_timings)

    result = results[0]
    assert result.dimensions == {
        "search": "data scientist",
        "location_mode": "remote_india",
    }
    assert result.connector_class == "FlakyConnector"
    assert result.attempts == 2
    assert result.retried is True
    assert result.retry_succeeded is True
    assert result.started_at is not None
    assert result.ended_at is not None
    assert len(result.attempt_details) == 2
    assert result.attempt_details[0].status == "error"
    assert result.attempt_details[0].error_type == "RuntimeError"
    assert "must-not-appear" not in result.attempt_details[0].error
    assert "?<redacted>" in result.attempt_details[0].error
    assert result.attempt_details[1].status == "succeeded"
    assert result.attempt_details[1].fetched_count == 1
    assert "parallel_fetch_wall_s" in phase_timings
    assert "browser_fetch_wall_s" in phase_timings
    assert "retry_pass_wall_s" in phase_timings


def test_runner_respects_a_connector_one_attempt_ceiling():
    class OneTryConnector:
        source_name = "instahyre"
        max_attempts = 1

        def __init__(self):
            self.calls = 0

        def fetch(self):
            self.calls += 1
            raise RuntimeError("HTTP 429")

    connector = OneTryConnector()
    results = fetch_all([connector])
    retry_failed(results)

    assert connector.calls == 1
    assert results[0].attempts == 1
    assert results[0].retried is False


def test_gate_hydrates_only_relevant_jobs_before_upsert(monkeypatch):
    now = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
    hydrated_ids: list[str] = []

    class TwoStageConnector:
        source_name = "instahyre"

        def hydrate_relevant_jobs(self, jobs):
            hydrated_ids.extend(job.external_job_id for job in jobs)
            for job in jobs:
                job.description_raw = "Detailed requirements"
            return {"eligible": len(jobs), "hydrated": len(jobs)}

    relevant = _job("kept", "Data Scientist", now, complete=False)
    irrelevant = _job("dropped", "Account Executive", now, complete=False)
    relevant.source = irrelevant.source = "instahyre"
    results = [
        FetchResult(
            connector=TwoStageConnector(),
            source="instahyre",
            jobs=[relevant, irrelevant],
            elapsed_s=0.1,
        )
    ]

    @contextmanager
    def no_database():
        yield object()

    monkeypatch.setattr(runner, "get_session", no_database)
    monkeypatch.setattr(runner, "upsert_job", lambda _session, _item: None)

    summary, upserted, errors = gate_and_upsert(
        results, cutoff_at=now - timedelta(days=15)
    )

    assert hydrated_ids == ["kept"]
    assert upserted == 1
    assert errors == 0
    assert summary[0].field_completeness["description"] == {"count": 1, "pct": 50.0}


def test_gate_reuses_stored_details_and_hydrates_only_missing_jobs(monkeypatch):
    now = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
    reused = _job("stored", "Data Scientist", now)
    missing = _job("new", "Data Analyst", now)
    reused.source = missing.source = "linkedin"
    hydrated_ids: list[str] = []

    class DatabaseAwareConnector:
        source_name = "linkedin"
        reuse_stored_details = True

        def hydrate_relevant_jobs(self, jobs):
            hydrated_ids.extend(job.external_job_id for job in jobs)
            for job in jobs:
                job.description_raw = "Newly fetched description"
            return {"eligible": len(jobs), "hydrated": len(jobs), "failed": 0}

    class StoredRows:
        def all(self):
            return [
                (
                    "stored",
                    "Stored complete description",
                    "Mid-Senior level",
                    "Full-time",
                    {"industry_scraped": "Technology", "job_function": "Engineering"},
                )
            ]

    class FakeSession:
        def execute(self, _statement):
            return StoredRows()

    @contextmanager
    def fake_database():
        yield FakeSession()

    monkeypatch.setattr(runner, "get_session", fake_database)
    monkeypatch.setattr(runner, "upsert_job", lambda _session, _item: None)

    summary, upserted, errors = gate_and_upsert(
        [
            FetchResult(
                connector=DatabaseAwareConnector(),
                source="linkedin",
                jobs=[reused, missing],
                elapsed_s=0.1,
            )
        ],
        cutoff_at=now - timedelta(days=15),
    )

    assert hydrated_ids == ["new"]
    assert reused.description_raw == "Stored complete description"
    assert reused.seniority == "Mid-Senior level"
    assert reused.employment_type == "Full-time"
    assert reused.raw_payload["industry_scraped"] == "Technology"
    assert missing.description_raw == "Newly fetched description"
    assert upserted == 2
    assert errors == 0
    assert summary[0].field_completeness["description"] == {"count": 2, "pct": 100.0}


def test_parallel_tier_overlaps_priority_and_regular_browser_sources():
    parallel_started = threading.Event()
    priority_observed_parallel = threading.Event()
    browser_observed_parallel = threading.Event()
    release_parallel = threading.Event()

    class ParallelConnector:
        source_name = "parallel-example"

        def fetch(self):
            parallel_started.set()
            assert release_parallel.wait(timeout=1)
            return []

    class PriorityBrowserConnector:
        source_name = "indeed"
        pipeline_priority = -100

        def fetch(self):
            if parallel_started.wait(timeout=0.2):
                priority_observed_parallel.set()
            return []

    class BrowserConnector:
        source_name = "browser-example"

        def fetch(self):
            if parallel_started.is_set() and not release_parallel.is_set():
                browser_observed_parallel.set()
            release_parallel.set()
            return []

    PriorityBrowserConnector.__module__ = "app.collectors.browser.indeed"
    BrowserConnector.__module__ = "app.collectors.browser.example"

    fetch_all(
        [ParallelConnector(), PriorityBrowserConnector(), BrowserConnector()]
    )

    assert priority_observed_parallel.is_set()
    assert browser_observed_parallel.is_set()


def test_completed_parallel_result_is_published_while_browser_work_continues():
    parallel_published = threading.Event()
    browser_started = threading.Event()
    release_browser = threading.Event()

    class ParallelConnector:
        source_name = "parallel-example"

        def fetch(self):
            return []

    class SlowBrowserConnector:
        source_name = "browser-example"

        def fetch(self):
            browser_started.set()
            assert release_browser.wait(timeout=2)
            return []

    SlowBrowserConnector.__module__ = "app.collectors.browser.example"

    run_thread = threading.Thread(
        target=fetch_all,
        args=([ParallelConnector(), SlowBrowserConnector()],),
        kwargs={"on_result": lambda result: parallel_published.set()
                if result.source == "parallel-example" else None},
    )
    run_thread.start()
    assert browser_started.wait(timeout=1)
    assert parallel_published.wait(timeout=1)
    assert run_thread.is_alive()
    release_browser.set()
    run_thread.join(timeout=2)
    assert not run_thread.is_alive()


def test_incremental_persister_upserts_each_run_unique_job_immediately(monkeypatch):
    now = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
    upserted_ids: list[str] = []

    class FakeSession:
        def rollback(self):
            pass

    @contextmanager
    def no_database():
        yield FakeSession()

    monkeypatch.setattr(runner, "get_session", no_database)
    monkeypatch.setattr(
        runner,
        "upsert_job",
        lambda _session, item: upserted_ids.append(item.external_job_id),
    )

    first = FetchResult(
        connector=object(),
        source="example",
        jobs=[_job("1", "Data Scientist", now, complete=True)],
    )
    duplicate = FetchResult(
        connector=object(),
        source="example",
        jobs=[_job("1", "Data Scientist", now, complete=True)],
    )
    partial = FetchResult(
        connector=object(),
        source="example",
        jobs=[_job("2", "Data Analyst", now, complete=True)],
        error=RuntimeError("terminal partial failure"),
    )

    persister = IncrementalRunPersister(
        cutoff_at=now - timedelta(days=15)
    )
    persister.persist(first)
    persister.persist(first)
    persister.persist(duplicate)
    persister.persist_remaining([first, duplicate, partial])

    assert upserted_ids == ["1", "2"]


def test_incremental_persister_isolates_one_bad_job_with_savepoints(monkeypatch):
    now = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
    attempted_ids: list[str] = []
    savepoints_opened = 0

    class FakeSession:
        @contextmanager
        def begin_nested(self):
            nonlocal savepoints_opened
            savepoints_opened += 1
            yield

    @contextmanager
    def fake_database():
        yield FakeSession()

    def sometimes_fails(_session, item):
        attempted_ids.append(item.external_job_id)
        if item.external_job_id == "oversized":
            raise ValueError("value too long")

    monkeypatch.setattr(runner, "get_session", fake_database)
    monkeypatch.setattr(runner, "upsert_job", sometimes_fails)

    result = FetchResult(
        connector=object(),
        source="example",
        jobs=[
            _job("before", "Data Scientist", now, complete=True),
            _job("oversized", "Data Analyst", now, complete=True),
            _job("after", "Data Engineer", now, complete=True),
        ],
    )
    persister = IncrementalRunPersister(
        cutoff_at=now - timedelta(days=15)
    )

    persister.persist(result)

    assert attempted_ids == ["before", "oversized", "after"]
    assert savepoints_opened == 3


def test_partial_fetch_retry_merges_jobs_and_keeps_terminal_outcome_truthful(
    monkeypatch, caplog
):
    caplog.set_level(logging.INFO, logger="pipeline")
    now = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)

    class PartialConnector:
        source_name = "example"
        search = "data scientist"
        location_mode = "remote_india"

        def __init__(self):
            self.calls = 0

        def fetch(self):
            self.calls += 1
            if self.calls == 1:
                jobs = [
                    _job("1", "Data Scientist", now, complete=True),
                    _job("2", "Data Analyst", now, complete=True),
                ]
            else:
                jobs = [
                    _job("2", "Data Analyst", now, complete=True),
                    _job("3", "Data Engineer", now, complete=True),
                ]
            raise PartialFetchError(
                "blocked https://wellfound.com/role/r/data-scientist?page=3",
                jobs,
                RuntimeError("HTTP 429"),
            )

    results = fetch_all([PartialConnector()])
    retry_failed(results)

    result = results[0]
    assert result.error is not None
    assert [job.external_job_id for job in result.jobs] == ["1", "2", "3"]
    assert [attempt.status for attempt in result.attempt_details] == [
        "partial-error",
        "partial-error",
    ]
    assert [attempt.fetched_count for attempt in result.attempt_details] == [2, 2]
    assert "?page=3" not in result.attempt_details[0].error
    assert "?<redacted>" in result.attempt_details[0].error
    assert "?page=3" not in caplog.text

    @contextmanager
    def no_database():
        yield object()

    monkeypatch.setattr(runner, "get_session", no_database)
    monkeypatch.setattr(runner, "upsert_job", lambda _session, _item: None)
    summary, successful_upserts, errors = gate_and_upsert(
        results, cutoff_at=now - timedelta(days=15)
    )

    assert successful_upserts == 3
    assert errors == 1
    assert summary[0].raw_count == 3
    assert summary[0].unique_count == 3
    assert summary[0].instances_failed == 1
    assert summary[0].errored is False
