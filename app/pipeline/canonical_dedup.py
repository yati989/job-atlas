"""Apply the repository's established cross-source job matching policy."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.orm import Job
from scripts.dedup_jobs import find_candidates


def _root(job: Job, by_id: dict[int, Job]) -> Job:
    seen: set[int] = set()
    current = job
    while current.duplicate_of_job_id is not None and current.id not in seen:
        seen.add(current.id)
        parent = by_id.get(current.duplicate_of_job_id)
        if parent is None:
            break
        current = parent
    return current


def apply_cross_source_job_dedup(session: Session) -> dict[str, int]:
    """Flag high-confidence duplicates without deleting any stored posting."""
    jobs = list(session.scalars(select(Job).where(
        Job.status == "active", Job.duplicate_of_job_id.is_(None),
    )))
    by_id = {job.id: job for job in jobs}
    auto_flag, review, skipped = find_candidates(jobs)
    flagged = 0
    for job_a, job_b, score in auto_flag:
        older, newer = (
            (job_a, job_b)
            if job_a.first_seen_at <= job_b.first_seen_at
            else (job_b, job_a)
        )
        canonical = _root(older, by_id)
        if newer.id == canonical.id or newer.duplicate_of_job_id is not None:
            continue
        newer.duplicate_of_job_id = canonical.id
        newer.duplicate_score = score
        flagged += 1
    session.flush()
    return {
        "scanned": len(jobs),
        "flagged": flagged,
        "review_candidates": len(review),
        "skipped_groups": len(skipped),
    }
