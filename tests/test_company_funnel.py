from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.dashboard.queries import decision_run_company_funnel, decision_run_company_funnel_drilldown
from app.decision_runs.company_funnel import (
    CompanyEnrichmentResult, CompanyFailureDisposition, CompanyPhase, CompanyProcessingOutcome,
    PhaseBEvidenceOutcome, freeze_company_funnel_manifest,
    begin_company_phase_b,
    company_failure_dispositions, company_terminal_outcome, record_company_terminal_outcomes,
    report_company_phase_a, report_company_phase_b,
    scope_company_phase_a_to_approval,
)
from app.decision_runs.progress import (
    StageName, StageTransitionError, read_run_progress, reconcile_stale_stages,
)
from app.models.orm import (Base, Company, DecisionRun, DecisionRunCompany,
                            DecisionRunCompanyFunnelManifest, DecisionRunJob,
                            DecisionRunSelectedJob)
from app.models.orm import Job
from app.decision_runs.service import finalize_decision_run
import pytest
from tests.progress_support import complete_stage_chain


NOW = datetime(2026, 8, 25, tzinfo=timezone.utc)


def _session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, future=True)()
    session.add(DecisionRun(id="run-1", since_at=NOW - timedelta(days=1), cutoff_at=NOW,
                            input_timezone="UTC", state="preparing", policy_snapshot={}, target_count=3,
                            telemetry_version="decision_funnels_v1"))
    session.flush()
    complete_stage_chain(session, "run-1", StageName.JOB_ENRICHMENT)
    return session


def _run_job(session, company, version_id, outcome, source="board-a"):
    session.add(DecisionRunJob(run_id="run-1", posting_version_id=version_id, job_id=version_id,
                               company_id=company.id, outcome=outcome,
                               snapshot={"source": source, "job_url": f"https://jobs.test/{version_id}"}))


