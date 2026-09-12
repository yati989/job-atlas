from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.decision_runs.outreach_funnel import (
    DraftOutcome, DraftResult, GmailOutcome, GmailResult,
    ChosenPairing,
    freeze_draft_preparation_manifest, freeze_gmail_draft_manifest,
    report_draft_preparation, report_final_report, report_gmail_posting,
    begin_final_report,
)
from app.decision_runs.progress import StageName, StageTransitionError, finish_stage, start_stage
from app.models.orm import (Base, Company, Contact, DecisionRun,
    DecisionRunContactEnrichmentManifest, DecisionRunResumeTailoringManifest,
    DecisionRunSelectedJob, Job, JobPostingVersion, OutreachDraft, OutreachDeliveryAttempt, OutreachMessage)
from app.outreach import drafts as draft_ops


def _session():
    engine = create_engine("sqlite://", future=True); Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, future=True)()
    s.add(DecisionRun(id="run", since_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
        cutoff_at=datetime(2026, 8, 2, tzinfo=timezone.utc), input_timezone="UTC",
        state="processing", policy_snapshot={}, target_count=1, telemetry_version="decision-run-progress-v1"))
    s.flush(); return s


def _complete(s, name):
    from app.decision_runs.progress import PREDECESSORS
    from app.models.orm import DecisionRunStage
    from sqlalchemy import select
    if s.execute(select(DecisionRunStage).where(
        DecisionRunStage.run_id == "run", DecisionRunStage.name == StageName(name).value,
    )).scalar_one_or_none() is not None:
        return
    for parent in PREDECESSORS.get(StageName(name), ()):
        _complete(s, parent)
    start_stage(s, "run", name, expected_count=0, reason="fixture")
    finish_stage(s, "run", name, reason="fixture")


def _fixture():
    s = _session(); company = Company(name="Acme"); s.add(company); s.flush()
    contact = Contact(company_id=company.id, full_name="Jane Doe", title="Data Scientist", linkedin_url="https://x/jane", email_guess="jane@example.com", seniority_tier="ic")
    s.add(contact); s.flush()
    s.add(DecisionRunContactEnrichmentManifest(run_id="run", company_id=company.id, search_group="data_ai",
        outcome="enriched", reused_contact_ids=[contact.id], new_contact_ids=[])); s.flush()
    _complete(s, StageName.RESUME_TAILORING); _complete(s, StageName.CONTACT_ENRICHMENT)
    return s, company, contact


def _master_choice(*contacts):
    return [ChosenPairing(contact.id, None, None) for contact in contacts]


def test_frozen_draft_and_gmail_manifests_use_exact_contacts_and_do_not_duplicate():
    s, company, contact = _fixture()
    manifest = freeze_draft_preparation_manifest(s, "run", chosen_pairings=_master_choice(contact))
    assert len(manifest) == 1 and manifest[0].contact_id == contact.id
    target = draft_ops.resolve_recipient(s, to_email="jane@example.com")
    draft = draft_ops.save_draft(s, target, to_name="Jane", recipient_title="Data", recipient_tier="ic",
        resume_kind="master", pairing_reason="no_jobs_at_company", subject="S", body="B", decision_run_id="run")
    report_draft_preparation(s, "run", [DraftResult(contact.id, DraftOutcome.PREPARED, outreach_draft_id=draft.id)])
    assert freeze_draft_preparation_manifest(s, "run")[0].id == manifest[0].id
    gmail = freeze_gmail_draft_manifest(s, "run")
    assert len(gmail) == 1 and gmail[0].outreach_draft_id == draft.id
    attempt = s.get(__import__('app.models.orm', fromlist=['OutreachDeliveryAttempt']).OutreachDeliveryAttempt, gmail[0].delivery_attempt_id)
    attempt.gmail_draft_id = "gmail-draft"; attempt.gmail_thread_id = "thread"
    report_gmail_posting(s, "run", [GmailResult(gmail[0].delivery_attempt_id, GmailOutcome.POSTED)])
    assert freeze_gmail_draft_manifest(s, "run")[0].id == gmail[0].id


