"""Local XLSX exports for one public-workflow run.

The workbook is deliberately a read-only local artifact: it makes no network
requests and contains paths to resume artifacts rather than copying binaries.
"""
from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.styles import Alignment, Font, PatternFill
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.orm import (
    Company,
    Job,
    JobPostingVersion,
    LinkedInProfileLink,
    ProfileDiscoveryAuthorization,
    ProfiledSearchOutcome,
    PublicSelectionScope,
    PublicSelectionScopeItem,
    PublicTailoredResume,
)
from app.dashboard.public_read_model import combine_public_snapshots, load_public_snapshot
from app.reporting.excel import _excel_value


def _cell(value: Any) -> Any:
    """Make provider text safe and stable when opened in spreadsheet software."""
    if isinstance(value, (dict, list, tuple)):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    value = _excel_value(value)
    # A job title or scraped value beginning with one of these is a formula in
    # Excel.  Preserve it as literal text instead.
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def _all_collected_rows(session: Session, run_id: str) -> list[dict[str, Any]]:
    rows = session.execute(
        select(ProfiledSearchOutcome, Job)
        .outerjoin(Job, ProfiledSearchOutcome.job_id == Job.id)
        .where(ProfiledSearchOutcome.run_id == run_id)
        .order_by(ProfiledSearchOutcome.source, ProfiledSearchOutcome.external_job_id)
    ).all()
    result = []
    for outcome, job in rows:
        evidence = outcome.evidence if isinstance(outcome.evidence, dict) else {}
        remote = job.is_remote if job is not None else evidence.get("is_remote")
        result.append({
            "source": outcome.source,
            "external_job_id": outcome.external_job_id,
            "title": job.title if job is not None else evidence.get("title"),
            "company": job.company_name_raw if job is not None else evidence.get("company_name_raw"),
            "location": job.location_raw if job is not None else evidence.get("location_raw"),
            "work_mode": "remote" if remote is True else "onsite_or_unspecified",
            "posted_at": job.posted_at if job is not None else evidence.get("posted_at"),
            "outcome": outcome.outcome,
            "failed_gate": outcome.first_failed_axis,
            "decision_reason": outcome.reason,
            "job_url": (job.job_url if job is not None else None) or outcome.job_url,
            "apply_url": (job.apply_url if job is not None else None) or outcome.apply_url,
            "description_available": bool(job and job.description_raw),
        })
    return result


def _enriched_job_rows(
    session: Session, run_id: str, scope_id: int,
) -> list[dict[str, Any]]:
    selected_versions = set(session.scalars(select(
        PublicSelectionScopeItem.posting_version_id,
    ).where(PublicSelectionScopeItem.scope_id == scope_id)))
    rows = []
    for job in load_public_snapshot(session, run_id).kept_jobs:
        rows.append({
            "selected": job["posting_version_id"] in selected_versions,
            "source": job["source"],
            "title": job["title"],
            "company": job["company"],
            "location": job["location"],
            "work_mode": job["work_mode"],
            "posted_at": job["posted_at"],
            "employment_type": job["employment_type"],
            "seniority": job["seniority"],
            "salary": job["salary"],
            "salary_evidence": job["salary_evidence"],
            "experience_min_years": job["experience_min_years"],
            "experience_max_years": job["experience_max_years"],
            "education": job["education"],
            "other_qualifications": job["other_qualifications"],
            "hard_skills": job["hard_skills"],
            "soft_skills": job["soft_skills"],
            "enrichment_status": job["enrichment_status"],
            "job_url": job["job_url"],
            "apply_url": job["apply_url"],
        })
    return rows


def _company_rows(session: Session, run_id: str, scope_id: int) -> list[dict[str, Any]]:
    selected_company_ids = set(session.scalars(select(
        PublicSelectionScopeItem.company_id,
    ).where(PublicSelectionScopeItem.scope_id == scope_id)))
    selected_company_names = set(session.scalars(select(Company.name).where(
        Company.id.in_(selected_company_ids)
    ))) if selected_company_ids else set()
    rows = []
    for company in load_public_snapshot(session, run_id).companies:
        row = dict(company)
        row["selected"] = company["company"] in selected_company_names
        rows.append(row)
    return rows


