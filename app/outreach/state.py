"""Immutable delivery-ledger state machine for fresh outreach and calibration."""
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.orm import (DomainEmailPattern, OutreachDeliveryAttempt, OutreachMessage,
                            RunCompanyCalibration, CompanyDomainAlias, utcnow)
from app.models.orm import Contact

PATTERNS = ("first.last", "first.l", "f.last", "first_last")

class MailboxPort(Protocol):
    def create_or_update_draft(self, *, draft_id: str | None, to_email: str, subject: str, body: str, attachment_path: str | None) -> tuple[str, str]: ...
    def delete_unsent_draft(self, draft_id: str) -> None: ...
    def sent_message(self, gmail_draft_id: str, gmail_thread_id: str | None = None) -> dict | None: ...
    def thread_events(self, gmail_thread_id: str) -> list[dict]: ...
    def delivery_failures(self, gmail_thread_id: str) -> list[dict]: ...
    def unclassified_events(self, gmail_thread_id: str) -> list[dict]: ...

@dataclass
class InMemoryMailbox:
    drafts: dict = None
    sent: dict = None
    unclassified: dict = None
    def __post_init__(self): self.drafts, self.sent, self.unclassified = self.drafts or {}, self.sent or {}, self.unclassified or {}
    def create_or_update_draft(self, *, draft_id, to_email, subject, body, attachment_path):
        draft_id = draft_id or f"draft-{len(self.drafts)+1}"; thread = f"thread-{draft_id}"
        self.drafts[draft_id] = {"to": to_email, "subject": subject, "body": body,
                                 "attachment_path": attachment_path, "thread_id": thread}; return draft_id, thread
    def delete_unsent_draft(self, draft_id): self.drafts.pop(draft_id, None)
    def sent_message(self, gmail_draft_id, gmail_thread_id=None): return self.sent.get(gmail_draft_id)
    def thread_events(self, gmail_thread_id): return [v for v in self.sent.values() if v.get("thread_id") == gmail_thread_id and v.get("kind") == "reply"]
    def delivery_failures(self, gmail_thread_id): return [v for v in self.sent.values() if v.get("thread_id") == gmail_thread_id and v.get("kind") == "bounce"]
    def unclassified_events(self, gmail_thread_id): return list(self.unclassified.get(gmail_thread_id, []))
    def send(self, draft_id, *, at, message_id=None):
        d = self.drafts.pop(draft_id); self.sent[draft_id] = {"message_id": message_id or f"sent-{draft_id}", "thread_id": d["thread_id"], "sent_at": at, "kind": "sent"}
    def bounce(self, draft_id, *, at): self.sent[draft_id] = {**self.sent[draft_id], "kind": "bounce", "at": at}
    def reply(self, draft_id, *, at): self.sent[draft_id] = {**self.sent[draft_id], "kind": "reply", "at": at}
    def record_unclassified(self, draft_id, *, event_id=None, at=None):
        thread_id = self.drafts[draft_id]["thread_id"] if draft_id in self.drafts else self.sent[draft_id]["thread_id"]
        self.unclassified.setdefault(thread_id, []).append({"gmail_message_id": event_id or f"unknown-{draft_id}", "at": at})

def fresh_outreach_eligibility(session: Session, company_id: int, *, threshold: int = 4) -> dict:
    messages = session.execute(select(OutreachMessage).where(OutreachMessage.company_id == company_id, OutreachMessage.message_kind == "initial")).scalars().all()
    successes = 0
    for message in messages:
        attempts = session.execute(select(OutreachDeliveryAttempt).where(OutreachDeliveryAttempt.message_id == message.id)).scalars()
        if any(a.state in ("presumed_delivered", "replied") for a in attempts): successes += 1
    return {"eligible": successes < threshold, "successful_initial_contacts": successes, "state": "outbound_reached" if successes >= threshold else "eligible"}