def test_approved_draft_persistence_reports_the_frozen_manifest_not_a_global_backlog():
    s, company, contact = _fixture(); freeze_draft_preparation_manifest(s, "run", chosen_pairings=_master_choice(contact))
    target = draft_ops.resolve_recipient(s, to_email="jane@example.com")
    row = draft_ops.save_draft(s, target, to_name="Jane", recipient_title="Data", recipient_tier="ic",
        resume_kind="master", pairing_reason="no_jobs_at_company", subject="S", body="B",
        decision_run_id="run")
    assert row.id
    assert freeze_draft_preparation_manifest(s, "run")[0].outcome == "prepared"


def test_company_inbox_is_preserved_in_the_run_ledger_with_its_published_source():
    s, company, _contact = _fixture()
    target = draft_ops.resolve_recipient(s, to_email="careers@acme.example", company_id=company.id)

    draft = draft_ops.save_draft(
        s, target, to_name=None, recipient_title="Careers team", recipient_tier="unknown",
        resume_kind="master", pairing_reason="no_jobs_at_company", subject="S", body="B",
        decision_run_id="run", recipient_evidence={
            "source_url": "https://acme.example/careers",
            "verification": "Published on the company's careers page",
        },
    )

    message = s.execute(select(OutreachMessage).where(
        OutreachMessage.legacy_draft_id == draft.id,
    )).scalar_one()
    attempt = s.execute(select(OutreachDeliveryAttempt).where(
        OutreachDeliveryAttempt.message_id == message.id,
    )).scalar_one()
    assert message.contact_id is None
    assert attempt.to_email == "careers@acme.example"
    assert attempt.evidence == {
        "source_url": "https://acme.example/careers",
        "verification": "Published on the company's careers page",
    }


def test_company_inbox_requires_published_source_evidence_for_a_run():
    s, company, _contact = _fixture()
    target = draft_ops.resolve_recipient(s, to_email="careers@acme.example", company_id=company.id)

    with pytest.raises(draft_ops.InvalidDraft, match="recipient_evidence"):
        draft_ops.save_draft(
            s, target, to_name=None, recipient_title="Careers team", recipient_tier="unknown",
            resume_kind="master", pairing_reason="no_jobs_at_company", subject="S", body="B",
            decision_run_id="run",
        )


def test_backfill_adds_an_existing_company_inbox_draft_to_the_run_ledger():
    s, company, _contact = _fixture()
    target = draft_ops.resolve_recipient(s, to_email="careers@acme.example", company_id=company.id)
    draft = draft_ops.save_draft(
        s, target, to_name=None, recipient_title="Careers team", recipient_tier="unknown",
        resume_kind="master", pairing_reason="no_jobs_at_company", subject="S", body="B",
    )
    draft.gmail_draft_id, draft.gmail_thread_id, draft.status = "gmail-existing", "thread-existing", "pushed"

    draft_ops.backfill_company_inbox_ledger(
        s, draft_id=draft.id, decision_run_id="run", recipient_evidence={
            "source_url": "https://acme.example/careers",
            "verification": "Published on the company's careers page",
        },
    )

    message = s.execute(select(OutreachMessage).where(
        OutreachMessage.legacy_draft_id == draft.id,
    )).scalar_one()
    attempt = s.execute(select(OutreachDeliveryAttempt).where(
        OutreachDeliveryAttempt.message_id == message.id,
    )).scalar_one()
    assert (attempt.state, attempt.gmail_draft_id, attempt.gmail_thread_id) == (
        "pushed", "gmail-existing", "thread-existing",
    )


