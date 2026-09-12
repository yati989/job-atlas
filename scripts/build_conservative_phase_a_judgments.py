"""Build evidence-conservative Phase A judgments for invalid manifest rows.

This is a review fallback: it never manufactures company facts or domains.
Existing verified domains are retained; everything else is explicitly marked
unresolvable, and the recorded posting title is the only stated hiring fact.
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from sqlalchemy import select

from app.db.session import SessionLocal
from app.contacts.email_resolution import _get_mx_host
from app.models.orm import Company, Job


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    with args.manifest.open(newline="", encoding="utf-8") as handle:
        ids = [int(row["company_id"]) for row in csv.DictReader(handle)]
    with SessionLocal() as session:
        companies = {c.id: c for c in session.scalars(select(Company).where(Company.id.in_(ids)))}
        rows = []
        for company_id in ids:
            c = companies[company_id]
            domain_done = (
                c.domain_resolution_status == "done"
                and bool(c.canonical_domain)
                and bool(_get_mx_host(c.canonical_domain))
            )
            profile_valid = c.company_type in {"employer", "staffing"} and isinstance(c.contact_search_groups, list) and c.domain_resolution_status in {"done", "unresolvable"}
            facts_valid = bool(
                c.industry and c.description and c.pain_points and c.enrichment_status == "done"
                and all(line.strip().startswith(("[posting-inferred]", "[company-inferred]", "[insufficient-evidence]")) for line in c.pain_points.splitlines() if line.strip())
            )
            if profile_valid and facts_valid:
                continue
            job = session.scalar(select(Job).where(Job.company_id == company_id).order_by(Job.posted_at.desc()))
            title = " ".join((job.title if job and job.title else "a role in the stored job evidence").split())
            groups = c.contact_search_groups if isinstance(c.contact_search_groups, list) and c.contact_search_groups else ["data_ai"]
            rows.append({
                "company_id": company_id,
                "industry": c.industry if c.industry and c.industry != "Other / Unclassified" else "Unclassified",
                "employee_count_range": c.employee_count_range,
                "founding_year": c.founding_year,
                "description": c.description if c.description and not c.description.startswith("Current stored postings") else f"[insufficient-evidence] Stored job evidence identifies {c.name} as hiring for {title}; no further company-profile fact is asserted.",
                "pain_points": f"[insufficient-evidence] Stored job evidence identifies hiring for {title}; further company research is required before asserting a specific business need.",
                "canonical_domain": c.canonical_domain if domain_done else None,
                "domain_resolution_status": "done" if domain_done else "unresolvable",
                "company_type": c.company_type if c.company_type in {"employer", "staffing"} else "employer",
                "search_groups": groups,
            })
    args.output.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
    print(f"judgments={len(rows)}")


if __name__ == "__main__":
    main()
