from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.decision_runs import (approve_decision_run, create_decision_run,
    prepare_decision_run, phase_b_never_attempted_company_ids)
from app.decision_runs.ranking import group_for, qualified_wlb
from app.decision_runs.screening import evaluate_screening
from app.models.orm import (
    Base,
    Company,
)
from app.models.schemas import NormalizedJob
from app.pipeline.upsert import upsert_job
from app.outreach.state import (InMemoryMailbox, push_attempt, reconcile_outreach,
    prepare_company_outreach, record_initial_message)
from app.models.orm import OutreachMessage, OutreachDeliveryAttempt
from app.models.orm import Contact, RunCompanyCalibration, DomainEmailPattern, CompanyDomainAlias
from app.models.orm import JobApplication, JobPostingVersion, JobScreeningFact, JobSkill
from app.reporting.decision_report import build_decision_workbook
from app.reporting.final_run_report import build_final_workbook
from app.jobs.screening_facts import record_screening_facts

def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def legacy_run(session, **kwargs):
    """Exercise the pre-telemetry decision path intentionally."""
    run = create_decision_run(session, **kwargs)
    run.telemetry_version = None
    return run


def test_reconcile_later_release_failure_preserves_earlier_gmail_draft_without_duplicate():
    class FailsSecondMailbox(InMemoryMailbox):
        calls = 0
        def create_or_update_draft(self, **kwargs):
            self.calls += 1
            if self.calls == 2:
                raise OSError("second provider call failed")
            return super().create_or_update_draft(**kwargs)
    s = session(); company = Company(name="Acme"); s.add(company); s.flush()
    first_contact = Contact(company_id=company.id, full_name="Jane Doe", linkedin_url="https://x/jane", email_guess="jane@example.com")
    second_contact = Contact(company_id=company.id, full_name="John Doe", linkedin_url="https://x/john", email_guess="john@example.com")
    s.add_all([first_contact, second_contact]); s.flush()
    first = record_initial_message(s, run_id=None, company_id=company.id, contact_id=first_contact.id, subject="S", body="B", resume_path=None, pairing_reason=None, posting_version_id=None, to_email=first_contact.email_guess, pattern_name=None)
    second = record_initial_message(s, run_id=None, company_id=company.id, contact_id=second_contact.id, subject="S", body="B", resume_path=None, pairing_reason=None, posting_version_id=None, to_email=second_contact.email_guess, pattern_name=None)
    first.state = second.state = "ready"; mailbox = FailsSecondMailbox()
    import pytest
    with pytest.raises(OSError, match="second provider"):
        reconcile_outreach(s, mailbox, now=datetime(2026, 8, 22, tzinfo=timezone.utc))
    first_attempt = s.query(OutreachDeliveryAttempt).filter_by(message_id=first.id).one()
    second_attempt = s.query(OutreachDeliveryAttempt).filter_by(message_id=second.id).one()
    assert first_attempt.state == "pushed" and first_attempt.gmail_draft_id in mailbox.drafts
    assert second_attempt.state == "indeterminate" and mailbox.calls == 2

def test_hard_reject_boundaries_are_evidence_bound():
    assert not evaluate_screening({"guaranteed_max_lpa": 25}, None, None)
    assert evaluate_screening({"guaranteed_max_lpa": 24.9}, None, None)[0].reason_code == "salary_below_25_lpa"
    assert evaluate_screening({"guaranteed_max_lpa": 30}, None, {"years": 2, "mandatory": True})
    assert not evaluate_screening({"guaranteed_max_lpa": 30.1}, None, {"years": 2, "mandatory": True})

def test_group_and_wlb_boundaries():
    assert group_for(True, 20) == 1
    assert group_for(False, 20) == 2
    assert group_for(True, 19.9) == 3
    assert qualified_wlb(4.2, 49, 50) is None
    assert qualified_wlb(4.2, 50, 50) == 4.2

