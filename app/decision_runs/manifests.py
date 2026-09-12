"""Exact-ID adapters for agent-driven stages of an approved decision run.

These functions deliberately return data, not perform agent judgement.  They
are the seam each skill crosses so it cannot accidentally fall back to a
mutable status queue or timestamp query.
"""
from dataclasses import dataclass
from enum import Enum
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.orm import (Company, Contact, DecisionRunApproval, DecisionRunSelectedJob, Job,
                            JobPostingVersion, OutreachDeliveryAttempt, OutreachMessage)
from app.outreach.pairing import (PairingResult, REASON_NO_MATCHING_JOB,
                                  REASON_NO_JOBS_AT_COMPANY,
                                  REASON_RECIPIENT_FUNCTION_MATCH,
                                  REASON_RECIPIENT_FUNCTION_UNKNOWN,
                                  REASON_RECRUITER_BEST_MATCH,
                                  recipient_search_group)
from app.config.categories import classify_title
from app.contacts.ladder import CATEGORY_TO_SEARCH_GROUP, TALENT_ACQUISITION

NO_CONTACT_SELECTION = "none"
NO_CATEGORY_MATCH = "no_category_match"
STAFFING_RECRUITER = "staffing_recruiter"


class ContactObligationKind(str, Enum):
    SEARCH = "search"
    EXCLUDED = "excluded"
    NO_MATCH = "no_match"


class ContactFunction(str, Enum):
    """Real searchable functions; persistence sentinels are deliberately absent."""
    DATA_AI = "data_ai"
    CREDIT_RISK = "credit_risk"
    STAFFING_RECRUITER = "staffing_recruiter"


def contact_function(value: str | ContactFunction) -> ContactFunction:
    """Typed compatibility boundary rejecting manifest-only sentinels."""
    try:
        return ContactFunction(value)
    except ValueError as exc:
        raise ValueError(f"{value!r} is not a real contact-search function") from exc


@dataclass(frozen=True)
class ApprovedContactObligation:
    company_id: int
    kind: ContactObligationKind
    search_groups: tuple[ContactFunction, ...]
    reason: str | None = None


def enrich_jobs_for_versions(session: Session, posting_version_ids: list[int]) -> list[JobPostingVersion]:
    return list(session.execute(select(JobPostingVersion).where(JobPostingVersion.id.in_(posting_version_ids)).order_by(JobPostingVersion.id)).scalars())


def enrich_companies_for_run(session: Session, company_ids: list[int], *, phase_b: str = "never_attempted") -> list[Company]:
    if phase_b != "never_attempted":
        raise ValueError("approved runs only support phase_b='never_attempted'; explicit refresh is separate")
    return list(session.execute(select(Company).where(Company.id.in_(company_ids)).order_by(Company.id)).scalars())


def phase_b_never_attempted_company_ids(session: Session, company_ids: list[int], *, source: str) -> list[int]:
    """Return only the approved source/company pairs without prior evidence.

    `missing`, terminal errors, and successful evidence are all durable prior
    attempts.  A later refresh must opt into a separate command rather than
    silently retrying them in a decision run.
    """
    rows = enrich_companies_for_run(session, company_ids)
    result = []
    for company in rows:
        evidence = company.market_profile_evidence or {}
        sources = evidence.get("sources", {}) if isinstance(evidence, dict) else {}
        if source not in sources:
            result.append(company.id)
    return result


def selected_jobs(session: Session, run_id: str, company_id: int) -> list[Job]:
    return list(session.execute(
        select(Job).join(JobPostingVersion, JobPostingVersion.job_id == Job.id).join(
            DecisionRunSelectedJob, DecisionRunSelectedJob.posting_version_id == JobPostingVersion.id
        ).where(DecisionRunSelectedJob.run_id == run_id, DecisionRunSelectedJob.company_id == company_id).order_by(Job.id)
    ).scalars())


