"""
Mechanical support for the /draft-outreach skill: recipient resolution,
invariant enforcement, resume attachment, dedup, and status transitions.

Same split as app/prospects/batch.py and app/contacts/agentic_batch.py:
this module owns everything that erodes silently under pace or must never
be gotten wrong twice (the addressing invariant, the hook-needs-a-source
rule, collision-risk flagging) — it does not write the email. The subject
and body are the skill's judgement, over evidence this module hands it.

Collision-risk contacts (docs/adr/0012-collision-risk-drafts-are-pushed.md):
`collision_risk` is set and persisted for every draft, but no longer
gates pushing — a collision-risk draft is created and pushed exactly like
any other, informational only. STATUS_BLOCKED_COLLISION_RISK is kept as a
historical status value (no row is ever assigned it going forward) so old
data and `push_draft`'s defensive check both stay meaningful.
"""
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.orm import (
    Company,
    Contact,
    OutreachDeliveryAttempt,
    OutreachDraft,
    OutreachMessage,
    Prospect,
    TailoredResume,
)
from app.models.orm import utcnow
from app.outreach.pairing import MASTER_REASONS, VALID_PAIRING_REASONS
from app.resume.render import render_resume
from app.resume.schema import load_master

STATUS_DRAFTED = "drafted"
STATUS_PUSHED = "pushed"
STATUS_SENT = "sent"
STATUS_BLOCKED_COLLISION_RISK = "blocked_collision_risk"

MASTER_RESUME_DIR = Path("output") / "master_resume"


class RecipientResolutionError(ValueError):
    """Raised when the (company/prospect, email) input can't be resolved to
    exactly one addressing target, or resolves to none."""


class InvalidDraft(ValueError):
    """Raised when a draft's fields violate an invariant this module
    enforces — a hook with only one of URL/quote, a resume_kind that
    disagrees with its pairing_reason, etc."""


def normalize_email(email: str) -> str:
    return email.strip().lower()


@dataclass
class RecipientTarget:
    to_email: str
    contact_id: int | None
    company_id: int | None
    prospect_id: int | None
    # Filled from the matched Contact when one exists; None otherwise — the
    # caller (skill/CLI) must supply to_name/recipient_title/recipient_tier
    # itself for a manually-supplied address.
    contact: Contact | None = None


def resolve_recipient(
    session: Session,
    *,
    to_email: str,
    company_id: int | None = None,
    prospect_id: int | None = None,
) -> RecipientTarget:
    """Resolve a (company-or-prospect, email) input to an addressing target.

    Tries a Contact match on the email first — regardless of which company/
    prospect was named, since the stored contact is the more reliable source
    of company_id than whatever the caller typed. Falls back to whichever of
    company_id/prospect_id was supplied. Refuses ambiguous input (both a
    company and a prospect id given) and empty input (neither, and no
    matching contact) rather than guessing.
    """
    email = normalize_email(to_email)
    if not email or "@" not in email:
        raise RecipientResolutionError(f"{to_email!r} is not a usable email address")

    if company_id is not None and prospect_id is not None:
        raise RecipientResolutionError(
            "both company_id and prospect_id given — a recipient belongs to exactly one"
        )

    contact = session.execute(
        select(Contact).where(Contact.email_guess == email)
    ).scalar_one_or_none()

    if contact is not None:
        return RecipientTarget(
            to_email=email, contact_id=contact.id,
            company_id=contact.company_id, prospect_id=None, contact=contact,
        )

    if company_id is not None:
        if session.get(Company, company_id) is None:
            raise RecipientResolutionError(f"no company with id {company_id}")
        return RecipientTarget(to_email=email, contact_id=None, company_id=company_id, prospect_id=None)

    if prospect_id is not None:
        if session.get(Prospect, prospect_id) is None:
            raise RecipientResolutionError(f"no prospect with id {prospect_id}")
        return RecipientTarget(to_email=email, contact_id=None, company_id=None, prospect_id=prospect_id)

    raise RecipientResolutionError(
        f"{email!r} matches no stored contact, and no company_id/prospect_id was given — "
        "nothing to address this to"
    )


def render_master_resume() -> str:
    """Render (or reuse a cached render of) the plain resume master, for
    recipients whose pairing resolved to resume_kind='master'.

    Cached on disk rather than re-rendered per recipient: the master doesn't
    change between drafts in a run, and Tectonic compilation is the slowest
    step in this whole pipeline.
    """
    pdf_path = MASTER_RESUME_DIR / "resume.pdf"
    if pdf_path.exists():
        return str(pdf_path)
    render_resume(load_master(), MASTER_RESUME_DIR, basename="resume").raise_if_failed()
    return str(pdf_path)


