"""Harvest one exact company-manifest shard from the public Naukri taxonomy."""

from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timezone
from pathlib import Path

from app.companies.ambitionbox import AmbitionBoxTarget, NaukriTaxonomyResolver
from app.companies.market_profile_batch import _source_slug


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--shard", type=int, required=True)
    parser.add_argument("--shards", type=int, default=4)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    if not 1 <= args.shard <= args.shards:
        parser.error("--shard must be within 1..--shards")

    rows = list(csv.DictReader(Path(args.manifest).open(encoding="utf-8", newline="")))
    shard_size = (len(rows) + args.shards - 1) // args.shards
    start = (args.shard - 1) * shard_size
    selected = rows[start : start + shard_size]
    targets = [
        AmbitionBoxTarget(
            company_id=int(row["company_id"]),
            company_name=row["company_name"],
            salary_role=row["latest_job_title"],
            slug=_source_slug(row["company_name"]),
            salary_url=(
                "https://www.ambitionbox.com/salaries/"
                f"{_source_slug(row['company_name'])}-salaries"
            ),
        )
        for row in selected
    ]
    with NaukriTaxonomyResolver(workers=1) as resolver:
        resolutions = resolver.resolve(targets)

    judgments = []
    for target, resolution in zip(targets, resolutions, strict=True):
        evidence = resolution.evidence
        if resolution.status == "resolved":
            judgments.append({
                "company_id": target.company_id,
                "company": target.company_name,
                "status": "accepted",
                "accepted_slug": evidence["resolved_slug"],
                "accepted_company": evidence["resolved_company"],
                "salary_url": evidence["resolved_url"],
                "match_type": evidence["judgment"]["decision"],
                "reason": evidence["judgment"]["reason"],
                "evidence": evidence,
            })
        elif resolution.status == "review_required":
            judgments.append({
                "company_id": target.company_id,
                "company": target.company_name,
                "status": "review_required",
                "accepted_slug": None,
                "accepted_company": None,
                "salary_url": None,
                "match_type": None,
                "reason": "Naukri candidates require company-ID-keyed review before collection.",
                "evidence": evidence,
            })
        else:
            judgments.append({
                "company_id": target.company_id,
                "company": target.company_name,
                "status": resolution.status,
                "accepted_slug": None,
                "accepted_company": None,
                "salary_url": None,
                "match_type": None,
                "reason": resolution.error or "No compatible Naukri taxonomy candidate returned.",
                "evidence": evidence,
            })

    output = {
        "schema_version": 1,
        "source": "public_naukri_taxonomy",
        "manifest": args.manifest,
        "shard": args.shard,
        "shards": args.shards,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "judgments": judgments,
    }
    Path(args.output).write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
    counts: dict[str, int] = {}
    for judgment in judgments:
        counts[judgment["status"]] = counts.get(judgment["status"], 0) + 1
    print(f"shard={args.shard} rows={len(judgments)} status_counts={counts}")


if __name__ == "__main__":
    main()
