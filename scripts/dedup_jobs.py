"""
Cross-source duplicate detection: the same real-world posting often appears
on more than one job board (e.g. LinkedIn and Naukri) under a different
(source, external_job_id), so the existing DB-level dedup key never catches
it — see app/pipeline/upsert.py's module docstring for that key's scope.

This is a flag, not a merge: a later-first-seen duplicate gets
duplicate_of_job_id pointed at the earliest-seen canonical row for the same
real job. Nothing is deleted, and downstream consumers (Excel export,
Milestone 2 prospecting) filter WHERE duplicate_of_job_id IS NULL to see the
canonical set. False merges are worse than missed duplicates here (a merge
silently hides a real distinct job), so matching is deliberately
conservative:

- Blocking key is the *exact* normalized company name (legal-suffix and
  punctuation stripped) — jobs at different companies are never compared,
  so most of the false-positive risk is already fenced off before any fuzzy
  title comparison happens.
- Within a company block, only cross-source pairs are compared (same-source
  dupes are already caught by the (source, external_job_id) key).
- Title similarity (rapidfuzz token_sort_ratio on normalized titles) above
  HIGH_THRESHOLD auto-flags; the band between REVIEW_THRESHOLD and
  HIGH_THRESHOLD is written to a CSV for manual review only, never
  auto-flagged.
- High-volume employers with generic, repeated titles (e.g. several
  distinct "Data Scientist" reqs open at once at the same company) defeat
  title+company matching alone — confirmed against real data, where two
  genuinely separate instahyre postings for "Data Scientist" at the same
  company, 9 days apart, would otherwise have been merged. So a
  posted_at-proximity gate (POSTED_AT_TOLERANCE_DAYS) is also required for
  auto-flagging: both jobs must have a posted_at within that window. If
  either side is missing posted_at (true for some sources), a high title
  score is downgraded to the review-only tier instead of auto-flagged.
- Some employers post the same role text per-country as genuinely distinct
  reqs (confirmed against real data: Bjak's "Applied AI Engineer" posted
  separately for India/Austria/Malaysia/Ireland) — and title normalization
  strips parenthetical content like "(India)" as noise (originally meant
  to strip "(Remote)"/"(Hybrid)"), which erases exactly the tag that would
  disambiguate them. So a location-compatibility gate is also required:
  if both jobs have a non-empty location_raw and share no normalized
  location token, a high title score is downgraded to review instead of
  auto-flagged, even if posted_at lines up.
- Blocking groups larger than MAX_GROUP_SIZE are skipped and reported
  (mostly "Unknown"/empty company names) rather than doing an O(n^2)
  comparison with a blocking key too weak to be useful there.

Usage:
    python -m scripts.dedup_jobs --dry-run   # report only, no DB writes
    python -m scripts.dedup_jobs             # flag high-confidence matches
"""
import argparse
import csv
import re
from collections import defaultdict
from datetime import date as date_cls, datetime, timezone
from pathlib import Path

from rapidfuzz import fuzz

from app.models.orm import Job

LOG_ROOT = Path(__file__).resolve().parent.parent / "logs" / "dedup_runs"

HIGH_THRESHOLD = 95.0     # auto-flag as duplicate
REVIEW_THRESHOLD = 85.0   # log for manual review, don't auto-flag
MAX_GROUP_SIZE = 60       # skip blocking groups larger than this (weak key)
POSTED_AT_TOLERANCE_DAYS = 5  # second gate for auto-flagging, see module docstring

_LEGAL_SUFFIXES = re.compile(
    r"\b(pvt|private|ltd|limited|llc|inc|incorporated|corp|corporation|plc|gmbh|co)\b\.?",
    re.IGNORECASE,
)
_NON_ALNUM = re.compile(r"[^a-z0-9 ]+")
_PAREN = re.compile(r"\([^)]*\)")
_WHITESPACE = re.compile(r"\s+")


def normalize_company(name: str) -> str:
    name = (name or "").lower()
    name = _LEGAL_SUFFIXES.sub(" ", name)
    name = _NON_ALNUM.sub(" ", name)
    return _WHITESPACE.sub(" ", name).strip()


def normalize_title(title: str) -> str:
    title = (title or "").lower()
    title = _PAREN.sub(" ", title)
    title = _NON_ALNUM.sub(" ", title)
    return _WHITESPACE.sub(" ", title).strip()


_LOCATION_NOISE = {
    "remote", "hybrid", "onsite", "on-site", "various", "multiple",
    "locations", "location", "in", "india",
}


def _location_tokens(location_raw: str) -> set[str]:
    cleaned = _NON_ALNUM.sub(" ", (location_raw or "").lower())
    return {t for t in cleaned.split() if t and t not in _LOCATION_NOISE}


def _location_compatible(job_a: Job, job_b: Job) -> bool:
    tokens_a = _location_tokens(job_a.location_raw)
    tokens_b = _location_tokens(job_b.location_raw)
    if not tokens_a or not tokens_b:
        return True  # missing/uninformative data on one side — don't block on it
    return bool(tokens_a & tokens_b)


