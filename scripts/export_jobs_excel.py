"""
Export the jobs table to an Excel workbook (one row per job, joined to its
company name). Read-only — no DB writes. Usage:

    python -m scripts.export_jobs_excel [output_path.xlsx]
    python -m scripts.export_jobs_excel [output_path.xlsx] --since "2026-07-26 00:00"

--since filters to jobs with last_seen_at >= the given timestamp (the
pipeline touches last_seen_at on every upsert, so this scopes the export to
"jobs a given run actually fetched/refreshed" rather than the whole table).
Omit it for a full export.

Skips the raw_payload JSON blob (not spreadsheet-friendly) and truncates any
single cell to Excel's 32,767-character limit.
"""
import argparse
from datetime import datetime

import pandas as pd
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from app.db.session import SessionLocal
from app.models.orm import Job

EXCEL_CELL_LIMIT = 32767


def _clip(value):
    if isinstance(value, str):
        # openpyxl rejects ASCII control chars (\x00-\x08, \x0b, \x0c, \x0e-\x1f);
        # some scraped descriptions contain them, so strip before length-clipping.
        value = ILLEGAL_CHARACTERS_RE.sub("", value)
        if len(value) > EXCEL_CELL_LIMIT:
            return value[: EXCEL_CELL_LIMIT - 1] + "…"
    return value


def _fmt_dt(value):
    # Excel can't store tz-aware datetimes; render as naive string.
    return value.strftime("%Y-%m-%d %H:%M") if isinstance(value, datetime) else value


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("output_path", nargs="?", default="job_list.xlsx")
    parser.add_argument(
        "--since",
        default=None,
        metavar="TIMESTAMP",
        help="Only include jobs with last_seen_at >= this timestamp, e.g. '2026-07-26 00:00'.",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    out_path = args.output_path

    session = SessionLocal()
    query = (
        select(Job)
        .options(joinedload(Job.company))
        .where(Job.duplicate_of_job_id.is_(None))
        .order_by(Job.company_id, Job.id)
    )
    if args.since:
        since_dt = datetime.fromisoformat(args.since)
        query = query.where(Job.last_seen_at >= since_dt)
    jobs = session.execute(query).scalars().all()

    rows = []
    for j in jobs:
        rows.append({
            "job_id": j.id,
            "source": j.source,
            "company": j.company.name if j.company else j.company_name_raw,
            "title": j.title,
            "location": j.location_raw,
            "is_remote": j.is_remote,
            "remote_scope": j.remote_scope,
            "employment_type": j.employment_type,
            "seniority": j.seniority,
            "experience_min_years": j.experience_min_years,
            "experience_max_years": j.experience_max_years,
            "education_requirement": j.education_requirement,
            "salary": j.salary_raw,
            "status": j.status,
            "posted_at": _fmt_dt(j.posted_at),
            "first_seen_at": _fmt_dt(j.first_seen_at),
            "last_seen_at": _fmt_dt(j.last_seen_at),
            "apply_url": j.apply_url,
            "job_url": j.job_url,
            "description": _clip(j.description_raw),
        })

    df = pd.DataFrame(rows)
    df.to_excel(out_path, index=False, engine="openpyxl")
    print(f"Wrote {len(df)} jobs to {out_path}")


if __name__ == "__main__":
    main()