def test_company_funnel_uses_exact_eligible_jobs_deduplicates_and_keeps_overlapping_anomaly_outside_entry():
    session = _session()
    alpha = Company(name="Alpha", enrichment_status="done", industry="Data", description="desc", pain_points="pain",
                    contact_search_groups=[], contact_profile_enriched_at=NOW, domain_resolution_status="unresolvable", enriched_at=NOW)
    beta = Company(name="Beta")
    anomaly_only = Company(name="Anomaly")
    session.add_all((alpha, beta, anomaly_only)); session.flush()
    _run_job(session, alpha, 1, "eligible")
    _run_job(session, alpha, 2, "eligible", "board-b")
    _run_job(session, alpha, 3, "anomaly")  # same employer must not erase eligible entry
    _run_job(session, beta, 4, "eligible")
    _run_job(session, anomaly_only, 5, "anomaly")
    session.flush()

    manifest = freeze_company_funnel_manifest(session, "run-1")
    assert [(row.company_id, row.eligible_job_count) for row in manifest] == [(alpha.id, 2), (beta.id, 1)]
    progress = {row.name: row for row in read_run_progress(session, "run-1").stages}
    stage = progress[StageName.COMPANY_DEDUPLICATION.value]
    assert (stage.counts.input, stage.counts.advanced, stage.counts.dropped) == (2, 2, 0)
    assert (progress[StageName.COMPANY_PHASE_B.value].status,
            progress[StageName.COMPANY_PHASE_B.value].counts.pending,
            progress[StageName.COMPANY_PHASE_B.value].attempt_number) == (
        "pending", 2, 0,
    )

    report_company_phase_a(session, "run-1", [CompanyEnrichmentResult(beta.id, CompanyProcessingOutcome.ENRICHED)])
    report_company_phase_b(session, "run-1", [
        CompanyEnrichmentResult(alpha.id, CompanyProcessingOutcome.ENRICHED, evidence_outcome=PhaseBEvidenceOutcome.CONFIRMED_MISSING, source="glassdoor"),
        CompanyEnrichmentResult(alpha.id, CompanyProcessingOutcome.ENRICHED, evidence_outcome=PhaseBEvidenceOutcome.CONFIRMED_MISSING, source="ambitionbox"),
        CompanyEnrichmentResult(beta.id, CompanyProcessingOutcome.FAILED, "brightdata_timeout", PhaseBEvidenceOutcome.SOURCE_ERROR, source="glassdoor", evidence_url="https://evidence.test/beta", disposition=CompanyFailureDisposition.EXCLUDED),
        CompanyEnrichmentResult(beta.id, CompanyProcessingOutcome.ENRICHED, evidence_outcome=PhaseBEvidenceOutcome.CONFIRMED_MISSING, source="ambitionbox"),
    ])
    phase_b = next(row for row in read_run_progress(session, "run-1").stages
                   if row.name == StageName.COMPANY_PHASE_B.value)
    assert (phase_b.status, phase_b.counts.advanced, phase_b.counts.failed, phase_b.counts.pending) == (
        "completed_with_errors", 1, 1, 0)
    assert company_failure_dispositions(session, "run-1", CompanyPhase.B) == {
        beta.id: CompanyFailureDisposition.EXCLUDED,
    }
    assert [(item.record_id, item.disposition.value)
            for item in phase_b.failed_record_dispositions] == [
        (str(beta.id), "excluded"),
    ]

    before_ranking = decision_run_company_funnel(session, "run-1").set_index("stage").loc["company_terminal_outcomes"]
    assert before_ranking["pending"] == 2
    assert not before_ranking["available"]

    session.add(DecisionRunCompany(run_id="run-1", company_id=alpha.id, group_number=1, rank=1,
                                   company_type="employer"))
    session.flush()
    record_company_terminal_outcomes(session, "run-1")
    funnel = decision_run_company_funnel(session, "run-1").set_index("stage")
    assert funnel.loc[StageName.COMPANY_DEDUPLICATION.value, "duplicates"] == 1
    assert funnel.loc[StageName.COMPANY_PHASE_B.value, "confirmed_missing"] == 1
    assert funnel.loc[StageName.COMPANY_PHASE_B.value, "source_error"] == 1
    terminal = funnel.loc["company_terminal_outcomes"]
    assert (terminal["group_1"], terminal["ungroupable"], terminal["companies"], terminal["eligible_jobs"]) == (1, 1, 2, 3)
    assert terminal["advanced"] + terminal["pending"] == terminal["companies"]

    missing = decision_run_company_funnel_drilldown(session, "run-1", stage_name=StageName.COMPANY_PHASE_B.value)
    assert set(missing["phase_b_evidence_outcome"]) == {"confirmed_missing", "source_error"}
    assert set(missing["source"]) == {"board-a", "board-b"}
    assert any(item["url"] == "https://evidence.test/beta" for item in missing.iloc[-1]["company_evidence"])
    ungroupable = decision_run_company_funnel_drilldown(session, "run-1", stage_name="company_terminal_outcomes", reason="brightdata_timeout")
    assert ungroupable[["company", "outcome", "reason"]].to_dict("records") == [{
        "company": "Beta", "outcome": "ungroupable", "reason": "brightdata_timeout",
    }]


def test_phase_b_terminal_missing_is_processed_not_failed_and_reused_phase_a_is_enriched():
    session = _session()
    company = Company(name="Reuse", enrichment_status="done", industry="Data", description="desc", pain_points="pain",
                      contact_search_groups=[], contact_profile_enriched_at=NOW, domain_resolution_status="unresolvable", enriched_at=NOW,
                      market_profile_status="done", market_profile_evidence={
        "sources": {"glassdoor": {"status": "missing"}, "ambitionbox": {"status": "missing"}}
    })
    session.add(company); session.flush()
    _run_job(session, company, 1, "eligible")
    session.flush()
    freeze_company_funnel_manifest(session, "run-1")
    report_company_phase_a(session, "run-1", [])
    report_company_phase_b(session, "run-1", [])
    progress = {row.name: row for row in read_run_progress(session, "run-1").stages}
    assert (progress[StageName.COMPANY_PHASE_A.value].counts.advanced, progress[StageName.COMPANY_PHASE_A.value].counts.failed) == (1, 0)
    assert (progress[StageName.COMPANY_PHASE_B.value].counts.advanced, progress[StageName.COMPANY_PHASE_B.value].counts.failed) == (1, 0)
    phase_b = decision_run_company_funnel(session, "run-1").set_index("stage").loc[StageName.COMPANY_PHASE_B.value]
    assert (phase_b["confirmed_missing"], phase_b["source_error"]) == (1, 0)


