"""Attended Decision Run lifecycle actions around the shared progress seam.

This is deliberately an orchestration boundary: it durably reconciles stale
work before an attended command continues, and it records named approval
without making the read-only dashboard mutate state.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Callable

from sqlalchemy.orm import Session

from . import progress as stage_progress


def reconcile_attended_run(
    session: Session, run_id: str, *, now: datetime | None = None,
    stale_after: timedelta = timedelta(minutes=5),
    process_is_alive: Callable[[int | None], bool] | None = None,
) -> int:
    """Persist stale interruption before an attended lifecycle action."""
    kwargs = {"now": now, "stale_after": stale_after}
    if process_is_alive is not None:
        kwargs["process_is_alive"] = process_is_alive
    return stage_progress.reconcile_stale_stages(session, run_id, **kwargs)


def await_named_approval(
    session: Session, run_id: str, *, available_company_count: int,
    now: datetime | None = None,
) -> None:
    """Open the approval gate and begin its dedicated human-wait clock."""
    stage_progress.open_approval_gate(
        session, run_id, available_company_count=available_company_count, now=now,
    )


def begin_approval_preparation(
    session: Session, run_id: str, *, now: datetime | None = None,
) -> None:
    stage_progress.begin_approval_preparation(session, run_id, now=now)


def approval_artifact_ready(
    session: Session, run_id: str, *, available_company_count: int,
    gmail_message_id: str | None = None, gmail_thread_id: str | None = None,
    google_spreadsheet_id: str | None = None,
    google_spreadsheet_url: str | None = None,
    now: datetime | None = None,
) -> None:
    stage_progress.mark_approval_artifact_ready(
        session, run_id, available_company_count=available_company_count,
        gmail_message_id=gmail_message_id, gmail_thread_id=gmail_thread_id,
        google_spreadsheet_id=google_spreadsheet_id,
        google_spreadsheet_url=google_spreadsheet_url,
        now=now,
    )


def approval_email_thread_id(session: Session, run_id: str) -> str:
    """Return the durable Gmail thread owning this run's approval artifact."""
    return stage_progress.approval_artifact_thread_id(session, run_id)


def approval_google_spreadsheet(session: Session, run_id: str) -> tuple[str, str]:
    """Return the exact Google Sheet recorded for this approval gate."""
    return stage_progress.approval_artifact_spreadsheet(session, run_id)


def replace_approval_artifact_delivery(
    session: Session, run_id: str, *, gmail_message_id: str,
    gmail_thread_id: str, google_spreadsheet_id: str,
    google_spreadsheet_url: str,
) -> None:
    """Replace the authoritative approval Sheet/email pair during correction."""
    stage_progress.replace_approval_artifact_delivery(
        session, run_id,
        gmail_message_id=gmail_message_id,
        gmail_thread_id=gmail_thread_id,
        google_spreadsheet_id=google_spreadsheet_id,
        google_spreadsheet_url=google_spreadsheet_url,
    )


def approval_input_received(
    session: Session, run_id: str, *, now: datetime | None = None,
) -> None:
    stage_progress.resume_approval_processing(session, run_id, now=now)


def approval_input_rejected(
    session: Session, run_id: str, *, reason: str,
    now: datetime | None = None,
) -> None:
    stage_progress.wait_for_approval(
        session, run_id, stage_progress.StageName.APPROVAL_GATE,
        reason=reason, now=now,
    )


def record_named_approval(
    session: Session, run_id: str, *, available_company_count: int,
    selected_company_count: int, selected_job_count: int,
    now: datetime | None = None,
) -> None:
    """Complete approval while retaining the original wait and attempt history."""
    approval_input_received(session, run_id, now=now)
    stage_progress.complete_approval_gate(
        session, run_id, available_company_count=available_company_count,
        selected_company_count=selected_company_count,
        selected_job_count=selected_job_count, now=now,
    )
