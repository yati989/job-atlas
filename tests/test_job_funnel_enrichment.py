from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.dashboard.queries import decision_run_job_funnel, decision_run_job_funnel_drilldown
from app.decision_runs.enrichment_funnel import (
    EnrichmentResult,
    FailureDisposition,
    freeze_job_enrichment_manifest,
    report_job_enrichment,
)
from app.decision_runs.ingestion_funnel import CanonicalJobReference, report_canonical_jobs
from app.decision_runs.progress import StageName, StageTransitionError, read_run_progress
from app.decision_runs.service import finalize_decision_run, prepare_decision_run
from app.decision_runs.company_funnel import (
    CompanyEnrichmentResult, CompanyProcessingOutcome, PhaseBEvidenceOutcome,
    report_company_phase_a, report_company_phase_b,
)
from app.models.orm import (
    Base,
    Company,
    DecisionRun,
    DecisionRunIngestionObservation,
    Job,
    JobPostingVersion,
)
from tests.progress_support import complete_stage_chain


NOW = datetime(2026, 8, 25, tzinfo=timezone.utc)


def _session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, future=True)()
    session.add(DecisionRun(
        id="run-1", since_at=NOW - timedelta(days=1), cutoff_at=NOW,
        input_timezone="UTC", state="preparing", policy_snapshot={}, target_count=1,
        telemetry_version="decision_funnels_v1",
    ))
    session.flush()
    complete_stage_chain(session, "run-1", StageName.RELEVANCE_STORAGE)
    return session


def _job(
    session,
    external_id,
    *,
    status="pending",
    description="work",
    source="board-a",
    posted_at=NOW,
):
    company = Company(name=f"Acme {external_id}")
    session.add(company)
    session.flush()
    job = Job(
        source=source, external_job_id=external_id, company_id=company.id,
        company_name_raw=company.name, title="Data Engineer", description_raw=description,
        job_url=f"https://jobs.test/{external_id}", enrichment_status=status,
        posted_at=posted_at,
    )
    session.add(job)
    session.flush()
    version = JobPostingVersion(
        job_id=job.id, canonical_duplicate_root_id=job.id, posted_at=posted_at,
        material_content_hash=f"hash-{external_id}", posting_instance_key=f"version-{external_id}",
        snapshot={"job_url": job.job_url},
    )
    session.add(version)
    observation = DecisionRunIngestionObservation(
        run_id="run-1", source=source, external_job_id=external_id,
        connector_instance=job.id, connector_dimensions={}, fetch_attempt=1,
        occurrence_ordinal=1, job_url=job.job_url,
    )
    session.add(observation)
    session.flush()
    return job, version, CanonicalJobReference(observation.id)


def test_exact_manifest_combines_reused_and_new_enrichment_without_backlog():
    session = _session()
    reused, reused_version, reused_ref = _job(session, "reused", status="done")
    new, new_version, new_ref = _job(session, "new", status="pending")
    backlog, _, _ = _job(session, "backlog", status="pending")
    # Backlog was never observed by this Decision Run.
    session.query(DecisionRunIngestionObservation).filter_by(external_job_id="backlog").delete()
    report_canonical_jobs(session, "run-1", [reused_ref, new_ref])

    manifest = freeze_job_enrichment_manifest(session, "run-1")
    assert manifest.posting_version_ids == (reused_version.id, new_version.id)
    assert [row.job_id for row in manifest.jobs] == [reused.id, new.id]

    report_job_enrichment(session, "run-1", [
        EnrichmentResult(new_version.id, "enriched"),
    ])
    stage = next(item for item in read_run_progress(session, "run-1").stages
                 if item.name == StageName.JOB_ENRICHMENT.value)
    assert (stage.status, stage.counts.advanced, stage.counts.failed, stage.counts.pending) == (
        "completed", 2, 0, 0,
    )
    assert backlog.enrichment_status == "pending"


@pytest.mark.parametrize(
    ("source", "expected_outcome"),
    (("instahyre", "eligible"), ("linkedin", "anomaly")),
)
def test_current_manifest_missing_date_source_policy(source, expected_outcome):
    session = _session()
    _, _, reference = _job(
        session,
        "undated",
        status="done",
        source=source,
        posted_at=None,
    )
    report_canonical_jobs(session, "run-1", [reference])
    freeze_job_enrichment_manifest(session, "run-1")
    report_job_enrichment(session, "run-1", [])

    assert prepare_decision_run(session, "run-1")["outcomes"] == {
        expected_outcome: 1,
    }


