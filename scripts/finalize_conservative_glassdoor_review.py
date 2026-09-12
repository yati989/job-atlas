"""Finalize non-exact Glassdoor candidates as unresolved after bounded review."""
from __future__ import annotations
import argparse, json
from pathlib import Path

def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("source", type=Path)
    p.add_argument("output", type=Path)
    args = p.parse_args()
    payload = json.loads(args.source.read_text(encoding="utf-8"))
    reviewed = []
    for row in payload["resolutions"]:
        if row.get("status") == "accepted":
            reviewed.append(row)
            continue
        reviewed.append({
            "company_id": row["company_id"],
            "company_name": row["company_name"],
            "status": "unresolved",
            "observed_name": None,
            "employer_id": None,
            "overview_url": None,
            "resolution_pass": row.get("resolution_pass", "review"),
            "review_note": "No exact/core-brand Glassdoor Overview identity accepted after the two permitted shallow searches; candidate rejected or unresolved.",
        })
    args.output.write_text(json.dumps({**payload, "resolutions": reviewed, "status_counts": {"accepted": sum(x["status"] == "accepted" for x in reviewed), "unresolved": sum(x["status"] == "unresolved" for x in reviewed)}}, indent=2) + "\n", encoding="utf-8")
    print(f"reviewed={len(reviewed)}")

if __name__ == "__main__":
    main()
