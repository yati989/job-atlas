"""
Fuzzy-name company dedup — the sequel to the ad-hoc casing-only dedup run
once mid-session and never checked in (see 2026-08-04 handoff: "166
duplicate company records collapsed... The dedup script is not checked
into the repo... there's no scripts/dedup_companies.py to just re-run").

That earlier pass only merged EXACT-casing duplicates (CrowdStrike vs
Crowdstrike). It missed the much larger class exposed while running
scripts/resolve_company_domains.py at scale: subsidiary/regional-suffix
variants of the same real company — "NTT DATA Services" / "NTT DATA
Americas, Inc" / "ntt data north america" / "NTT DATA, Inc." (9 variants
for NTT DATA alone), "Fractal Analytics Private Limited" / "Fractal
Analytics Ltd.", "Target Corporation India Pvt Ltd" / "TARGET". These
never collided on canonical_domain (both correctly stayed unresolved,
`ambiguous`) so the domain-based collision audit never surfaced them —
this script matches on NORMALIZED NAME instead.

The normalization rule itself lives in app/companies/naming.py, so that
prospect discovery can ask the same "is this the same company?" question
against the identical rule (a second, drifting copy would silently
re-introduce the very duplicate class this script exists to collapse).
Canonical-row selection (`pick_canonical`) lives in app/companies/dedup.py
for the same reason — `app/pipeline/upsert.py` needs the identical rule.

Two tiers, mirroring the caution the domain-resolver's collision guard
already established for this project:
  - Tier 1 (auto-merge): groups whose normalized name is IDENTICAL.
  - Tier 2 (printed, never auto-merged): groups sharing a normalized
    PREFIX of 2+ tokens but not identical — plausible but not certain,
    same "route to human/agent review rather than guess" policy as
    resolve_company_domains.py's ambiguous bucket.

Canonical-row selection per Tier-1 group (same rule as the earlier
casing dedup): prefer the row with real contact-search progress
(contact_enrichment_status != 'pending'), else the row with the most
jobs, tie-broken by lowest id. Jobs and contacts are re-pointed to the
canonical row BEFORE the loser is marked `contact_enrichment_status =
'duplicate'` — no data is deleted, and the merge is reversible with a
one-line status flip if a group turns out wrong.

Usage:
    python -m scripts.dedup_companies                # Tier 1 + Tier 2 preview, no writes
    python -m scripts.dedup_companies --apply         # Tier 1 groups actually merged
"""
import argparse
from collections import defaultdict

from sqlalchemy import select, update
from app.companies.dedup import pick_canonical
from app.companies.naming import PLACEHOLDER_NORMS as _PLACEHOLDER_NORMS, normalize
from app.db.session import get_session
from app.models.orm import Company, Job, Contact


def _emit(message: str) -> None:
    """Write progress best-effort; dedup must not depend on stdout."""
    try:
        print(message)
    except OSError:
        # A long-running full-pipeline process can outlive the shell wrapper
        # that owns its Windows stdout handle. Reporting is nonessential;
        # database and dedup exceptions must still propagate normally.
        pass


def _prefix_tokens(norm: str, n: int) -> tuple[str, ...]:
    return tuple(norm.split()[:n])


def find_tier1_groups(companies: list[Company]) -> dict[str, list[Company]]:
    groups: dict[str, list[Company]] = defaultdict(list)
    for c in companies:
        norm = normalize(c.name)
        if norm in _PLACEHOLDER_NORMS:
            continue
        # Require some real content — a normalized name of 1 very short
        # token (<=2 chars) is too generic to trust as a merge key (would
        # false-collide unrelated single-letter/acronym shells).
        if norm and len(norm.replace(" ", "")) > 2:
            groups[norm].append(c)
    return {k: v for k, v in groups.items() if len(v) > 1}


def find_tier2_candidates(companies: list[Company], tier1_norms: set[str]) -> dict[tuple[str, ...], list[Company]]:
    """2+ token normalized-prefix matches that aren't already exact (Tier 1)
    matches — printed for human/agent review, never auto-merged."""
    by_prefix: dict[tuple[str, ...], list[Company]] = defaultdict(list)
    for c in companies:
        norm = normalize(c.name)
        if norm in tier1_norms or norm in _PLACEHOLDER_NORMS:
            continue
        prefix = _prefix_tokens(norm, 2)
        if len(prefix) == 2 and sum(len(t) for t in prefix) > 4:
            by_prefix[prefix].append(c)
    return {k: v for k, v in by_prefix.items() if len(v) > 1}


def run(apply: bool) -> dict:
    """Tier 1 merge (+ Tier 2 preview), callable from the pipeline as well
    as the CLI. Tier 2 is NEVER merged here regardless of `apply` — see
    module docstring — its groups are only ever returned for a human/agent
    review surface (e.g. the full-pipeline workbook's Review sheet)."""
    with get_session() as session:
        companies = session.execute(select(Company)).scalars().all()
        tier1 = find_tier1_groups(companies)
        tier2 = find_tier2_candidates(companies, set(tier1.keys()))

        _emit(f"=== Tier 1: exact-normalized-name matches ({len(tier1)} groups) ===")
        total_losers = 0
        total_jobs_repointed = 0
        total_contacts_repointed = 0
        for norm, group in sorted(tier1.items(), key=lambda kv: -len(kv[1])):
            canonical = pick_canonical(group)
            losers = [c for c in group if c.id != canonical.id]
            _emit(f"  [{norm}] canonical={canonical.id} ({canonical.name!r}) "
                  f"<- {[(c.id, c.name) for c in losers]}")
            total_losers += len(losers)
            for loser in losers:
                job_count = len(loser.jobs)
                contact_count = len(loser.contacts)
                total_jobs_repointed += job_count
                total_contacts_repointed += contact_count
                if apply:
                    session.execute(update(Job).where(Job.company_id == loser.id).values(company_id=canonical.id))
                    session.execute(update(Contact).where(Contact.company_id == loser.id).values(company_id=canonical.id))
                    loser.contact_enrichment_status = "duplicate"

        _emit(f"\nTier 1 total: {total_losers} duplicate rows, "
              f"{total_jobs_repointed} jobs + {total_contacts_repointed} contacts to re-point")

        _emit(f"\n=== Tier 2: 2-token prefix matches, NOT auto-merged ({len(tier2)} groups) ===")
        for prefix, group in sorted(tier2.items(), key=lambda kv: -len(kv[1]))[:40]:
            _emit(f"  [{' '.join(prefix)}...] {[(c.id, c.name) for c in group]}")
        if len(tier2) > 40:
            _emit(f"  ... and {len(tier2) - 40} more groups")

        tier2_candidates = [
            {"prefix": " ".join(prefix), "companies": [(c.id, c.name) for c in group]}
            for prefix, group in sorted(tier2.items(), key=lambda kv: -len(kv[1]))
        ]

        if apply:
            session.commit()
            _emit("\n--apply: Tier 1 merges committed.")
        else:
            _emit("\nPreview only — rerun with --apply to commit Tier 1 merges.")

        return {
            "tier1_groups": len(tier1),
            "tier1_merged": total_losers if apply else 0,
            "jobs_repointed": total_jobs_repointed if apply else 0,
            "contacts_repointed": total_contacts_repointed if apply else 0,
            "tier2_groups": len(tier2),
            "tier2_candidates": tier2_candidates,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="actually merge Tier 1 groups (default: preview only)")
    args = parser.parse_args()
    run(apply=args.apply)


if __name__ == "__main__":
    main()
