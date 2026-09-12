"""Persistence seam used by the enrich-jobs skill; contains no policy logic."""
import hashlib
import json
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.models.orm import JobPostingVersion, JobScreeningFact

def record_screening_facts(session: Session, *, posting_version_id: int, salary: dict | None,
                           minimum_experience: dict | None, maximum_experience: dict | None,
                           extraction_version: str = "1") -> JobScreeningFact:
    version = session.get(JobPostingVersion, posting_version_id)
    if version is None:
        raise ValueError(f"unknown posting version {posting_version_id}")
    salary_raw = version.snapshot.get("salary_raw")
    if isinstance(salary_raw, str) and salary_raw.strip():
        if salary is None:
            raise ValueError(
                "salary disposition is required when the posting snapshot has salary_raw"
            )
        evidence = salary.get("evidence")
        unusable_reason = salary.get("unusable_reason")
        has_evidence = isinstance(evidence, str) and bool(evidence.strip())
        has_disposition = (
            salary.get("guaranteed_max_lpa") is not None
            or isinstance(unusable_reason, str) and bool(unusable_reason.strip())
        )
        if not has_evidence or not has_disposition:
            raise ValueError(
                "salary disposition must be evidence-backed and include either "
                "guaranteed_max_lpa or unusable_reason"
            )
    payload = {"salary": salary, "minimum": minimum_experience, "maximum": maximum_experience}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    row = session.execute(select(JobScreeningFact).where(JobScreeningFact.posting_version_id == posting_version_id, JobScreeningFact.extraction_version == extraction_version)).scalar_one_or_none()
    if row is None:
        row = JobScreeningFact(posting_version_id=posting_version_id, extraction_version=extraction_version)
        session.add(row)
    row.salary, row.minimum_experience, row.maximum_experience, row.evidence_hash = salary, minimum_experience, maximum_experience, digest
    session.flush(); return row
