"""Export only ambiguous company identity rows for agent review."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def prepare(payload: dict) -> dict:
    key = "judgments" if isinstance(payload.get("judgments"), list) else "resolutions"
    rows = []
    for row in payload.get(key, []):
        if row.get("status") != "review_required":
            continue
        evidence = row.get("evidence") if isinstance(row.get("evidence"), dict) else {}
        rows.append({
            "company_id": row.get("company_id"),
            "company": row.get("company") or row.get("company_name"),
            "status": "review_required",
            "reason": row.get("reason") or row.get("review_note"),
            "candidates": row.get("candidates") or evidence.get("candidates") or [],
        })
    return {"collection_key": key, "review_required": rows}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    compact = prepare(json.loads(args.input.read_text(encoding="utf-8")))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(compact, indent=2) + "\n", encoding="utf-8")
    print(f"review_required={len(compact['review_required'])} artifact={args.output}")


if __name__ == "__main__":
    main()
