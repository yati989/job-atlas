"""Materialize the approved company-enrichment cohort into exact wave manifests."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from app.db.session import SessionLocal
from app.models.orm import Company
from scripts.verify_company_enrichment_wave import verify


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cohort-csv", required=True)
    parser.add_argument("--manifest-dir", required=True)
    parser.add_argument("--checkpoint-dir", required=True)
    parser.add_argument("--wave-size", type=int, default=500)
    args = parser.parse_args()

    cohort_path = Path(args.cohort_csv)
    rows = list(csv.DictReader(cohort_path.open(encoding="utf-8", newline="")))
    ids = [int(row["company_id"]) for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("cohort CSV contains duplicate company IDs")

    manifest_dir = Path(args.manifest_dir)
    checkpoint_dir = Path(args.checkpoint_dir)
    manifest_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_dir.mkdir(parents=True, exist_ok=True)

    with SessionLocal() as session:
        companies = {
            company.id: company
            for company in session.execute(
                select(Company).where(Company.id.in_(ids))
            ).scalars()
        }
        missing = sorted(set(ids) - set(companies))
        if missing:
            raise ValueError(f"cohort IDs missing from database: {missing[:20]}")

        for wave_number, start in enumerate(range(0, len(rows), args.wave_size), 1):
            wave_rows = rows[start : start + args.wave_size]
            wave_ids = [int(row["company_id"]) for row in wave_rows]
            manifest = manifest_dir / f"wave_{wave_number:03d}.csv"
            with manifest.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
                writer.writeheader()
                writer.writerows(wave_rows)

            shard_size = (len(wave_ids) + 3) // 4
            shards = []
            for shard_number in range(4):
                shard_ids = wave_ids[shard_number * shard_size : (shard_number + 1) * shard_size]
                if not shard_ids:
                    continue
                shards.append({
                    "shard": shard_number + 1,
                    "first_row": start + shard_number * shard_size + 1,
                    "last_row": start + shard_number * shard_size + len(shard_ids),
                    "company_ids": shard_ids,
                    "status": "pending",
                })

            audit, _ = verify(manifest)
            phase_a = audit["phase_a"]
            completed_ids = sorted(
                set(phase_a["contact_profile_valid_ids"])
                & set(phase_a["facts_and_pain_points_valid_ids"])
            )

            checkpoint = {
                "schema_version": 1,
                "wave": wave_number,
                "cohort_csv": str(cohort_path),
                "manifest": str(manifest),
                "manifest_sha256": _sha256(manifest),
                "row_start": start + 1,
                "row_end": start + len(wave_rows),
                "company_count": len(wave_rows),
                "created_at": datetime.now(timezone.utc).isoformat(),
                "shards": shards,
                "phase_a": {
                    "audited_at": datetime.now(timezone.utc).isoformat(),
                    "status": "completed" if len(completed_ids) == len(wave_ids) else "incomplete",
                    "enrichment_status_counts": phase_a["enrichment_status_counts"],
                    "company_type_counts": phase_a["company_type_counts"],
                    "contact_profile_valid_count": phase_a["contact_profile_valid"],
                    "facts_and_pain_points_valid_count": phase_a["facts_and_pain_points_valid"],
                    "completed_ids": completed_ids,
                    "contact_profile_invalid_ids": phase_a["contact_profile_invalid_ids"],
                    "facts_and_pain_points_invalid_ids": phase_a["facts_and_pain_points_invalid_ids"],
                },
                "phase_b": {"glassdoor": "pending", "naukri": "pending"},
                "glassdoor": {"snapshot_id": None, "status": "not_triggered"},
                "ambitionbox": {"started_ids": [], "completed_ids": [], "status": "pending"},
                "next_safe_resume_step": "verify Phase A, then run four Phase B identity shards",
            }
            checkpoint_path = checkpoint_dir / f"enrichment_wave_{wave_number:03d}_checkpoint.json"
            checkpoint_path.write_text(json.dumps(checkpoint, indent=2) + "\n", encoding="utf-8")
            print(f"wave={wave_number:03d} rows={len(wave_rows)} manifest={manifest} checkpoint={checkpoint_path}")


if __name__ == "__main__":
    main()