def validate_resume_attachment(
    *, resume_kind: str, pairing_reason: str,
    tailored_resume_id: int | None, matched_job_id: int | None,
) -> None:
    if resume_kind not in ("tailored", "master"):
        raise InvalidDraft(f"resume_kind must be 'tailored' or 'master', got {resume_kind!r}")
    if pairing_reason not in VALID_PAIRING_REASONS:
        raise InvalidDraft(f"unknown pairing_reason {pairing_reason!r}")

    expect_master = pairing_reason in MASTER_REASONS
    if expect_master and resume_kind != "master":
        raise InvalidDraft(f"pairing_reason {pairing_reason!r} requires resume_kind='master'")
    if not expect_master and resume_kind != "tailored":
        raise InvalidDraft(f"pairing_reason {pairing_reason!r} requires resume_kind='tailored'")

    if resume_kind == "tailored":
        if tailored_resume_id is None or matched_job_id is None:
            raise InvalidDraft("resume_kind='tailored' requires both tailored_resume_id and matched_job_id")


def validate_hook(hook_url: str | None, hook_quote: str | None) -> None:
    """Both set or neither — a hook without its verbatim source is exactly
    the unverifiable claim references/hooks.md exists to keep out."""
    if bool(hook_url) != bool(hook_quote):
        raise InvalidDraft("hook_url and hook_quote must be set together or not at all")


def validate_generic_recipient_evidence(evidence: dict | None) -> dict:
    """Require reviewable provenance for a company inbox in a run ledger.

    A shared inbox has no person-level Contact evidence.  Its source and the
    judgement that made it usable therefore travel with the immutable delivery
    attempt, instead of being inferred later from its address.
    """
    if not isinstance(evidence, dict):
        raise InvalidDraft(
            "a run-scoped company inbox requires recipient_evidence with source_url and verification"
        )
    source_url = evidence.get("source_url")
    verification = evidence.get("verification")
    if not isinstance(source_url, str) or not source_url.startswith(("https://", "http://")):
        raise InvalidDraft("company-inbox recipient_evidence requires an http(s) source_url")
    if not isinstance(verification, str) or not verification.strip():
        raise InvalidDraft("company-inbox recipient_evidence requires a non-empty verification")
    return {"source_url": source_url, "verification": verification.strip()}