def pair_for_approved_scope(session: Session, run_id: str, contact_id: int) -> PairingResult:
    contact = session.get(Contact, contact_id)
    if contact is None: raise ValueError(f"unknown contact {contact_id}")
    jobs = selected_jobs(session, run_id, contact.company_id)
    if not jobs: return PairingResult("master", REASON_NO_JOBS_AT_COMPANY)
    if contact.seniority_tier == TALENT_ACQUISITION:
        return PairingResult("tailored", REASON_RECRUITER_BEST_MATCH, candidate_job_ids=[j.id for j in jobs])
    group = recipient_search_group(contact.title)
    if group is None: return PairingResult("master", REASON_RECIPIENT_FUNCTION_UNKNOWN)
    matching = [j for j in jobs if CATEGORY_TO_SEARCH_GROUP.get(classify_title(j.title) or "") == group]
    if not matching: return PairingResult("master", REASON_NO_MATCHING_JOB, search_group=group)
    return PairingResult("tailored", REASON_RECIPIENT_FUNCTION_MATCH, search_group=group, candidate_job_ids=[j.id for j in matching])


def approved_contact_obligation(session: Session, run_id: str,
                                company_id: int) -> ApprovedContactObligation:
    """Normalize approval choice and derived functions in one typed place."""
    company = session.get(Company, company_id)
    if company is None: raise ValueError(f"unknown company {company_id}")
    approval = session.execute(select(DecisionRunApproval).where(DecisionRunApproval.run_id == run_id)).scalar_one_or_none()
    rules = approval.normalized_rules if approval is not None and isinstance(approval.normalized_rules, dict) else {}
    selections = rules.get("contact_search_selections", {})
    if isinstance(selections, dict) and str(company_id) in selections:
        from app.decision_runs.workbook_approval import groups_for_selection
        explicit = groups_for_selection(selections[str(company_id)])
        if explicit == []:
            return ApprovedContactObligation(company_id, ContactObligationKind.EXCLUDED,
                                              (), "no_search_selection")
        if explicit is not None and company.company_type != "staffing":
            return ApprovedContactObligation(company_id, ContactObligationKind.SEARCH,
                                              tuple(contact_function(group) for group in explicit))
    if company.company_type == "staffing":
        return ApprovedContactObligation(company_id, ContactObligationKind.SEARCH,
                                          (ContactFunction.STAFFING_RECRUITER,))
    groups = {CATEGORY_TO_SEARCH_GROUP.get(classify_title(job.title) or "") for job in selected_jobs(session, run_id, company_id)}
    normalized = tuple(sorted(group for group in groups if group))
    if not normalized:
        return ApprovedContactObligation(company_id, ContactObligationKind.NO_MATCH,
                                          (), "no_category_match")
    return ApprovedContactObligation(company_id, ContactObligationKind.SEARCH,
                                      tuple(contact_function(group) for group in normalized))


def approved_contact_search_groups(session: Session, run_id: str, company_id: int) -> list[str]:
    """Compatibility adapter for callers needing only searchable functions."""
    return [group.value for group in approved_contact_obligation(
        session, run_id, company_id,
    ).search_groups]


def reusable_contact_ids(session: Session, run_id: str, company_id: int) -> list[int]:
    """Valid existing contacts not already successfully cold-contacted."""
    contacts = session.execute(select(Contact).where(Contact.company_id == company_id,
        Contact.email_guess.is_not(None), or_(Contact.email_verification_status.is_(None), Contact.email_verification_status != "invalid"))).scalars().all()
    reusable = []
    for contact in contacts:
        attempts = session.execute(select(OutreachDeliveryAttempt).join(OutreachMessage).where(
            OutreachMessage.contact_id == contact.id, OutreachMessage.message_kind == "initial",
            OutreachDeliveryAttempt.state.in_(("presumed_delivered", "replied")))).scalars().all()
        if not attempts: reusable.append(contact.id)
    return reusable
