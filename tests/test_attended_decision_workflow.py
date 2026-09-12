import argparse
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.decision_runs import (
    CompanyEnrichmentResult,
    CompanyProcessingOutcome,
    EnrichmentResult,
    PhaseBEvidenceOutcome,
    create_decision_run,
    freeze_job_enrichment_manifest,
    report_company_phase_a,
    report_company_phase_b,
    report_job_enrichment,
    screen_decision_run,
)
from app.decision_runs.ingestion_funnel import report_canonical_jobs, report_ingestion_funnel
from app.decision_runs.progress import (
    StageCounts, StageName, finish_stage, interrupt_stage, read_run_progress,
    start_stage,
)
from app.models.orm import Base, DecisionRunCompany
from app.models.schemas import NormalizedJob
from app.pipeline.runner import FetchObservation, GateOutcome, IngestionGateFacts
from app.pipeline.upsert import upsert_job
from scripts import run_full_pipeline


NOW = datetime(2026, 8, 25, 12, tzinfo=timezone.utc)


def test_attended_commands_follow_exact_run_manifests_and_resume_idempotently(monkeypatch, tmp_path):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, future=True)()
    run = create_decision_run(
        session, since=NOW - timedelta(days=1), cutoff=NOW,
    )
    normalized = NormalizedJob(
        source="fixture", external_job_id="job-1", title="Data Engineer",
        company_name_raw="Acme", description_raw="work", posted_at=NOW,
        is_remote=True, job_url="https://jobs.test/job-1",
    )
    job = upsert_job(session, normalized)

    start_stage(
        session, run.id, StageName.FETCH_JOBS, expected_count=1,
        reason="attended connector collection",
    )
    finish_stage(
        session, run.id, StageName.FETCH_JOBS,
        counts=StageCounts(1, 1, 0, 0, 0),
    )
    observation = FetchObservation(normalized, 1, 1)
    advanced = GateOutcome(observation, 1, "advanced")
    canonical = report_ingestion_funnel(
        session, run.id,
        IngestionGateFacts((advanced,), (advanced,), (advanced,)),
    )
    report_canonical_jobs(session, run.id, canonical)

    manifest = freeze_job_enrichment_manifest(session, run.id)
    report_job_enrichment(session, run.id, [
        EnrichmentResult(manifest.posting_version_ids[0], "enriched"),
    ])
    screen_decision_run(session, run.id)
    progress_after_screen = {stage.name: stage for stage in read_run_progress(
        session, run.id, project_interruptions=False,
    ).stages}
    assert progress_after_screen[StageName.SCREENING_RANKING_GROUPING.value].status == "pending"
    report_company_phase_a(session, run.id, [
        CompanyEnrichmentResult(job.company_id, CompanyProcessingOutcome.ENRICHED),
    ])
    report_company_phase_b(session, run.id, [
        CompanyEnrichmentResult(
            job.company_id, CompanyProcessingOutcome.ENRICHED,
            evidence_outcome=PhaseBEvidenceOutcome.CONFIRMED_MISSING,
            source="glassdoor",
        ),
        CompanyEnrichmentResult(
            job.company_id, CompanyProcessingOutcome.ENRICHED,
            evidence_outcome=PhaseBEvidenceOutcome.CONFIRMED_MISSING,
            source="ambitionbox",
        ),
    ])

    monkeypatch.setattr(run_full_pipeline, "get_session", lambda: nullcontext(session))
    monkeypatch.setattr("app.db.session.get_session", lambda: nullcontext(session))
    built = []
    monkeypatch.setattr(
        "app.reporting.decision_report.build_decision_workbook",
        lambda _session, _run_id, output: built.append(output),
    )

    run_full_pipeline.cmd_finalize_decision(argparse.Namespace(run_id=run.id))
    run_full_pipeline.cmd_finalize_decision(argparse.Namespace(run_id=run.id))
    progress = {stage.name: stage for stage in read_run_progress(
        session, run.id, project_interruptions=False,
    ).stages}
    assert progress[StageName.SCREENING_RANKING_GROUPING.value].status == "completed"
    assert progress[StageName.APPROVAL_GATE.value].status == "running"
    assert len(session.execute(select(DecisionRunCompany).where(
        DecisionRunCompany.run_id == run.id,
    )).scalars().all()) == 1
    interrupt_stage(
        session, run.id, StageName.APPROVAL_GATE,
        reason="artifact builder process disappeared",
    )

    prepare_args = argparse.Namespace(
        run_id=run.id, output=str(tmp_path / "approval.xlsx"),
        send_self_report=False,
    )
    run_full_pipeline.cmd_prepare_decision(prepare_args)
    run_full_pipeline.cmd_prepare_decision(prepare_args)
    approval = next(stage for stage in read_run_progress(
        session, run.id, project_interruptions=False,
    ).stages if stage.name == StageName.APPROVAL_GATE.value)
    assert approval.status == "waiting_for_approval"
    assert [(attempt.attempt_number, attempt.status) for attempt in approval.attempts] == [
        (1, "interrupted"), (2, "waiting_for_approval"),
    ]
    assert built == [prepare_args.output, prepare_args.output]

    run_full_pipeline.cmd_approve(argparse.Namespace(
        run_id=run.id, selection="target 1; G1 all; G2 0; G3 0; G4 0",
        workbook=None,
    ))
    approval = next(stage for stage in read_run_progress(
        session, run.id, project_interruptions=False,
    ).stages if stage.name == StageName.APPROVAL_GATE.value)
    assert approval.status == "completed"
    assert (approval.selected_company_count, approval.selected_job_count) == (1, 1)
    assert [stage.name for stage in read_run_progress(
        session, run.id, project_interruptions=False,
    ).stages[:10]] == [
        StageName.FETCH_JOBS.value,
        StageName.COLLECTION_DEDUPLICATION.value,
        StageName.RELEVANCE_STORAGE.value,
        StageName.JOB_DEDUPLICATION.value,
        StageName.JOB_ENRICHMENT.value,
        StageName.COMPANY_DEDUPLICATION.value,
        StageName.COMPANY_PHASE_B.value,
        StageName.SCREENING_RANKING_GROUPING.value,
        StageName.COMPANY_PHASE_A.value,
        StageName.APPROVAL_GATE.value,
    ]