def test_terminal_enrichment_report_publishes_records_once(monkeypatch):
    from app.decision_runs import enrichment_funnel

    session = _session()
    _, reused_version, reused_ref = _job(session, "reused", status="done")
    _, new_version, new_ref = _job(session, "new", status="pending")
    report_canonical_jobs(session, "run-1", [reused_ref, new_ref])
    freeze_job_enrichment_manifest(session, "run-1")
    calls = []
    original_update = enrichment_funnel.update_stage
    original_finish = enrichment_funnel.finish_stage
    monkeypatch.setattr(
        enrichment_funnel, "update_stage",
        lambda *args, **kwargs: calls.append("update") or original_update(*args, **kwargs),
    )
    monkeypatch.setattr(
        enrichment_funnel, "finish_stage",
        lambda *args, **kwargs: calls.append("finish") or original_finish(*args, **kwargs),
    )

    report_job_enrichment(session, "run-1", [
        EnrichmentResult(new_version.id, "enriched"),
    ])
    session.commit()

    assert calls == ["finish"]
    stage = next(item for item in read_run_progress(session, "run-1").stages
                 if item.name == StageName.JOB_ENRICHMENT.value)
    assert [record.record_id for record in stage.attempts[-1].records] == [
        str(reused_version.id), str(new_version.id),
    ]


def test_canonical_scope_freezes_exact_version_before_later_job_mutation():
    session = _session()
    job, original, ref = _job(session, "versioned", status="pending")
    report_canonical_jobs(session, "run-1", [ref])
    later = JobPostingVersion(
        job_id=job.id, canonical_duplicate_root_id=job.id, posted_at=NOW,
        material_content_hash="later", posting_instance_key="versioned-later",
        snapshot={"job_url": "https://jobs.test/versioned/later"},
    )
    session.add(later)
    session.flush()

    manifest = freeze_job_enrichment_manifest(session, "run-1")
    assert manifest.posting_version_ids == (original.id,)
    assert later.id not in manifest.posting_version_ids


def test_missing_description_advances_as_processed_and_failure_is_separate_and_drillable():
    session = _session()
    missing, missing_version, missing_ref = _job(session, "missing", status="no_description", description=None)
    broken, broken_version, broken_ref = _job(session, "broken", status="pending")
    report_canonical_jobs(session, "run-1", [missing_ref, broken_ref])

    freeze_job_enrichment_manifest(session, "run-1")
    report_job_enrichment(session, "run-1", [
        EnrichmentResult(broken_version.id, "failed", "extractor_unavailable"),
    ])
    stage = next(item for item in read_run_progress(session, "run-1").stages
                 if item.name == StageName.JOB_ENRICHMENT.value)
    assert stage.status == "completed_with_errors"
    assert (stage.counts.advanced, stage.counts.failed, stage.counts.pending) == (1, 1, 0)
    detail = decision_run_job_funnel_drilldown(
        session, "run-1", stage_name=StageName.JOB_ENRICHMENT.value,
        reason="extractor_unavailable",
    )
    assert detail.iloc[0]["job_url"] == broken.job_url
    assert detail.iloc[0]["posting_version_id"] == broken_version.id


