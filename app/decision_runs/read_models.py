"""Shared read models for Decision Run progress consumers."""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
from sqlalchemy.orm import Session

from app.decision_runs.progress import StageName, read_run_progress


def _planned_stage_row(stage: StageName) -> dict:
    """Project not-yet-reached work without inventing durable counts."""
    return {
        "stage": stage.value,
        "status": "pending",
        "processed": None,
        "expected": None,
        "input": None,
        "advanced": None,
        "dropped": None,
        "failed": None,
        "pending": None,
        "reason_counts": {},
        "attempt": 0,
        "attempt_history": [],
        "started_at": None,
        "updated_at": None,
        "ended_at": None,
        "reason": "waiting for preceding stage",
        "failed_record_dispositions": [],
        "waiting_started_at": None,
        "waiting_seconds": None,
        "active_elapsed_seconds": None,
        "selected_companies": None,
        "available_companies": None,
        "selected_jobs": None,
        "telemetry": "complete",
        "projected_interruption": False,
        "elapsed_seconds": None,
    }


def decision_run_progress(
    session: Session, run_id: str, *, now: datetime | None = None,
) -> pd.DataFrame:
    """Return durable stage progress for dashboards and completion reports."""
    timestamp = now or datetime.now(timezone.utc)
    progress = read_run_progress(session, run_id, now=timestamp)
    durable_rows = [{
        "stage": stage.name,
        "status": stage.status,
        "processed": stage.processed_count,
        "expected": stage.expected_count,
        "input": stage.counts.input,
        "advanced": stage.counts.advanced,
        "dropped": stage.counts.dropped,
        "failed": stage.counts.failed,
        "pending": stage.counts.pending,
        "reason_counts": stage.reason_counts,
        "attempt": stage.attempt_number,
        "attempt_history": [
            {
                "attempt": attempt.attempt_number,
                "status": attempt.status,
                "input": attempt.counts.input,
                "advanced": attempt.counts.advanced,
                "dropped": attempt.counts.dropped,
                "failed": attempt.counts.failed,
                "pending": attempt.counts.pending,
                "reason_counts": attempt.reason_counts,
                "failed_record_dispositions": [
                    {"record_type": item.record_type, "record_id": item.record_id,
                     "disposition": item.disposition.value}
                    for item in attempt.failed_record_dispositions
                ],
                "reason": attempt.reason,
                "started_at": attempt.started_at,
                "heartbeat_at": attempt.heartbeat_at,
                "ended_at": attempt.ended_at,
                "records": [record.__dict__ for record in attempt.records],
            }
            for attempt in stage.attempts
        ],
        "started_at": stage.started_at,
        "updated_at": stage.updated_at,
        "ended_at": stage.ended_at,
        "reason": stage.reason,
        "failed_record_dispositions": [
            {"record_type": item.record_type, "record_id": item.record_id,
             "disposition": item.disposition.value}
            for item in stage.failed_record_dispositions
        ],
        "waiting_started_at": stage.waiting_started_at,
        "waiting_seconds": stage.waiting_seconds,
        "active_elapsed_seconds": stage.active_elapsed_seconds,
        "selected_companies": stage.selected_company_count,
        "available_companies": stage.available_company_count,
        "selected_jobs": stage.selected_job_count,
        "telemetry": "complete",
        "projected_interruption": stage.projected_interruption,
        "elapsed_seconds": max(
            0.0, ((stage.ended_at or timestamp) - stage.started_at).total_seconds(),
        ) if stage.started_at else None,
    } for stage in progress.stages]
    if progress.legacy_telemetry:
        rows = durable_rows
    else:
        by_stage = {row["stage"]: row for row in durable_rows}
        rows = [
            by_stage.get(stage.value, _planned_stage_row(stage))
            for stage in StageName
        ]
    if not rows:
        rows.append({
            "stage": "—", "status": "unavailable", "processed": None,
            "expected": None, "input": None, "advanced": None,
            "dropped": None, "failed": None, "pending": None,
            "reason_counts": {}, "attempt": None, "attempt_history": [],
            "started_at": None, "updated_at": None, "ended_at": None,
            "reason": None, "telemetry": "incomplete legacy telemetry",
            "failed_record_dispositions": [], "waiting_started_at": None,
            "waiting_seconds": None, "active_elapsed_seconds": None,
            "selected_companies": None, "available_companies": None,
            "selected_jobs": None, "projected_interruption": False,
            "elapsed_seconds": None,
        })
    return pd.DataFrame(rows)