def _review_rows(session: Session, run_id: str) -> list[dict[str, Any]]:
    rows = session.scalars(
        select(ProfiledSearchOutcome)
        .where(
            ProfiledSearchOutcome.run_id == run_id,
            ProfiledSearchOutcome.outcome == "needs_review",
        )
        .order_by(ProfiledSearchOutcome.source, ProfiledSearchOutcome.external_job_id)
    ).all()
    return [{
        "source": row.source,
        "external_job_id": row.external_job_id,
        "title": row.evidence.get("title"),
        "company": row.evidence.get("company_name_raw"),
        "location": row.evidence.get("location_raw"),
        "reason": row.reason,
        "first_failed_axis": row.first_failed_axis,
        "job_url": row.job_url,
        "apply_url": row.apply_url,
    } for row in rows]


def _profile_link_rows(session: Session, run_id: str, scope_id: int) -> list[dict[str, Any]]:
    selected_company_ids = select(PublicSelectionScopeItem.company_id).where(
        PublicSelectionScopeItem.scope_id == scope_id,
    )
    rows = session.execute(
        select(LinkedInProfileLink, Company.name)
        .join(
            ProfileDiscoveryAuthorization,
            LinkedInProfileLink.authorization_id == ProfileDiscoveryAuthorization.id,
        )
        .join(Company, LinkedInProfileLink.company_id == Company.id)
        .where(
            ProfileDiscoveryAuthorization.run_id == run_id,
            LinkedInProfileLink.company_id.in_(selected_company_ids),
        )
        .order_by(Company.name, LinkedInProfileLink.linkedin_url)
    ).all()
    return [{
        "company": company_name,
        "name": link.full_name,
        "headline": link.headline,
        "search_group": link.search_group,
        "linkedin_url": link.linkedin_url,
        "evidence": link.evidence,
    } for link, company_name in rows]


def _selection_scope_rows(session: Session, scope_id: int) -> list[dict[str, Any]]:
    rows = session.execute(
        select(PublicSelectionScopeItem, JobPostingVersion, Job, Company.name)
        .join(JobPostingVersion, PublicSelectionScopeItem.posting_version_id == JobPostingVersion.id)
        .join(Job, JobPostingVersion.job_id == Job.id)
        .join(Company, PublicSelectionScopeItem.company_id == Company.id)
        .where(PublicSelectionScopeItem.scope_id == scope_id)
        .order_by(PublicSelectionScopeItem.posting_version_id)
    ).all()
    return [{
        "follow_up_decision": "Approved",
        "posting_version_id": item.posting_version_id,
        "company_id": item.company_id,
        "company": company_name,
        "job_id": job.id,
        "source": job.source,
        "external_job_id": job.external_job_id,
        "title": job.title,
        "job_url": job.job_url,
    } for item, _version, job, company_name in rows]


def _resume_rows(session: Session, scope_id: int) -> list[dict[str, Any]]:
    rows = session.execute(
        select(PublicTailoredResume, JobPostingVersion, Job)
        .join(
            JobPostingVersion,
            PublicTailoredResume.posting_version_id == JobPostingVersion.id,
        )
        .join(Job, JobPostingVersion.job_id == Job.id)
        .join(
            PublicSelectionScopeItem,
            PublicSelectionScopeItem.posting_version_id == JobPostingVersion.id,
        )
        .where(PublicSelectionScopeItem.scope_id == scope_id)
        .order_by(Job.source, Job.external_job_id, PublicTailoredResume.id)
    ).all()
    return [{
            "resume_id": resume.id,
            "posting_version_id": version.id,
            "job_id": job.id,
            "title": job.title,
            "company": job.company_name_raw,
            "score": resume.score,
            "artifact_directory": resume.artifact_dir,
            "updated_at": resume.updated_at,
        } for resume, version, job in rows]