def prepare_company_outreach(session: Session, approved_scope, company_id: int) -> RunCompanyCalibration:
    """Choose the one calibration person from an exact approved scope.

    Draft copy and actual messages are created by the drafting stage. This
    function only persists the deterministic release policy; it never queries
    a generic contact queue.
    """
    if company_id not in approved_scope.company_ids:
        raise ValueError("company is outside the approved scope")
    existing = session.execute(select(RunCompanyCalibration).where(
        RunCompanyCalibration.run_id == approved_scope.run_id,
        RunCompanyCalibration.company_id == company_id,
    )).scalar_one_or_none()
    if existing: return existing
    contacts = list(session.execute(select(Contact).where(Contact.company_id == company_id)).scalars())
    priority = {"talent_acquisition": 0, "ic": 1, "hiring_manager": 2, "head": 3}
    eligible = [c for c in contacts if c.first_name and c.last_name]
    eligible.sort(key=lambda c: (priority.get(c.seniority_tier or "", 99), bool(c.name_collision_risk), c.id))
    if not eligible:
        state, contact, domain = "no_calibration_contact", None, None
    else:
        contact = eligible[0]
        domain = (contact.email_guess or "").partition("@")[2].lower() or None
        state = "waiting_to_send" if domain else "no_calibration_contact"
    if domain:
        _observe_company_domain(session, company_id, domain)
    calibration = RunCompanyCalibration(run_id=approved_scope.run_id, company_id=company_id,
        domain=domain, contact_id=contact.id if contact else None, state=state)
    session.add(calibration); session.flush(); return calibration


def record_initial_message(session: Session, *, run_id: str, company_id: int, contact_id: int | None,
                           subject: str, body: str, resume_path: str | None, pairing_reason: str | None,
                           posting_version_id: int | None, to_email: str, pattern_name: str | None,
                           calibration: bool = False, legacy_draft_id: int | None = None,
                           recipient_evidence: dict | None = None) -> OutreachMessage:
    """Persist one run-scoped initial message and its first immutable attempt."""
    if legacy_draft_id is not None:
        message = session.execute(select(OutreachMessage).where(
            OutreachMessage.legacy_draft_id == legacy_draft_id,
        )).scalar_one_or_none()
    elif contact_id is not None:
        message = session.execute(select(OutreachMessage).where(OutreachMessage.run_id == run_id,
            OutreachMessage.contact_id == contact_id, OutreachMessage.message_kind == "initial")).scalar_one_or_none()
    else:
        raise ValueError("a generic inbox ledger entry requires its legacy draft id")
    if message is None:
        message = OutreachMessage(legacy_draft_id=legacy_draft_id, run_id=run_id,
            company_id=company_id, contact_id=contact_id,
            posting_version_id=posting_version_id, message_kind="initial", is_calibration=calibration,
            subject=subject, body=body, resume_path=resume_path, pairing_reason=pairing_reason,
            state="held")
        session.add(message); session.flush()
    if calibration:
        message.is_calibration = True
        calibration_row = session.execute(select(RunCompanyCalibration).where(
            RunCompanyCalibration.run_id == run_id, RunCompanyCalibration.company_id == company_id,
        )).scalar_one_or_none()
        if calibration_row: calibration_row.message_id = message.id
    existing = session.execute(select(OutreachDeliveryAttempt).where(OutreachDeliveryAttempt.message_id == message.id,
        OutreachDeliveryAttempt.sequence_number == 1)).scalar_one_or_none()
    if pattern_name is None and contact_id is not None:
        contact = session.get(Contact, contact_id)
        if contact and contact.first_name and contact.last_name:
            local = to_email.partition("@")[0].lower()
            first, last = contact.first_name.lower(), contact.last_name.lower()
            pattern_name = next((name for name, shape in {
                "first.last": f"{first}.{last}", "first.l": f"{first}.{last[:1]}",
                "f.last": f"{first[:1]}.{last}", "first_last": f"{first}_{last}",
            }.items() if shape == local), None)
    if existing is None:
        domain = to_email.partition("@")[2].lower()
        if domain:
            _observe_company_domain(session, company_id, domain)
        session.add(OutreachDeliveryAttempt(message_id=message.id, sequence_number=1, to_email=to_email.lower(),
            domain=domain, pattern_name=pattern_name, state="held", evidence=recipient_evidence))
    session.flush(); return message


