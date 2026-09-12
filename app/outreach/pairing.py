"""
Recipient <-> job pairing: which job (if any) is the right tailoring
evidence for a given recipient at a given company.

This is mechanics, not judgement — see references/tier-shapes.md in the
draft-outreach skill for the reasoning this module exists to protect. What
this module refuses to do is pick a job for a recipient whose function it
doesn't match: a credit-risk req tailored and mailed to a data-analytics lead
is worse than sending the plain master, which is the whole reason
`recipient_search_group` exists.

Reuses `classify_title` (app/config/categories.py) and
`CATEGORY_TO_SEARCH_GROUP` (app/contacts/ladder.py) rather than inventing a
second title vocabulary — the same classifier that already decides a JOB's
category is applied here to a PERSON's title, since both are free-text role
strings of the same shape.
"""
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config.categories import classify_title
from app.contacts.ladder import CATEGORY_TO_SEARCH_GROUP, TALENT_ACQUISITION
from app.models.orm import Job

REASON_RECIPIENT_FUNCTION_MATCH = "recipient_function_match"
REASON_RECRUITER_BEST_MATCH = "recruiter_best_match"
REASON_NO_MATCHING_JOB = "no_matching_job"
REASON_NO_JOBS_AT_COMPANY = "no_jobs_at_company"
REASON_COMPANY_IS_PROSPECT = "company_is_prospect"
REASON_RECIPIENT_FUNCTION_UNKNOWN = "recipient_function_unknown"

VALID_PAIRING_REASONS = frozenset({
    REASON_RECIPIENT_FUNCTION_MATCH, REASON_RECRUITER_BEST_MATCH,
    REASON_NO_MATCHING_JOB, REASON_NO_JOBS_AT_COMPANY,
    REASON_COMPANY_IS_PROSPECT, REASON_RECIPIENT_FUNCTION_UNKNOWN,
})

# The email subject line's domain word (draft-outreach's fixed template:
# "Data Scientist - <Domain> | Ex-...") is derived from the MATCHED JOB's
# category, not chosen by eye per recipient — deterministic, so it belongs
# here as code rather than left to the skill's judgement each time. A
# category with no listed domain (data_science itself) means the subject
# carries no " - <Domain>" suffix at all — "Data Scientist" alone already
# says it.
SUBJECT_DOMAIN_BY_CATEGORY: dict[str, str] = {
    "credit_risk": "Credit Risk",
    "analytics": "Analytics",
    "ai_ml_engineering": "Machine Learning",
    "quant_decision_science": "Decision Science",
    "data_engineering": "Data Engineering",
    # "data_science" deliberately absent — no suffix.
}


def subject_domain_for_job(job) -> str | None:
    """The subject line's " - <Domain>" suffix for a matched job, or None
    when the job's own category needs no suffix (data_science) or didn't
    classify at all. `job` is anything with a `.title` — same duck-typing
    convention as app.pipeline.relevance's predicates.

    Inherits classify_title()'s own documented ambiguity: a title matching
    more than one CATEGORY_KEYWORDS family returns the FIRST in declaration
    order. "Machine Learning Engineer" contains data_science's "machine
    learning" keyword, so it classifies data_science (-> no suffix) rather
    than ai_ml_engineering (-> "Machine Learning"), even though a human
    would call it an ML engineering role. Not something to special-case
    here — this function must agree with the one classifier the rest of
    the pipeline (job relevance, contact ladder) already uses, not invent a
    second, subtly different one."""
    category = classify_title(job.title)
    if category is None:
        return None
    return SUBJECT_DOMAIN_BY_CATEGORY.get(category)

# Reasons under which resume_kind must be "master" rather than "tailored" —
# every other reason implies a candidate job was found.
MASTER_REASONS = frozenset({
    REASON_NO_MATCHING_JOB, REASON_NO_JOBS_AT_COMPANY,
    REASON_COMPANY_IS_PROSPECT, REASON_RECIPIENT_FUNCTION_UNKNOWN,
})