def test_screening_facts_require_salary_disposition_when_snapshot_has_salary():
    s = session(); now = datetime(2026, 8, 22, tzinfo=timezone.utc)
    job = upsert_job(s, NormalizedJob(source="x", external_job_id="salary-1", title="Data Scientist", company_name_raw="Acme", description_raw="work", salary_raw="₹20L – ₹30L", posted_at=now))
    version = s.query(JobPostingVersion).filter_by(job_id=job.id).one()

    with __import__("pytest").raises(ValueError, match="salary disposition"):
        record_screening_facts(
            s,
            posting_version_id=version.id,
            salary=None,
            minimum_experience=None,
            maximum_experience=None,
        )

def test_screening_salary_disposition_must_be_evidence_backed_and_conclusive():
    s = session(); now = datetime(2026, 8, 22, tzinfo=timezone.utc)
    job = upsert_job(s, NormalizedJob(source="x", external_job_id="salary-2", title="Data Scientist", company_name_raw="Acme", description_raw="work", salary_raw="₹20L – ₹30L", posted_at=now))
    version = s.query(JobPostingVersion).filter_by(job_id=job.id).one()

    with __import__("pytest").raises(ValueError, match="evidence-backed"):
        record_screening_facts(
            s,
            posting_version_id=version.id,
            salary={"currency": "INR"},
            minimum_experience=None,
            maximum_experience=None,
        )

    row = record_screening_facts(
        s,
        posting_version_id=version.id,
        salary={
            "evidence": "₹20L – ₹30L",
            "unusable_reason": "period_unknown",
        },
        minimum_experience=None,
        maximum_experience=None,
    )
    assert row.salary["unusable_reason"] == "period_unknown"


def test_upsert_persists_combined_linkedin_job_enrichment_for_later_reuse():
    s = session(); now = datetime(2026, 8, 30, tzinfo=timezone.utc)
    job = upsert_job(s, NormalizedJob(
        source="linkedin",
        external_job_id="combined-1",
        title="Machine Learning Engineer",
        company_name_raw="Acme",
        description_raw="Requires 3+ years of Python and SQL experience.",
        salary_raw="₹25L – ₹35L per year",
        posted_at=now,
        raw_payload={
            "pre_gate_job_enrichment": {
                "status": "done",
                "experience_min_years": 3,
                "education_requirement": "Bachelor's degree",
                "qualification_other": "AWS certification preferred",
                "hard_skills": ["Python", "SQL", "Python", "X" * 120],
                "soft_skills": ["Communication"],
                "enriched_at": now.isoformat(),
                "salary": {
                    "evidence": "₹25L – ₹35L per year",
                    "guaranteed_max_lpa": 35,
                },
                "minimum_experience": {
                    "years": 3,
                    "mandatory": True,
                    "evidence": "Requires 3+ years",
                },
            },
        },
    ))

    assert job.enrichment_status == "done"
    assert job.experience_min_years == 3
    assert job.education_requirement == "Bachelor's degree"
    assert {(row.skill, row.skill_type) for row in s.query(JobSkill).all()} == {
        ("Python", "hard"),
        ("SQL", "hard"),
        ("X" * 100, "hard"),
        ("Communication", "soft"),
    }
    version = s.query(JobPostingVersion).filter_by(job_id=job.id).one()
    facts = s.query(JobScreeningFact).filter_by(posting_version_id=version.id).one()
    assert facts.salary["guaranteed_max_lpa"] == 35
    assert facts.minimum_experience["years"] == 3


def test_upsert_bounds_agent_education_to_database_column():
    s = session()
    full_evidence = "Bachelor's degree required. " * 20
    job = upsert_job(s, NormalizedJob(
        source="linkedin",
        external_job_id="combined-long-education",
        title="Machine Learning Engineer",
        company_name_raw="Acme",
        description_raw=full_evidence,
        raw_payload={
            "pre_gate_job_enrichment": {
                "status": "done",
                "education_requirement": full_evidence,
            },
        },
    ))

    assert job.education_requirement == full_evidence[:255]
    assert job.raw_payload["pre_gate_job_enrichment"][
        "education_requirement"
    ] == full_evidence

