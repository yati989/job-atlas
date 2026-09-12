"""Persist agent-reviewed Phase A company judgments in bounded transactions.

The input must be a JSON array of company-ID-keyed judgments.  This command
does not research or infer values; it validates and persists the agent's
review so a bulk enrollment cannot mistake a coarse status flag for a complete
contact profile.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.contacts.company_profile import record_contact_company_profile
from app.contacts.email_resolution import _get_mx_host
from app.db.session import SessionLocal
from app.models.orm import Company


PAIN_POINT_PREFIXES = (
    "[posting-inferred]",
    "[company-inferred]",
    "[insufficient-evidence]",
)


def _require_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _validate_judgment(judgment: dict[str, Any]) -> None:
    _require_text(judgment.get("industry"), "industry")
    _require_text(judgment.get("description"), "description")
    pain_points = _require_text(judgment.get("pain_points"), "pain_points")
    if not all(
        line.strip().startswith(PAIN_POINT_PREFIXES)
        for line in pain_points.splitlines()
        if line.strip()
    ):
        raise ValueError("each pain-point line needs an approved evidence prefix")
    if judgment.get("company_type") not in {"employer", "staffing"}:
        raise ValueError("company_type must be employer or staffing")
    groups = judgment.get("search_groups")
    if not isinstance(groups, list) or any(
        group not in {"data_ai", "credit_risk"} for group in groups
    ):
        raise ValueError("search_groups must be a list of known groups")
    if len(groups) != len(set(groups)):
        raise ValueError("search_groups must not contain duplicates")
    status = judgment.get("domain_resolution_status")
    domain = judgment.get("canonical_domain")
    if status == "done":
        _require_text(domain, "canonical_domain")
    elif status == "unresolvable":
        if domain is not None:
            raise ValueError("unresolvable domain status requires null canonical_domain")
    else:
        raise ValueError("domain_resolution_status must be done or unresolvable")


def apply(judgments: list[dict[str, Any]], *, commit_every: int) -> None:
    ids = [judgment.get("company_id") for judgment in judgments]
    if not all(isinstance(company_id, int) for company_id in ids):
        raise ValueError("every judgment needs an integer company_id")
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate company_id in judgments")
    for judgment in judgments:
        _validate_judgment(judgment)

    with SessionLocal() as session:
        for index, judgment in enumerate(judgments, 1):
            company_id = judgment["company_id"]
            company = session.get(Company, company_id)
            if company is None:
                raise ValueError(f"company {company_id} does not exist")
            domain = judgment.get("canonical_domain")
            if domain and not _get_mx_host(domain):
                raise ValueError(f"company {company_id}: {domain} does not MX-verify")

            company.industry = _require_text(judgment["industry"], "industry")
            company.description = _require_text(judgment["description"], "description")
            company.pain_points = _require_text(judgment["pain_points"], "pain_points")
            company.employee_count_range = judgment.get("employee_count_range")
            company.founding_year = judgment.get("founding_year")
            company.enrichment_status = "done"
            company.enriched_at = datetime.now(timezone.utc)
            record_contact_company_profile(
                session,
                company_id,
                canonical_domain=domain,
                domain_resolution_status=judgment["domain_resolution_status"],
                company_type=judgment["company_type"],
                search_groups=judgment["search_groups"],
            )
            if index % commit_every == 0:
                session.commit()
        session.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("judgments", type=Path)
    parser.add_argument("--commit-every", type=int, default=15)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    if args.commit_every < 1:
        raise ValueError("--commit-every must be positive")
    parsed = json.loads(args.judgments.read_text(encoding="utf-8"))
    if not isinstance(parsed, list) or not all(isinstance(row, dict) for row in parsed):
        raise ValueError("judgments must be a JSON array of objects")
    selected = parsed[args.offset : args.offset + args.limit if args.limit is not None else None]
    apply(selected, commit_every=args.commit_every)
    print(f"persisted={len(selected)}")


if __name__ == "__main__":
    main()
