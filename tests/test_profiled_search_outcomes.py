from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.models.orm import Base, DecisionRun, Job, ProfiledSearchOutcome
from app.models.schemas import NormalizedJob
from app.pipeline.profiled_outcomes import record_profiled_outcomes
from app.pipeline.relevance import RelevancePolicy
from app.workflows.planning import SearchProfile


def _session():
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True)()


def _profile():
    return SearchProfile.model_validate(
        {
            "version": 1,
            "name": "india-operations",
            "professions": ["operations analyst"],
            "search_terms": ["operations analyst"],
            "relevance_terms": ["operations analyst"],
            "countries": ["IN"],
            "cities": ["Bengaluru"],
            "arrangements": ["remote"],
            "seniority": ["individual_contributor"],
            "collection_window_days": 30,
            "source_selection": {"mode": "all_compatible"},
        }
    )


def _job(job_id, title, location):
    return NormalizedJob(
        source="fixture",
        external_job_id=job_id,
        title=title,
        company_name_raw="Example Co",
        location_raw=location,
        is_remote=True,
        job_url=f"https://jobs.example/{job_id}",
    )


def test_records_all_outcomes_but_only_kept_jobs_enter_canonical_storage():
    session = _session()
    profile = _profile()
    run = DecisionRun(
        id="run-1",
        since_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        cutoff_at=datetime(2026, 9, 2, tzinfo=timezone.utc),
        input_timezone="UTC",
        state="collecting",
        policy_snapshot=profile.model_dump(mode="json"),
        target_count=0,
    )
    session.add(run)
    session.flush()

    result = record_profiled_outcomes(
        session,
        run_id=run.id,
        profile_fingerprint="fingerprint-1",
        policy=RelevancePolicy.from_profile(profile),
        jobs=[
            _job("kept", "Operations Analyst", "Remote - India"),
            _job("review", "Operations Analyst", "Remote"),
            _job("rejected", "Data Scientist", "Remote - India"),
        ],
    )

    assert result.counts == {"kept": 1, "rejected": 1, "needs_review": 1}
    rows = list(session.scalars(select(ProfiledSearchOutcome).order_by(ProfiledSearchOutcome.external_job_id)))
    assert [(row.external_job_id, row.outcome) for row in rows] == [
        ("kept", "kept"),
        ("rejected", "rejected"),
        ("review", "needs_review"),
    ]
    review = next(row for row in rows if row.outcome == "needs_review")
    assert review.job_url == "https://jobs.example/review"
    assert review.reason == "remote country is unclear"
    assert review.profile_fingerprint == "fingerprint-1"
    assert [job.external_job_id for job in session.scalars(select(Job))] == ["kept"]


def test_recording_same_run_is_repeat_safe_without_mutating_another_run():
    session = _session()
    profile = _profile()
    for run_id in ("run-1", "run-2"):
        session.add(
            DecisionRun(
                id=run_id,
                since_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
                cutoff_at=datetime(2026, 9, 2, tzinfo=timezone.utc),
                input_timezone="UTC",
                state="collecting",
                policy_snapshot=profile.model_dump(mode="json"),
                target_count=0,
            )
        )
    session.flush()
    kwargs = {
        "profile_fingerprint": "fingerprint-1",
        "policy": RelevancePolicy.from_profile(profile),
        "jobs": [_job("review", "Operations Analyst", "Remote")],
    }

    record_profiled_outcomes(session, run_id="run-1", **kwargs)
    record_profiled_outcomes(session, run_id="run-1", **kwargs)
    record_profiled_outcomes(session, run_id="run-2", **kwargs)

    rows = list(session.scalars(select(ProfiledSearchOutcome)))
    assert len(rows) == 2
    assert {row.run_id for row in rows} == {"run-1", "run-2"}
