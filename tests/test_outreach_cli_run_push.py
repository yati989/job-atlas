"""Production CLI path: approved-run pushes use ledger attempts and Gmail Drafts only."""
from contextlib import nullcontext
from types import SimpleNamespace
import pytest

from app.decision_runs.outreach_funnel import ChosenPairing, freeze_draft_preparation_manifest, freeze_gmail_draft_manifest
from app.models.orm import Contact, DecisionRunContactEnrichmentManifest
from app.outreach import cli, drafts
from tests.test_outreach_funnel import _fixture


class _Request:
    def execute(self): return {"id": "gmail-draft", "message": {"threadId": "thread"}}
class _Drafts:
    def __init__(self): self.created = 0
    def create(self, **_): self.created += 1; return _Request()
    def update(self, **_): return _Request()
class _Users:
    def __init__(self, drafts): self._drafts = drafts
    def drafts(self): return self._drafts
class _Service:
    """Intentionally has no messages() method: outreach must never send."""
    def __init__(self): self.drafts_resource = _Drafts()
    def users(self): return _Users(self.drafts_resource)


def test_run_push_posts_every_frozen_ledger_attempt_without_sending(monkeypatch):
    session, company, contact = _fixture()
    other = Contact(company_id=company.id, full_name="John Doe", title="Recruiter",
                    linkedin_url="https://x/john", email_guess="john@example.com",
                    seniority_tier="talent_acquisition")
    session.add(other); session.flush()
    contact_manifest = session.query(DecisionRunContactEnrichmentManifest).one()
    contact_manifest.reused_contact_ids = [contact.id, other.id]
    freeze_draft_preparation_manifest(session, "run", chosen_pairings=[
        ChosenPairing(contact.id, None, None), ChosenPairing(other.id, None, None),
    ])
    row = drafts.save_draft(session, drafts.resolve_recipient(session, to_email=contact.email_guess),
        to_name="Jane", recipient_title="Data", recipient_tier="ic",
        resume_kind="master", pairing_reason="no_jobs_at_company", subject="S", body="B",
        decision_run_id="run", calibration=True)
    other_row = drafts.save_draft(session, drafts.resolve_recipient(session, to_email=other.email_guess),
        to_name="John", recipient_title="Recruiter", recipient_tier="talent_acquisition",
        resume_kind="master", pairing_reason="no_jobs_at_company", subject="S2", body="B2",
        decision_run_id="run")
    manifest = freeze_gmail_draft_manifest(session, "run")
    assert row.id and other_row.id and len(manifest) == 2
    service = _Service()
    monkeypatch.setattr(cli, "get_session", lambda: nullcontext(session))
    monkeypatch.setattr(cli.gmail_ops, "get_service", lambda: service)
    cli._cmd_push(SimpleNamespace(run_id="run", all=False, to_email=None))
    assert service.drafts_resource.created == 2
    assert {item.outcome for item in manifest} == {"posted"}


def test_run_push_config_failure_keeps_item_pending_and_explicit_retry_posts_same_attempt(monkeypatch):
    session, _, contact = _fixture(); freeze_draft_preparation_manifest(session, "run", chosen_pairings=[ChosenPairing(contact.id, None, None)])
    row = drafts.save_draft(session, drafts.resolve_recipient(session, to_email=contact.email_guess), to_name="Jane", recipient_title="Data", recipient_tier="ic", resume_kind="master", pairing_reason="no_jobs_at_company", subject="S", body="B", decision_run_id="run", calibration=True)
    manifest = freeze_gmail_draft_manifest(session, "run")[0]; service = _Service()
    monkeypatch.setattr(cli, "get_session", lambda: nullcontext(session))
    monkeypatch.setattr(cli.gmail_ops, "get_service", lambda: (_ for _ in ()).throw(cli.gmail_ops.GmailNotConfigured("missing")))
    with pytest.raises(SystemExit): cli._cmd_push(SimpleNamespace(run_id="run", all=False, to_email=None))
    assert manifest.outcome is None
    assert session.get(__import__('app.models.orm', fromlist=['OutreachDeliveryAttempt']).OutreachDeliveryAttempt, manifest.delivery_attempt_id).state == "held"
    monkeypatch.setattr(cli.gmail_ops, "get_service", lambda: service)
    cli._cmd_push(SimpleNamespace(run_id="run", all=False, to_email=None))
    assert manifest.outcome == "posted" and service.drafts_resource.created == 1
