"""
Persistence/invariant layer for outreach drafts (app/outreach/drafts.py).

What's testable here mirrors app/prospects/batch.py's tests: not whether an
email is well-written (that's the skill's judgement), but everything built
to stop a mistake from being sent — the addressing invariant (ADR-0010),
the hook-needs-its-source rule, and the collision-risk block (ADR-0009).

SQLite in-memory, same pattern as test_prospect_batch.py.
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.orm import Base, Company, Contact, Job, Prospect, TailoredResume
from app.outreach import drafts as draft_ops
from app.outreach import pairing


def _session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _master_draft(session, target, **overrides):
    kwargs = dict(
        to_name="A Person", recipient_title="Data Scientist", recipient_tier="ic",
        resume_kind="master", pairing_reason=pairing.REASON_NO_JOBS_AT_COMPANY,
        subject="Subject", body="Body text.",
    )
    kwargs.update(overrides)
    return draft_ops.save_draft(session, target, **kwargs)


# ------------------------------------------------------- resolve_recipient ---

def test_resolve_recipient_prefers_a_matching_contact():
    session = _session()
    c = Company(name="Acme")
    session.add(c)
    session.flush()
    contact = Contact(company_id=c.id, full_name="Jane Doe", linkedin_url="https://x/in/jane", email_guess="jane@example.com")
    session.add(contact)
    session.flush()

    target = draft_ops.resolve_recipient(session, to_email="Jane@example.com")
    assert target.contact_id == contact.id
    assert target.company_id == c.id
    assert target.prospect_id is None


def test_resolve_recipient_falls_back_to_explicit_company_id():
    session = _session()
    c = Company(name="Acme")
    session.add(c)
    session.flush()

    target = draft_ops.resolve_recipient(session, to_email="unknown@example.com", company_id=c.id)
    assert target.contact_id is None
    assert target.company_id == c.id


def test_resolve_recipient_falls_back_to_explicit_prospect_id():
    session = _session()
    p = Prospect(name="Volt Money", normalized_name="volt money", thesis="t")
    session.add(p)
    session.flush()

    target = draft_ops.resolve_recipient(session, to_email="ceo@example.com", prospect_id=p.id)
    assert target.prospect_id == p.id
    assert target.company_id is None


def test_resolve_recipient_refuses_both_company_and_prospect():
    session = _session()
    with pytest.raises(draft_ops.RecipientResolutionError):
        draft_ops.resolve_recipient(session, to_email="x@example.com", company_id=1, prospect_id=1)


def test_resolve_recipient_refuses_nothing_to_address_to():
    session = _session()
    with pytest.raises(draft_ops.RecipientResolutionError):
        draft_ops.resolve_recipient(session, to_email="nobody@example.com")


def test_resolve_recipient_refuses_unusable_email():
    session = _session()
    with pytest.raises(draft_ops.RecipientResolutionError):
        draft_ops.resolve_recipient(session, to_email="not-an-email", company_id=1)


def test_resolve_recipient_refuses_unknown_company_id():
    session = _session()
    with pytest.raises(draft_ops.RecipientResolutionError):
        draft_ops.resolve_recipient(session, to_email="x@example.com", company_id=999)


# ------------------------------------------------------------- save_draft ---

def test_save_draft_persists_a_master_draft():
    session = _session()
    c = Company(name="Acme")
    session.add(c)
    session.flush()
    target = draft_ops.RecipientTarget(to_email="x@example.com", contact_id=None, company_id=c.id, prospect_id=None)

    row = _master_draft(session, target)
    assert row.status == draft_ops.STATUS_DRAFTED
    assert row.resume_kind == "master"


def test_save_draft_rejects_tailored_without_job_and_resume_id():
    session = _session()
    c = Company(name="Acme")
    session.add(c)
    session.flush()
    target = draft_ops.RecipientTarget(to_email="x@example.com", contact_id=None, company_id=c.id, prospect_id=None)

    with pytest.raises(draft_ops.InvalidDraft):
        draft_ops.save_draft(
            session, target, to_name="A", recipient_title="Data Scientist", recipient_tier="ic",
            resume_kind="tailored", pairing_reason=pairing.REASON_RECIPIENT_FUNCTION_MATCH,
            subject="S", body="B",
        )


def test_save_draft_rejects_mismatched_kind_and_reason():
    session = _session()
    c = Company(name="Acme")
    session.add(c)
    session.flush()
    target = draft_ops.RecipientTarget(to_email="x@example.com", contact_id=None, company_id=c.id, prospect_id=None)

    with pytest.raises(draft_ops.InvalidDraft):
        draft_ops.save_draft(
            session, target, to_name="A", recipient_title="Data Scientist", recipient_tier="ic",
            resume_kind="master", pairing_reason=pairing.REASON_RECIPIENT_FUNCTION_MATCH,
            subject="S", body="B",
        )


def test_save_draft_requires_a_real_tailored_resume_row():
    session = _session()
    c = Company(name="Acme")
    j = Job(source="t", external_job_id="1", company_id=c.id, title="Data Scientist")
    session.add_all([c, j])
    session.flush()
    target = draft_ops.RecipientTarget(to_email="x@example.com", contact_id=None, company_id=c.id, prospect_id=None)

    with pytest.raises(draft_ops.InvalidDraft):
        draft_ops.save_draft(
            session, target, to_name="A", recipient_title="Data Scientist", recipient_tier="ic",
            resume_kind="tailored", pairing_reason=pairing.REASON_RECIPIENT_FUNCTION_MATCH,
            tailored_resume_id=999, matched_job_id=j.id, resume_path="x.pdf",
            subject="S", body="B",
        )


def test_save_draft_accepts_a_real_tailored_resume():
    session = _session()
    c = Company(name="Acme")
    j = Job(source="t", external_job_id="1", company_id=c.id, title="Data Scientist")
    session.add_all([c, j])
    session.flush()
    tr = TailoredResume(job_id=j.id, score=80.0)
    session.add(tr)
    session.flush()
    target = draft_ops.RecipientTarget(to_email="x@example.com", contact_id=None, company_id=c.id, prospect_id=None)

    row = draft_ops.save_draft(
        session, target, to_name="A", recipient_title="Data Scientist", recipient_tier="ic",
        resume_kind="tailored", pairing_reason=pairing.REASON_RECIPIENT_FUNCTION_MATCH,
        tailored_resume_id=tr.id, matched_job_id=j.id, resume_path="output/tailored_resumes/job_1/resume.pdf",
        subject="S", body="B",
    )
    assert row.resume_kind == "tailored"
    assert row.tailored_resume_id == tr.id


def test_save_draft_rejects_hook_with_only_url():
    session = _session()
    c = Company(name="Acme")
    session.add(c)
    session.flush()
    target = draft_ops.RecipientTarget(to_email="x@example.com", contact_id=None, company_id=c.id, prospect_id=None)

    with pytest.raises(draft_ops.InvalidDraft):
        _master_draft(session, target, hook_url="https://acme.com/news")


def test_save_draft_rejects_hook_with_only_quote():
    session = _session()
    c = Company(name="Acme")
    session.add(c)
    session.flush()
    target = draft_ops.RecipientTarget(to_email="x@example.com", contact_id=None, company_id=c.id, prospect_id=None)

    with pytest.raises(draft_ops.InvalidDraft):
        _master_draft(session, target, hook_quote="we raised a Series A")


def test_save_draft_accepts_hook_with_both():
    session = _session()
    c = Company(name="Acme")
    session.add(c)
    session.flush()
    target = draft_ops.RecipientTarget(to_email="x@example.com", contact_id=None, company_id=c.id, prospect_id=None)

    row = _master_draft(session, target, hook_url="https://acme.com/news", hook_quote="we raised a Series A")
    assert row.hook_url and row.hook_quote


def test_save_draft_rejects_empty_subject_or_body():
    session = _session()
    c = Company(name="Acme")
    session.add(c)
    session.flush()
    target = draft_ops.RecipientTarget(to_email="x@example.com", contact_id=None, company_id=c.id, prospect_id=None)

    with pytest.raises(draft_ops.InvalidDraft):
        _master_draft(session, target, subject="   ")
    with pytest.raises(draft_ops.InvalidDraft):
        _master_draft(session, target, body="")


def test_save_draft_dedupes_on_to_email():
    session = _session()
    c = Company(name="Acme")
    session.add(c)
    session.flush()
    target = draft_ops.RecipientTarget(to_email="x@example.com", contact_id=None, company_id=c.id, prospect_id=None)

    first = _master_draft(session, target, subject="First")
    second = _master_draft(session, target, subject="Second")

    assert first.id == second.id
    assert session.query(draft_ops.OutreachDraft).count() == 1
    assert second.subject == "Second"


def test_status_column_fits_the_longest_status_value():
    """SQLite doesn't enforce VARCHAR length, so a too-narrow column here
    passes every SQLite-backed test and only breaks against real Postgres —
    confirmed live: status was String(20) and 'blocked_collision_risk' (22
    chars) truncated with a DataError on insert. Assert directly against the
    declared column length rather than relying on a live Postgres check."""
    from app.models.orm import OutreachDraft
    max_len = OutreachDraft.__table__.c.status.type.length
    longest_value = max(
        len(v) for v in (
            draft_ops.STATUS_DRAFTED, draft_ops.STATUS_PUSHED,
            draft_ops.STATUS_SENT, draft_ops.STATUS_BLOCKED_COLLISION_RISK,
        )
    )
    assert max_len >= longest_value


def test_save_draft_flags_collision_risk_without_blocking_contact():
    session = _session()
    c = Company(name="Acme")
    session.add(c)
    session.flush()
    contact = Contact(
        company_id=c.id, full_name="Jane Doe", linkedin_url="https://x/in/jane",
        email_guess="jane@example.com", name_collision_risk=True,
    )
    session.add(contact)
    session.flush()
    target = draft_ops.resolve_recipient(session, to_email="jane@example.com")

    row = _master_draft(session, target)
    assert row.status == draft_ops.STATUS_DRAFTED
    assert row.collision_risk is True


# -------------------------------------------------------- status machine ---

def test_mark_pushed_allows_a_collision_risk_draft():
    session = _session()
    c = Company(name="Acme")
    session.add(c)
    session.flush()
    contact = Contact(
        company_id=c.id, full_name="Jane Doe", linkedin_url="https://x/in/jane",
        email_guess="jane@example.com", name_collision_risk=True,
    )
    session.add(contact)
    session.flush()
    target = draft_ops.resolve_recipient(session, to_email="jane@example.com")
    row = _master_draft(session, target)

    draft_ops.mark_pushed(
        session, row, gmail_draft_id="d1", gmail_thread_id="t1"
    )

    assert row.status == draft_ops.STATUS_PUSHED
    assert row.collision_risk is True


def test_mark_pushed_then_mark_sent():
    from app.models.orm import utcnow
    session = _session()
    c = Company(name="Acme")
    session.add(c)
    session.flush()
    target = draft_ops.RecipientTarget(to_email="x@example.com", contact_id=None, company_id=c.id, prospect_id=None)
    row = _master_draft(session, target)

    draft_ops.mark_pushed(session, row, gmail_draft_id="d1", gmail_thread_id="t1")
    assert row.status == draft_ops.STATUS_PUSHED

    draft_ops.mark_sent(session, row, sent_body="Edited body.", sent_at=utcnow())
    assert row.status == draft_ops.STATUS_SENT
    assert row.sent_body == "Edited body."


def test_mark_pushed_refuses_an_already_sent_draft():
    from app.models.orm import utcnow
    session = _session()
    c = Company(name="Acme")
    session.add(c)
    session.flush()
    target = draft_ops.RecipientTarget(to_email="x@example.com", contact_id=None, company_id=c.id, prospect_id=None)
    row = _master_draft(session, target)
    draft_ops.mark_pushed(session, row, gmail_draft_id="d1", gmail_thread_id="t1")
    draft_ops.mark_sent(session, row, sent_body="B", sent_at=utcnow())

    with pytest.raises(draft_ops.InvalidDraft):
        draft_ops.mark_pushed(session, row, gmail_draft_id="d2", gmail_thread_id="t2")


def test_re_drafting_a_sent_row_does_not_reopen_it():
    """A sent email is not un-sent by re-running the drafting step (e.g. a
    re-tailored resume) — status must not silently revert to drafted."""
    from app.models.orm import utcnow
    session = _session()
    c = Company(name="Acme")
    session.add(c)
    session.flush()
    target = draft_ops.RecipientTarget(to_email="x@example.com", contact_id=None, company_id=c.id, prospect_id=None)
    row = _master_draft(session, target)
    draft_ops.mark_pushed(session, row, gmail_draft_id="d1", gmail_thread_id="t1")
    draft_ops.mark_sent(session, row, sent_body="B", sent_at=utcnow())

    row2 = _master_draft(session, target, subject="Re-drafted")
    assert row2.id == row.id
    assert row2.status == draft_ops.STATUS_SENT


def test_pending_for_push_includes_collision_risk_drafts():
    session = _session()
    c = Company(name="Acme")
    session.add(c)
    session.flush()

    drafted = draft_ops.RecipientTarget(to_email="ok@example.com", contact_id=None, company_id=c.id, prospect_id=None)
    _master_draft(session, drafted)

    contact = Contact(
        company_id=c.id, full_name="Jane Doe", linkedin_url="https://x/in/jane",
        email_guess="risky@example.com", name_collision_risk=True,
    )
    session.add(contact)
    session.flush()
    blocked_target = draft_ops.resolve_recipient(session, to_email="risky@example.com")
    _master_draft(session, blocked_target)

    pending = draft_ops.pending_for_push(session)
    assert [d.to_email for d in pending] == ["ok@example.com", "risky@example.com"]

    risky = draft_ops.collision_risk_drafts(session)
    assert [d.to_email for d in risky] == ["risky@example.com"]