def test_posted_at_decision_snapshot_and_approval_are_immutable():
    s = session(); now = datetime(2026, 8, 22, tzinfo=timezone.utc)
    job = upsert_job(s, NormalizedJob(source="x", external_job_id="1", title="ML Engineer", company_name_raw="Acme", description_raw="work", posted_at=now - timedelta(hours=1), is_remote=True))
    run = legacy_run(s, since=(now - timedelta(days=1)).isoformat(), cutoff=now)
    summary = prepare_decision_run(s, run.id)
    assert summary["outcomes"]["eligible"] == 1
    scope = approve_decision_run(s, run.id, "target 1; G1 all; G2 0; G3 0; G4 0")
    assert scope.company_ids == (job.company_id,)
    assert scope.job_ids == (job.id,)

def test_applied_root_suppresses_cross_source_duplicate_cluster():
    s = session(); now = datetime(2026, 8, 22, tzinfo=timezone.utc)
    canonical = upsert_job(s, NormalizedJob(source="a", external_job_id="1", title="Data Scientist", company_name_raw="Acme", description_raw="work", posted_at=now))
    duplicate = upsert_job(s, NormalizedJob(source="b", external_job_id="2", title="Data Scientist", company_name_raw="Acme", description_raw="work", posted_at=now))
    duplicate.duplicate_of_job_id = canonical.id
    version = s.query(JobPostingVersion).filter_by(job_id=canonical.id).one()
    s.add(JobApplication(posting_version_id=version.id, canonical_duplicate_root_id=canonical.id, applied_at=now))
    run = legacy_run(s, since=(now - timedelta(days=1)).isoformat(), cutoff=now)
    assert prepare_decision_run(s, run.id)["outcomes"]["applied_duplicate"] == 1

def test_new_posting_episode_after_application_remains_eligible():
    s = session(); now = datetime(2026, 8, 22, tzinfo=timezone.utc)
    job = upsert_job(s, NormalizedJob(source="a", external_job_id="1", title="Data Scientist", company_name_raw="Acme", description_raw="old", posted_at=now - timedelta(days=2)))
    old = s.query(JobPostingVersion).filter_by(job_id=job.id).one()
    s.add(JobApplication(posting_version_id=old.id, canonical_duplicate_root_id=job.id, applied_at=now))
    upsert_job(s, NormalizedJob(source="a", external_job_id="1", title="Data Scientist", company_name_raw="Acme", description_raw="revised", posted_at=now))
    run = legacy_run(s, since=(now - timedelta(days=1)).isoformat(), cutoff=now)
    assert prepare_decision_run(s, run.id)["outcomes"]["eligible"] == 1

def test_future_posted_version_is_reported_as_anomaly_not_ranked():
    s = session(); now = datetime(2026, 8, 22, tzinfo=timezone.utc)
    upsert_job(s, NormalizedJob(source="a", external_job_id="1", title="Data Scientist", company_name_raw="Acme", description_raw="work", posted_at=now + timedelta(days=1)))
    run = legacy_run(s, since=(now - timedelta(days=1)).isoformat(), cutoff=now)
    assert prepare_decision_run(s, run.id)["outcomes"]["anomaly"] == 1

def test_missing_posted_date_is_reported_as_anomaly():
    s = session(); now = datetime(2026, 8, 22, tzinfo=timezone.utc)
    upsert_job(s, NormalizedJob(source="a", external_job_id="1", title="Data Scientist", company_name_raw="Acme", description_raw="work"))
    run = legacy_run(s, since=(now - timedelta(days=1)).isoformat(), cutoff=now)
    assert prepare_decision_run(s, run.id)["outcomes"]["anomaly"] == 1


def test_new_run_expires_unapproved_prior_run():
    s = session(); now = datetime(2026, 8, 22, tzinfo=timezone.utc)
    old = create_decision_run(s, since=(now - timedelta(days=2)).isoformat(), cutoff=now)
    old.state = "awaiting_approval"
    create_decision_run(s, since=(now - timedelta(days=1)).isoformat(), cutoff=now)
    assert old.state == "expired"

def test_phase_b_manifest_never_retries_attempted_source():
    s = session(); first, second = Company(name="First"), Company(name="Second", market_profile_evidence={"sources": {"glassdoor": {"status": "missing"}}})
    s.add_all([first, second]); s.flush()
    assert phase_b_never_attempted_company_ids(s, [first.id, second.id], source="glassdoor") == [first.id]

