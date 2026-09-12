"""
Persist a `ContactFindResult` (from `pipeline.find_contacts_for_company`)
into the database: upserts `Contact` rows deduplicated by
`(company_id, linkedin_url)`, and updates the owning `Company`'s
contact-enrichment status and timestamp. Mirrors
`app.pipeline.upsert`'s upsert-by-natural-key pattern for jobs.
"""
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.contacts.email_resolution import split_name
from app.contacts.people_search import ContactCandidate
from app.contacts.pipeline import ContactFindResult
from app.models.orm import Company, Contact

PUBLIC_SEARCH_SOURCE = "linkedin_public_search"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def upsert_contact(session: Session, company_id: int, candidate: ContactCandidate) -> Contact:
    stmt = select(Contact).where(
        Contact.company_id == company_id,
        Contact.linkedin_url == candidate.linkedin_url,
    )
    existing = session.execute(stmt).scalar_one_or_none()
    names = split_name(candidate.full_name)
    first_name, last_name = names if names else (None, None)

    if existing:
        existing.last_seen_at = utcnow()
        existing.title = candidate.title or existing.title
        existing.email_guess = candidate.email_guess or existing.email_guess
        existing.email_verification_status = (
            candidate.email_verification_status or existing.email_verification_status
        )
        existing.name_collision_risk = candidate.name_collision_risk
        existing.first_name = existing.first_name or first_name
        existing.last_name = existing.last_name or last_name
        return existing

    row = Contact(
        company_id=company_id,
        full_name=candidate.full_name,
        first_name=first_name,
        last_name=last_name,
        title=candidate.title,
        linkedin_url=candidate.linkedin_url,
        seniority_tier=candidate.seniority_tier,
        email_guess=candidate.email_guess,
        email_verification_status=candidate.email_verification_status,
        name_collision_risk=candidate.name_collision_risk,
        source=PUBLIC_SEARCH_SOURCE,
        first_seen_at=utcnow(),
        last_seen_at=utcnow(),
    )
    session.add(row)
    return row


def apply_contact_find_result(session: Session, company: Company, result: ContactFindResult) -> list[Contact]:
    """Upsert every contact in `result` and update `company`'s
    contact-enrichment bookkeeping. Returns the upserted Contact rows."""
    rows = [upsert_contact(session, company.id, candidate) for candidate in result.contacts]

    company.contact_enrichment_status = result.status
    company.contact_enriched_at = utcnow()

    return rows
