"""Final approved-run workbook read model."""
from pathlib import Path

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.orm import (DecisionRun, DecisionRunCompany, DecisionRunJob,
    DecisionRunSelectedJob, OutreachDeliveryAttempt, OutreachMessage,
    RunCompanyCalibration, TailoredResume)
from app.reporting.excel import excel_rows


def _write(writer, name: str, rows: list[dict], columns: list[str]) -> None:
    pd.DataFrame(excel_rows(rows), columns=columns).to_excel(writer, sheet_name=name, index=False)


def build_final_workbook(session: Session, run_id: str, out_path: str | Path) -> Path:
    run = session.get(DecisionRun, run_id)
    if run is None:
        raise ValueError(f"unknown run {run_id}")
    selected = list(session.execute(select(DecisionRunSelectedJob).where(DecisionRunSelectedJob.run_id == run_id).order_by(DecisionRunSelectedJob.company_order)).scalars())
    selected_by_version = {row.posting_version_id: row for row in selected}
    companies = {row.company_id: row for row in session.execute(select(DecisionRunCompany).where(DecisionRunCompany.run_id == run_id)).scalars()}
    run_jobs = list(session.execute(select(DecisionRunJob).where(DecisionRunJob.run_id == run_id)).scalars())
    run_jobs_by_version = {row.posting_version_id: row for row in run_jobs}
    messages = list(session.execute(select(OutreachMessage).where(OutreachMessage.run_id == run_id)).scalars())
    attempts = list(session.execute(select(OutreachDeliveryAttempt).join(OutreachMessage).where(OutreachMessage.run_id == run_id)).scalars())
    calibrations = list(session.execute(select(RunCompanyCalibration).where(RunCompanyCalibration.run_id == run_id)).scalars())

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(out, engine="openpyxl") as writer:
        fresh = []
        application_only = []
        jobs = []
        for selected_row in selected:
            run_job = run_jobs_by_version.get(selected_row.posting_version_id)
            company = companies.get(selected_row.company_id)
            row = {"company_order": selected_row.company_order, "company_id": selected_row.company_id,
                   "posting_version_id": selected_row.posting_version_id, "job_id": run_job.job_id if run_job else None,
                   "title": run_job.snapshot.get("title") if run_job else None,
                   "selection_kind": selected_row.selection_kind, "resume_id": None, "resume_path": None, "score": None}
            if run_job:
                resume = session.execute(select(TailoredResume).where(TailoredResume.job_id == run_job.job_id)).scalar_one_or_none()
                if resume: row.update(resume_id=resume.id, resume_path=resume.artifact_dir, score=resume.score)
            jobs.append(row)
            (fresh if selected_row.selection_kind == "fresh_outreach" else application_only).append(row)
        _write(writer, "Fresh outreach companies", fresh, list(jobs[0].keys()) if jobs else ["company_order", "company_id", "posting_version_id", "job_id", "title", "selection_kind", "resume_id", "resume_path", "score"])
        _write(writer, "Application only", application_only, list(jobs[0].keys()) if jobs else ["company_order", "company_id", "posting_version_id", "job_id", "title", "selection_kind", "resume_id", "resume_path", "score"])
        _write(writer, "Jobs and resumes", jobs, list(jobs[0].keys()) if jobs else ["company_order", "company_id", "posting_version_id", "job_id", "title", "selection_kind", "resume_id", "resume_path", "score"])

        contact_rows = [{"message_id": message.id, "company_id": message.company_id, "contact_id": message.contact_id,
                         "posting_version_id": message.posting_version_id, "pairing_reason": message.pairing_reason,
                         "resume_path": message.resume_path, "state": message.state} for message in messages]
        _write(writer, "Contacts and pairing", contact_rows, ["message_id", "company_id", "contact_id", "posting_version_id", "pairing_reason", "resume_path", "state"])
        calibration_rows = [{"company_id": row.company_id, "domain": row.domain, "contact_id": row.contact_id,
                             "message_id": row.message_id, "state": row.state, "first_sent_at": row.first_sent_at,
                             "deadline_at": row.deadline_at, "confirmed_pattern": row.confirmed_pattern} for row in calibrations]
        _write(writer, "Calibration and patterns", calibration_rows, ["company_id", "domain", "contact_id", "message_id", "state", "first_sent_at", "deadline_at", "confirmed_pattern"])
        delivery = [{"message_id": attempt.message_id, "email": attempt.to_email, "domain": attempt.domain,
                     "pattern": attempt.pattern_name, "state": attempt.state, "pushed_at": attempt.pushed_at,
                     "sent_at": attempt.sent_at, "delivered_at": attempt.presumed_delivered_at,
                     "replied_at": attempt.replied_at, "bounced_at": attempt.bounced_at} for attempt in attempts]
        _write(writer, "Delivery state", delivery, ["message_id", "email", "domain", "pattern", "state", "pushed_at", "sent_at", "delivered_at", "replied_at", "bounced_at"])
        failures = [{"job_id": job.job_id, "company_id": job.company_id, "outcome": job.outcome,
                     "detail": job.snapshot.get("anomaly")} for job in run_jobs if job.outcome != "eligible"]
        _write(writer, "Failures", failures, ["job_id", "company_id", "outcome", "detail"])
        _write(writer, "Run summary", [{"run_id": run.id, "state": run.state, "selected_jobs": len(selected),
                                          "messages": len(messages), "attempts": len(attempts)}], ["run_id", "state", "selected_jobs", "messages", "attempts"])
    return out
