"""Small durable lifecycle seam for public post-collection stages."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.orm import DecisionRun, PublicWorkflowStage


STAGE_NAMES = (
    "role_review", "location_review", "phase_a", "phase_b", "selection", "tailoring",
    "profile_links", "export",
)
STATUSES = {
    "not_requested", "awaiting_confirmation", "running", "partial",
    "failed", "completed", "skipped",
}


def initialize_public_stages(session: Session, run_id: str) -> None:
    existing = set(session.scalars(select(PublicWorkflowStage.name).where(
        PublicWorkflowStage.run_id == run_id,
    )))
    session.add_all(PublicWorkflowStage(
        run_id=run_id, name=name, status="not_requested",
        expected_count=0, completed_count=0, failed_count=0, detail={},
    ) for name in STAGE_NAMES if name not in existing)
    session.flush()


def record_public_stage(
    session: Session,
    run_id: str,
    name: str,
    status: str,
    *,
    expected_count: int = 0,
    completed_count: int = 0,
    failed_count: int = 0,
    detail: dict | None = None,
) -> PublicWorkflowStage:
    if session.get(DecisionRun, run_id) is None:
        raise ValueError(f"unknown guided run: {run_id}")
    if name not in STAGE_NAMES or status not in STATUSES:
        raise ValueError("unknown public stage name or status")
    if min(expected_count, completed_count, failed_count) < 0:
        raise ValueError("stage counts cannot be negative")
    if completed_count + failed_count > expected_count:
        raise ValueError("terminal stage counts exceed expected count")
    row = session.scalar(select(PublicWorkflowStage).where(
        PublicWorkflowStage.run_id == run_id,
        PublicWorkflowStage.name == name,
    ))
    if row is None:
        row = PublicWorkflowStage(run_id=run_id, name=name)
        session.add(row)
    row.status = status
    row.expected_count = expected_count
    row.completed_count = completed_count
    row.failed_count = failed_count
    row.detail = detail or {}
    row.updated_at = datetime.now(timezone.utc)
    session.flush()
    return row


def public_stage_snapshot(session: Session, run_id: str) -> list[dict]:
    initialize_public_stages(session, run_id)
    rows = list(session.scalars(select(PublicWorkflowStage).where(
        PublicWorkflowStage.run_id == run_id,
    ).order_by(PublicWorkflowStage.id)))
    return [{
        "name": row.name,
        "status": row.status,
        "expected_count": row.expected_count,
        "completed_count": row.completed_count,
        "failed_count": row.failed_count,
        "detail": row.detail,
        "updated_at": row.updated_at,
    } for row in rows]
