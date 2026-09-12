"""
Canonical-row selection for duplicate `Company` rows — the rule both
`scripts/dedup_companies.py` (the merge script) and
`app/pipeline/upsert.py` (`get_or_create_company`, which must resolve to
one row *during* a live upsert, not just during a batch merge) need to
agree on.

Extracted from `scripts/dedup_companies.py`, where it originally lived,
for the same reason `app/companies/naming.py` was extracted from there
first: a second, drifting copy in the upsert path would pick a different
winner than the dedup script and silently re-split companies the dedup
script had already merged. One rule, one module, imported by both.
"""
from app.models.orm import Company


def pick_canonical(group: list[Company]) -> Company:
    """Prefer real contact-search progress ('partial'/'done'), then most
    jobs, then lowest id. A row already marked 'duplicate' — from a prior
    merge — must never be eligible: treating "non-pending" as "has
    progress" silently included 'duplicate' too, which let a stale
    already-merged shell row outrank a genuinely untouched company and get
    chosen as the new canonical (confirmed live: 141 groups this way, jobs
    re-pointed onto a row that was itself already flagged as a duplicate
    of something else). Only falls back to an already-duplicate row if
    literally every group member is duplicate."""
    real_progress = {"partial", "done"}
    eligible = [c for c in group if c.contact_enrichment_status != "duplicate"] or group

    def score(c: Company) -> tuple[int, int, int]:
        has_progress = 0 if c.contact_enrichment_status in real_progress else 1
        job_count = -len(c.jobs)
        return (has_progress, job_count, c.id)
    return sorted(eligible, key=score)[0]