def test_application_only_company_does_not_consume_fresh_selection_target():
    s = session(); now = datetime(2026, 8, 22, tzinfo=timezone.utc)
    reached = Company(name="Reached"); s.add(reached); s.flush()
    for number in range(4):
        message = OutreachMessage(company_id=reached.id, message_kind="initial", subject="x", body="x")
        s.add(message); s.flush(); s.add(OutreachDeliveryAttempt(message_id=message.id, sequence_number=1, to_email=f"{number}@reached.com", domain="reached.com", state="presumed_delivered"))
    for source, company in (("a", "Reached"), ("b", "Fresh")):
        upsert_job(s, NormalizedJob(source=source, external_job_id="1", title="ML Engineer", company_name_raw=company, description_raw="work", posted_at=now, is_remote=True))
    run = legacy_run(s, since=(now - timedelta(days=1)).isoformat(), cutoff=now)
    prepare_decision_run(s, run.id); scope = approve_decision_run(s, run.id, "target 1; G1 all; G2 0; G3 0; G4 0")
    assert len(scope.company_ids) == 2

def test_reports_read_from_run_snapshots(tmp_path):
    openpyxl = __import__("pytest").importorskip("openpyxl")
    s = session(); now = datetime(2026, 8, 22, tzinfo=timezone.utc)
    upsert_job(s, NormalizedJob(source="x", external_job_id="1", title="ML Engineer", company_name_raw="Acme", description_raw="work", posted_at=now, is_remote=True, job_url="https://jobs.example/acme"))
    run = legacy_run(s, since=(now - timedelta(days=1)).isoformat(), cutoff=now)
    prepare_decision_run(s, run.id); approve_decision_run(s, run.id, "target 1; G1 all; G2 0; G3 0; G4 0")
    decision_path, final_path = tmp_path / "decision.xlsx", tmp_path / "final.xlsx"
    build_decision_workbook(s, run.id, decision_path); build_final_workbook(s, run.id, final_path)
    decision = openpyxl.load_workbook(decision_path)
    assert {"Group 1", "Group 2", "Group 3", "Group 4", "Rejected jobs", "Anomalies", "Distributions", "Run summary"} <= set(decision.sheetnames)
    headers = [cell.value for cell in decision["Group 1"][1]]
    assert "company_name" in headers and "job_link" in headers and "work_mode" in headers
    job_link_cell = decision["Group 1"].cell(row=2, column=headers.index("job_link") + 1)
    assert job_link_cell.hyperlink.target == "https://jobs.example/acme"
    assert {"Fresh outreach companies", "Application only", "Jobs and resumes", "Contacts and pairing", "Calibration and patterns", "Delivery state", "Failures", "Run summary"} <= set(openpyxl.load_workbook(final_path).sheetnames)

def test_decision_workbook_strips_excel_illegal_characters(tmp_path):
    openpyxl = __import__("pytest").importorskip("openpyxl")
    s = session(); now = datetime(2026, 8, 22, tzinfo=timezone.utc)
    upsert_job(s, NormalizedJob(source="x", external_job_id="1", title="Enterprise Architect \x06 Generative AI", company_name_raw="Acme", description_raw="work", posted_at=now, is_remote=True))
    run = create_decision_run(s, since=(now - timedelta(days=1)).isoformat(), cutoff=now)
    run.telemetry_version = None  # This fixture intentionally covers a legacy snapshot.
    prepare_decision_run(s, run.id)
    decision_path = tmp_path / "decision.xlsx"
    build_decision_workbook(s, run.id, decision_path)
    workbook = openpyxl.load_workbook(decision_path)
    headers = [cell.value for cell in workbook["Group 1"][1]]
    assert workbook["Group 1"].cell(row=2, column=headers.index("title") + 1).value == "Enterprise Architect  Generative AI"

def test_delivery_ledger_promotes_then_reverses_late_bounce():
    s = session(); c = Company(name="Acme"); s.add(c); s.flush()
    message = OutreachMessage(company_id=c.id, message_kind="initial", subject="Hi", body="Body")
    s.add(message); s.flush()
    attempt = OutreachDeliveryAttempt(message_id=message.id, sequence_number=1, to_email="a@example.com", domain="acme.com", state="held")
    s.add(attempt); s.flush(); mailbox = InMemoryMailbox()
    push_attempt(s, mailbox, attempt); when = datetime(2026, 8, 22, tzinfo=timezone.utc)
    mailbox.send(attempt.gmail_draft_id, at=when); reconcile_outreach(s, mailbox, now=when + timedelta(minutes=31))
    assert attempt.state == "presumed_delivered"
    mailbox.bounce(attempt.gmail_draft_id, at=when + timedelta(minutes=40)); reconcile_outreach(s, mailbox, now=when + timedelta(minutes=41))
    assert attempt.state == "bounced"

