"""
Builds the full-pipeline run workbook — the .xlsx that
`app.outreach.gmail.send_self_report` emails to the candidate after each
run (issue #82, #87). Pure function: a `Session` and this run's metadata
in, an .xlsx path out — no Streamlit/Gmail import here, same read/render
split as `app/dashboard/queries.py`.

Four sheets:
  1. Contacts — the action sheet: one row per outreach draft touched this
     run, with a Gmail draft link so the candidate can open and send.
  2. Jobs — this run's ingested batch, with tailoring score/resume path.
  3. Run summary — which stages ran/were skipped/failed, and dedup counts.
  4. Review — Tier 2 company-dedup candidates (never auto-merged, see
     app/companies/dedup.py) for the candidate to merge by hand if real.

Reuses scripts/export_db_excel.py's cell-shaping helpers (datetime ->
naive string, dict/list -> compact JSON, truncate at Excel's 32,767-char
cell limit) rather than duplicating them.
"""
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.orm import Company, Contact, Job, OutreachDraft, TailoredResume

EXCEL_CELL_LIMIT = 32767

# Gmail draft deep link: thread-based, not the `#drafts?compose=<id>` shape.
# Verified live (2026-08-08) against a real pushed draft — the Gmail API's
# drafts().get() returns only the draft's own opaque `id` (matches
# outreach_drafts.gmail_draft_id) plus a nested message with `threadId`
# (matches outreach_drafts.gmail_thread_id, confirmed equal). The
# `compose=` URL parameter requires a separate internal encoding the API
# never exposes, so it can only be guessed; `#all/<threadId>` is Gmail's
# documented, stable thread-deep-link convention and needs no encoding
# beyond the threadId already verified above. A draft is the only message
# in its own thread, so this opens directly to the compose/edit view.
GMAIL_DRAFT_URL = "https://mail.google.com/mail/u/0/#all/{thread_id}"


def _cell(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, default=str)
    if isinstance(value, str) and len(value) > EXCEL_CELL_LIMIT:
        return value[: EXCEL_CELL_LIMIT - 1] + "…"
    return value


def _draft_link(draft: OutreachDraft) -> str | None:
    if not draft.gmail_thread_id:
        return None
    return GMAIL_DRAFT_URL.format(thread_id=draft.gmail_thread_id)


def _contacts_rows(session: Session, since: datetime) -> list[dict]:
    stmt = (
        select(OutreachDraft)
        .where(OutreachDraft.created_at >= since)
        .order_by(OutreachDraft.company_id, OutreachDraft.recipient_tier)
    )
    drafts = session.execute(stmt).scalars().all()

    rows = []
    for d in drafts:
        contact = d.contact
        rows.append({
            "to_email": d.to_email,
            "to_name": d.to_name,
            "recipient_title": d.recipient_title,
            "recipient_tier": d.recipient_tier,
            "company": d.company.name if d.company else None,
            # name_collision_risk is the only collision signal persisted on
            # Contact today — the richer free-text explanation
            # (agentic_batch.CandidateContact.collision_note) only exists
            # in-memory during a find-contacts session and is never written
            # to the DB, so it cannot be surfaced here. Flagging this as a
            # known gap rather than fabricating a note.
            "collision_risk": bool(contact.name_collision_risk) if contact else d.collision_risk,
            "matched_job_title": d.matched_job.title if d.matched_job else None,
            "pairing_reason": d.pairing_reason,
            "resume_kind": d.resume_kind,
            "score": d.tailored_resume.score if d.tailored_resume else None,
            "status": d.status,
            "gmail_draft_link": _draft_link(d),
            "created_at": d.created_at,
        })
    return rows


def _jobs_rows(session: Session, since: datetime) -> list[dict]:
    stmt = (
        select(Job)
        .where(Job.first_seen_at >= since, Job.duplicate_of_job_id.is_(None))
        .order_by(Job.first_seen_at.desc())
    )
    jobs = session.execute(stmt).scalars().all()

    rows = []
    for j in jobs:
        # Job has no direct ORM relationship to TailoredResume (only the
        # reverse FK on tailored_resumes.job_id) — look it up explicitly.
        tr = session.execute(
            select(TailoredResume).where(TailoredResume.job_id == j.id)
        ).scalar_one_or_none()
        rows.append({
            "job_id": j.id,
            "title": j.title,
            "company": j.company_name_raw,
            "source": j.source,
            "location": j.location_raw,
            "score": tr.score if tr else None,
            "gap_report": tr.gap_report if tr else None,
            "resume_path": tr.artifact_dir if tr else None,
            "job_url": j.job_url,
            "first_seen_at": j.first_seen_at,
        })
    return rows


def _run_summary_rows(run_meta: dict) -> list[dict]:
    rows = []
    for stage in run_meta.get("stage_status", []):
        rows.append({
            "stage": stage.get("stage"),
            "status": stage.get("status"),
            "detail": stage.get("detail"),
        })

    dedup = run_meta.get("dedup_stats") or {}
    if dedup:
        rows.append({
            "stage": "company_dedup",
            "status": "ran",
            "detail": (
                f"Tier 1: {dedup.get('tier1_groups', 0)} groups, "
                f"{dedup.get('tier1_merged', 0)} rows merged, "
                f"{dedup.get('jobs_repointed', 0)} jobs + "
                f"{dedup.get('contacts_repointed', 0)} contacts re-pointed. "
                f"Tier 2 (not merged, see Review sheet): {dedup.get('tier2_groups', 0)} groups."
            ),
        })

    skills = run_meta.get("skills_dedup_stats") or {}
    if skills:
        rows.append({
            "stage": "skills_dedup",
            "status": "ran",
            "detail": f"{skills.get('groups', 0)} case-variant groups, "
                      f"{skills.get('renamed', 0)} renamed, {skills.get('deleted', 0)} deleted.",
        })

    return rows


def _review_rows(run_meta: dict) -> list[dict]:
    dedup = run_meta.get("dedup_stats") or {}
    candidates = dedup.get("tier2_candidates") or []
    rows = []
    for group in candidates:
        rows.append({
            "prefix": group.get("prefix"),
            "companies": group.get("companies"),
        })
    return rows


def build_workbook(session: Session, run_meta: dict, out_path: str | Path) -> Path:
    """`run_meta` keys used:
      - since (datetime, required): the run window start — Contacts/Jobs
        sheets scope to rows touched at or after this timestamp.
      - stage_status (list[dict], optional): [{stage, status, detail}, ...]
      - dedup_stats (dict, optional): return value of
        scripts.dedup_companies.run()
      - skills_dedup_stats (dict, optional): return value of
        scripts.normalize_skill_casing.run()
    """
    since = run_meta["since"]
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    sheets = {
        "Contacts": pd.DataFrame(_contacts_rows(session, since)),
        "Jobs": pd.DataFrame(_jobs_rows(session, since)),
        "Run summary": pd.DataFrame(_run_summary_rows(run_meta)),
        "Review": pd.DataFrame(_review_rows(run_meta)),
    }

    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        for sheet_name, df in sheets.items():
            if df.empty:
                df = pd.DataFrame(columns=["(no rows)"])
            else:
                df = df.map(_cell)
            df.to_excel(writer, sheet_name=sheet_name, index=False)

    return out_path