def save_draft(
    session: Session,
    target: RecipientTarget,
    *,
    to_name: str | None,
    recipient_title: str | None,
    recipient_tier: str | None,
    resume_kind: str,
    pairing_reason: str,
    subject: str,
    body: str,
    tailored_resume_id: int | None = None,
    resume_path: str | None = None,
    matched_job_id: int | None = None,
    hook_url: str | None = None,
    hook_quote: str | None = None,
    decision_run_id: str | None = None,
    posting_version_id: int | None = None,
    calibration: bool = False,
    recipient_evidence: dict | None = None,
) -> OutreachDraft:
    """Create or update the one draft for this recipient (dedup on
    to_email). A collision-risk contact's draft is written and drafted
    exactly like any other — `collision_risk` is stored on the row for
    the run workbook to flag, but does not change its status or block it
    from being pushed (see module docstring / ADR-0012).
    """
    validate_resume_attachment(
        resume_kind=resume_kind, pairing_reason=pairing_reason,
        tailored_resume_id=tailored_resume_id, matched_job_id=matched_job_id,
    )
    validate_hook(hook_url, hook_quote)
    if resume_kind == "tailored" and tailored_resume_id is not None:
        if session.get(TailoredResume, tailored_resume_id) is None:
            raise InvalidDraft(f"no tailored_resumes row with id {tailored_resume_id}")

    if not subject.strip():
        raise InvalidDraft("subject is empty")
    if not body.strip():
        raise InvalidDraft("body is empty")

    collision_risk = bool(target.contact and target.contact.name_collision_risk)

    existing = session.execute(
        select(OutreachDraft).where(OutreachDraft.to_email == target.to_email)
    ).scalar_one_or_none()

    row = existing or OutreachDraft(to_email=target.to_email)
    row.to_name = to_name or (target.contact.full_name if target.contact else None)
    row.recipient_title = recipient_title or (target.contact.title if target.contact else None)
    row.recipient_tier = recipient_tier or (target.contact.seniority_tier if target.contact else None)
    row.contact_id = target.contact_id
    row.company_id = target.company_id
    row.prospect_id = target.prospect_id
    row.resume_kind = resume_kind
    row.pairing_reason = pairing_reason
    row.tailored_resume_id = tailored_resume_id
    row.resume_path = resume_path
    row.matched_job_id = matched_job_id
    row.subject = subject
    row.body = body
    row.hook_url = hook_url
    row.hook_quote = hook_quote
    row.collision_risk = collision_risk
    # A previously-pushed/sent draft being re-run (e.g. re-tailored) reopens
    # to drafted, EXCEPT once sent — a sent email is not un-sent by editing
    # the row, and re-drafting must not silently claim to push again.
    # collision_risk is stored above but never changes this — see ADR-0012.
    if row.status != STATUS_SENT:
        row.status = STATUS_DRAFTED

    if existing is None:
        session.add(row)
    session.flush()
    # Temporary compatibility bridge: standalone callers continue to use the
    # legacy draft row, while approved pipeline callers additionally create
    # the immutable message/attempt ledger record.  No legacy state is
    # retired here; that requires a reconciled shadow run.
    if decision_run_id is not None and target.company_id is not None:
        from app.outreach.state import record_initial_message
        generic_inbox_evidence = (
            validate_generic_recipient_evidence(recipient_evidence)
            if target.contact_id is None else None
        )
        record_initial_message(
            session, run_id=decision_run_id, company_id=target.company_id,
            contact_id=target.contact_id, subject=subject, body=body,
            resume_path=resume_path, pairing_reason=pairing_reason,
            posting_version_id=posting_version_id, to_email=target.to_email,
            pattern_name=None, calibration=calibration, legacy_draft_id=row.id,
            recipient_evidence=generic_inbox_evidence,
        )
        if target.contact_id is not None:
            from app.decision_runs.outreach_funnel import report_saved_draft
            report_saved_draft(session, run_id=decision_run_id, draft=row)
    return row


def backfill_company_inbox_ledger(
    session: Session, *, draft_id: int, decision_run_id: str,
    recipient_evidence: dict, posting_version_id: int | None = None,
) -> OutreachDraft:
    """Attach a pre-ledger company-inbox draft to its immutable run record.

    This is a local repair only: it does not create or update anything in
    Gmail.  If the existing legacy draft already has a Gmail id, that known
    provider receipt is copied into the newly-created delivery attempt.
    """
    row = session.get(OutreachDraft, draft_id)
    if row is None:
        raise InvalidDraft(f"no outreach_drafts row with id {draft_id}")
    if row.company_id is None or row.contact_id is not None:
        raise InvalidDraft("backfill is only for a company inbox without a Contact row")
    from app.outreach.state import record_initial_message, record_legacy_gmail_draft
    message = record_initial_message(
        session, run_id=decision_run_id, company_id=row.company_id, contact_id=None,
        subject=row.subject, body=row.body, resume_path=row.resume_path,
        pairing_reason=row.pairing_reason, posting_version_id=posting_version_id,
        to_email=row.to_email, pattern_name=None, legacy_draft_id=row.id,
        recipient_evidence=validate_generic_recipient_evidence(recipient_evidence),
    )
    if row.gmail_draft_id:
        record_legacy_gmail_draft(
            session, draft_id=row.id, gmail_draft_id=row.gmail_draft_id,
            gmail_thread_id=row.gmail_thread_id or "",
        )
    return row


