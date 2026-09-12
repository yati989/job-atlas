"""Sequentially retry only error rows in an exact Naukri taxonomy artifact."""

from __future__ import annotations

import argparse
import csv
import json
import os
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.companies.ambitionbox import AmbitionBoxTarget, NaukriTaxonomyResolver
from app.companies.market_profile_batch import _source_slug


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def _judgment(target: AmbitionBoxTarget, resolution: Any) -> dict[str, Any]:
    evidence = resolution.evidence
    if resolution.status == "resolved":
        return {
            "company_id": target.company_id,
            "company": target.company_name,
            "status": "accepted",
            "accepted_slug": evidence["resolved_slug"],
            "accepted_company": evidence["resolved_company"],
            "salary_url": evidence["resolved_url"],
            "match_type": evidence["judgment"]["decision"],
            "reason": evidence["judgment"]["reason"],
            "evidence": evidence,
        }
    if resolution.status == "review_required":
        return {
            "company_id": target.company_id,
            "company": target.company_name,
            "status": "review_required",
            "accepted_slug": None,
            "accepted_company": None,
            "salary_url": None,
            "match_type": None,
            "reason": "Naukri candidates require company-ID-keyed review before collection.",
            "evidence": evidence,
        }
    return {
        "company_id": target.company_id,
        "company": target.company_name,
        "status": resolution.status,
        "accepted_slug": None,
        "accepted_company": None,
        "salary_url": None,
        "match_type": None,
        "reason": resolution.error or "No compatible Naukri taxonomy candidate returned.",
        "evidence": evidence,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--interval-seconds", type=float, default=2.0)
    parser.add_argument("--rate-limit-retries", type=int, default=5)
    args = parser.parse_args()

    manifest_path = Path(args.manifest)
    source_path = Path(args.input)
    output_path = Path(args.output)
    manifest_rows = list(
        csv.DictReader(manifest_path.open(encoding="utf-8", newline=""))
    )
    manifest_by_id = {int(row["company_id"]): row for row in manifest_rows}

    payload = json.loads(
        (output_path if output_path.exists() else source_path).read_text()
    )
    judgments = payload["judgments"]
    assert len(judgments) == len(manifest_rows)
    assert {int(item["company_id"]) for item in judgments} == set(manifest_by_id)
    retry_rows = [item for item in judgments if item["status"] == "error"]
    payload.update(
        source="public_naukri_taxonomy_sequential_retry",
        manifest=str(manifest_path),
        retried_from=str(source_path),
        updated_at=datetime.now(timezone.utc).isoformat(),
    )
    _atomic_write(output_path, payload)

    by_id = {int(item["company_id"]): item for item in judgments}
    with NaukriTaxonomyResolver(workers=1) as resolver:
        for index, previous in enumerate(retry_rows, start=1):
            company_id = int(previous["company_id"])
            row = manifest_by_id[company_id]
            target = AmbitionBoxTarget(
                company_id=company_id,
                company_name=row["company_name"],
                salary_role=row["latest_job_title"],
                slug=_source_slug(row["company_name"]),
                salary_url=(
                    "https://www.ambitionbox.com/salaries/"
                    f"{_source_slug(row['company_name'])}-salaries"
                ),
            )
            resolution = None
            for attempt in range(args.rate_limit_retries + 1):
                resolution = resolver.resolve([target])[0]
                if resolution.error not in {"HTTP 403", "HTTP 429"}:
                    break
                if attempt < args.rate_limit_retries:
                    time.sleep(max(15.0, args.interval_seconds * (2 ** attempt)))
            assert resolution is not None
            replacement = _judgment(target, resolution)
            by_id[company_id].clear()
            by_id[company_id].update(replacement)
            payload["updated_at"] = datetime.now(timezone.utc).isoformat()
            payload["last_retried_company_id"] = company_id
            payload["retry_progress"] = {
                "completed": index,
                "total": len(retry_rows),
            }
            _atomic_write(output_path, payload)
            print(
                f"retry={index}/{len(retry_rows)} company_id={company_id} "
                f"status={replacement['status']}",
                flush=True,
            )
            if index < len(retry_rows):
                time.sleep(args.interval_seconds)

    counts = Counter(item["status"] for item in judgments)
    payload["status_counts"] = dict(counts)
    payload["completed_at"] = datetime.now(timezone.utc).isoformat()
    _atomic_write(output_path, payload)
    print(f"status_counts={dict(counts)}")


if __name__ == "__main__":
    main()
