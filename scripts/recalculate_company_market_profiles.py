"""Recalculate combined company market fields for an exact manifest."""
from __future__ import annotations

import argparse
import csv
from pathlib import Path

from app.companies.market_profile import calculate_market_profile
from app.db.session import SessionLocal


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--commit-every", type=int, default=25)
    args = parser.parse_args()
    with args.manifest.open(newline="", encoding="utf-8") as handle:
        ids = [int(row["company_id"]) for row in csv.DictReader(handle)]
    with SessionLocal() as session:
        for index, company_id in enumerate(ids, start=1):
            calculate_market_profile(session, company_id)
            if index % args.commit_every == 0:
                session.commit()
        session.commit()
    print(f"recalculated={len(ids)}")


if __name__ == "__main__":
    main()