def record_legacy_gmail_draft(session: Session, *, draft_id: int,
                              gmail_draft_id: str, gmail_thread_id: str) -> OutreachDeliveryAttempt | None:
    """Mirror a legacy Gmail Draft into its immutable run-scoped attempt.

    Company inboxes do not fit the contact-only DecisionRun manifests, but
    still need the same durable recipient and provider audit record.
    """
    message = session.execute(select(OutreachMessage).where(
        OutreachMessage.legacy_draft_id == draft_id,
    )).scalar_one_or_none()
    if message is None:
        return None
    attempt = session.execute(select(OutreachDeliveryAttempt).where(
        OutreachDeliveryAttempt.message_id == message.id,
    ).order_by(OutreachDeliveryAttempt.sequence_number.desc())).scalars().first()
    if attempt is None:
        raise RuntimeError("run-scoped outreach message has no delivery attempt")
    attempt.gmail_draft_id, attempt.gmail_thread_id, attempt.state = gmail_draft_id, gmail_thread_id, "pushed"
    message.state = "pushed"
    session.flush()
    return attempt

def _observe_company_domain(session: Session, company_id: int, domain: str) -> CompanyDomainAlias:
    """Persist provenance for a domain without making it a delivery claim."""
    domain = domain.strip().lower()
    alias = session.execute(select(CompanyDomainAlias).where(
        CompanyDomainAlias.company_id == company_id, CompanyDomainAlias.domain == domain,
    )).scalar_one_or_none()
    if alias is None:
        alias = CompanyDomainAlias(company_id=company_id, domain=domain,
                                   first_observed_at=utcnow(), last_observed_at=utcnow(), is_current=True)
        session.add(alias)
    else:
        alias.last_observed_at, alias.is_current = utcnow(), True
    return alias

def push_attempt(session: Session, mailbox: MailboxPort, attempt: OutreachDeliveryAttempt) -> OutreachDeliveryAttempt:
    message = session.get(OutreachMessage, attempt.message_id)
    # The intent must survive a process crash before the external API call.
    # Callers commit this state before invoking the mailbox; never re-call an
    # attempt in posting_intent/indeterminate state without reconciliation.
    if attempt.state != "held":
        raise RuntimeError(f"delivery attempt {attempt.id} is not authorized to post (state={attempt.state})")
    attempt.state, message.state = "posting_intent", "posting_intent"
    session.flush()
    session.commit()
    try:
        draft_id, thread_id = mailbox.create_or_update_draft(draft_id=attempt.gmail_draft_id, to_email=attempt.to_email,
            subject=message.subject, body=message.body, attachment_path=message.resume_path)
    except Exception as exc:
        # The provider may have accepted the request before the transport
        # failed. Preserve uncertainty and require reconciliation, not retry.
        attempt.state, message.state = "indeterminate", "indeterminate"
        attempt.evidence = {**(attempt.evidence or {}), "post_call_error": type(exc).__name__}
        session.flush()
        if message.run_id:
            from app.decision_runs.outreach_funnel import GmailOutcome, GmailResult, report_gmail_posting
            report_gmail_posting(session, message.run_id, [GmailResult(attempt.id, GmailOutcome.INDETERMINATE, f"gmail_post_call_error:{type(exc).__name__}", "non_blocking")])
        session.commit()
        raise
    attempt.gmail_draft_id, attempt.gmail_thread_id, attempt.state, attempt.pushed_at = draft_id, thread_id, "pushed", utcnow()
    message.state = "pushed"; session.flush()
    if message.run_id:
        # One authoritative seam for calibration retries and later released
        # batches.  It is deliberately attempt-keyed, never legacy-draft keyed.
        from app.decision_runs.outreach_funnel import GmailOutcome, GmailResult, report_gmail_posting
        from app.models.orm import DecisionRun
        from app.decision_runs.telemetry import is_legacy_telemetry
        run = session.get(DecisionRun, message.run_id)
        if run is not None and not is_legacy_telemetry(run.telemetry_version):
            report_gmail_posting(session, message.run_id, [GmailResult(attempt.id, GmailOutcome.POSTED)])
    return attempt

