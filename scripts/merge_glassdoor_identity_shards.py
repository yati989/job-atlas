"""Merge and validate exact-manifest Glassdoor identity shard artifacts."""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


_EMPLOYER_ID = re.compile(r"EI_IE(\d+)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--shard", action="append", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    manifest_rows = list(csv.DictReader(Path(args.manifest).open(encoding="utf-8", newline="")))
    manifest_ids = [int(row["company_id"]) for row in manifest_rows]
    manifest_names = {int(row["company_id"]): row["company_name"] for row in manifest_rows}
    merged: dict[int, dict] = {}
    for shard_path in args.shard:
        payload = json.loads(Path(shard_path).read_text(encoding="utf-8"))
        for row in payload.get("resolutions", []):
            company_id = row.get("company_id")
            if company_id not in manifest_names:
                raise ValueError(f"company {company_id} is outside the manifest")
            if company_id in merged:
                raise ValueError(f"duplicate company {company_id} across shards")
            if row.get("company_name") != manifest_names[company_id]:
                raise ValueError(f"company name mismatch for {company_id}")
            status = row.get("status")
            if status not in {"accepted", "review_required", "unresolved", "error"}:
                raise ValueError(f"invalid status {status!r} for {company_id}")
            if status == "accepted":
                employer_id = str(row.get("employer_id") or "")
                match = _EMPLOYER_ID.search(str(row.get("overview_url") or ""))
                if not employer_id.isdigit() or match is None or match.group(1) != employer_id:
                    raise ValueError(f"invalid accepted identity for {company_id}")
            merged[company_id] = row

    missing = [company_id for company_id in manifest_ids if company_id not in merged]
    if missing:
        raise ValueError(f"manifest companies absent from shards: {missing[:20]}")
    ordered = [merged[company_id] for company_id in manifest_ids]
    payload = {
        "schema_version": 1,
        "source": "public_serper_glassdoor_search",
        "manifest": args.manifest,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "resolutions": ordered,
        "status_counts": dict(Counter(row["status"] for row in ordered)),
    }
    Path(args.output).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"merged={len(ordered)} status_counts={payload['status_counts']}")


if __name__ == "__main__":
    main()