def test_phase_a_scope_skips_unapproved_companies_and_completes_selected_only():
    session = _session()
    selected, rejected = Company(name="Selected"), Company(name="Rejected")
    session.add_all((selected, rejected)); session.flush()
    _run_job(session, selected, 1, "eligible")
    _run_job(session, rejected, 2, "eligible")
    session.flush()
    freeze_company_funnel_manifest(session, "run-1")
    session.add(DecisionRunSelectedJob(
        run_id="run-1", company_id=selected.id, posting_version_id=1,
        company_order=1, selection_kind="fresh_outreach",
    ))
    session.get(DecisionRun, "run-1").state = "approved"
    session.flush()

    assert scope_company_phase_a_to_approval(session, "run-1") == (selected.id,)
    stage = next(row for row in read_run_progress(session, "run-1").stages
                 if row.name == StageName.COMPANY_PHASE_A.value)
    assert (stage.counts.dropped, stage.counts.pending) == (1, 1)

    report_company_phase_a(session, "run-1", [
        CompanyEnrichmentResult(selected.id, CompanyProcessingOutcome.ENRICHED),
    ])
    stage = next(row for row in read_run_progress(session, "run-1").stages
                 if row.name == StageName.COMPANY_PHASE_A.value)
    assert (stage.status, stage.counts.advanced, stage.counts.dropped) == (
        "completed", 1, 1,
    )


def test_phase_b_resumes_idempotently_per_source_before_final_aggregate():
    session = _session()
    company = Company(name="Resume")
    session.add(company); session.flush()
    _run_job(session, company, 1, "eligible"); session.flush()
    freeze_company_funnel_manifest(session, "run-1")
    report_company_phase_a(session, "run-1", [CompanyEnrichmentResult(company.id, CompanyProcessingOutcome.ENRICHED)])
    report_company_phase_b(session, "run-1", [
        CompanyEnrichmentResult(company.id, CompanyProcessingOutcome.ENRICHED, source="glassdoor", evidence_outcome=PhaseBEvidenceOutcome.FOUND, evidence_url="https://gd.test/1"),
    ])
    stage = next(row for row in read_run_progress(session, "run-1", project_interruptions=False).stages if row.name == StageName.COMPANY_PHASE_B.value)
    assert (stage.status, stage.counts.pending) == ("running", 1)
    # Replaying the same source is an idempotent resume, not a second item.
    report_company_phase_b(session, "run-1", [
        CompanyEnrichmentResult(company.id, CompanyProcessingOutcome.ENRICHED, source="glassdoor", evidence_outcome=PhaseBEvidenceOutcome.FOUND, evidence_url="https://gd.test/1"),
        CompanyEnrichmentResult(company.id, CompanyProcessingOutcome.ENRICHED, source="ambitionbox", evidence_outcome=PhaseBEvidenceOutcome.CONFIRMED_MISSING, evidence_url="https://ab.test/1"),
    ])
    stage = next(row for row in read_run_progress(session, "run-1").stages if row.name == StageName.COMPANY_PHASE_B.value)
    assert (stage.status, stage.counts.advanced, stage.counts.failed) == ("completed", 1, 0)


def test_phase_b_is_durably_running_before_source_workers_are_dispatched():
    session = _session()
    company = Company(name="Dispatched")
    session.add(company)
    session.flush()
    _run_job(session, company, 1, "eligible")
    session.flush()
    freeze_company_funnel_manifest(session, "run-1")

    assert begin_company_phase_b(session, "run-1") == (company.id,)
    stage = next(
        row for row in read_run_progress(session, "run-1", project_interruptions=False).stages
        if row.name == StageName.COMPANY_PHASE_B.value
    )
    assert (stage.status, stage.counts.pending, stage.attempt_number) == (
        "running", 1, 1,
    )
    assert stage.attempts[-1].process_id is None
    assert stage.reason == "Glassdoor and AmbitionBox source lanes dispatched"

    # A resumed coordinator must refresh the existing attempt, not create a
    # misleading second attempt before any source result is reported.
    assert begin_company_phase_b(session, "run-1") == (company.id,)
    resumed = next(
        row for row in read_run_progress(session, "run-1", project_interruptions=False).stages
        if row.name == StageName.COMPANY_PHASE_B.value
    )
    assert (resumed.status, resumed.attempt_number) == ("running", 1)