def test_screening_terminal_outcomes_are_exclusive_and_reconcile_from_enriched():
    session = _session()
    eligible, eligible_version, eligible_ref = _job(session, "eligible", status="done")
    anomaly, anomaly_version, anomaly_ref = _job(session, "anomaly", status="no_description", description=None)
    rejected, rejected_version, rejected_ref = _job(session, "rejected", status="done")
    report_canonical_jobs(session, "run-1", [eligible_ref, anomaly_ref, rejected_ref])
    freeze_job_enrichment_manifest(session, "run-1")
    report_job_enrichment(session, "run-1", [])

    from app.jobs.screening_facts import record_screening_facts
    record_screening_facts(
        session, posting_version_id=rejected_version.id,
        salary={"guaranteed_max_lpa": 20}, minimum_experience=None,
        maximum_experience=None,
    )
    summary = prepare_decision_run(session, "run-1")
    assert summary["outcomes"] == {"eligible": 1, "anomaly": 1, "hard_rejected": 1}

    flow = decision_run_job_funnel(session, "run-1")
    terminal = flow[flow["stage"] == StageName.SCREENING_RANKING_GROUPING.value].iloc[0]
    assert (terminal["eligible"], terminal["anomalies"], terminal["rejected"]) == (1, 1, 1)
    assert terminal["available"]
    assert terminal["pending"] == 3
    assert terminal["outcomes_available"]
    screening = next(item for item in read_run_progress(session, "run-1").stages
                     if item.name == StageName.SCREENING_RANKING_GROUPING.value)
    assert (screening.status, screening.attempt_number) == ("pending", 0)
    enriched = flow[flow["stage"] == StageName.JOB_ENRICHMENT.value].iloc[0]
    assert terminal["input"] == terminal["eligible"] + terminal["anomalies"] + terminal["rejected"]
    findings = decision_run_job_funnel_drilldown(
        session, "run-1", stage_name=StageName.SCREENING_RANKING_GROUPING.value,
        reason="salary_below_25_lpa",
    )
    assert findings.iloc[0]["posting_version_id"] == rejected_version.id
    assert findings.iloc[0]["job_url"] == rejected.job_url
    report_company_phase_a(session, "run-1", [
        CompanyEnrichmentResult(eligible.company_id, CompanyProcessingOutcome.ENRICHED),
    ])
    report_company_phase_b(session, "run-1", [
        CompanyEnrichmentResult(eligible.company_id, CompanyProcessingOutcome.ENRICHED,
                                evidence_outcome=PhaseBEvidenceOutcome.CONFIRMED_MISSING, source="glassdoor"),
        CompanyEnrichmentResult(eligible.company_id, CompanyProcessingOutcome.ENRICHED,
                                evidence_outcome=PhaseBEvidenceOutcome.CONFIRMED_MISSING, source="ambitionbox"),
    ])
    finalize_decision_run(session, "run-1")
    completed = decision_run_job_funnel(session, "run-1")
    completed = completed[completed["stage"] == StageName.SCREENING_RANKING_GROUPING.value].iloc[0]
    assert completed["available"]
    assert completed["input"] == enriched["advanced"]


def test_screening_requires_finished_enrichment_without_non_blocking_failures():
    session = _session()
    _, version, ref = _job(session, "failed", status="pending")
    report_canonical_jobs(session, "run-1", [ref])
    freeze_job_enrichment_manifest(session, "run-1")
    report_job_enrichment(session, "run-1", [EnrichmentResult(version.id, "failed", "timeout")])
    stage = next(item for item in read_run_progress(
        session, "run-1", project_interruptions=False,
    ).stages if item.name == StageName.JOB_ENRICHMENT.value)
    assert stage.status == "completed_with_errors"
    assert stage.failed_record_dispositions == ()
    with pytest.raises(StageTransitionError, match="non-blocking"):
        prepare_decision_run(session, "run-1")


def test_explicitly_non_blocking_failure_is_excluded_from_screening_input():
    session = _session()
    good, good_version, good_ref = _job(session, "good", status="done")
    _, failed_version, failed_ref = _job(session, "failed", status="pending")
    _, excluded_version, excluded_ref = _job(session, "excluded", status="pending")
    report_canonical_jobs(session, "run-1", [good_ref, failed_ref, excluded_ref])
    freeze_job_enrichment_manifest(session, "run-1")
    report_job_enrichment(session, "run-1", [
        EnrichmentResult(
            failed_version.id, "failed", "source_payload_lost",
            disposition=FailureDisposition.NON_BLOCKING,
        ),
        EnrichmentResult(
            excluded_version.id, "failed", "description_unusable",
            disposition=FailureDisposition.EXCLUDED,
        ),
    ])
    stage = next(item for item in read_run_progress(
        session, "run-1", project_interruptions=False,
    ).stages if item.name == StageName.JOB_ENRICHMENT.value)
    assert {
        (item.record_id, item.disposition) for item in stage.failed_record_dispositions
    } == {
        (str(failed_version.id), FailureDisposition.NON_BLOCKING),
        (str(excluded_version.id), FailureDisposition.EXCLUDED),
    }

    prepare_decision_run(session, "run-1")
    flow = decision_run_job_funnel(session, "run-1").set_index("stage")
    assert flow.loc[StageName.JOB_ENRICHMENT.value, "failed"] == 2
    assert flow.loc[StageName.SCREENING_RANKING_GROUPING.value, "input"] == 1
    assert flow.loc[StageName.SCREENING_RANKING_GROUPING.value, "eligible"] == 1
    detail = decision_run_job_funnel_drilldown(
        session, "run-1", stage_name=StageName.JOB_ENRICHMENT.value,
    ).set_index("posting_version_id")
    assert detail.loc[failed_version.id, "failure_disposition"] == "non_blocking"
    assert detail.loc[excluded_version.id, "failure_disposition"] == "excluded"


