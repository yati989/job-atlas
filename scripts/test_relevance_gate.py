"""
Synthetic-case verification for the central relevance gate (issue #8). No
pytest in this repo — this is the script-based equivalent, per CLAUDE.md.

Usage:
    python -m scripts.test_relevance_gate

Exits nonzero if any case fails.
"""
import sys
from datetime import datetime, timedelta, timezone

from app.models.schemas import NormalizedJob
from app.pipeline.relevance import filter_relevant

NOW = datetime.now(timezone.utc)


def job(
    title: str,
    location_raw: str | None = None,
    is_remote: bool | None = None,
    remote_scope: str | None = None,
    posted_at: datetime | None = None,
    description_raw: str | None = None,
    job_id: str = "1",
) -> NormalizedJob:
    return NormalizedJob(
        source="test",
        external_job_id=job_id,
        title=title,
        company_name_raw="Acme",
        location_raw=location_raw,
        is_remote=is_remote,
        remote_scope=remote_scope,
        posted_at=posted_at,
        description_raw=description_raw,
    )


CASES = [
    # (name, job, expect_kept)
    (
        "off-title-but-in-description (drop)",
        job("Senior Software Engineer", location_raw="Bangalore",
            description_raw="Our team does data science and machine learning"),
        False,
    ),
    (
        "in-title (keep)",
        job("Data Scientist", location_raw="Bangalore"),
        True,
    ),
    (
        "off-role-exclude-word-in-title (drop)",
        job("Sales Data Analyst", location_raw="Bangalore"),
        False,
    ),
    (
        "non-Bangalore onsite (drop)",
        job("Data Analyst", location_raw="Mumbai", is_remote=False),
        False,
    ),
    (
        "Bangalore onsite (keep)",
        job("Data Analyst", location_raw="Bangalore", is_remote=False),
        True,
    ),
    (
        "Bangalore hybrid (keep)",
        job("Data Analyst", location_raw="Hybrid - Bengaluru", is_remote=False),
        True,
    ),
    (
        "non-Bangalore hybrid (drop)",
        job("Data Analyst", location_raw="Hybrid - Mumbai", is_remote=False),
        False,
    ),
    (
        "foreign-only remote (drop)",
        job("Data Scientist", location_raw="United States", is_remote=True),
        False,
    ),
    (
        "foreign remote with 'Remote' token (drop)",  # the common board format
        job("Data Scientist", location_raw="Remote - United States", is_remote=True),
        False,
    ),
    (
        "foreign remote in remote_scope (drop)",
        job("Data Scientist", location_raw="Remote", remote_scope="USA", is_remote=True),
        False,
    ),
    (
        "US-state substring must not read as India (drop)",  # 'Indiana' vs 'india'
        job("Data Scientist", location_raw="Remote - Indiana, USA", is_remote=True),
        False,
    ),
    (
        "bare 'Remote' string location (keep)",
        job("Data Scientist", location_raw="Remote", is_remote=True),
        True,
    ),
    (
        "US-city-pinned remote (drop)",  # US roles pin by city, not country
        job("Data Scientist", location_raw="San Francisco", is_remote=True),
        False,
    ),
    (
        "bare 'US' abbreviation remote (drop)",
        job("Data Scientist", location_raw="Remote, US", is_remote=True),
        False,
    ),
    (
        "India-inclusive multi-region remote (keep)",  # India check wins
        job("Data Scientist", location_raw="Remote - India or New York", is_remote=True),
        True,
    ),
    (
        # Issue #76: Built In / Talent.com write the ISO code, not "India".
        # Without "ind" in INDIA_ELIGIBLE_MARKERS the India check misses and
        # the foreign marker wins, dropping a genuinely India-eligible role.
        "IND country code, India-inclusive multi-region remote (keep)",
        job("Data Scientist", location_raw="Remote - IND or New York", is_remote=True),
        True,
    ),
    (
        "IND country code with US co-location (keep)",  # same shape as above
        job("Data Scientist", location_raw="IND, United States", is_remote=True),
        True,
    ),
    (
        # The reason "ind" is safe to add: _word_match applies word
        # boundaries, so the US state/city never matches the country code and
        # cannot rescue an otherwise-foreign listing. USA is included because
        # "Indiana" itself is absent from FOREIGN_ONLY_MARKERS — without a
        # real foreign marker the row is kept by the ambiguous fallthrough,
        # which would make this case pass for the wrong reason.
        "Indianapolis must not read as IND (drop)",
        job("Data Scientist", location_raw="Remote - Indianapolis, Indiana, USA", is_remote=True),
        False,
    ),
    (
        "underscore-joined title+city (keep)",  # _word_match normalizes '_'
        job("Senior Data Scientist_Bangalore", location_raw="Bangalore", is_remote=False),
        True,
    ),
    (
        "India/APAC remote (keep)",
        job("Data Scientist", location_raw="APAC", is_remote=True),
        True,
    ),
    (
        "bare-remote empty location (keep)",
        job("Data Scientist", location_raw=None, is_remote=True),
        True,
    ),
    (
        "stale (drop)",
        job("Data Scientist", location_raw="Bangalore",
            posted_at=NOW - timedelta(days=200)),
        False,
    ),
    (
        "recent (keep)",
        job("Data Scientist", location_raw="Bangalore",
            posted_at=NOW - timedelta(days=10)),
        True,
    ),
    (
        "undated (keep)",
        job("Data Scientist", location_raw="Bangalore", posted_at=None),
        True,
    ),
    (
        "intern (drop)",
        job("Data Science Intern", location_raw="Bangalore"),
        False,
    ),
    (
        "senior-exec/Director (drop)",
        job("Director of Data Science", location_raw="Bangalore"),
        False,
    ),
    (
        "in-band IC (keep)",
        job("Senior Data Scientist", location_raw="Bangalore"),
        True,
    ),
    (
        "AVP below VP is in-band (keep)",  # override wins over 'vice president'
        job("Associate Vice President, Credit Risk", location_raw="Bengaluru"),
        True,
    ),
    (
        "actual VP still exec (drop)",
        job("VP of Data Science", location_raw="Bangalore"),
        False,
    ),
    (
        "Credit Underwriter (keep)",  # 'underwriter' variant, not just 'underwriting'
        job("Credit Underwriter", location_raw="Bangalore"),
        True,
    ),
    (
        "ML Ops Engineer (keep)",  # 'ml ops' with a space, not just 'mlops'
        job("ML Ops Engineer", location_raw="Bangalore"),
        True,
    ),
    (
        "GenAI Engineer (keep)",  # 'genai' variant
        job("GenAI Engineer", location_raw="Bangalore"),
        True,
    ),
    (
        "Artificial Intelligence Engineer (keep)",  # spelled-out 'ai engineer' alias (issue #49)
        job("Artificial Intelligence Engineer", location_raw="Bangalore"),
        True,
    ),
    (
        "Machine-Learning Engineer, hyphenated (keep)",  # hyphen-as-separator fix (issue #49)
        job("Machine-Learning Engineer", location_raw="Bangalore"),
        True,
    ),
    (
        "Artificial Intelligence in Healthcare Sales Rep (drop)",  # near-miss: role kw present but off-role marker also fires
        job("Artificial Intelligence Sales Representative", location_raw="Bangalore"),
        False,
    ),
]


def main() -> int:
    failures = 0
    for i, (name, test_job, expect_kept) in enumerate(CASES):
        kept, _drop_counts, _drop_details = filter_relevant([test_job])
        actual_kept = len(kept) == 1
        status = "PASS" if actual_kept == expect_kept else "FAIL"
        if status == "FAIL":
            failures += 1
        print(f"[{status}] case {i + 1}: {name} (expected kept={expect_kept}, got kept={actual_kept})")

    total = len(CASES)
    print(f"\n{total - failures}/{total} passed.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
