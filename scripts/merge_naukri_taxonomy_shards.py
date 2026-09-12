"""Merge and validate exact-manifest Naukri identity shard artifacts."""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--shard", action="append", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    manifest_rows = list(csv.DictReader(Path(args.manifest).open(encoding="utf-8", newline="")))
    manifest_ids = [int(row["company_id"]) for row in manifest_rows]
    manifest_id_set = set(manifest_ids)
    merged: dict[int, dict] = {}
    for raw_path in args.shard:
        payload = json.loads(Path(raw_path).read_text(encoding="utf-8"))
        rows = payload.get("judgments")
        if not isinstance(rows, list):
            raise ValueError(f"invalid Naukri shard artifact: {raw_path}")
        for row in rows:
            company_id = row.get("company_id")
            if not isinstance(company_id, int):
                raise ValueError(f"invalid company_id in {raw_path}")
            if company_id not in manifest_id_set:
                raise ValueError(f"company {company_id} is outside the manifest")
            if company_id in merged:
                raise ValueError(f"duplicate company {company_id} across shard artifacts")
            merged[company_id] = row
    missing = [company_id for company_id in manifest_ids if company_id not in merged]
    if missing:
        raise ValueError(f"manifest companies absent from Naukri artifacts: {missing[:20]}")

    ordered = [merged[company_id] for company_id in manifest_ids]
    output = {
        "schema_version": 1,
        "source": "public_naukri_taxonomy",
        "manifest": args.manifest,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "judgments": ordered,
        "status_counts": dict(Counter(row["status"] for row in ordered)),
    }
    Path(args.output).write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
    print(f"merged={len(ordered)} status_counts={output['status_counts']}")


if __name__ == "__main__":
    main()
