from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.models.orm import Base, DecisionRun, GuidedRunPlan, GuidedSourceRun, Job
from app.models.schemas import NormalizedJob
from app.workflows.guided_run import collect_guided_run, start_guided_run
from app.workflows.planning import SourceCapability, prepare_run
from app.workflows.public_database import public_session


CATALOG = (
    SourceCapability(
        name="one", runtime="http", countries=frozenset({"IN"}),
        arrangements=frozenset({"remote"}), rationale="fixture one",
    ),
    SourceCapability(
        name="two", runtime="http", countries=frozenset({"IN"}),
        arrangements=frozenset({"remote"}), rationale="fixture two",
    ),
)


def _session():
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True)()


def _preview():
    return prepare_run({
        "version": 1,
        "name": "india-operations",
        "professions": ["operations analyst"],
        "search_terms": ["operations analyst"],
        "relevance_terms": ["operations analyst"],
        "countries": ["IN"],
        "arrangements": ["remote"],
        "seniority": ["individual_contributor"],
        "collection_window_days": 30,
        "source_selection": {"mode": "named", "sources": ["one", "two"]},
    }, CATALOG)


def _job(source):
    return NormalizedJob(
        source=source, external_job_id="1", title="Operations Analyst",
        company_name_raw=f"{source} Co", location_raw="Remote - India",
        is_remote=True, job_url=f"https://jobs.example/{source}/1",
    )


def test_start_requires_explicit_confirmation_and_freezes_exact_plan():
    session = _session()
    with pytest.raises(ValueError, match="confirmation"):
        start_guided_run(session, preview=_preview(), profile_fingerprint="fp", confirmed=False)

    started = start_guided_run(
        session, preview=_preview(), profile_fingerprint="fp", confirmed=True,
        now=datetime(2026, 9, 6, tzinfo=timezone.utc), run_id="run-1",
    )
    plan = session.scalar(select(GuidedRunPlan))
    assert started.run_id == "run-1"
    assert plan.profile_fingerprint == "fp"
    assert plan.selected_sources == ["one", "two"]
    assert plan.query_instance_count == 2


def test_partial_source_failure_is_visible_and_resume_only_retries_incomplete_source():
    session = _session()
    start_guided_run(
        session, preview=_preview(), profile_fingerprint="fp", confirmed=True,
        now=datetime(2026, 9, 6, tzinfo=timezone.utc), run_id="run-1",
    )
    calls = []

    def one(_profile):
        calls.append("one")
        return [_job("one")]

    def two_fails(_profile):
        calls.append("two")
        raise RuntimeError("source unavailable")

    result = collect_guided_run(session, run_id="run-1", fetchers={"one": one, "two": two_fails})
    assert result.state == "partial"
    assert result.source_counts == {"completed": 1, "failed": 1}
    assert session.get(DecisionRun, "run-1").state == "partial"

    def two_recovers(_profile):
        calls.append("two")
        return [_job("two")]

    resumed = collect_guided_run(session, run_id="run-1", fetchers={"one": one, "two": two_recovers})
    assert resumed.state == "collected"
    assert calls == ["one", "two", "two"]
    assert {job.source for job in session.scalars(select(Job))} == {"one", "two"}
    assert [(row.source, row.status) for row in session.scalars(
        select(GuidedSourceRun).order_by(GuidedSourceRun.source)
    )] == [("one", "completed"), ("two", "completed")]


def test_collection_duplicates_are_classified_and_counted_once():
    session = _session()
    start_guided_run(
        session, preview=_preview(), profile_fingerprint="fp", confirmed=True,
        now=datetime(2026, 9, 6, tzinfo=timezone.utc), run_id="run-1",
    )
    collect_guided_run(session, run_id="run-1", fetchers={
        "one": lambda _profile: [_job("one"), _job("one")],
        "two": lambda _profile: [],
    })

    source = session.scalar(
        select(GuidedSourceRun).where(GuidedSourceRun.source == "one")
    )
    assert source.outcome_counts == {
        "kept": 1, "rejected": 0, "needs_review": 0,
    }


def test_cross_source_duplicate_detection_runs_after_collection():
    session = _session()
    start_guided_run(
        session, preview=_preview(), profile_fingerprint="fp", confirmed=True,
        now=datetime(2026, 9, 6, tzinfo=timezone.utc), run_id="run-1",
    )

    def same_posting(source):
        job = _job(source)
        job.company_name_raw = "Shared Company"
        job.posted_at = datetime(2026, 9, 5, tzinfo=timezone.utc)
        job.location_raw = "Remote - India"
        return job

    result = collect_guided_run(session, run_id="run-1", fetchers={
        "one": lambda _profile: [same_posting("one")],
        "two": lambda _profile: [same_posting("two")],
    })

    jobs = list(session.scalars(select(Job).order_by(Job.id)))
    assert result.canonical_dedup["flagged"] == 1
    assert sum(job.duplicate_of_job_id is not None for job in jobs) == 1


def test_source_progress_is_visible_to_a_separate_dashboard_reader(tmp_path):
    observed = []
    with public_session(tmp_path) as session:
        start_guided_run(
            session, preview=_preview(), profile_fingerprint="fp", confirmed=True,
            now=datetime(2026, 9, 6, tzinfo=timezone.utc), run_id="run-live",
        )
        session.commit()

        def observe(source):
            def fetch(_profile):
                with public_session(tmp_path) as reader:
                    observed.append([
                        (row.source, row.status)
                        for row in reader.scalars(
                            select(GuidedSourceRun)
                            .where(GuidedSourceRun.run_id == "run-live")
                            .order_by(GuidedSourceRun.id)
                        )
                    ])
                return [_job(source)]
            return fetch

        collect_guided_run(
            session,
            run_id="run-live",
            fetchers={"one": observe("one"), "two": observe("two")},
        )

    assert observed == [
        [("one", "running"), ("two", "pending")],
        [("one", "completed"), ("two", "running")],
    ]
