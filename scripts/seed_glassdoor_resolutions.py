"""Seed an exact-manifest Glassdoor identity artifact from verified stored evidence."""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from sqlalchemy import select

from app.db.session import SessionLocal
from app.models.orm import Company


_EMPLOYER_ID = re.compile(r"EI_IE(\d+)")


def _stored_glassdoor(company: Company) -> dict[str, Any] | None:
    evidence = company.market_profile_evidence
    sources = evidence.get("sources") if isinstance(evidence, Mapping) else None
    source = sources.get("glassdoor") if isinstance(sources, Mapping) else None
    if not isinstance(source, Mapping) or source.get("status") != "ok":
        return None
    employer_id = str(company.glassdoor_employer_id or source.get("source_id") or "")
    url = str(source.get("url") or source.get("requested_url") or "")
    match = _EMPLOYER_ID.search(url)
    if not employer_id.isdigit() or match is None or match.group(1) != employer_id:
        return None
    observed_name = str(source.get("company") or "").strip()
    if not observed_name:
        return None
    return {
        "company_id": company.id,
        "company_name": company.name,
        "status": "accepted",
        "observed_name": observed_name,
        "employer_id": employer_id,
        "overview_url": url,
        "resolution_pass": "stored_verified",
        "review_note": "Reused fresh persisted Glassdoor evidence with matching employer ID.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--reviewed-artifact",
        action="append",
        default=[],
        help="Existing reviewed Glassdoor artifact to reuse within this manifest.",
    )
    args = parser.parse_args()
    manifest_rows = list(csv.DictReader(Path(args.manifest).open(encoding="utf-8", newline="")))
    ids = [int(row["company_id"]) for row in manifest_rows]
    with SessionLocal() as session:
        companies = {
            company.id: company
            for company in session.execute(select(Company).where(Company.id.in_(ids))).scalars()
        }
        missing = set(ids) - set(companies)
        if missing:
            raise ValueError(f"manifest IDs missing from database: {sorted(missing)}")
        resolutions = []
        for company_id in ids:
            company = companies[company_id]
            stored = _stored_glassdoor(company)
            resolutions.append(stored or {
                "company_id": company.id,
                "company_name": company.name,
                "status": "pending_search",
                "reason": "No fresh verified stored Glassdoor Overview identity.",
            })
    by_id = {item["company_id"]: item for item in resolutions}
    for artifact_path in args.reviewed_artifact:
        artifact = json.loads(Path(artifact_path).read_text(encoding="utf-8"))
        for item in artifact.get("resolutions", []):
            company_id = item.get("company_id")
            if company_id not in by_id:
                raise ValueError(
                    f"reviewed artifact ID {company_id} is outside the manifest"
                )
            if item.get("status") not in {"accepted", "unresolved"}:
                raise ValueError(f"invalid reviewed Glassdoor status for {company_id}")
            if item.get("company_name") != by_id[company_id]["company_name"]:
                raise ValueError(f"company name mismatch in reviewed artifact for {company_id}")
            by_id[company_id] = dict(item)
    resolutions = [by_id[company_id] for company_id in ids]
    payload = {
        "schema_version": 1,
        "source": "stored_glassdoor_evidence",
        "manifest": args.manifest,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "resolutions": resolutions,
        "status_counts": dict(Counter(item["status"] for item in resolutions)),
    }
    Path(args.output).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"status_counts={payload['status_counts']}")


if __name__ == "__main__":
    main()
