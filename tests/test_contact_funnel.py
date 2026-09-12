from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.decision_runs.contact_funnel import (ContactEnrichmentResult, ContactOutcome,
    freeze_contact_enrichment_manifest, report_contact_enrichment)
from app.decision_runs.progress import (FailureDisposition, StageCounts, StageName,
    finish_stage, interrupt_stage, start_stage, read_run_progress)
from app.contacts.agentic_batch import CompanyContext, JudgedContact, contexts_for_approved_companies, record_company
from app.contacts import ladder as L
from app.decision_runs.types import ApprovedScope
from app.models.orm import (Base, Company, Contact, DecisionRun, DecisionRunApproval,
                            DecisionRunSelectedJob, Job, JobPostingVersion,
                            OutreachDeliveryAttempt, OutreachMessage)


def session():
    engine = create_engine("sqlite://"); Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def ready_run(s, *, selection="data_ai", company_type="employer", second_title=None):
    now = datetime.now(timezone.utc)
    company = Company(name="Acme", company_type=company_type)
    job = Job(source="test", external_job_id="1", company=company, title="Data Scientist")
    s.add_all([company, job]); s.flush()
    second = None
    if second_title:
        second = Job(source="test", external_job_id="2", company_id=company.id, title=second_title)
        s.add(second); s.flush()
    version = JobPostingVersion(job_id=job.id, canonical_duplicate_root_id=job.id,
                                material_content_hash="v1", posting_instance_key="test:1:v1",
                                snapshot={}, first_seen_at=now, last_seen_at=now)
    run = DecisionRun(id="run", since_at=now-timedelta(days=1), cutoff_at=now, input_timezone="UTC", state="approved", policy_snapshot={}, target_count=1, telemetry_version="decision-run-v1")
    s.add_all([version, run]); s.flush()
    s.add(DecisionRunSelectedJob(run_id=run.id, company_id=company.id, posting_version_id=version.id, company_order=1, selection_kind="primary"))
    if second is not None:
        version2 = JobPostingVersion(job_id=second.id, canonical_duplicate_root_id=second.id,
            material_content_hash="v2", posting_instance_key="test:2:v1", snapshot={}, first_seen_at=now, last_seen_at=now)
        s.add(version2); s.flush()
        s.add(DecisionRunSelectedJob(run_id=run.id, company_id=company.id, posting_version_id=version2.id, company_order=1, selection_kind="additional"))
    s.add(DecisionRunApproval(run_id=run.id, original_text="ok", normalized_rules={"contact_search_selections": {str(company.id): selection}}, target_count=1))
    # Material stages use a zero-count fixture, but retain the real graph.
    for name in (StageName.FETCH_JOBS, StageName.COLLECTION_DEDUPLICATION,
                 StageName.RELEVANCE_STORAGE, StageName.JOB_DEDUPLICATION,
                 StageName.COMPANY_DEDUPLICATION, StageName.JOB_ENRICHMENT,
                 StageName.COMPANY_PHASE_A, StageName.COMPANY_PHASE_B,
                 StageName.SCREENING_RANKING_GROUPING):
        start_stage(s, run.id, name, expected_count=0, reason="fixture")
        finish_stage(s, run.id, name, counts=StageCounts(0, 0, 0, 0, 0), reason="fixture")
    start_stage(s, run.id, StageName.APPROVAL_GATE, expected_count=1, reason="approved")
    finish_stage(s, run.id, StageName.APPROVAL_GATE, counts=StageCounts(1, 1, 0, 0, 0), reason="approved")
    s.flush(); return run, company


def test_freeze_reconciles_no_selection_as_explicit_excluded():
    s = session(); run, company = ready_run(s, selection="none")
    rows = freeze_contact_enrichment_manifest(s, run.id)
    assert [(row.company_id, row.search_group, row.outcome) for row in rows] == [(company.id, "none", "excluded")]
    stage = next(x for x in read_run_progress(s, run.id).stages if x.name == "contact_enrichment")
    assert (stage.counts.input, stage.counts.dropped, stage.counts.pending) == (1, 1, 0)


def test_reused_and_new_coverage_is_one_enriched_function():
    s = session(); run, company = ready_run(s)
    old = Contact(company_id=company.id, full_name="Old", linkedin_url="https://x/old", email_guess="old@example.com")
    new = Contact(company_id=company.id, full_name="New", linkedin_url="https://x/new", email_guess="new@example.com")
    s.add_all([old, new]); s.flush(); rows = freeze_contact_enrichment_manifest(s, run.id)
    report_contact_enrichment(s, run.id, [ContactEnrichmentResult(company.id, "data_ai", ContactOutcome.ENRICHED, reused_contact_ids=(old.id,), new_contact_ids=(new.id,), coverage={"tiers": "complete", "reused_contact_evidence": [{"contact_id": old.id, "search_group": "data_ai", "evidence": "Senior Data Scientist"}], "new_contact_evidence": [{"contact_id": new.id, "search_group": "data_ai", "evidence": "Data Science Manager"}]})])
    assert rows[0].outcome == "enriched"
    stage = next(x for x in read_run_progress(s, run.id).stages if x.name == "contact_enrichment")
    assert (stage.counts.advanced, stage.counts.failed, stage.counts.pending) == (1, 0, 0)