def test_phase_b_restarts_after_stale_worker_reconciliation():
    session = _session()
    company = Company(name="Restarted")
    session.add(company); session.flush()
    _run_job(session, company, 1, "eligible"); session.flush()
    freeze_company_funnel_manifest(session, "run-1")

    begin_company_phase_b(session, "run-1")
    running = next(
        row for row in read_run_progress(session, "run-1", project_interruptions=False).stages
        if row.name == StageName.COMPANY_PHASE_B.value
    )
    assert reconcile_stale_stages(
        session, "run-1", now=running.heartbeat_at + timedelta(minutes=6),
        process_is_alive=lambda _: False,
    ) == 1

    interrupted = next(
        row for row in read_run_progress(session, "run-1", project_interruptions=False).stages
        if row.name == StageName.COMPANY_PHASE_B.value
    )
    assert (interrupted.status, interrupted.attempt_number) == ("interrupted", 1)

    assert begin_company_phase_b(session, "run-1") == (company.id,)
    resumed = next(
        row for row in read_run_progress(session, "run-1", project_interruptions=False).stages
        if row.name == StageName.COMPANY_PHASE_B.value
    )
    assert (resumed.status, resumed.attempt_number) == ("running", 2)

    report_company_phase_b(session, "run-1", [
        CompanyEnrichmentResult(
            company.id, CompanyProcessingOutcome.ENRICHED,
            source="glassdoor", evidence_outcome=PhaseBEvidenceOutcome.FOUND,
        ),
        CompanyEnrichmentResult(
            company.id, CompanyProcessingOutcome.ENRICHED,
            source="ambitionbox", evidence_outcome=PhaseBEvidenceOutcome.CONFIRMED_MISSING,
        ),
    ])
    completed = next(
        row for row in read_run_progress(session, "run-1").stages
        if row.name == StageName.COMPANY_PHASE_B.value
    )
    assert (completed.status, completed.attempt_number) == ("completed", 2)


def test_phase_b_aggregates_new_evidence_with_production_autoflush_disabled():
    session = _session()
    session.autoflush = False
    company = Company(name="No Autoflush")
    session.add(company)
    session.flush()
    _run_job(session, company, 1, "eligible")
    session.flush()
    freeze_company_funnel_manifest(session, "run-1")
    report_company_phase_a(session, "run-1", [
        CompanyEnrichmentResult(company.id, CompanyProcessingOutcome.ENRICHED),
    ])

    report_company_phase_b(session, "run-1", [
        CompanyEnrichmentResult(
            company.id,
            CompanyProcessingOutcome.ENRICHED,
            source="glassdoor",
            evidence_outcome=PhaseBEvidenceOutcome.FOUND,
        ),
        CompanyEnrichmentResult(
            company.id,
            CompanyProcessingOutcome.ENRICHED,
            source="ambitionbox",
            evidence_outcome=PhaseBEvidenceOutcome.CONFIRMED_MISSING,
        ),
    ])

    stage = next(
        row for row in read_run_progress(session, "run-1").stages
        if row.name == StageName.COMPANY_PHASE_B.value
    )
    assert (stage.status, stage.counts.advanced, stage.counts.pending) == (
        "completed", 1, 0,
    )


def test_current_run_cannot_rank_until_company_phase_b_is_terminal():
    session = _session()
    company = Company(name="Sequenced")
    session.add(company); session.flush()
    job = Job(source="board", external_job_id="1", company_id=company.id, company_name_raw=company.name,
              title="Data Engineer", description_raw="work", is_remote=True, posted_at=NOW)
    session.add(job); session.flush()
    session.add(DecisionRunJob(run_id="run-1", posting_version_id=1, job_id=job.id, company_id=company.id,
                               outcome="eligible", snapshot={"source": "board", "job_url": "https://jobs.test/1"}))
    session.flush(); freeze_company_funnel_manifest(session, "run-1")
    session.get(DecisionRun, "run-1").state = "enriching_companies"
    with pytest.raises(ValueError, match="Phase B"):
        finalize_decision_run(session, "run-1")
    report_company_phase_b(session, "run-1", [
        CompanyEnrichmentResult(company.id, CompanyProcessingOutcome.ENRICHED, source="glassdoor", evidence_outcome=PhaseBEvidenceOutcome.FOUND),
        CompanyEnrichmentResult(company.id, CompanyProcessingOutcome.ENRICHED, source="ambitionbox", evidence_outcome=PhaseBEvidenceOutcome.CONFIRMED_MISSING),
    ])
    finalize_decision_run(session, "run-1")
    assert session.get(DecisionRun, "run-1").state == "awaiting_approval"


