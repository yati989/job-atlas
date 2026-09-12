"""Portable read model for the agent-driven mock-interview skill."""
from __future__ import annotations

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models.orm import Company, Job, JobSkill


def _iso(value):
    return value.isoformat() if value is not None else None


def find_interview_jobs(
    session: Session, search: str, *, limit: int = 20,
) -> list[dict[str, object]]:
    """Return bounded stored-job choices without assuming a SQL dialect."""
    needle = f"%{search.strip().lower()}%"
    company_name = func.coalesce(Company.name, Job.company_name_raw)
    rows = session.execute(
        select(Job, company_name.label("resolved_company_name"))
        .outerjoin(Company, Company.id == Job.company_id)
        .where(or_(
            func.lower(Job.title).like(needle),
            func.lower(company_name).like(needle),
        ))
        .order_by(Job.posted_at.desc().nullslast(), Job.id.desc())
        .limit(limit)
    ).all()
    return [{
        "job_id": job.id,
        "title": job.title,
        "company_name": resolved_company_name,
        "source": job.source,
        "posted_at": _iso(job.posted_at),
    } for job, resolved_company_name in rows]


def load_interview_context(session: Session, job_id: int) -> dict[str, object]:
    """Return all stored evidence needed to compose a self-contained prompt."""
    row = session.execute(
        select(Job, Company)
        .outerjoin(Company, Company.id == Job.company_id)
        .where(Job.id == job_id)
    ).one_or_none()
    if row is None:
        raise ValueError(f"unknown job id: {job_id}")
    job, company = row
    skills: dict[str, list[str]] = {"hard": [], "soft": []}
    for skill in session.scalars(
        select(JobSkill).where(JobSkill.job_id == job_id)
        .order_by(JobSkill.skill_type, JobSkill.skill)
    ):
        skills.setdefault(skill.skill_type, []).append(skill.skill)
    return {
        "job": {
            "id": job.id,
            "title": job.title,
            "company_name": company.name if company else job.company_name_raw,
            "source": job.source,
            "posted_at": _iso(job.posted_at),
            "seniority": job.seniority,
            "employment_type": job.employment_type,
            "description": job.description_raw,
            "experience_min_years": job.experience_min_years,
            "experience_max_years": job.experience_max_years,
            "education_requirement": job.education_requirement,
            "qualification_other": job.qualification_other,
        },
        "company": {
            "name": company.name if company else job.company_name_raw,
            "industry": company.industry if company else None,
            "pain_points": company.pain_points if company else None,
            "description": company.description if company else None,
            "employee_count_range": company.employee_count_range if company else None,
        },
        "skills": skills,
    }