def recipient_search_group(recipient_title: str | None) -> str | None:
    """Which search group (data_ai|credit_risk) a recipient's own title
    belongs to, or None if the title doesn't classify. Empty/missing titles
    always return None — there is nothing to match without one."""
    if not recipient_title or not recipient_title.strip():
        return None
    category = classify_title(recipient_title)
    if category is None:
        return None
    return CATEGORY_TO_SEARCH_GROUP.get(category)


def _jobs_at_company(session: Session, company_id: int) -> list[Job]:
    """Every job at a company, enriched jobs first (they carry decomposed
    requirements the tailoring skill reasons over) then most recent — a
    ranking hint, not a verdict. The skill picks the actual job_id from
    whatever this returns; it is not auto-selected here."""
    rows = session.execute(
        select(Job).where(Job.company_id == company_id, Job.duplicate_of_job_id.is_(None))
    ).scalars().all()
    return sorted(
        rows,
        key=lambda j: (j.enrichment_status != "done", j.posted_at is None, j.posted_at),
        reverse=False,
    )


@dataclass
class PairingResult:
    resume_kind: str  # "tailored" | "master"
    reason: str
    search_group: str | None = None
    candidate_job_ids: list[int] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.reason not in VALID_PAIRING_REASONS:
            raise ValueError(f"unknown pairing reason {self.reason!r}")
        expect_master = self.reason in MASTER_REASONS
        if expect_master and self.resume_kind != "master":
            raise ValueError(f"reason {self.reason!r} requires resume_kind='master'")
        if not expect_master and self.resume_kind != "tailored":
            raise ValueError(f"reason {self.reason!r} requires resume_kind='tailored'")


def pair(
    session: Session,
    *,
    company_id: int | None,
    recipient_tier: str | None,
    recipient_title: str | None,
) -> PairingResult:
    """Decide master-vs-tailored and, when tailored, the candidate job pool
    a job_id must come from.

    Three paths:
      - No company (a prospect, or a company we don't scrape jobs from) ->
        master. There is no job to be evidence for.
      - Recruiter (talent_acquisition) -> candidates are every enriched job
        at the company, regardless of function — recruiters route across
        every req, so the actual pick is "best match to the candidate",
        which is the skill's judgement, not this function's.
      - Anyone else -> candidates are restricted to jobs in the RECIPIENT'S
        OWN function. Empty or unclassifiable title -> master. This is the
        refusal that exists to stop a credit-risk resume reaching a data
        analytics lead.
    """
    if company_id is None:
        return PairingResult(resume_kind="master", reason=REASON_COMPANY_IS_PROSPECT)

    jobs = _jobs_at_company(session, company_id)
    if not jobs:
        return PairingResult(resume_kind="master", reason=REASON_NO_JOBS_AT_COMPANY)

    if recipient_tier == TALENT_ACQUISITION:
        enriched = [j for j in jobs if j.enrichment_status == "done"]
        pool = enriched or jobs
        return PairingResult(
            resume_kind="tailored", reason=REASON_RECRUITER_BEST_MATCH,
            search_group=None, candidate_job_ids=[j.id for j in pool],
        )

    group = recipient_search_group(recipient_title)
    if group is None:
        return PairingResult(resume_kind="master", reason=REASON_RECIPIENT_FUNCTION_UNKNOWN)

    matching = [
        j for j in jobs
        if CATEGORY_TO_SEARCH_GROUP.get(classify_title(j.title) or "") == group
    ]
    if not matching:
        return PairingResult(
            resume_kind="master", reason=REASON_NO_MATCHING_JOB, search_group=group,
        )

    return PairingResult(
        resume_kind="tailored", reason=REASON_RECIPIENT_FUNCTION_MATCH,
        search_group=group, candidate_job_ids=[j.id for j in matching],
    )