def _posted_at_close_enough(job_a: Job, job_b: Job) -> bool:
    if job_a.posted_at is None or job_b.posted_at is None:
        return False

    def as_utc(value: datetime) -> datetime:
        # SQLite can return a timezone-aware column without its offset, while
        # another in-memory/source value in the same comparison remains aware.
        # Stored naive timestamps in this project represent UTC.
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    delta = abs((as_utc(job_a.posted_at) - as_utc(job_b.posted_at)).total_seconds())
    return delta <= POSTED_AT_TOLERANCE_DAYS * 86400


def _resolve_canonical(job: Job, by_id: dict[int, Job]) -> Job:
    """Follow an existing duplicate_of_job_id chain to its root, so a job
    that itself already got flagged never becomes another job's canonical
    target (keeps the duplicate graph flat, never chained)."""
    seen = set()
    current = job
    while current.duplicate_of_job_id is not None and current.id not in seen:
        seen.add(current.id)
        parent = by_id.get(current.duplicate_of_job_id)
        if parent is None:
            break
        current = parent
    return current


def find_candidates(jobs: list[Job]) -> tuple[list[tuple[Job, Job, float]], list[tuple[Job, Job, float]], list[str]]:
    """Returns (auto_flag_pairs, review_pairs, skipped_group_labels)."""
    by_id = {j.id: j for j in jobs}
    groups: dict[str, list[Job]] = defaultdict(list)
    for job in jobs:
        key = normalize_company(job.company_name_raw)
        groups[key].append(job)

    auto_flag: list[tuple[Job, Job, float]] = []
    review: list[tuple[Job, Job, float]] = []
    skipped: list[str] = []

    for company_key, group in groups.items():
        if not company_key:
            continue
        if len(group) > MAX_GROUP_SIZE:
            skipped.append(f"{company_key!r} ({len(group)} jobs)")
            continue

        for i in range(len(group)):
            job_a = group[i]
            norm_title_a = normalize_title(job_a.title)
            for j in range(i + 1, len(group)):
                job_b = group[j]
                if job_a.source == job_b.source:
                    continue  # same-source dupes are already caught upstream

                score = fuzz.token_sort_ratio(norm_title_a, normalize_title(job_b.title))
                if score >= HIGH_THRESHOLD:
                    if _posted_at_close_enough(job_a, job_b) and _location_compatible(job_a, job_b):
                        auto_flag.append((job_a, job_b, score))
                    else:
                        review.append((job_a, job_b, score))
                elif score >= REVIEW_THRESHOLD:
                    review.append((job_a, job_b, score))

    return auto_flag, review, skipped


def run(dry_run: bool) -> dict:
    # The public SQLite workflow imports ``find_candidates`` from this module.
    # Keep the legacy PostgreSQL session lazy so importing the installed CLI
    # does not require the optional psycopg2 adapter.
    from app.db.session import get_session

    with get_session() as session:
        jobs = (
            session.query(Job)
            .filter(Job.status == "active", Job.duplicate_of_job_id.is_(None))
            .all()
        )
        by_id = {j.id: j for j in jobs}

        auto_flag, review, skipped = find_candidates(jobs)

        print(f"Active, unflagged jobs scanned: {len(jobs)}")
        print(f"Auto-flag candidates (score >= {HIGH_THRESHOLD}): {len(auto_flag)}")
        print(f"Review-only candidates ({REVIEW_THRESHOLD} <= score < {HIGH_THRESHOLD}): {len(review)}")
        if skipped:
            print(f"Skipped {len(skipped)} oversized/weak blocking group(s): {', '.join(skipped)}")

        run_date = date_cls.today().isoformat()
        run_dir = LOG_ROOT / run_date
        run_dir.mkdir(parents=True, exist_ok=True)
        review_path = run_dir / "review_candidates.csv"
        with review_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["score", "job_a_id", "job_a_source", "job_a_title", "job_b_id", "job_b_source", "job_b_title", "company"])
            for job_a, job_b, score in sorted(review, key=lambda t: -t[2]):
                writer.writerow([f"{score:.1f}", job_a.id, job_a.source, job_a.title, job_b.id, job_b.source, job_b.title, job_a.company_name_raw])
        print(f"Review candidates written to {review_path}")

        if dry_run:
            print("\n--dry-run: no rows flagged.")
            return {
                "scanned": len(jobs),
                "auto_flag_candidates": len(auto_flag),
                "review_candidates": len(review),
                "flagged": 0,
                "review_path": str(review_path),
            }

        flagged = 0
        for job_a, job_b, score in auto_flag:
            # Canonical = earliest first_seen_at (following any existing
            # chain so a job already flagged this run isn't re-targeted).
            older, newer = (job_a, job_b) if job_a.first_seen_at <= job_b.first_seen_at else (job_b, job_a)
            canonical = _resolve_canonical(older, by_id)
            if newer.id == canonical.id or newer.duplicate_of_job_id is not None:
                continue
            newer.duplicate_of_job_id = canonical.id
            newer.duplicate_score = score
            flagged += 1

        session.commit()
        print(f"\nFlagged {flagged} job(s) as duplicates.")
        return {
            "scanned": len(jobs),
            "auto_flag_candidates": len(auto_flag),
            "review_candidates": len(review),
            "flagged": flagged,
            "review_path": str(review_path),
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Report candidates without writing duplicate_of_job_id to the DB.",
    )
    args = parser.parse_args()
    run(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
