"""Durable, opted-in evidence for central relevance-gate rejections."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from app.db.session import get_session
from app.models.orm import RelevanceAuditObservation, RelevanceAuditRun
from app.pipeline.relevance import filter_relevant


def _json_safe(value: Any) -> Any:
    """Preserve nested connector evidence while making it JSON-column safe."""
    return json.loads(json.dumps(value, default=str))


def _job_snapshot(job: object) -> dict[str, Any]:
    posted_at = getattr(job, "posted_at", None)
    return _json_safe({
        "source": getattr(job, "source", None),
        "external_job_id": getattr(job, "external_job_id", None),
        "title": getattr(job, "title", None),
        "company_name_raw": getattr(job, "company_name_raw", None),
        "location_raw": getattr(job, "location_raw", None),
        "is_remote": getattr(job, "is_remote", None),
        "remote_scope": getattr(job, "remote_scope", None),
        "employment_type": getattr(job, "employment_type", None),
        "seniority": getattr(job, "seniority", None),
        "description_raw": getattr(job, "description_raw", None),
        "salary_raw": getattr(job, "salary_raw", None),
        "apply_url": getattr(job, "apply_url", None),
        "job_url": getattr(job, "job_url", None),
        "posted_at": posted_at.isoformat() if isinstance(posted_at, datetime) else None,
        "raw_payload": getattr(job, "raw_payload", None),
    })


def start_audit_run(
    *, run_id: str, since_at: datetime, command: str, instance_count: int
) -> None:
    with get_session() as session:
        session.add(RelevanceAuditRun(
            id=run_id,
            since_at=since_at,
            command=command,
            instance_count=instance_count,
            state="collecting",
        ))


def dropped_observation_mappings(
    results: list[object], *, audit_run_id: str, cutoff_at: datetime
) -> list[dict[str, Any]]:
    """Map every raw dropped occurrence before production deduplication."""
    observed_at = datetime.now(timezone.utc)
    mappings: list[dict[str, Any]] = []
    for result in results:
        dimensions = _json_safe(getattr(result, "dimensions", {}))
        for job in getattr(result, "jobs", None) or []:
            _, _, details = filter_relevant([job], cutoff_at=cutoff_at)
            if not details:
                continue
            detail = details[0]
            mappings.append({
                "audit_run_id": audit_run_id,
                "source": job.source,
                "external_job_id": job.external_job_id,
                "first_failed_axis": detail["axis"],
                "location_reason": detail.get("location_reason"),
                "dimensions": dimensions,
                "job_snapshot": _job_snapshot(job),
                "observed_at": observed_at,
            })
    return mappings


def record_dropped_observations(
    *, run_id: str, results: list[object], cutoff_at: datetime
) -> int:
    mappings = dropped_observation_mappings(
        results, audit_run_id=run_id, cutoff_at=cutoff_at
    )
    with get_session() as session:
        if mappings:
            session.bulk_insert_mappings(RelevanceAuditObservation, mappings)
        audit = session.get(RelevanceAuditRun, run_id)
        if audit is None:
            raise ValueError(f"Unknown relevance audit run: {run_id}")
        audit.drop_count = (audit.drop_count or 0) + len(mappings)
    return len(mappings)


def finish_audit_run(
    *, run_id: str, state: str, failure_detail: str | None = None
) -> None:
    with get_session() as session:
        audit = session.get(RelevanceAuditRun, run_id)
        if audit is None:
            raise ValueError(f"Unknown relevance audit run: {run_id}")
        audit.state = state
        audit.completed_at = datetime.now(timezone.utc)
        audit.failure_detail = failure_detail
