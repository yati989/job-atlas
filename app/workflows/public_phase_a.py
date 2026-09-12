"""Public SQLite seam for agent-owned pre-selection Phase A evidence."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.contacts.company_profile import record_profile_discovery_company_profile
from app.jobs.screening_facts import record_screening_facts
from app.models.orm import (
    Company, DecisionRun, Job, JobPostingVersion, JobSkill,
    ProfiledSearchOutcome, PublicJobPhaseAEvidence,
)
from app.workflows.planning import SearchProfile
from app.contacts.company_profile import search_group_for_profession


class PublicJobPhaseAResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    posting_version_id: int
    status: Literal["done", "no_description"]
    experience_min_years: int | None = Field(default=None, ge=0, le=80)
    experience_max_years: int | None = Field(default=None, ge=0, le=80)
    education_requirement: str | None = None
    qualification_other: str | None = None
    hard_skills: tuple[str, ...] = ()
    soft_skills: tuple[str, ...] = ()
    salary: dict | None = None
    minimum_experience: dict | None = None
    maximum_experience: dict | None = None

    @model_validator(mode="after")
    def coherent(self):
        if (
            self.experience_min_years is not None
            and self.experience_max_years is not None
            and self.experience_min_years > self.experience_max_years
        ):
            raise ValueError("minimum experience cannot exceed maximum experience")
        if self.status == "no_description" and any((
            self.experience_min_years is not None,
            self.experience_max_years is not None,
            self.education_requirement,
            self.qualification_other,
            self.hard_skills,
            self.soft_skills,
            self.salary,
            self.minimum_experience,
            self.maximum_experience,
        )):
            raise ValueError("no_description cannot contain extracted evidence")
        return self


class PublicCompanyPhaseAResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    company_id: int
    industry: str | None = None
    employee_count_range: str | None = None
    founding_year: int | None = Field(default=None, ge=1600, le=2200)
    description: str | None = None
    pain_points: str | None = None
    company_type: Literal["employer", "staffing"] = "employer"
    canonical_domain: str | None = None
    search_groups: tuple[str, ...] = ()


def _phase_a_rows(session: Session, run_id: str):
    return list(session.execute(
        select(ProfiledSearchOutcome, JobPostingVersion, Job)
        .join(JobPostingVersion, ProfiledSearchOutcome.posting_version_id == JobPostingVersion.id)
        .join(Job, JobPostingVersion.job_id == Job.id)
        .where(
            ProfiledSearchOutcome.run_id == run_id,
            (
                (ProfiledSearchOutcome.outcome == "kept")
                | (
                    (ProfiledSearchOutcome.outcome == "needs_review")
                    & (ProfiledSearchOutcome.first_failed_axis == "experience")
                )
            ),
        )
        .order_by(JobPostingVersion.id)
    ))


def _context_fingerprint(jobs: list[dict], companies: list[dict]) -> str:
    return hashlib.sha256(json.dumps(
        {"jobs": jobs, "companies": companies},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()).hexdigest()


def phase_a_context(session: Session, run_id: str) -> dict[str, object]:
    """Return compact exact-run inputs plus reusable completion state."""
    rows = _phase_a_rows(session, run_id)
    jobs: list[dict] = []
    reused_jobs: list[dict] = []
    company_jobs: dict[int, list[dict]] = {}
    for _outcome, version, job in rows:
        snapshot = version.snapshot if isinstance(version.snapshot, dict) else {}
        job_item = {
            "posting_version_id": version.id,
            "job_id": job.id,
            "title": snapshot.get("title") or job.title,
            "description_raw": snapshot.get("description_raw") or job.description_raw,
            "salary_raw": snapshot.get("salary_raw") or job.salary_raw,
            "enrichment_status": job.enrichment_status,
        }
        version_evidence = session.scalar(select(PublicJobPhaseAEvidence).where(
            PublicJobPhaseAEvidence.posting_version_id == version.id,
        ))
        if version_evidence is not None:
            job_item["enrichment_status"] = version_evidence.status
            reused_jobs.append(job_item)
        else:
            jobs.append(job_item)
        if job.company_id is not None:
            company_jobs.setdefault(job.company_id, []).append({
                "posting_version_id": version.id,
                "title": snapshot.get("title") or job.title,
                "description_raw": snapshot.get("description_raw") or job.description_raw,
            })
    companies = []
    reused_companies = []
    for company_id, evidence in sorted(company_jobs.items()):
        company = session.get(Company, company_id)
        company_item = {
            "company_id": company_id,
            "name": company.name if company else None,
            "enrichment_status": company.enrichment_status if company else "missing",
            "profile_groups": company.contact_search_groups if company else None,
            "jobs": evidence,
        }
        newest_posting = max(
            (job.last_seen_at for _outcome, _version, job in rows if job.company_id == company_id),
            default=None,
        )
        reusable = bool(
            company
            and company.enrichment_status == "done"
            and company.contact_search_groups is not None
            and company.enriched_at is not None
            and (newest_posting is None or company.enriched_at >= newest_posting)
        )
        (reused_companies if reusable else companies).append(company_item)
    return {
        "context_fingerprint": _context_fingerprint(jobs, companies),
        "jobs": jobs,
        "companies": companies,
        "reused_jobs": reused_jobs,
        "reused_companies": reused_companies,
    }


def record_job_phase_a(
    session: Session, run_id: str, result: PublicJobPhaseAResult | dict,
) -> Job:
    result = PublicJobPhaseAResult.model_validate(result)
    match = next(
        ((outcome, version, job) for outcome, version, job in _phase_a_rows(session, run_id)
         if version.id == result.posting_version_id),
        None,
    )
    if match is None:
        raise ValueError("job Phase A result is outside the exact kept run scope")
    outcome, version, job = match
    snapshot = version.snapshot if isinstance(version.snapshot, dict) else {}
    description = snapshot.get("description_raw") or job.description_raw
    if result.status == "no_description" and description:
        raise ValueError("a posting with description evidence cannot be no_description")
    evidence_snapshot = result.model_dump(mode="json", exclude={"posting_version_id"})
    existing_evidence = session.scalar(select(PublicJobPhaseAEvidence).where(
        PublicJobPhaseAEvidence.posting_version_id == version.id,
    ))
    if existing_evidence is not None:
        if existing_evidence.evidence != evidence_snapshot:
            raise ValueError("posting version already has different Phase A evidence")
        return job

    job.enrichment_status = result.status
    job.enriched_at = datetime.now(timezone.utc)
    if result.status == "done":
        job.experience_min_years = result.experience_min_years
        job.experience_max_years = result.experience_max_years
        job.education_requirement = result.education_requirement
        job.qualification_other = result.qualification_other
        existing = {(row.skill.casefold(), row.skill_type) for row in job.skills}
        for skill_type, skills in (("hard", result.hard_skills), ("soft", result.soft_skills)):
            for skill in dict.fromkeys(item.strip() for item in skills if item.strip()):
                if (skill.casefold(), skill_type) not in existing:
                    session.add(JobSkill(job_id=job.id, skill=skill, skill_type=skill_type))
                    existing.add((skill.casefold(), skill_type))
        record_screening_facts(
            session,
            posting_version_id=version.id,
            salary=result.salary,
            minimum_experience=result.minimum_experience,
            maximum_experience=result.maximum_experience,
        )
    session.add(PublicJobPhaseAEvidence(
        posting_version_id=version.id,
        status=result.status,
        evidence=evidence_snapshot,
    ))
    if outcome.outcome == "needs_review" and outcome.first_failed_axis == "experience":
        run = session.get(DecisionRun, run_id)
        profile = SearchProfile.model_validate(run.policy_snapshot)
        required_min = result.experience_min_years
        required_max = result.experience_max_years
        if required_min is None and required_max is None:
            outcome.reason = "experience requirement remains unknown after Phase A"
        elif (
            profile.experience.maximum_years is not None
            and required_min is not None
            and required_min > profile.experience.maximum_years
        ):
            outcome.outcome = "rejected"
            outcome.reason = "minimum required experience exceeds profile"
        elif (
            profile.experience.minimum_years is not None
            and required_max is not None
            and required_max < profile.experience.minimum_years
        ):
            outcome.outcome = "rejected"
            outcome.reason = "maximum required experience is below profile"
        else:
            outcome.outcome = "kept"
            outcome.first_failed_axis = None
            outcome.reason = None
    session.flush()
    return job


def record_company_phase_a(
    session: Session, run_id: str, result: PublicCompanyPhaseAResult | dict,
) -> Company:
    result = PublicCompanyPhaseAResult.model_validate(result)
    company_ids = {job.company_id for _outcome, _version, job in _phase_a_rows(session, run_id)}
    if result.company_id not in company_ids:
        raise ValueError("company Phase A result is outside the exact kept run scope")
    company = session.get(Company, result.company_id)
    company.industry = result.industry
    company.employee_count_range = result.employee_count_range
    company.founding_year = result.founding_year
    company.description = result.description
    company.pain_points = result.pain_points
    company.enrichment_status = "done"
    company.enriched_at = datetime.now(timezone.utc)
    record_profile_discovery_company_profile(
        session,
        company.id,
        company_type=result.company_type,
        search_groups=list(result.search_groups),
        canonical_domain=result.canonical_domain,
    )
    session.flush()
    return company


def apply_phase_a_results(
    session: Session, run_id: str, payload: dict,
) -> dict[str, int]:
    if set(payload) != {"context_fingerprint", "jobs", "companies"}:
        raise ValueError(
            "Phase A payload must contain exactly context_fingerprint, jobs, and companies"
        )
    expected = phase_a_context(session, run_id)
    supplied_fingerprint = payload["context_fingerprint"]
    if (
        not isinstance(supplied_fingerprint, str)
        or supplied_fingerprint != expected["context_fingerprint"]
    ):
        raise ValueError("Phase A context is stale; fetch fresh context and retry")
    jobs = [PublicJobPhaseAResult.model_validate(item) for item in payload["jobs"]]
    companies = [PublicCompanyPhaseAResult.model_validate(item) for item in payload["companies"]]
    job_ids = [item.posting_version_id for item in jobs]
    company_ids = [item.company_id for item in companies]
    if len(set(job_ids)) != len(job_ids) or len(set(company_ids)) != len(company_ids):
        raise ValueError("Phase A payload contains duplicate job or company results")
    expected_job_ids = {item["posting_version_id"] for item in expected["jobs"]}
    expected_company_ids = {item["company_id"] for item in expected["companies"]}
    if set(job_ids) != expected_job_ids:
        raise ValueError(
            f"Phase A job coverage mismatch: expected {sorted(expected_job_ids)}, got {sorted(job_ids)}"
        )
    if set(company_ids) != expected_company_ids:
        raise ValueError(
            "Phase A company coverage mismatch: expected "
            f"{sorted(expected_company_ids)}, got {sorted(company_ids)}"
        )
    run = session.get(DecisionRun, run_id)
    profile = SearchProfile.model_validate(run.policy_snapshot)
    allowed_groups = {search_group_for_profession(item) for item in profile.professions}
    for item in companies:
        unknown = set(item.search_groups) - allowed_groups
        if unknown:
            raise ValueError(
                "company search groups are outside the frozen professions: "
                + ", ".join(sorted(unknown))
            )
    for item in jobs:
        record_job_phase_a(session, run_id, item)
    for item in companies:
        record_company_phase_a(session, run_id, item)
    return {"jobs_recorded": len(jobs), "companies_recorded": len(companies)}