def test_retargeting_company_inbox_updates_the_existing_unsent_gmail_draft_ledger():
    s, company, contact = _fixture()
    generic = draft_ops.resolve_recipient(s, to_email="careers@acme.example", company_id=company.id)
    draft = draft_ops.save_draft(
        s, generic, to_name=None, recipient_title="Careers team", recipient_tier="unknown",
        resume_kind="master", pairing_reason="no_jobs_at_company", subject="S", body="B",
        decision_run_id="run", recipient_evidence={
            "source_url": "https://acme.example/careers",
            "verification": "Published on the company's careers page",
        },
    )
    draft.gmail_draft_id, draft.gmail_thread_id, draft.status = "gmail-existing", "thread-existing", "pushed"
    message = s.execute(select(OutreachMessage).where(OutreachMessage.legacy_draft_id == draft.id)).scalar_one()
    attempt = s.execute(select(OutreachDeliveryAttempt).where(OutreachDeliveryAttempt.message_id == message.id)).scalar_one()
    attempt.gmail_draft_id, attempt.gmail_thread_id, attempt.state = "gmail-existing", "thread-existing", "pushed"
    s.flush()

    changed = draft_ops.retarget_draft_to_contact(s, draft_id=draft.id, contact_id=contact.id)

    assert (changed.id, changed.to_email, changed.to_name, changed.contact_id, changed.status) == (
        draft.id, "jane@example.com", "Jane Doe", contact.id, "drafted",
    )
    assert (changed.gmail_draft_id, changed.gmail_thread_id) == ("gmail-existing", "thread-existing")
    assert s.query(OutreachMessage).filter_by(legacy_draft_id=draft.id).count() == 1
    assert (message.contact_id, message.state) == (contact.id, "held")
    assert (attempt.to_email, attempt.domain, attempt.state) == ("jane@example.com", "example.com", "held")
    assert (attempt.gmail_draft_id, attempt.gmail_thread_id) == ("gmail-existing", "thread-existing")
    assert attempt.evidence == {
        "recipient_kind": "stored_contact", "contact_id": contact.id,
        "linkedin_url": "https://x/jane",
        "retargeted_from": {
            "to_email": "careers@acme.example",
            "recipient_evidence": {
                "source_url": "https://acme.example/careers",
                "verification": "Published on the company's careers page",
            },
        },
    }


def test_retargeting_refuses_a_sent_draft():
    s, company, contact = _fixture()
    generic = draft_ops.resolve_recipient(s, to_email="careers@acme.example", company_id=company.id)
    draft = draft_ops.save_draft(
        s, generic, to_name=None, recipient_title="Careers team", recipient_tier="unknown",
        resume_kind="master", pairing_reason="no_jobs_at_company", subject="S", body="B",
    )
    draft.status = "sent"

    with pytest.raises(draft_ops.InvalidDraft, match="already sent"):
        draft_ops.retarget_draft_to_contact(s, draft_id=draft.id, contact_id=contact.id)


def test_gmail_posted_requires_an_actual_gmail_draft_and_final_report_requires_file(tmp_path):
    s, company, contact = _fixture(); freeze_draft_preparation_manifest(s, "run", chosen_pairings=_master_choice(contact))
    target = draft_ops.resolve_recipient(s, to_email="jane@example.com")
    draft = draft_ops.save_draft(s, target, to_name="Jane", recipient_title="Data", recipient_tier="ic",
        resume_kind="master", pairing_reason="no_jobs_at_company", subject="S", body="B", decision_run_id="run")
    report_draft_preparation(s, "run", [DraftResult(contact.id, DraftOutcome.PREPARED, outreach_draft_id=draft.id)])
    freeze_gmail_draft_manifest(s, "run")
    with pytest.raises(StageTransitionError, match="Gmail Draft exists"):
        report_gmail_posting(s, "run", [GmailResult(freeze_gmail_draft_manifest(s, "run")[0].delivery_attempt_id, GmailOutcome.POSTED)])
    gmail = freeze_gmail_draft_manifest(s, "run"); attempt = s.get(__import__('app.models.orm', fromlist=['OutreachDeliveryAttempt']).OutreachDeliveryAttempt, gmail[0].delivery_attempt_id); attempt.gmail_draft_id = "d"; report_gmail_posting(s, "run", [GmailResult(gmail[0].delivery_attempt_id, GmailOutcome.POSTED)])
    begin_final_report(s, "run")
    with pytest.raises(StageTransitionError, match="workbook exists"):
        report_final_report(s, "run", output=str(tmp_path / "missing.xlsx"), self_report_requested=False, self_report_delivered=False)
    path = tmp_path / "final.xlsx"; path.write_text("x")
    report_final_report(s, "run", output=str(path), self_report_requested=False, self_report_delivered=False)


