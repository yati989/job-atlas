"""Verify an exact company-enrichment wave against the live database.

This is intentionally a read-only verifier.  The wave manifest, rather than
the database's rolling selector, owns cohort membership.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.db.session import SessionLocal
from app.models.orm import Company


VALID_COMPANY_TYPES = {"employer", "staffing"}
VALID_DOMAIN_STATUSES = {"done", "unresolvable"}
VALID_SEARCH_GROUPS = {"data_ai", "credit_risk"}
PAIN_POINT_PREFIXES = (
    "[posting-inferred]",
    "[company-inferred]",
    "[insufficient-evidence]",
)


def _manifest_ids(path: Path) -> list[int]:
    with path.open(newline="", encoding="utf-8") as handle:
        return [int(row["company_id"]) for row in csv.DictReader(handle)]


def _source_status(company: Company, source: str) -> str:
    evidence = company.market_profile_evidence
    if not isinstance(evidence, dict):
        return "absent"
    sources = evidence.get("sources")
    if not isinstance(sources, dict):
        return "absent"
    source_evidence = sources.get(source)
    if not isinstance(source_evidence, dict):
        return "absent"
    status = source_evidence.get("status")
    return status if isinstance(status, str) else "absent"


def _valid_contact_profile(company: Company) -> bool:
    groups = company.contact_search_groups
    domain_is_valid = (
        company.domain_resolution_status == "done"
        and bool(company.canonical_domain)
    ) or (
        company.domain_resolution_status == "unresolvable"
        and company.canonical_domain is None
    )
    return bool(
        company.company_type in VALID_COMPANY_TYPES
        and isinstance(groups, list)
        and all(group in VALID_SEARCH_GROUPS for group in groups)
        and len(groups) == len(set(groups))
        and company.domain_resolution_status in VALID_DOMAIN_STATUSES
        and domain_is_valid
        and company.contact_profile_enriched_at is not None
    )


def _valid_pain_points(value: str | None) -> bool:
    if not value or not value.strip():
        return False
    entries = [line.strip() for line in value.splitlines() if line.strip()]
    return bool(entries) and all(entry.startswith(PAIN_POINT_PREFIXES) for entry in entries)


def _counts(values: list[str]) -> dict[str, int]:
    return dict(sorted(Counter(values).items()))


def verify(path: Path) -> tuple[dict[str, Any], bool]:
    ids = _manifest_ids(path)
    unique_ids = set(ids)
    with SessionLocal() as session:
        companies = list(
            session.scalars(select(Company).where(Company.id.in_(unique_ids)))
        )

    by_id = {company.id: company for company in companies}
    missing_db_ids = sorted(unique_ids - set(by_id))
    ordered = [by_id[company_id] for company_id in ids if company_id in by_id]

    contact_invalid_ids = sorted(
        company.id for company in ordered if not _valid_contact_profile(company)
    )
    contact_valid_ids = sorted(
        company.id for company in ordered if _valid_contact_profile(company)
    )
    fact_invalid_ids = sorted(
        company.id
        for company in ordered
        if not company.industry
        or not company.description
        or not _valid_pain_points(company.pain_points)
        or company.enrichment_status != "done"
        or company.enriched_at is None
    )
    fact_valid_ids = sorted(set(ids) - set(fact_invalid_ids) - set(missing_db_ids))
    historical_levels_fyi_ids = sorted(
        company.id
        for company in ordered
        if company.levels_fyi_estimated_salary_lpa is not None
        or _source_status(company, "levels_fyi") != "absent"
    )
    bad_wlb_authority_ids: list[int] = []
    bad_salary_authority_ids: list[int] = []
    for company in ordered:
        evidence = company.market_profile_evidence
        combined = evidence.get("combined", {}) if isinstance(evidence, dict) else {}
        wlb = combined.get("wlb", {}) if isinstance(combined, dict) else {}
        salary = combined.get("salary", {}) if isinstance(combined, dict) else {}
        if company.wlb_rating is not None and (
            not isinstance(wlb, dict) or wlb.get("source") != "glassdoor"
        ):
            bad_wlb_authority_ids.append(company.id)
        salary_sources = salary.get("sources", []) if isinstance(salary, dict) else []
        if company.estimated_salary_lpa is not None and salary_sources != ["ambitionbox"]:
            bad_salary_authority_ids.append(company.id)

    report: dict[str, Any] = {
        "manifest": str(path),
        "manifest_rows": len(ids),
        "unique_manifest_ids": len(unique_ids),
        "duplicate_manifest_ids": sorted(
            company_id for company_id, count in Counter(ids).items() if count > 1
        ),
        "database_rows": len(ordered),
        "missing_database_ids": missing_db_ids,
        "phase_a": {
            "enrichment_status_counts": _counts(
                [company.enrichment_status or "null" for company in ordered]
            ),
            "company_type_counts": _counts(
                [company.company_type or "null" for company in ordered]
            ),
            "domain_status_counts": _counts(
                [company.domain_resolution_status or "null" for company in ordered]
            ),
            "contact_profile_valid": len(ordered) - len(contact_invalid_ids),
            "contact_profile_valid_ids": contact_valid_ids,
            "contact_profile_invalid_ids": contact_invalid_ids,
            "facts_and_pain_points_valid": len(ordered) - len(fact_invalid_ids),
            "facts_and_pain_points_valid_ids": fact_valid_ids,
            "facts_and_pain_points_invalid_ids": fact_invalid_ids,
        },
        "phase_b": {
            "market_profile_status_counts": _counts(
                [company.market_profile_status or "null" for company in ordered]
            ),
            "glassdoor_status_counts": _counts(
                [_source_status(company, "glassdoor") for company in ordered]
            ),
            "ambitionbox_status_counts": _counts(
                [_source_status(company, "ambitionbox") for company in ordered]
            ),
            "bad_wlb_authority_ids": sorted(bad_wlb_authority_ids),
            "bad_salary_authority_ids": sorted(bad_salary_authority_ids),
            "historical_levels_fyi_value_or_evidence_ids": historical_levels_fyi_ids,
        },
    }
    valid = not any(
        (
            len(ids) != len(unique_ids),
            missing_db_ids,
            contact_invalid_ids,
            fact_invalid_ids,
            bad_wlb_authority_ids,
            bad_salary_authority_ids,
        )
    )
    report["valid"] = valid
    return report, valid


def update_checkpoint(path: Path, report: dict[str, Any]) -> None:
    """Atomically record the real Phase A audit without touching Phase B."""
    checkpoint = json.loads(path.read_text(encoding="utf-8"))
    if checkpoint.get("manifest") != report["manifest"]:
        raise ValueError("checkpoint manifest does not match verification manifest")
    phase_a = report["phase_a"]
    completed_ids = sorted(
        set(phase_a["contact_profile_valid_ids"])
        & set(phase_a["facts_and_pain_points_valid_ids"])
    )
    checkpoint["phase_a"] = {
        "audited_at": datetime.now(timezone.utc).isoformat(),
        "status": "completed" if len(completed_ids) == report["manifest_rows"] else "incomplete",
        "enrichment_status_counts": phase_a["enrichment_status_counts"],
        "company_type_counts": phase_a["company_type_counts"],
        "contact_profile_valid_count": phase_a["contact_profile_valid"],
        "facts_and_pain_points_valid_count": phase_a["facts_and_pain_points_valid"],
        "completed_ids": completed_ids,
        "contact_profile_invalid_ids": phase_a["contact_profile_invalid_ids"],
        "facts_and_pain_points_invalid_ids": phase_a["facts_and_pain_points_invalid_ids"],
    }
    directory = path.parent
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(checkpoint, handle, indent=2)
            handle.write("\n")
        os.replace(temporary_name, path)
    except Exception:
        Path(temporary_name).unlink(missing_ok=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--summary", action="store_true")
    parser.add_argument("--checkpoint", type=Path,
                        help="atomically replace only the checkpoint's Phase A audit")
    args = parser.parse_args()

    report, valid = verify(args.manifest)
    rendered = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    if args.checkpoint:
        update_checkpoint(args.checkpoint, report)
    if args.summary:
        summary = {
            "manifest_rows": report["manifest_rows"],
            "database_rows": report["database_rows"],
            "phase_a_contact_valid": report["phase_a"]["contact_profile_valid"],
            "phase_a_facts_valid": report["phase_a"]["facts_and_pain_points_valid"],
            "market_profile_status_counts": report["phase_b"]["market_profile_status_counts"],
            "glassdoor_status_counts": report["phase_b"]["glassdoor_status_counts"],
            "ambitionbox_status_counts": report["phase_b"]["ambitionbox_status_counts"],
            "bad_wlb_authority_count": len(report["phase_b"]["bad_wlb_authority_ids"]),
            "bad_salary_authority_count": len(report["phase_b"]["bad_salary_authority_ids"]),
            "historical_levels_fyi_count": len(
                report["phase_b"]["historical_levels_fyi_value_or_evidence_ids"]
            ),
            "valid": valid,
        }
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(rendered)
    return 0 if valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
