"""
Persistence for DB-sourced tailored resumes.

One current tailored resume per job: `upsert_tailored_resume` updates the
existing row for a `job_id` rather than inserting a duplicate, so re-running the
tailoring pass is idempotent — the same discipline the ingestion pipeline holds
for jobs/companies in `app.pipeline.upsert`.

Ad-hoc runs (a JD not in the DB) are intentionally *not* persisted through this
module — they return their artifacts to the caller instead.
"""
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.models.orm import TailoredResume


def upsert_tailored_resume(
    session: Session,
    *,
    job_id: int,
    score: Optional[float] = None,
    gap_report: Optional[dict[str, Any]] = None,
    tailored_yaml: Optional[dict[str, Any]] = None,
    artifact_dir: Optional[str] = None,
    jd_hash: Optional[str] = None,
    jd_snapshot: Optional[str] = None,
) -> TailoredResume:
    """Insert or update the single tailored resume for `job_id`.

    Returns the persisted row. Caller controls the transaction (commit/rollback)
    so a batch can roll back one bad item without cascading — mirroring
    `app.pipeline.upsert`.
    """
    row = session.query(TailoredResume).filter_by(job_id=job_id).one_or_none()
    if row is None:
        row = TailoredResume(job_id=job_id)
        session.add(row)

    row.score = score
    row.gap_report = gap_report
    row.tailored_yaml = tailored_yaml
    row.artifact_dir = artifact_dir
    row.jd_hash = jd_hash
    row.jd_snapshot = jd_snapshot
    session.flush()
    return row