@pytest.mark.parametrize("outcome", [ContactOutcome.EXHAUSTED, ContactOutcome.NO_MATCH])
def test_terminal_missing_search_outcomes_are_processed_not_failed(outcome):
    s = session(); run, company = ready_run(s); freeze_contact_enrichment_manifest(s, run.id)
    report_contact_enrichment(s, run.id, [ContactEnrichmentResult(company.id, "data_ai", outcome, reason="coverage_exhausted")])
    stage = next(x for x in read_run_progress(s, run.id).stages if x.name == "contact_enrichment")
    assert (stage.counts.advanced, stage.counts.failed) == (1, 0)


def test_source_failure_needs_explicit_disposition_and_unrelated_backlog_is_ignored():
    s = session(); run, company = ready_run(s); other = Company(name="Backlog")
    s.add(other); s.flush(); freeze_contact_enrichment_manifest(s, run.id)
    with pytest.raises(ValueError):
        report_contact_enrichment(s, run.id, [ContactEnrichmentResult(company.id, "data_ai", ContactOutcome.FAILED, reason="source down")])
    report_contact_enrichment(s, run.id, [ContactEnrichmentResult(company.id, "data_ai", ContactOutcome.FAILED, reason="source down", disposition=FailureDisposition.NON_BLOCKING)])
    stage = next(x for x in read_run_progress(s, run.id).stages if x.name == "contact_enrichment")
    assert (stage.counts.input, stage.counts.failed) == (1, 1)


def test_record_company_reports_only_the_completed_function_with_exact_new_evidence():
    s = session(); run, company = ready_run(s, selection="both", second_title="Credit Risk Analyst")
    rows = freeze_contact_enrichment_manifest(s, run.id)
    context = CompanyContext(company.id, company.name, "employer", "acme.com", None, None,
        [L.DATA_AI, L.CREDIT_RISK], {g: L.ladder_for("employer", g) for g in (L.DATA_AI, L.CREDIT_RISK)}, L.quotas_for("employer"))
    judged = JudgedContact("A Data Lead", "Head of Data", "https://x/data", L.HEAD,
        L.DATA_AI, "owns data", "current Head of Data at Acme", "query")
    record_company(s, context, [judged], run_id=run.id, function_results=[
        ContactEnrichmentResult(company.id, L.DATA_AI, ContactOutcome.EXHAUSTED,
                                reason="function_search_exhausted", coverage={"queries": ["query"]}),
    ])
    by_group = {row.search_group: row for row in rows}
    assert by_group[L.DATA_AI].outcome == "exhausted"
    assert len(by_group[L.DATA_AI].new_contact_ids) == 1
    assert by_group[L.DATA_AI].coverage["new_contact_evidence"][0]["judged_text"] == "current Head of Data at Acme"
    assert by_group[L.CREDIT_RISK].outcome is None
    assert by_group[L.CREDIT_RISK].new_contact_ids == []


def test_staffing_explicit_no_search_stays_excluded_and_has_no_context():
    s = session(); run, company = ready_run(s, selection="none", company_type="staffing")
    rows = freeze_contact_enrichment_manifest(s, run.id)
    assert [(row.search_group, row.outcome, row.reason) for row in rows] == [("none", "excluded", "no_search_selection")]
    scope = ApprovedScope(run.id, (company.id,), (), ())
    assert contexts_for_approved_companies(s, scope) == []


def test_derived_empty_function_is_terminal_no_match_not_approver_exclusion():
    s = session(); run, company = ready_run(s, selection="auto")
    s.query(Job).filter_by(company_id=company.id).update({Job.title: "Software Engineer"})
    rows = freeze_contact_enrichment_manifest(s, run.id)
    assert [(row.search_group, row.outcome, row.reason) for row in rows] == [("no_category_match", "no_match", "no_category_match")]


def test_public_freeze_resumes_an_interrupted_contact_attempt_without_losing_counts():
    s = session(); run, _company = ready_run(s); freeze_contact_enrichment_manifest(s, run.id)
    interrupt_stage(s, run.id, StageName.CONTACT_ENRICHMENT, reason="agent session stopped")
    freeze_contact_enrichment_manifest(s, run.id)
    stage = next(x for x in read_run_progress(s, run.id, project_interruptions=False).stages if x.name == "contact_enrichment")
    assert stage.status == "running"
    assert stage.attempt_number == 2
    assert stage.counts.input == 1 and stage.counts.pending == 1


