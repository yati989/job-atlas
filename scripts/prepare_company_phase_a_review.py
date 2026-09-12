"""Export a bounded exact-manifest Phase A review batch for agent judgment.

This is a read-only evidence seam.  It deliberately does not infer or persist
company profiles; the agent reviews the exported postings and produces the
judgment artifact consumed by ``apply_company_phase_a_judgments``.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.contacts.company_profile import suggested_search_groups
from app.contacts.company_type import classify
from app.db.session import SessionLocal
from app.models.orm import Company, Job
from scripts.verify_company_enrichment_wave import _valid_contact_profile, _valid_pain_points


def _ids(path: Path) -> list[int]:
    with path.open(newline="", encoding="utf-8") as handle:
        return [int(row["company_id"]) for row in csv.DictReader(handle)]


def _excerpt(value: str | None) -> str | None:
    if not value:
        return None
    text = re.sub(r"<[^>]+>", " ", value)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:900] if text else None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--offset", type=int, default=0,
                        help="offset into the ordered incomplete-record list")
    parser.add_argument("--limit", type=int, default=15)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.offset < 0 or args.limit < 1:
        raise ValueError("offset must be non-negative and limit must be positive")

    manifest_ids = _ids(args.manifest)
    with SessionLocal() as session:
        companies = {
            company.id: company
            for company in session.scalars(select(Company).where(Company.id.in_(manifest_ids)))
        }
        incomplete_ids = [
            company_id
            for company_id in manifest_ids
            if company_id in companies
            and (
                not _valid_contact_profile(companies[company_id])
                or not companies[company_id].industry
                or not companies[company_id].description
                or not _valid_pain_points(companies[company_id].pain_points)
                or companies[company_id].enrichment_status != "done"
                or companies[company_id].enriched_at is None
            )
        ]
        batch_ids = incomplete_ids[args.offset : args.offset + args.limit]
        rows: list[dict[str, Any]] = []
        for company_id in batch_ids:
            company = companies[company_id]
            jobs = list(session.scalars(
                select(Job)
                .where(Job.company_id == company_id, Job.status == "active")
                .order_by(Job.posted_at.desc().nullslast(), Job.id.desc())
                .limit(3)
            ))
            rows.append({
                "company_id": company.id,
                "company_name": company.name,
                "current": {
                    "industry": company.industry,
                    "employee_count_range": company.employee_count_range,
                    "founding_year": company.founding_year,
                    "description": company.description,
                    "pain_points": company.pain_points,
                    "canonical_domain": company.canonical_domain,
                    "domain_resolution_status": company.domain_resolution_status,
                    "company_type": company.company_type,
                    "contact_search_groups": company.contact_search_groups,
                },
                "suggestions": {
                    "search_groups": suggested_search_groups(session, company_id),
                    "company_type": classify(
                        company.industry,
                        company_name=company.name,
                        description=company.description,
                    ),
                },
                "jobs": [
                    {
                        "job_id": job.id,
                        "title": job.title,
                        "posted_at": job.posted_at.isoformat() if job.posted_at else None,
                        "description_excerpt": _excerpt(job.description_raw),
                        "job_url": job.job_url,
                    }
                    for job in jobs
                ],
            })
    payload = {
        "manifest": str(args.manifest),
        "incomplete_count": len(incomplete_ids),
        "offset": args.offset,
        "next_offset": args.offset + len(rows),
        "rows": rows,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"incomplete={len(incomplete_ids)} exported={len(rows)} next_offset={payload['next_offset']}")


if __name__ == "__main__":
    main()
