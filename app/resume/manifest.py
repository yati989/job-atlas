"""Exact approved-scope selector; preserves standalone backlog selectors."""
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.models.orm import Job, JobPostingVersion

def select_jobs_needing_resume(session: Session, *, job_ids: list[int]) -> list[Job]:
    return list(session.execute(select(Job).where(Job.id.in_(job_ids)).order_by(Job.id)).scalars())

def select_versions_needing_resume(session: Session, *, posting_version_ids: list[int]) -> list[JobPostingVersion]:
    return list(session.execute(select(JobPostingVersion).where(JobPostingVersion.id.in_(posting_version_ids)).order_by(JobPostingVersion.id)).scalars())