def test_zero_item_freezes_are_idempotent_and_terminal():
    s = _session(); _complete(s, StageName.RESUME_TAILORING); _complete(s, StageName.CONTACT_ENRICHMENT)
    assert freeze_draft_preparation_manifest(s, "run") == ()
    assert freeze_draft_preparation_manifest(s, "run") == ()
    assert freeze_gmail_draft_manifest(s, "run") == ()
    assert freeze_gmail_draft_manifest(s, "run") == ()


def test_exhausted_contact_and_same_email_collision_are_frozen_not_lost():
    s, company, contact = _fixture()
    other = Contact(company_id=company.id, full_name="Jane Other", linkedin_url="https://x/other",
                    email_guess="jane@example.com", seniority_tier="ic")
    s.add(other); s.flush()
    s.add(DecisionRunContactEnrichmentManifest(run_id="run", company_id=company.id, search_group="credit_risk",
        outcome="exhausted", reused_contact_ids=[other.id], new_contact_ids=[])); s.flush()
    rows = freeze_draft_preparation_manifest(s, "run", chosen_pairings=_master_choice(contact, other))
    assert len(rows) == 2
    dropped = next(row for row in rows if row.contact_id == other.id)
    assert dropped.outcome == "dropped" and "recipient_email_collision" in dropped.reason


def test_multiple_approved_versions_freeze_the_exact_deterministic_pairing_version():
    s, company, contact = _fixture()
    first = Job(source="x", external_job_id="one", company_id=company.id, title="Data Scientist")
    second = Job(source="x", external_job_id="two", company_id=company.id, title="Data Scientist")
    s.add_all([first, second]); s.flush()
    v1 = JobPostingVersion(job_id=first.id, canonical_duplicate_root_id=first.id, material_content_hash="one", posting_instance_key="one", snapshot={"job_url": "https://one"})
    v2 = JobPostingVersion(job_id=second.id, canonical_duplicate_root_id=second.id, material_content_hash="two", posting_instance_key="two", snapshot={"job_url": "https://two"})
    s.add_all([v1, v2]); s.flush()
    s.add_all([
        DecisionRunSelectedJob(run_id="run", company_id=company.id, posting_version_id=v1.id, company_order=1, selection_kind="fresh_outreach"),
        DecisionRunSelectedJob(run_id="run", company_id=company.id, posting_version_id=v2.id, company_order=1, selection_kind="fresh_outreach"),
        DecisionRunResumeTailoringManifest(run_id="run", posting_version_id=v1.id, job_id=first.id, outcome="tailored"),
        DecisionRunResumeTailoringManifest(run_id="run", posting_version_id=v2.id, job_id=second.id, outcome="tailored"),
    ]); s.flush()
    row = freeze_draft_preparation_manifest(s, "run", chosen_pairings=[ChosenPairing(contact.id, second.id, v2.id)])[0]
    assert (row.job_id, row.posting_version_id) == (second.id, v2.id)