def reconcile_outreach(session: Session, mailbox: MailboxPort, *, now: datetime,
                       run_id: str | None = None, push_released: bool = True) -> dict:
    changed = {"sent": 0, "bounced": 0, "replied": 0, "delivered": 0, "unclassified": 0}
    statement = select(OutreachDeliveryAttempt).join(OutreachMessage).where(
        OutreachDeliveryAttempt.state.in_(("pushed", "sent", "presumed_delivered", "unclassified_mail_event")))
    if run_id is not None:
        statement = statement.where(OutreachMessage.run_id == run_id)
    attempts = session.execute(statement).scalars().all()
    for attempt in attempts:
        message = session.get(OutreachMessage, attempt.message_id)
        sent = mailbox.sent_message(attempt.gmail_draft_id, attempt.gmail_thread_id) if attempt.gmail_draft_id else None
        if sent and attempt.sent_at is None:
            attempt.sent_at, attempt.gmail_message_id, attempt.state, message.state = sent["sent_at"], sent["message_id"], "sent", "sent"; changed["sent"] += 1
        if not attempt.gmail_thread_id: continue
        failures, replies = mailbox.delivery_failures(attempt.gmail_thread_id), mailbox.thread_events(attempt.gmail_thread_id)
        prior_state = attempt.state
        if failures and attempt.state != "bounced":
            attempt.state, attempt.bounced_at, message.state = "bounced", failures[-1].get("at", now), "bounced"; changed["bounced"] += 1
            _record_pattern_observation(session, attempt, now, "bounce", reverse_success=prior_state in ("presumed_delivered", "replied"))
        elif replies and attempt.state != "replied":
            attempt.state, attempt.replied_at, message.state = "replied", replies[-1].get("at", now), "replied"; changed["replied"] += 1
            _record_pattern_observation(session, attempt, now, "reply")
        elif attempt.sent_at and attempt.state == "sent" and now >= attempt.sent_at + timedelta(minutes=30):
            attempt.state, attempt.presumed_delivered_at, message.state = "presumed_delivered", now, "delivered"; changed["delivered"] += 1
            _record_pattern_observation(session, attempt, now, "delivery")
        elif not sent and not failures and not replies:
            events = mailbox.unclassified_events(attempt.gmail_thread_id)
            evidence = dict(attempt.evidence or {})
            recorded = set(evidence.get("unclassified_mail_event_ids", []))
            new_ids = [event.get("gmail_message_id") for event in events if event.get("gmail_message_id") and event.get("gmail_message_id") not in recorded]
            if new_ids:
                evidence["unclassified_mail_event_ids"] = [*evidence.get("unclassified_mail_event_ids", []), *new_ids]
                attempt.evidence = evidence
                attempt.state, message.state = "unclassified_mail_event", "unclassified_mail_event"
                changed["unclassified"] += len(new_ids)
        _advance_attempt_calibration(session, attempt, now)
    if push_released:
        # Releasing a learned pattern means materialising the already-approved
        # held messages as Gmail drafts; it never sends a message.
        ready_attempts = session.execute(select(OutreachDeliveryAttempt).join(OutreachMessage).where(
            OutreachDeliveryAttempt.state == "held", OutreachMessage.state == "ready",
            OutreachMessage.run_id == run_id if run_id is not None else True)).scalars().all()
        for attempt in ready_attempts:
            push_attempt(session, mailbox, attempt)
    session.flush(); return changed

def advance_calibration(session: Session, company_id: int, *, now: datetime) -> RunCompanyCalibration | None:
    calibration = session.execute(select(RunCompanyCalibration).where(RunCompanyCalibration.company_id == company_id).order_by(RunCompanyCalibration.id.desc())).scalar_one_or_none()
    if calibration is None: return None
    if calibration.first_sent_at and calibration.deadline_at and now >= calibration.deadline_at and calibration.state == "testing": calibration.state = "pattern_unresolved"
    session.flush(); return calibration


def _record_pattern_observation(session: Session, attempt: OutreachDeliveryAttempt, now: datetime, kind: str, *, reverse_success: bool = False) -> None:
    if not attempt.pattern_name: return
    pattern = session.execute(select(DomainEmailPattern).where(
        DomainEmailPattern.domain == attempt.domain, DomainEmailPattern.pattern_name == attempt.pattern_name,
    )).scalar_one_or_none()
    if pattern is None:
        pattern = DomainEmailPattern(domain=attempt.domain, pattern_name=attempt.pattern_name,
            first_observed_at=now, confidence="unknown", success_count=0, reply_count=0, bounce_count=0)
        session.add(pattern)
    pattern.last_observed_at = now
    if kind == "bounce":
        pattern.bounce_count += 1
        if reverse_success: pattern.success_count = max(0, pattern.success_count - 1)
        pattern.confidence = "demoted" if pattern.success_count == 0 else "provisional"
    else:
        pattern.success_count += 1
        if kind == "reply": pattern.reply_count += 1; pattern.confidence = "confirmed"
        elif pattern.confidence in ("unknown", "demoted"): pattern.confidence = "provisional"