def test_incremental_batches_preserve_prior_failure_reason_and_processed_items():
    session = _session()
    first, first_version, first_ref = _job(session, "first", status="pending")
    second, second_version, second_ref = _job(session, "second", status="pending")
    third, third_version, third_ref = _job(session, "third", status="pending")
    report_canonical_jobs(session, "run-1", [first_ref, second_ref, third_ref])
    freeze_job_enrichment_manifest(session, "run-1")

    report_job_enrichment(session, "run-1", [
        EnrichmentResult(first_version.id, "failed", "timeout"),
        EnrichmentResult(second_version.id, "enriched"),
    ])
    report_job_enrichment(session, "run-1", [
        EnrichmentResult(
            third_version.id, "failed", "bad_payload",
            disposition=FailureDisposition.EXCLUDED,
        ),
    ])
    stage = next(item for item in read_run_progress(session, "run-1", project_interruptions=False).stages
                 if item.name == StageName.JOB_ENRICHMENT.value)
    assert (stage.counts.advanced, stage.counts.failed, stage.counts.pending) == (1, 2, 0)
    failed = {record.record_id: record.reason for record in stage.attempts[-1].records
              if record.outcome == "failed"}
    assert failed == {str(first_version.id): "timeout", str(third_version.id): "bad_payload"}


def test_pending_enrichment_is_drillable_without_becoming_a_terminal_stage_record():
    session = _session()
    _, completed_version, completed_ref = _job(session, "completed", status="pending")
    pending, pending_version, pending_ref = _job(session, "pending", status="pending")
    report_canonical_jobs(session, "run-1", [completed_ref, pending_ref])
    freeze_job_enrichment_manifest(session, "run-1")
    report_job_enrichment(session, "run-1", [
        EnrichmentResult(completed_version.id, "enriched"),
    ])

    stage = next(item for item in read_run_progress(
        session, "run-1", project_interruptions=False,
    ).stages if item.name == StageName.JOB_ENRICHMENT.value)
    assert stage.counts.pending == 1
    assert all(record.record_id != str(pending_version.id)
               for record in stage.attempts[-1].records)

    pending_rows = decision_run_job_funnel_drilldown(
        session, "run-1", stage_name=StageName.JOB_ENRICHMENT.value,
    )
    pending_rows = pending_rows[pending_rows["outcome"] == "pending"]
    assert pending_rows[["posting_version_id", "outcome", "job_url"]].to_dict("records") == [{
        "posting_version_id": pending_version.id,
        "outcome": "pending",
        "job_url": pending.job_url,
    }]


def test_current_run_without_enrichment_manifest_fails_closed_but_legacy_falls_back():
    session = _session()
    _job(session, "current", status="done")
    with pytest.raises(StageTransitionError, match="manifest"):
        prepare_decision_run(session, "run-1")

    session.query(DecisionRun).filter_by(id="run-1").update({"telemetry_version": None})
    assert prepare_decision_run(session, "run-1")["outcomes"] == {"eligible": 1}


def test_screening_drilldown_preserves_every_finding_for_a_posting_version():
    session = _session()
    _, version, ref = _job(session, "many-findings", status="done")
    report_canonical_jobs(session, "run-1", [ref])
    freeze_job_enrichment_manifest(session, "run-1")
    report_job_enrichment(session, "run-1", [])
    from app.jobs.screening_facts import record_screening_facts
    record_screening_facts(
        session, posting_version_id=version.id,
        salary={"guaranteed_max_lpa": 20}, minimum_experience=None,
        maximum_experience={"years": 2, "mandatory": True},
    )
    prepare_decision_run(session, "run-1")
    rows = decision_run_job_funnel_drilldown(
        session, "run-1", stage_name=StageName.SCREENING_RANKING_GROUPING.value,
    )
    assert set(rows["reason"]) == {"salary_below_25_lpa", "maximum_experience_2_or_less"}
    assert set(rows["posting_version_id"]) == {version.id}
    assert set(rows["job_url"]) == {"https://jobs.test/many-findings"}