def _write_sheet(workbook: Workbook, title: str, rows: list[dict[str, Any]], headers: list[str]) -> None:
    worksheet = workbook.create_sheet(title)
    worksheet.append(headers)
    for row in rows:
        worksheet.append([_cell(row.get(header)) for header in headers])
    worksheet.freeze_panes = "A2"
    worksheet.auto_filter.ref = worksheet.dimensions
    header_fill = PatternFill("solid", fgColor="243B53")
    for cell in worksheet[1]:
        cell.fill = header_fill
        cell.font = Font(color="FFFFFF", bold=True)
    link_columns = {
        index for index, header in enumerate(headers, start=1)
        if header in {"job_url", "apply_url", "linkedin_url", "company_url"}
    }
    for row in worksheet.iter_rows(min_row=2):
        for index in link_columns:
            cell = row[index - 1]
            if isinstance(cell.value, str):
                target = cell.value.strip()
            else:
                target = ""
            if target.startswith(("http://", "https://")):
                cell.value = target
                cell.hyperlink = target
                cell.style = "Hyperlink"
    if "follow_up_decision" in headers and worksheet.max_row >= 2:
        decision_column = headers.index("follow_up_decision") + 1
        validation = DataValidation(
            type="list", formula1='"Approved,Declined"', allow_blank=False,
        )
        validation.error = "Choose Approved or Declined."
        validation.errorTitle = "Invalid shortlist decision"
        validation.prompt = "Approved jobs remain eligible for the optional next steps."
        validation.promptTitle = "Follow-up decision"
        validation.showErrorMessage = True
        validation.showInputMessage = True
        worksheet.add_data_validation(validation)
        validation.add(
            f"{worksheet.cell(1, decision_column).column_letter}2:"
            f"{worksheet.cell(worksheet.max_row, decision_column).column_letter}{worksheet.max_row}"
        )
    for index, header in enumerate(headers, start=1):
        values = [str(cell.value or "") for cell in worksheet.iter_cols(
            min_col=index, max_col=index, min_row=1, max_row=min(worksheet.max_row, 200)
        ).__next__()]
        worksheet.column_dimensions[worksheet.cell(1, index).column_letter].width = min(
            max(len(header) + 2, *(len(value) + 2 for value in values)), 50,
        )


def cumulative_workbook_bytes(session: Session, run_ids: list[str]) -> bytes:
    """Build one in-memory workbook spanning every selected saved search."""
    snapshots = tuple(load_public_snapshot(session, run_id) for run_id in run_ids)
    combined = combine_public_snapshots(snapshots)
    workbook = Workbook()
    workbook.remove(workbook.active)
    collected = []
    for snapshot in snapshots:
        for row in _all_collected_rows(session, snapshot.run_id):
            collected.append({"search": snapshot.run["label"], **row})
    _write_sheet(workbook, "All collected", collected, [
        "search", "source", "external_job_id", "title", "company", "location",
        "work_mode", "posted_at", "outcome", "failed_gate", "decision_reason",
        "job_url", "apply_url", "description_available",
    ])
    jobs = [{
        "searches": job["search"],
        "selected": job["selected"],
        "source": job["source"],
        "title": job["title"],
        "company": job["company"],
        "location": job["location"],
        "work_mode": job["work_mode"],
        "posted_at": job["posted_at"],
        "employment_type": job["employment_type"],
        "seniority": job["seniority"],
        "salary": job["salary"],
        "salary_evidence": job["salary_evidence"],
        "experience_min_years": job["experience_min_years"],
        "experience_max_years": job["experience_max_years"],
        "education": job["education"],
        "other_qualifications": job["other_qualifications"],
        "hard_skills": job["hard_skills"],
        "soft_skills": job["soft_skills"],
        "enrichment_status": job["enrichment_status"],
        "job_url": job["job_url"],
        "apply_url": job["apply_url"],
    } for job in combined.kept_jobs]
    _write_sheet(workbook, "Eligible jobs", jobs, [
        "searches", "selected", "source", "title", "company", "location",
        "work_mode", "posted_at", "employment_type", "seniority", "salary",
        "salary_evidence", "experience_min_years", "experience_max_years",
        "education", "other_qualifications", "hard_skills", "soft_skills",
        "enrichment_status", "job_url", "apply_url",
    ])
    _write_sheet(workbook, "Companies", [dict(row) for row in combined.companies], [
        "searches", "selected", "company", "company_type", "industry",
        "employee_count", "founded", "description", "pain_points", "search_groups",
        "enrichment_status", "jobs", "overall_rating", "wlb_rating",
        "estimated_salary_lpa", "company_url",
    ])
    stream = BytesIO()
    workbook.save(stream)
    return stream.getvalue()