def test_draft_freeze_requires_explicit_mixed_choices_and_never_falls_back():
    s, company, contact = _fixture()
    with pytest.raises(StageTransitionError, match="explicit chosen pairings"):
        freeze_draft_preparation_manifest(s, "run")
    job = Job(source="x", external_job_id="one", company_id=company.id, title="Data Scientist")
    s.add(job); s.flush()
    version = JobPostingVersion(job_id=job.id, canonical_duplicate_root_id=job.id, material_content_hash="one", posting_instance_key="one", snapshot={})
    s.add(version); s.flush()
    s.add_all([DecisionRunSelectedJob(run_id="run", company_id=company.id, posting_version_id=version.id, company_order=1, selection_kind="fresh_outreach"),
               DecisionRunResumeTailoringManifest(run_id="run", posting_version_id=version.id, job_id=job.id, outcome="tailored")]); s.flush()
    with pytest.raises(StageTransitionError, match="explicit master"):
        freeze_draft_preparation_manifest(s, "run", chosen_pairings=[ChosenPairing(contact.id, None, None)])
    row = freeze_draft_preparation_manifest(s, "run", chosen_pairings=[ChosenPairing(contact.id, job.id, version.id)])[0]
    assert (row.job_id, row.posting_version_id, row.resume_kind) == (job.id, version.id, "tailored")


def test_gmail_manifest_is_recipient_stable_across_a_released_sequence_two_attempt():
    s, company, contact = _fixture()
    freeze_draft_preparation_manifest(s, "run", chosen_pairings=_master_choice(contact))
    draft = draft_ops.save_draft(s, draft_ops.resolve_recipient(s, to_email=contact.email_guess),
        to_name="Jane", recipient_title="Data", recipient_tier="ic", resume_kind="master",
        pairing_reason="no_jobs_at_company", subject="S", body="B", decision_run_id="run")
    report_draft_preparation(s, "run", [DraftResult(contact.id, DraftOutcome.PREPARED, outreach_draft_id=draft.id)])
    manifest = freeze_gmail_draft_manifest(s, "run")[0]
    first = s.get(OutreachDeliveryAttempt, manifest.delivery_attempt_id)
    report_gmail_posting(s, "run", [GmailResult(first.id, GmailOutcome.FAILED, "confirmed provider refusal", "non_blocking")])
    second = OutreachDeliveryAttempt(message_id=first.message_id, sequence_number=2, to_email=first.to_email,
        domain=first.domain, state="held", gmail_draft_id="second", gmail_thread_id="thread-second")
    s.add(second); s.flush()
    from app.decision_runs.outreach_funnel import prepare_gmail_retry
    prepare_gmail_retry(s, "run")
    report_gmail_posting(s, "run", [GmailResult(second.id, GmailOutcome.POSTED)])
    assert manifest.id == freeze_gmail_draft_manifest(s, "run")[0].id
    assert manifest.delivery_attempt_id == second.id
    assert [item["attempt_id"] for item in manifest.attempt_evidence["attempts"]] == [first.id, second.id]


def test_terminal_posted_manifest_rejects_later_failed_mutation():
    s, _, contact = _fixture(); freeze_draft_preparation_manifest(s, "run", chosen_pairings=_master_choice(contact))
    draft = draft_ops.save_draft(s, draft_ops.resolve_recipient(s, to_email=contact.email_guess), to_name="Jane", recipient_title="Data", recipient_tier="ic", resume_kind="master", pairing_reason="no_jobs_at_company", subject="S", body="B", decision_run_id="run")
    report_draft_preparation(s, "run", [DraftResult(contact.id, DraftOutcome.PREPARED, outreach_draft_id=draft.id)])
    manifest = freeze_gmail_draft_manifest(s, "run")[0]; attempt = s.get(OutreachDeliveryAttempt, manifest.delivery_attempt_id); attempt.gmail_draft_id = "d"
    report_gmail_posting(s, "run", [GmailResult(attempt.id, GmailOutcome.POSTED)])
    with pytest.raises(StageTransitionError, match="terminal Gmail"):
        report_gmail_posting(s, "run", [GmailResult(attempt.id, GmailOutcome.FAILED, "no", "non_blocking")])