def test_phase_b_source_failures_are_order_independent_and_disposition_is_typed():
    session = _session()
    first, second = Company(name="First"), Company(name="Second")
    session.add_all((first, second)); session.flush()
    _run_job(session, first, 1, "eligible"); _run_job(session, second, 2, "eligible"); session.flush()
    freeze_company_funnel_manifest(session, "run-1")
    report_company_phase_a(session, "run-1", [
        CompanyEnrichmentResult(first.id, CompanyProcessingOutcome.ENRICHED),
        CompanyEnrichmentResult(second.id, CompanyProcessingOutcome.ENRICHED),
    ])
    glassdoor = lambda company_id: CompanyEnrichmentResult(
        company_id, CompanyProcessingOutcome.FAILED, "gd_timeout",
        PhaseBEvidenceOutcome.SOURCE_ERROR, source="glassdoor",
        disposition=CompanyFailureDisposition.EXCLUDED,
    )
    ambitionbox = lambda company_id: CompanyEnrichmentResult(
        company_id, CompanyProcessingOutcome.FAILED, "ab_timeout",
        PhaseBEvidenceOutcome.SOURCE_ERROR, source="ambitionbox",
        disposition=CompanyFailureDisposition.NON_BLOCKING,
    )
    report_company_phase_b(session, "run-1", [
        glassdoor(first.id), ambitionbox(first.id),
        ambitionbox(second.id), glassdoor(second.id),
    ])
    assert company_failure_dispositions(session, "run-1", CompanyPhase.B) == {
        first.id: CompanyFailureDisposition.NON_BLOCKING,
        second.id: CompanyFailureDisposition.NON_BLOCKING,
    }
    rows = {row.company_id: row for row in session.query(DecisionRunCompanyFunnelManifest).all()}
    assert rows[first.id].phase_b_reason == rows[second.id].phase_b_reason == "ab_timeout"


def test_undisposed_failure_and_raw_enum_values_are_rejected():
    session = _session(); company = Company(name="Typed"); session.add(company); session.flush()
    _run_job(session, company, 1, "eligible"); session.flush(); freeze_company_funnel_manifest(session, "run-1")
    with pytest.raises(StageTransitionError, match="CompanyProcessingOutcome"):
        report_company_phase_a(session, "run-1", [CompanyEnrichmentResult(company.id, "enriched")])
    report_company_phase_a(session, "run-1", [CompanyEnrichmentResult(company.id, CompanyProcessingOutcome.ENRICHED)])
    with pytest.raises(StageTransitionError, match="requires non_blocking or excluded"):
        report_company_phase_b(session, "run-1", [CompanyEnrichmentResult(
            company.id, CompanyProcessingOutcome.FAILED, "timeout",
            PhaseBEvidenceOutcome.SOURCE_ERROR, source="glassdoor",
        )])
    with pytest.raises(StageTransitionError, match="terminal group"):
        company_terminal_outcome(5)


def test_phase_a_failure_does_not_block_preapproval_phase_b_market_evidence():
    session = _session(); company = Company(name="Undisposed"); session.add(company); session.flush()
    _run_job(session, company, 1, "eligible"); session.flush()
    manifest = freeze_company_funnel_manifest(session, "run-1")[0]
    manifest.phase_a_outcome = CompanyProcessingOutcome.FAILED.value
    manifest.phase_a_reason = "reviewed source failed"

    report_company_phase_a(session, "run-1", [])
    phase_a = next(row for row in read_run_progress(
        session, "run-1", project_interruptions=False,
    ).stages if row.name == StageName.COMPANY_PHASE_A.value)
    assert phase_a.status == "completed_with_errors"
    assert phase_a.failed_record_dispositions == ()
    report_company_phase_b(session, "run-1", [])
    phase_b = next(row for row in read_run_progress(
        session, "run-1", project_interruptions=False,
    ).stages if row.name == StageName.COMPANY_PHASE_B.value)
    assert phase_b.status == "running"