def retarget_draft_to_contact(
    session: Session, *, draft_id: int, contact_id: int,
) -> OutreachDraft:
    """Retarget an unsent company-inbox draft to a stored company contact.

    This is intentionally local-only.  It preserves a Gmail draft receipt so
    the next explicit ``push_draft`` updates that same Gmail Draft in place.
    The existing run-ledger message and first delivery attempt are updated in
    place as well; a recipient correction must not manufacture a second
    immutable message for the same legacy draft.
    """
    draft = session.get(OutreachDraft, draft_id)
    if draft is None:
        raise InvalidDraft(f"no outreach_drafts row with id {draft_id}")
    if draft.status == STATUS_SENT:
        raise InvalidDraft(f"draft {draft.id} was already sent")
    if draft.company_id is None:
        raise InvalidDraft("retargeting requires a company-scoped draft")

    contact = session.get(Contact, contact_id)
    if contact is None:
        raise InvalidDraft(f"no contacts row with id {contact_id}")
    if contact.company_id != draft.company_id:
        raise InvalidDraft("contact must belong to the draft's company")
    email = normalize_email(contact.email_guess or "")
    if not email or "@" not in email:
        raise InvalidDraft("contact requires a usable email address")

    collision = session.execute(
        select(OutreachDraft).where(
            OutreachDraft.to_email == email,
            OutreachDraft.id != draft.id,
        )
    ).scalar_one_or_none()
    if collision is not None:
        raise InvalidDraft(
            f"another outreach draft already addresses {email} (id {collision.id})"
        )

    message = session.execute(
        select(OutreachMessage).where(OutreachMessage.legacy_draft_id == draft.id)
    ).scalar_one_or_none()
    attempts: list[OutreachDeliveryAttempt] = []
    if message is not None:
        conflicting_message = session.execute(
            select(OutreachMessage).where(
                OutreachMessage.run_id == message.run_id,
                OutreachMessage.contact_id == contact.id,
                OutreachMessage.message_kind == "initial",
                OutreachMessage.id != message.id,
            )
        ).scalar_one_or_none()
        if conflicting_message is not None:
            raise InvalidDraft(
                f"run already has an initial outreach message for contact {contact.id}"
            )
        attempts = list(session.execute(
            select(OutreachDeliveryAttempt).where(
                OutreachDeliveryAttempt.message_id == message.id,
            )
        ).scalars())
        if message.state == STATUS_SENT or any(
            attempt.state in {STATUS_SENT, "presumed_delivered", "replied", "bounced"}
            for attempt in attempts
        ):
            raise InvalidDraft(f"draft {draft.id} has already-sent delivery evidence")

    old_email = draft.to_email
    draft.to_email = email
    draft.to_name = contact.full_name
    draft.recipient_title = contact.title
    draft.recipient_tier = contact.seniority_tier
    draft.contact_id = contact.id
    draft.prospect_id = None
    draft.collision_risk = bool(contact.name_collision_risk)
    draft.status = STATUS_DRAFTED

    if message is not None:
        message.contact_id = contact.id
        message.company_id = contact.company_id
        message.state = "held"
        for attempt in attempts:
            prior_evidence = attempt.evidence
            attempt.to_email = email
            attempt.domain = email.partition("@")[2]
            attempt.pattern_name = None
            attempt.state = "held"
            attempt.evidence = {
                "recipient_kind": "stored_contact",
                "contact_id": contact.id,
                "linkedin_url": contact.linkedin_url,
                "retargeted_from": {
                    "to_email": old_email,
                    "recipient_evidence": prior_evidence,
                },
            }

    session.flush()
    return draft


def pending_for_push(session: Session) -> list[OutreachDraft]:
    """Drafts ready to push to Gmail — drafted, not blocked."""
    return list(session.execute(
        select(OutreachDraft).where(OutreachDraft.status == STATUS_DRAFTED)
    ).scalars())


def collision_risk_drafts(session: Session) -> list[OutreachDraft]:
    """Drafts flagged collision_risk=True, for review — these ARE pushed
    like any other draft (ADR-0012), this just surfaces which ones to
    double-check before the candidate hits send."""
    return list(session.execute(
        select(OutreachDraft).where(OutreachDraft.collision_risk.is_(True))
    ).scalars())


def mark_pushed(session: Session, draft: OutreachDraft, *, gmail_draft_id: str, gmail_thread_id: str) -> OutreachDraft:
    # Legacy defensive check — no row is assigned STATUS_BLOCKED_COLLISION_RISK
    # going forward (ADR-0012); see push_draft's docstring in gmail.py.
    if draft.status == STATUS_BLOCKED_COLLISION_RISK:
        raise InvalidDraft(f"draft {draft.id} has the legacy blocked_collision_risk status")
    if draft.status == STATUS_SENT:
        raise InvalidDraft(f"draft {draft.id} was already sent")
    draft.status = STATUS_PUSHED
    draft.gmail_draft_id = gmail_draft_id
    draft.gmail_thread_id = gmail_thread_id
    session.flush()
    return draft


def mark_sent(session: Session, draft: OutreachDraft, *, sent_body: str, sent_at) -> OutreachDraft:
    draft.status = STATUS_SENT
    draft.sent_body = sent_body
    draft.sent_at = sent_at
    session.flush()
    return draft