def export_public_workbook(
    session: Session, run_id: str, scope_id: int, out_path: str | Path,
) -> Path:
    """Export one run's collected, enriched, and selected data to XLSX.

    Sheets are always present, even when a run has no rows of a type, so
    callers can consume the artifact without guessing which stages ran.
    The explicit selection scope bounds resume artifact references.
    """
    scope = session.get(PublicSelectionScope, scope_id)
    if scope is None or scope.run_id != run_id:
        raise ValueError("selection scope does not belong to this run")
    if scope.purpose != "selection":
        raise ValueError("export requires a final selection scope")
    workbook = Workbook()
    workbook.remove(workbook.active)
    instructions = workbook.create_sheet("Start here")
    instructions.append(["Choose jobs for optional next steps"])
    instructions.append([])
    instructions.append([
        "Open the Shortlist sheet. Every selected job starts as Approved."
    ])
    instructions.append([
        "Change Follow-up decision to Declined for any job you do not want to use for "
        "resume tailoring, company/profile research, or LinkedIn profile links."
    ])
    instructions.append([
        "Save the workbook, then give it to your coding agent or run: "
        "job-atlas apply-workbook-shortlist --run-id <run-id> "
        "--scope-id <scope-id> --input <workbook.xlsx> --confirm"
    ])
    instructions.append([
        "The import creates an exact new shortlist. It does not start paid research or "
        "resume work; those remain separate choices."
    ])
    instructions.column_dimensions["A"].width = 115
    instructions["A1"].font = Font(bold=True, size=14, color="243B53")
    for row in range(3, 7):
        instructions.cell(row, 1).alignment = Alignment(wrap_text=True, vertical="top")
    _write_sheet(workbook, "All collected", _all_collected_rows(session, run_id), [
        "source", "external_job_id", "title", "company", "location", "work_mode",
        "posted_at", "outcome", "failed_gate", "decision_reason", "job_url",
        "apply_url", "description_available",
    ])
    _write_sheet(workbook, "Jobs", _enriched_job_rows(session, run_id, scope.id), [
        "selected", "source", "title", "company", "location", "work_mode", "posted_at",
        "employment_type", "seniority", "salary", "salary_evidence",
        "experience_min_years", "experience_max_years", "education",
        "other_qualifications", "hard_skills", "soft_skills", "enrichment_status",
        "job_url", "apply_url",
    ])
    _write_sheet(workbook, "Companies", _company_rows(session, run_id, scope.id), [
        "selected", "company", "company_type", "industry", "employee_count", "founded",
        "description", "pain_points", "search_groups", "enrichment_status", "jobs",
        "overall_rating", "wlb_rating", "estimated_salary_lpa", "company_url",
    ])
    _write_sheet(workbook, "Needs review", _review_rows(session, run_id), [
        "source", "external_job_id", "title", "company", "location", "reason",
        "first_failed_axis", "job_url", "apply_url",
    ])
    _write_sheet(workbook, "Shortlist", _selection_scope_rows(session, scope.id), [
        "follow_up_decision", "posting_version_id", "company_id", "company", "job_id", "source",
        "external_job_id", "title", "job_url",
    ])
    _write_sheet(workbook, "LinkedIn profile links", _profile_link_rows(session, run_id, scope.id), [
        "company", "name", "headline", "search_group", "linkedin_url", "evidence",
    ])
    _write_sheet(workbook, "Resume artifacts", _resume_rows(session, scope.id), [
        "resume_id", "posting_version_id", "job_id", "title", "company", "score",
        "artifact_directory", "updated_at",
    ])
    destination = Path(out_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(destination)
    return destination