def test_unclassified_mail_event_is_retained_without_counting_as_delivery():
    s = session(); c = Company(name="Acme"); s.add(c); s.flush()
    message = OutreachMessage(company_id=c.id, message_kind="initial", subject="Hi", body="Body")
    s.add(message); s.flush()
    attempt = OutreachDeliveryAttempt(message_id=message.id, sequence_number=1, to_email="a@example.com", domain="acme.com", state="held")
    s.add(attempt); s.flush(); mailbox = InMemoryMailbox(); push_attempt(s, mailbox, attempt)
    mailbox.record_unclassified(attempt.gmail_draft_id, event_id="ambiguous-1")
    first = reconcile_outreach(s, mailbox, now=datetime(2026, 8, 22, tzinfo=timezone.utc))
    second = reconcile_outreach(s, mailbox, now=datetime(2026, 8, 22, tzinfo=timezone.utc))
    assert attempt.state == "unclassified_mail_event"
    assert attempt.evidence["unclassified_mail_event_ids"] == ["ambiguous-1"]
    assert (first["unclassified"], second["unclassified"]) == (1, 0)

def test_company_domain_aliases_preserve_shared_pattern_provenance():
    s = session(); first, second = Company(name="First"), Company(name="Second")
    s.add_all([first, second]); s.flush()
    contact = Contact(company_id=first.id, full_name="Jane Doe", first_name="Jane", last_name="Doe", title="Recruiter", linkedin_url="https://x/j")
    s.add(contact); s.flush()
    record_initial_message(s, run_id="run-a", company_id=first.id, contact_id=contact.id, subject="Hi", body="Body", resume_path=None, pairing_reason=None, posting_version_id=None, to_email="jane.doe@shared.example", pattern_name="first.last")
    other = Contact(company_id=second.id, full_name="Bob Doe", first_name="Bob", last_name="Doe", title="Recruiter", linkedin_url="https://x/b")
    s.add(other); s.flush()
    record_initial_message(s, run_id="run-b", company_id=second.id, contact_id=other.id, subject="Hi", body="Body", resume_path=None, pairing_reason=None, posting_version_id=None, to_email="bob.doe@shared.example", pattern_name="first.last")
    assert s.query(CompanyDomainAlias).filter_by(domain="shared.example").count() == 2

def test_read_only_reconciliation_never_pushes_released_drafts():
    s = session(); c = Company(name="Acme"); s.add(c); s.flush()
    message = OutreachMessage(company_id=c.id, message_kind="initial", subject="Hi", body="Body", state="ready")
    s.add(message); s.flush()
    attempt = OutreachDeliveryAttempt(message_id=message.id, sequence_number=1, to_email="a@example.com", domain="acme.com", state="held")
    s.add(attempt); s.flush(); mailbox = InMemoryMailbox()
    reconcile_outreach(s, mailbox, now=datetime(2026, 8, 22, tzinfo=timezone.utc), push_released=False)
    assert attempt.state == "held"
    assert mailbox.drafts == {}