def test_post_call_failure_is_indeterminate_and_never_silently_loses_current_run_telemetry():
    class FailingMailbox:
        def create_or_update_draft(self, **_): raise OSError("provider transport vanished")
    s, _, contact = _fixture()
    freeze_draft_preparation_manifest(s, "run", chosen_pairings=_master_choice(contact))
    draft = draft_ops.save_draft(s, draft_ops.resolve_recipient(s, to_email=contact.email_guess),
        to_name="Jane", recipient_title="Data", recipient_tier="ic", resume_kind="master",
        pairing_reason="no_jobs_at_company", subject="S", body="B", decision_run_id="run")
    report_draft_preparation(s, "run", [DraftResult(contact.id, DraftOutcome.PREPARED, outreach_draft_id=draft.id)])
    manifest = freeze_gmail_draft_manifest(s, "run")[0]
    from app.outreach.state import push_attempt
    with pytest.raises(OSError, match="transport"):
        push_attempt(s, FailingMailbox(), s.get(OutreachDeliveryAttempt, manifest.delivery_attempt_id))
    assert s.get(OutreachDeliveryAttempt, manifest.delivery_attempt_id).state == "indeterminate"
    assert manifest.outcome == GmailOutcome.INDETERMINATE.value


def test_operator_reconciliation_of_indeterminate_provider_draft_completes_same_manifest():
    class FailingMailbox:
        def create_or_update_draft(self, **_): raise OSError("timeout")
    s, _, contact = _fixture(); freeze_draft_preparation_manifest(s, "run", chosen_pairings=_master_choice(contact))
    draft = draft_ops.save_draft(s, draft_ops.resolve_recipient(s, to_email=contact.email_guess), to_name="Jane", recipient_title="Data", recipient_tier="ic", resume_kind="master", pairing_reason="no_jobs_at_company", subject="S", body="B", decision_run_id="run")
    report_draft_preparation(s, "run", [DraftResult(contact.id, DraftOutcome.PREPARED, outreach_draft_id=draft.id)])
    manifest = freeze_gmail_draft_manifest(s, "run")[0]; attempt = s.get(OutreachDeliveryAttempt, manifest.delivery_attempt_id)
    from app.outreach.state import push_attempt
    with pytest.raises(OSError): push_attempt(s, FailingMailbox(), attempt)
    from app.decision_runs.outreach_funnel import reconcile_indeterminate_gmail
    reconcile_indeterminate_gmail(s, "run", delivery_attempt_id=attempt.id, provider_draft_id="recovered", provider_thread_id="thread")
    assert (manifest.outcome, attempt.state, attempt.gmail_draft_id) == ("posted", "pushed", "recovered")


def test_operator_reconciles_crash_left_posting_intent_to_same_posted_manifest():
    s, _, contact = _fixture(); freeze_draft_preparation_manifest(s, "run", chosen_pairings=_master_choice(contact))
    draft = draft_ops.save_draft(s, draft_ops.resolve_recipient(s, to_email=contact.email_guess), to_name="Jane", recipient_title="Data", recipient_tier="ic", resume_kind="master", pairing_reason="no_jobs_at_company", subject="S", body="B", decision_run_id="run")
    report_draft_preparation(s, "run", [DraftResult(contact.id, DraftOutcome.PREPARED, outreach_draft_id=draft.id)])
    manifest = freeze_gmail_draft_manifest(s, "run")[0]; attempt = s.get(OutreachDeliveryAttempt, manifest.delivery_attempt_id); attempt.state = "posting_intent"
    from app.decision_runs.outreach_funnel import reconcile_indeterminate_gmail
    reconcile_indeterminate_gmail(s, "run", delivery_attempt_id=attempt.id, provider_draft_id="found", provider_thread_id="thread")
    assert manifest.outcome == "posted" and attempt.state == "pushed"
