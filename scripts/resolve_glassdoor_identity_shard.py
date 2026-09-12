"""Harvest one exact-manifest Glassdoor identity shard through public Serper search."""

from __future__ import annotations

import argparse
import csv
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from app.companies.market_profile_batch import (
    _glassdoor_title_company,
    _source_slug,
)
from app.companies.naming import normalize
from app.config import settings
from app.contacts.search_source import SERPER_ENDPOINT


_EMPLOYER_ID = re.compile(r"EI_IE(\d+)")


def _search(query: str, fallback_query: str) -> list[dict[str, str]]:
    headers = {
        "X-API-KEY": str(settings.SERPER_API_KEY),
        "Content-Type": "application/json",
    }
    with httpx.Client(timeout=20.0, headers=headers) as client:
        for candidate_query in (query, fallback_query):
            response = client.post(
                SERPER_ENDPOINT,
                json={"q": candidate_query, "gl": "in", "num": 10},
            )
            if response.status_code == 400 and candidate_query != fallback_query:
                continue
            response.raise_for_status()
            data = response.json()
            return [
                {
                    "title": str(row.get("title") or ""),
                    "link": str(row.get("link") or ""),
                    "snippet": str(row.get("snippet") or ""),
                }
                for row in (data.get("organic") or [])
            ]
    return []


def _candidates(results: list[dict[str, str]]) -> list[dict[str, str]]:
    candidates: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for result in results:
        link = str(result.get("link") or "")
        match = _EMPLOYER_ID.search(link)
        if match is None or "/Overview/" not in link:
            continue
        observed_name = _glassdoor_title_company(str(result.get("title") or ""))
        if not observed_name:
            continue
        key = (match.group(1), observed_name)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(
            {
                "observed_name": observed_name,
                "employer_id": match.group(1),
                "overview_url": link,
            }
        )
    return candidates


def _resolve(company_id: int, company_name: str) -> dict[str, Any]:
    attempts: list[dict[str, Any]] = []
    exact_query = f'site:glassdoor.com/Overview "Working at" "{company_name}"'
    exact_results = _search(exact_query, f"{company_name} Glassdoor Overview")
    candidates = _candidates(exact_results)
    attempts.append(
        {
            "pass": "original",
            "query": exact_query,
            "candidate_count": len(candidates),
        }
    )

    core = normalize(company_name)
    if not candidates and core and core.casefold() != company_name.strip().casefold():
        core_query = f'site:glassdoor.com/Overview "Working at" "{core}"'
        core_candidates = _candidates(
            _search(core_query, f"{core} Glassdoor Overview"),
        )
        attempts.append(
            {
                "pass": "core",
                "query": core_query,
                "candidate_count": len(core_candidates),
            }
        )
        candidates.extend(core_candidates)

    legal_matches = [
        candidate
        for candidate in candidates
        if _source_slug(candidate["observed_name"]) == _source_slug(company_name)
    ]
    unique_legal_ids = {candidate["employer_id"] for candidate in legal_matches}
    if len(unique_legal_ids) == 1:
        candidate = legal_matches[0]
        return {
            "company_id": company_id,
            "company_name": company_name,
            "status": "accepted",
            **candidate,
            "resolution_pass": attempts[-1]["pass"],
            "review_note": "Exact or legal-suffix-only public Overview identity.",
            "search_attempts": attempts,
        }

    normalized_matches = [
        candidate
        for candidate in candidates
        if normalize(candidate["observed_name"]) == normalize(company_name)
    ]
    review_candidates = normalized_matches or candidates
    if review_candidates:
        return {
            "company_id": company_id,
            "company_name": company_name,
            "status": "review_required",
            "observed_name": None,
            "employer_id": None,
            "overview_url": None,
            "resolution_pass": attempts[-1]["pass"],
            "review_note": (
                "Parent, regional, duplicate, or looser candidates require "
                "company-ID-keyed agent review."
            ),
            "search_attempts": attempts,
            "candidates": review_candidates,
        }

    return {
        "company_id": company_id,
        "company_name": company_name,
        "status": "unresolved",
        "observed_name": None,
        "employer_id": None,
        "overview_url": None,
        "resolution_pass": attempts[-1]["pass"],
        "review_note": "No public Glassdoor Overview candidate found in two shallow passes.",
        "search_attempts": attempts,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--seed", required=True)
    parser.add_argument("--shard", type=int, required=True)
    parser.add_argument("--shards", type=int, default=4)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    if not 1 <= args.shard <= args.shards:
        parser.error("--shard must be within 1..--shards")

    rows = list(csv.DictReader(Path(args.manifest).open(encoding="utf-8", newline="")))
    seeded = {
        item["company_id"]: item
        for item in json.loads(Path(args.seed).read_text(encoding="utf-8"))["resolutions"]
    }
    shard_size = (len(rows) + args.shards - 1) // args.shards
    start = (args.shard - 1) * shard_size
    selected = rows[start : start + shard_size]
    output_path = Path(args.output)
    completed: dict[int, dict[str, Any]] = {}
    if output_path.exists():
        existing = json.loads(output_path.read_text(encoding="utf-8"))
        completed = {
            item["company_id"]: item
            for item in existing.get("resolutions", [])
            if item.get("status") != "error"
        }
    resolutions: list[dict[str, Any]] = []
    for row in selected:
        company_id = int(row["company_id"])
        if company_id in completed:
            resolutions.append(completed[company_id])
            continue
        seed = seeded[company_id]
        if seed["status"] == "accepted":
            resolutions.append(seed)
        else:
            try:
                resolutions.append(_resolve(company_id, row["company_name"]))
            except Exception as exc:
                resolutions.append(
                    {
                        "company_id": company_id,
                        "company_name": row["company_name"],
                        "status": "error",
                        "observed_name": None,
                        "employer_id": None,
                        "overview_url": None,
                        "resolution_pass": "original",
                        "review_note": f"{type(exc).__name__}: {exc}",
                    }
                )

        incremental = {
            "schema_version": 1,
            "source": "public_serper_glassdoor_search",
            "manifest": args.manifest,
            "shard": args.shard,
            "shards": args.shards,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "resolutions": resolutions,
        }
        temporary = output_path.with_suffix(output_path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(incremental, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(output_path)

    payload = {
        "schema_version": 1,
        "source": "public_serper_glassdoor_search",
        "manifest": args.manifest,
        "shard": args.shard,
        "shards": args.shards,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "resolutions": resolutions,
    }
    output_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    counts: dict[str, int] = {}
    for resolution in resolutions:
        status = resolution["status"]
        counts[status] = counts.get(status, 0) + 1
    print(f"shard={args.shard} rows={len(resolutions)} status_counts={counts}")


if __name__ == "__main__":
    main()