def test_calibration_success_releases_company_messages():
    s = session(); c = Company(name="Acme"); s.add(c); s.flush()
    contact = Contact(company_id=c.id, full_name="Jane Doe", first_name="Jane", last_name="Doe", title="Recruiter", seniority_tier="talent_acquisition", linkedin_url="https://x/j", email_guess="jane.doe@example.com")
    s.add(contact); s.flush()
    calibration = prepare_company_outreach(s, SimpleNamespace(run_id="run", company_ids=(c.id,)), c.id)
    message = record_initial_message(s, run_id="run", company_id=c.id, contact_id=contact.id, subject="Hi", body="Body", resume_path=None, pairing_reason=None, posting_version_id=None, to_email=contact.email_guess, pattern_name="first.last", calibration=True)
    held = OutreachMessage(run_id="run", company_id=c.id, subject="Later", body="Body", state="held")
    s.add(held); s.flush()
    attempt = s.query(OutreachDeliveryAttempt).filter_by(message_id=message.id).one(); mailbox = InMemoryMailbox(); push_attempt(s, mailbox, attempt)
    when = datetime(2026, 8, 22, tzinfo=timezone.utc); mailbox.send(attempt.gmail_draft_id, at=when)
    reconcile_outreach(s, mailbox, now=when + timedelta(minutes=31))
    assert calibration.state == "pattern_confirmed"
    assert held.state == "ready"
    assert s.query(DomainEmailPattern).one().confidence == "provisional"

def test_calibration_success_retargets_held_contact_to_learned_pattern():
    s = session(); c = Company(name="Acme"); s.add(c); s.flush()
    first = Contact(company_id=c.id, full_name="Jane Doe", first_name="Jane", last_name="Doe", title="Recruiter", seniority_tier="talent_acquisition", linkedin_url="https://x/j", email_guess="jane.doe@example.com")
    second = Contact(company_id=c.id, full_name="Bob Smith", first_name="Bob", last_name="Smith", title="Data Scientist", seniority_tier="ic", linkedin_url="https://x/b", email_guess="bob.s@example.com")
    s.add_all([first, second]); s.flush(); prepare_company_outreach(s, SimpleNamespace(run_id="run", company_ids=(c.id,)), c.id)
    calibration = record_initial_message(s, run_id="run", company_id=c.id, contact_id=first.id, subject="Hi", body="Body", resume_path=None, pairing_reason=None, posting_version_id=None, to_email=first.email_guess, pattern_name="first.last", calibration=True)
    held = record_initial_message(s, run_id="run", company_id=c.id, contact_id=second.id, subject="Hi", body="Body", resume_path=None, pairing_reason=None, posting_version_id=None, to_email=second.email_guess, pattern_name="first.l")
    attempt = s.query(OutreachDeliveryAttempt).filter_by(message_id=calibration.id).one(); mailbox = InMemoryMailbox(); push_attempt(s, mailbox, attempt)
    when = datetime(2026, 8, 22, tzinfo=timezone.utc); mailbox.send(attempt.gmail_draft_id, at=when); reconcile_outreach(s, mailbox, now=when + timedelta(minutes=31))
    held_attempt = s.query(OutreachDeliveryAttempt).filter_by(message_id=held.id).one()
    assert (held.state, held_attempt.to_email, held_attempt.pattern_name) == ("pushed", "bob.smith@example.com", "first.last")

def test_calibration_bounce_prepares_only_next_pattern():
    s = session(); c = Company(name="Acme"); s.add(c); s.flush()
    contact = Contact(company_id=c.id, full_name="Jane Doe", first_name="Jane", last_name="Doe", title="Recruiter", seniority_tier="talent_acquisition", linkedin_url="https://x/j", email_guess="jane.doe@example.com")
    s.add(contact); s.flush(); prepare_company_outreach(s, SimpleNamespace(run_id="run", company_ids=(c.id,)), c.id)
    message = record_initial_message(s, run_id="run", company_id=c.id, contact_id=contact.id, subject="Hi", body="Body", resume_path=None, pairing_reason=None, posting_version_id=None, to_email=contact.email_guess, pattern_name="first.last", calibration=True)
    attempt = s.query(OutreachDeliveryAttempt).filter_by(message_id=message.id).one(); mailbox = InMemoryMailbox(); push_attempt(s, mailbox, attempt)
    when = datetime(2026, 8, 22, tzinfo=timezone.utc); mailbox.send(attempt.gmail_draft_id, at=when); mailbox.bounce(attempt.gmail_draft_id, at=when + timedelta(minutes=1))
    reconcile_outreach(s, mailbox, now=when + timedelta(minutes=2))
    attempts = s.query(OutreachDeliveryAttempt).filter_by(message_id=message.id).order_by(OutreachDeliveryAttempt.sequence_number).all()
    assert [(a.pattern_name, a.state) for a in attempts] == [("first.last", "bounced"), ("first.l", "held")]
