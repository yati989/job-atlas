"""
push_draft's create-vs-update decision (app/outreach/gmail.py).

No live Gmail credentials in CI, so `service` is a minimal fake recording
which of drafts().create()/update() was called — the only thing worth
protecting here, since a re-run that always creates would leave a stale
duplicate sitting in the user's real Drafts folder every time a resume gets
re-tailored or the email copy changes (confirmed by hand on 2026-08-06:
re-pushing the same to_email after editing a draft updated the existing
Gmail draft id rather than creating a second one).
"""
from datetime import datetime, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.models.orm import Base, Company, Contact, DecisionRun, OutreachDeliveryAttempt, OutreachMessage
from app.outreach import drafts as draft_ops
from app.outreach import gmail as gmail_ops


def _session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


class _FakeDraftsResource:
    def __init__(self):
        self.create_calls = 0
        self.update_calls = 0
        self.updated_ids = []

    def create(self, userId, body):
        self.create_calls += 1
        return _FakeRequest({"id": "new-draft-id", "message": {"id": "m1", "threadId": "t1"}})

    def update(self, userId, id, body):
        self.update_calls += 1
        self.updated_ids.append(id)
        return _FakeRequest({"id": id, "message": {"id": "m1", "threadId": "t1"}})


class _FakeRequest:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class _FakeUsers:
    def __init__(self, drafts_resource):
        self._drafts = drafts_resource

    def drafts(self):
        return self._drafts


class _FakeService:
    def __init__(self):
        self.drafts_resource = _FakeDraftsResource()

    def users(self):
        return _FakeUsers(self.drafts_resource)


def _draft(session):
    c = Company(name="Acme")
    session.add(c)
    session.flush()
    target = draft_ops.RecipientTarget(to_email="x@example.com", contact_id=None, company_id=c.id, prospect_id=None)
    return draft_ops.save_draft(
        session, target, to_name="A", recipient_title="Data Scientist", recipient_tier="ic",
        resume_kind="master", pairing_reason="no_jobs_at_company", subject="S", body="B",
    )


def test_first_push_creates():
    session = _session()
    row = _draft(session)
    service = _FakeService()

    gmail_ops.push_draft(session, service, row)

    assert service.drafts_resource.create_calls == 1
    assert service.drafts_resource.update_calls == 0
    assert row.gmail_draft_id == "new-draft-id"


def test_re_push_after_edit_updates_the_same_gmail_draft():
    session = _session()
    row = _draft(session)
    service = _FakeService()
    gmail_ops.push_draft(session, service, row)
    original_gmail_id = row.gmail_draft_id

    # Simulate re-drafting (e.g. re-tailored resume) — save_draft reopens a
    # non-sent row to STATUS_DRAFTED, matching real usage.
    row.subject = "Updated subject"
    row.status = draft_ops.STATUS_DRAFTED
    session.flush()

    gmail_ops.push_draft(session, service, row)

    assert service.drafts_resource.create_calls == 1
    assert service.drafts_resource.update_calls == 1
    assert service.drafts_resource.updated_ids == [original_gmail_id]
    assert row.gmail_draft_id == original_gmail_id


def test_push_mirrors_a_company_inbox_draft_to_its_run_delivery_attempt():
    session = _session()
    company = Company(name="Acme")
    session.add(company)
    session.add(DecisionRun(
        id="run", since_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
        cutoff_at=datetime(2026, 8, 2, tzinfo=timezone.utc), input_timezone="UTC",
        state="approved", policy_snapshot={}, target_count=1,
        telemetry_version="decision-run-progress-v1",
    ))
    session.flush()
    target = draft_ops.resolve_recipient(session, to_email="careers@acme.example", company_id=company.id)
    row = draft_ops.save_draft(
        session, target, to_name=None, recipient_title="Careers team", recipient_tier="unknown",
        resume_kind="master", pairing_reason="no_jobs_at_company", subject="S", body="B",
        decision_run_id="run", recipient_evidence={
            "source_url": "https://acme.example/careers",
            "verification": "Published on the company's careers page",
        },
    )

    gmail_ops.push_draft(session, _FakeService(), row)

    message = session.execute(select(OutreachMessage).where(
        OutreachMessage.legacy_draft_id == row.id,
    )).scalar_one()
    attempt = session.execute(select(OutreachDeliveryAttempt).where(
        OutreachDeliveryAttempt.message_id == message.id,
    )).scalar_one()
    assert (message.contact_id, message.state) == (None, "pushed")
    assert (attempt.state, attempt.gmail_draft_id, attempt.gmail_thread_id) == (
        "pushed", "new-draft-id", "t1",
    )
    assert attempt.evidence["source_url"] == "https://acme.example/careers"


def test_push_retargeted_company_inbox_updates_gmail_without_a_contact_manifest():
    session = _session()
    company = Company(name="Acme")
    session.add(company)
    session.add(DecisionRun(
        id="run", since_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
        cutoff_at=datetime(2026, 8, 2, tzinfo=timezone.utc), input_timezone="UTC",
        state="approved", policy_snapshot={}, target_count=1,
        telemetry_version="decision-run-progress-v1",
    ))
    session.flush()
    contact = Contact(
        company_id=company.id, full_name="Jane Doe", title="Recruiter",
        linkedin_url="https://linkedin.example/in/jane", email_guess="jane@acme.example",
        seniority_tier="talent_acquisition",
    )
    session.add(contact)
    generic = draft_ops.resolve_recipient(session, to_email="careers@acme.example", company_id=company.id)
    row = draft_ops.save_draft(
        session, generic, to_name=None, recipient_title="Careers team", recipient_tier="unknown",
        resume_kind="master", pairing_reason="no_jobs_at_company", subject="S", body="B",
        decision_run_id="run", recipient_evidence={
            "source_url": "https://acme.example/careers",
            "verification": "Published on the company's careers page",
        },
    )
    row.gmail_draft_id, row.gmail_thread_id, row.status = "existing-gmail", "existing-thread", "pushed"
    session.flush()
    draft_ops.retarget_draft_to_contact(session, draft_id=row.id, contact_id=contact.id)

    service = _FakeService()
    gmail_ops.push_draft(session, service, row)

    message = session.execute(select(OutreachMessage).where(OutreachMessage.legacy_draft_id == row.id)).scalar_one()
    attempt = session.execute(select(OutreachDeliveryAttempt).where(OutreachDeliveryAttempt.message_id == message.id)).scalar_one()
    assert (service.drafts_resource.create_calls, service.drafts_resource.update_calls) == (0, 1)
    assert service.drafts_resource.updated_ids == ["existing-gmail"]
    assert (row.status, row.gmail_draft_id, row.to_email) == ("pushed", "existing-gmail", "jane@acme.example")
    assert (message.contact_id, message.state, attempt.state) == (contact.id, "pushed", "pushed")
    assert session.query(OutreachMessage).filter_by(legacy_draft_id=row.id).count() == 1


def test_push_refuses_a_sent_draft():
    from app.models.orm import utcnow
    session = _session()
    row = _draft(session)
    service = _FakeService()
    gmail_ops.push_draft(session, service, row)
    draft_ops.mark_sent(session, row, sent_body="B", sent_at=utcnow())

    try:
        gmail_ops.push_draft(session, service, row)
    except draft_ops.InvalidDraft:
        pass
    else:
        raise AssertionError("expected push_draft to refuse an already-sent row")
