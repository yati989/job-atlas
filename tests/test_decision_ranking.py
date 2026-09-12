from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.decision_runs.company_funnel import (
    CompanyEnrichmentResult,
    CompanyProcessingOutcome,
    PhaseBEvidenceOutcome,
    report_company_phase_a,
    report_company_phase_b,
)
from app.decision_runs.enrichment_funnel import (
    EnrichmentResult,
    freeze_job_enrichment_manifest,
    report_job_enrichment,
)
from app.decision_runs.ingestion_funnel import (
    CanonicalJobReference,
    report_canonical_jobs,
)
from app.decision_runs.ranking import RankableJob, build_ranking_plan
from app.decision_runs.progress import StageName
from app.decision_runs.service import (
    create_decision_run,
    finalize_decision_run,
    prepare_decision_run,
)
from app.models.orm import (
    Base,
    Company,
    DecisionRunCompany,
    DecisionRunIngestionObservation,
    DecisionRunJob,
    Job,
    JobPostingVersion,
)
from app.models.schemas import NormalizedJob
from app.pipeline.upsert import upsert_job
from tests.progress_support import complete_stage_chain


NOW = datetime(2026, 8, 25, 12, tzinfo=timezone.utc)


def test_immutable_ranking_plan_is_reproducible_and_preserves_established_policy():
    jobs = (
        RankableJob(101, 1, 10, "analytics", None, 4.5, NOW, True),
        RankableJob(102, 2, 10, "ml_ai", 30, 4.0, NOW - timedelta(days=1), False),
        RankableJob(201, 3, 20, "data_science", 30, 4.2, NOW, True),
        RankableJob(301, 4, 30, "credit_risk", 19.9, None, NOW, True),
        RankableJob(401, 5, 40, "data_engineering", 19.9, None, NOW, False),
    )

    plan = build_ranking_plan(jobs)
    replay = build_ranking_plan(reversed(jobs))

    assert replay == plan
    assert [
        (row.run_job_id, row.group_number, row.within_company_rank, row.is_primary)
        for row in plan.jobs
    ] == [
        (101, 1, 2, True),
        (102, 2, 1, False),
        (201, 1, 1, True),
        (301, 3, 1, True),
        (401, 4, 1, True),
    ]
    assert [
        (row.company_id, row.group_number, row.rank, row.primary_run_job_id)
        for row in plan.companies
    ] == [
        (20, 1, 1, 201),
        (10, 1, 2, 101),
        (30, 3, 1, 301),
        (40, 4, 1, 401),
    ]
    with pytest.raises(FrozenInstanceError):
        plan.jobs[0].group_number = 4


def _session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True)()


def _seed_jobs(session):
    specs = (
        ("a", "1", "Data Analyst", "Alpha", True, NOW),
        ("a", "2", "ML Engineer", "Alpha", False, NOW - timedelta(days=1)),
        ("b", "1", "Data Scientist", "Beta", True, NOW),
    )
    jobs = []
    for source, external_id, title, company, remote, posted_at in specs:
        jobs.append(upsert_job(session, NormalizedJob(
            source=source,
            external_job_id=external_id,
            title=title,
            company_name_raw=company,
            description_raw="work",
            posted_at=posted_at,
            is_remote=remote,
        )))
    for company in session.execute(select(Company)).scalars():
        company.ambitionbox_estimated_salary_lpa = 30
        company.glassdoor_wlb_rating = 4.2
        company.glassdoor_review_count = 100
    session.flush()
    return jobs


def _placement_snapshot(session, run_id):
    jobs = list(session.execute(select(DecisionRunJob).where(
        DecisionRunJob.run_id == run_id,
    )).scalars())
    by_id = {job.id: job for job in jobs}
    companies = list(session.execute(select(DecisionRunCompany).where(
        DecisionRunCompany.run_id == run_id,
    ).order_by(DecisionRunCompany.group_number, DecisionRunCompany.rank)).scalars())
    return (
        sorted((
            job.snapshot["title"], job.group_number, job.within_company_rank,
            job.is_primary, job.effective_salary_lpa, job.qualified_wlb,
        ) for job in jobs if job.outcome == "eligible"),
        [(
            company.group_number, company.rank,
            by_id[company.primary_run_job_id].snapshot["title"],
        ) for company in companies],
    )


def _prepare_legacy(session):
    _seed_jobs(session)
    run = create_decision_run(
        session, since=NOW - timedelta(days=2), cutoff=NOW + timedelta(hours=1),
    )
    run.telemetry_version = None
    prepare_decision_run(session, run.id)
    return run.id


def _prepare_current(session):
    jobs = _seed_jobs(session)
    run = create_decision_run(
        session, since=NOW - timedelta(days=2), cutoff=NOW + timedelta(hours=1),
    )
    complete_stage_chain(session, run.id, StageName.RELEVANCE_STORAGE)
    references = []
    for ordinal, job in enumerate(jobs, 1):
        observation = DecisionRunIngestionObservation(
            run_id=run.id,
            source=job.source,
            external_job_id=job.external_job_id,
            connector_instance=ordinal,
            connector_dimensions={},
            fetch_attempt=1,
            occurrence_ordinal=1,
        )
        session.add(observation)
        session.flush()
        references.append(CanonicalJobReference(observation.id))
    report_canonical_jobs(session, run.id, references)
    manifest = freeze_job_enrichment_manifest(session, run.id)
    report_job_enrichment(session, run.id, [
        EnrichmentResult(version_id, "enriched")
        for version_id in manifest.posting_version_ids
    ])
    prepare_decision_run(session, run.id)

    company_ids = [company.id for company in session.execute(select(Company)).scalars()]
    report_company_phase_a(session, run.id, [
        CompanyEnrichmentResult(company_id, CompanyProcessingOutcome.ENRICHED)
        for company_id in company_ids
    ])
    report_company_phase_b(session, run.id, [
        CompanyEnrichmentResult(
            company_id, CompanyProcessingOutcome.ENRICHED, source=source,
            evidence_outcome=PhaseBEvidenceOutcome.FOUND,
        )
        for company_id in company_ids
        for source in ("glassdoor", "ambitionbox")
    ])

    # Ranking must use the immutable DecisionRunJob snapshot, not mutable Jobs.
    for job in jobs:
        job.title = "Mutated title"
        job.is_remote = not job.is_remote
    finalize_decision_run(session, run.id)
    return run.id


def test_legacy_and_current_finalization_have_strict_ranking_parity():
    legacy, current = _session(), _session()
    legacy_snapshot = _placement_snapshot(legacy, _prepare_legacy(legacy))
    current_snapshot = _placement_snapshot(current, _prepare_current(current))

    assert current_snapshot == legacy_snapshot
    assert legacy_snapshot == (
        [
            ("Data Analyst", 1, 2, True, 30, 4.2),
            ("Data Scientist", 1, 1, True, 30, 4.2),
            ("ML Engineer", 2, 1, False, 30, 4.2),
        ],
        [(1, 1, "Data Scientist"), (1, 2, "Data Analyst")],
    )