def _advance_attempt_calibration(session: Session, attempt: OutreachDeliveryAttempt, now: datetime) -> None:
    message = session.get(OutreachMessage, attempt.message_id)
    calibration = session.execute(select(RunCompanyCalibration).where(
        RunCompanyCalibration.run_id == message.run_id, RunCompanyCalibration.company_id == message.company_id,
    )).scalar_one_or_none()
    if calibration is None or calibration.message_id != message.id: return
    if attempt.state == "sent" and calibration.first_sent_at is None:
        calibration.first_sent_at = attempt.sent_at
        calibration.deadline_at = attempt.sent_at + timedelta(minutes=60)
        calibration.state = "testing"
    elif attempt.state in ("presumed_delivered", "replied"):
        calibration.state, calibration.confirmed_pattern = "pattern_confirmed", attempt.pattern_name
        # A learned domain pattern releases all unsent company messages.
        held = session.execute(select(OutreachMessage).where(OutreachMessage.run_id == message.run_id,
            OutreachMessage.company_id == message.company_id, OutreachMessage.state == "held")).scalars()
        for held_message in held:
            _retarget_held_message(session, held_message, calibration.domain, attempt.pattern_name)
            held_message.state = "ready"
    elif attempt.state == "bounced" and calibration.deadline_at and now >= calibration.deadline_at:
        calibration.state = "pattern_unresolved"
    elif attempt.state == "bounced":
        _queue_next_calibration_attempt(session, message, attempt, calibration)
    calibration.last_reconciled_at = now


def _retarget_held_message(session: Session, message: OutreachMessage, domain: str | None, pattern_name: str | None) -> None:
    """Update only unsent attempts; a sent Gmail address is immutable."""
    if not domain or not pattern_name or message.contact_id is None:
        return
    contact = session.get(Contact, message.contact_id)
    if contact is None or not contact.first_name or not contact.last_name:
        return
    first, last = contact.first_name.lower(), contact.last_name.lower()
    shapes = {"first.last": f"{first}.{last}", "first.l": f"{first}.{last[:1]}",
              "f.last": f"{first[:1]}.{last}", "first_last": f"{first}_{last}"}
    local = shapes.get(pattern_name)
    if not local:
        return
    attempts = session.execute(select(OutreachDeliveryAttempt).where(
        OutreachDeliveryAttempt.message_id == message.id,
        OutreachDeliveryAttempt.state == "held")).scalars()
    for held_attempt in attempts:
        held_attempt.to_email = f"{local}@{domain}"
        held_attempt.domain = domain
        held_attempt.pattern_name = pattern_name


def _queue_next_calibration_attempt(session: Session, message: OutreachMessage,
                                    failed: OutreachDeliveryAttempt,
                                    calibration: RunCompanyCalibration) -> None:
    """After a verified DSN, prepare exactly the next address shape, never send it."""
    contact = session.get(Contact, message.contact_id) if message.contact_id else None
    if contact is None or not contact.first_name or not contact.last_name or not calibration.domain:
        calibration.state = "no_calibration_contact"
        return
    first, last = contact.first_name.lower(), contact.last_name.lower()
    candidates = {
        "first.last": f"{first}.{last}@{calibration.domain}",
        "first.l": f"{first}.{last[:1]}@{calibration.domain}",
        "f.last": f"{first[:1]}.{last}@{calibration.domain}",
        "first_last": f"{first}_{last}@{calibration.domain}",
    }
    tried = set(session.execute(select(OutreachDeliveryAttempt.pattern_name).where(
        OutreachDeliveryAttempt.message_id == message.id)).scalars())
    next_pattern = next((pattern for pattern in PATTERNS if pattern not in tried), None)
    if next_pattern is None:
        message.state, calibration.state = "exhausted", "pattern_unresolved"
        return
    sequence = max(session.execute(select(OutreachDeliveryAttempt.sequence_number).where(
        OutreachDeliveryAttempt.message_id == message.id)).scalars()) + 1
    session.add(OutreachDeliveryAttempt(message_id=message.id, sequence_number=sequence,
        to_email=candidates[next_pattern], domain=calibration.domain, pattern_name=next_pattern, state="held"))
    message.state = "held"
