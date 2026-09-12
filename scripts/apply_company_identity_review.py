"""Overlay reviewed ambiguous identities onto a complete deterministic artifact."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def apply_reviews(base: dict, reviewed: dict) -> dict:
    key = reviewed.get("collection_key")
    if key not in {"judgments", "resolutions"} or not isinstance(base.get(key), list):
        raise ValueError("review artifact does not match the base collection")
    overlays = {int(row["company_id"]): row for row in reviewed.get("review_required", [])}
    if any(row.get("status") == "review_required" for row in overlays.values()):
        raise ValueError("every reviewed row must have a terminal status")
    allowed = {
        int(row["company_id"]) for row in base[key]
        if row.get("status") == "review_required"
    }
    if set(overlays) != allowed:
        raise ValueError("reviewed IDs must exactly match the ambiguous base rows")
    result = dict(base)
    result[key] = [
        ({**row, **overlays[int(row["company_id"])]}
         if int(row["company_id"]) in overlays else row)
        for row in base[key]
    ]
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base", type=Path)
    parser.add_argument("reviewed", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    merged = apply_reviews(
        json.loads(args.base.read_text(encoding="utf-8")),
        json.loads(args.reviewed.read_text(encoding="utf-8")),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
    key = next(key for key in ("judgments", "resolutions") if key in merged)
    print(f"merged={len(merged[key])} artifact={args.output}")


if __name__ == "__main__":
    main()