def test_reuse_requires_function_evidence_and_does_not_leak_to_other_function():
    s = session(); run, company = ready_run(s, selection="both", second_title="Credit Risk Analyst")
    contact = Contact(company_id=company.id, full_name="Data Lead", title="Head of Data", linkedin_url="https://x/reuse", email_guess="lead@example.com")
    s.add(contact); s.flush(); rows = freeze_contact_enrichment_manifest(s, run.id)
    assert all(row.reused_contact_ids == [] for row in rows)
    with pytest.raises(ValueError, match="function evidence"):
        report_contact_enrichment(s, run.id, [ContactEnrichmentResult(company.id, L.DATA_AI,
            ContactOutcome.ENRICHED, reused_contact_ids=(contact.id,), coverage={})])
    context = CompanyContext(company.id, company.name, "employer", "acme.com", None, None,
        [L.DATA_AI, L.CREDIT_RISK], {g: L.ladder_for("employer", g) for g in (L.DATA_AI, L.CREDIT_RISK)}, L.quotas_for("employer"))
    record_company(s, context, [], run_id=run.id, function_results=[ContactEnrichmentResult(
        company.id, L.DATA_AI, ContactOutcome.ENRICHED, reused_contact_ids=(contact.id,),
        coverage={"reused_contact_evidence": [{"contact_id": contact.id, "search_group": L.DATA_AI, "evidence": "Head of Data"}]})])
    by_group = {row.search_group: row for row in rows}
    assert by_group[L.DATA_AI].reused_contact_ids == [contact.id]
    assert by_group[L.CREDIT_RISK].reused_contact_ids == [] and by_group[L.CREDIT_RISK].outcome is None


@pytest.mark.parametrize("ineligible_kind", ["invalid_email", "prior_outreach"])
def test_reuse_must_also_pass_existing_reusable_contact_policy(ineligible_kind):
    s = session(); run, company = ready_run(s)
    contact = Contact(company_id=company.id, full_name="Old Lead", title="Head of Data",
        linkedin_url=f"https://x/{ineligible_kind}", email_guess="lead@example.com",
        email_verification_status="invalid" if ineligible_kind == "invalid_email" else "verified")
    s.add(contact); s.flush()
    if ineligible_kind == "prior_outreach":
        message = OutreachMessage(run_id=run.id, company_id=company.id, contact_id=contact.id,
            message_kind="initial", subject="Hello", body="Body")
        s.add(message); s.flush()
        s.add(OutreachDeliveryAttempt(message_id=message.id, sequence_number=1,
            to_email=contact.email_guess, domain="acme.com", state="presumed_delivered"))
        s.flush()
    freeze_contact_enrichment_manifest(s, run.id)
    with pytest.raises(ValueError, match="reuse policy"):
        report_contact_enrichment(s, run.id, [ContactEnrichmentResult(
            company.id, L.DATA_AI, ContactOutcome.ENRICHED,
            reused_contact_ids=(contact.id,), coverage={"reused_contact_evidence": [{
                "contact_id": contact.id, "search_group": L.DATA_AI, "evidence": "Head of Data",
            }]},
        )])


def test_record_company_rejects_cross_company_function_result_before_persisting():
    s = session(); run, company = ready_run(s)
    other = Company(name="Other"); s.add(other); s.flush()
    freeze_contact_enrichment_manifest(s, run.id)
    context = CompanyContext(company.id, company.name, "employer", "acme.com", None, None,
        [L.DATA_AI], {L.DATA_AI: L.ladder_for("employer", L.DATA_AI)}, L.quotas_for("employer"))
    judged = JudgedContact("Wrong Write", "Head of Data", "https://x/wrong", L.HEAD,
        L.DATA_AI, "owns data", "current", "query")
    with pytest.raises(ValueError, match="same company"):
        record_company(s, context, [judged], run_id=run.id, function_results=[
            ContactEnrichmentResult(other.id, L.DATA_AI, ContactOutcome.ENRICHED),
        ])
    assert s.query(Contact).filter_by(linkedin_url="https://x/wrong").count() == 0


def test_record_company_rejects_unreported_judged_function_atomically():
    s = session(); run, company = ready_run(s, selection="both", second_title="Credit Risk Analyst")
    freeze_contact_enrichment_manifest(s, run.id)
    context = CompanyContext(company.id, company.name, "employer", "acme.com", None, None,
        [L.DATA_AI, L.CREDIT_RISK], {g: L.ladder_for("employer", g) for g in (L.DATA_AI, L.CREDIT_RISK)}, L.quotas_for("employer"))
    judged = JudgedContact("Risk Lead", "Head of Credit Risk", "https://x/risk-unreported",
        L.HEAD, L.CREDIT_RISK, "owns risk", "current Head of Credit Risk", "risk query")
    with pytest.raises(ValueError, match="judged contact function"):
        record_company(s, context, [judged], run_id=run.id, function_results=[
            ContactEnrichmentResult(company.id, L.DATA_AI, ContactOutcome.EXHAUSTED,
                                    reason="data search exhausted"),
        ])
    assert s.query(Contact).filter_by(linkedin_url="https://x/risk-unreported").count() == 0


def test_manifest_sentinels_cannot_enter_real_function_result_api():
    s = session(); run, company = ready_run(s, selection="none")
    freeze_contact_enrichment_manifest(s, run.id)
    with pytest.raises(ValueError, match="real frozen function"):
        report_contact_enrichment(s, run.id, [ContactEnrichmentResult(
            company.id, "none", ContactOutcome.ENRICHED,
        )])
