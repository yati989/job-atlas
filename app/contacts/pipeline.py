"""
The single per-company seam for the contact-finding module: everything
needed to go from a `Company` row to a list of ranked contacts, internally
handling public profile discovery, ladder-derived classification, and email
guess/verification. Per issue #6 this is the one point exercised for
verification (`scripts/smoke_test_contacts.py`).

This function itself never writes to the database — callers (
`scripts/find_contacts.py`) are responsible for persisting the returned
result via `app.contacts.upsert`.
"""
import logging
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.config.categories import ROLE_LADDER
from app.contacts.company_categories import categories_for_company
from app.contacts.email_resolution import derive_email
from app.contacts.people_search import (
    ContactCandidate,
    classify_profiles,
)
from app.contacts.search_source import search_profiles
from app.models.orm import Company

logger = logging.getLogger("contacts.pipeline")

# Cap applies per matched category, not per company: a company whose job
# history spans e.g. both data_science and credit_risk gets a full
# 4-contact ladder walk for EACH (deduped across them), since those are
# genuinely different hiring chains worth separate coverage. Issue #6's
# original wording bounded multi-company category by one shared
# per-company cap; changed to per-category by user decision (2026-07-16).
FETCH_CAP_PER_CATEGORY = 4
# Floor below the cap: a category landing on only 0-1 contacts is too thin
# to be useful (a "primary and backup option" per issue #6 user story 11
# needs at least two). User decision (2026-07-17): recommended 4/category,
# minimum 2. Below this floor, _find_via_search_engine issues one fallback
# query with different title terms before giving up on that category.
MIN_CONTACTS_PER_CATEGORY = 2
# Hard ceiling across ALL of a company's categories combined — a company
# matching several categories could otherwise accumulate
# len(categories) * FETCH_CAP_PER_CATEGORY contacts. User decision
# (2026-07-21): cap at 15, keeping the most senior/relevant tiers when a
# company's combined ladder walks exceed it.
MAX_CONTACTS_PER_COMPANY = 15
STATUS_DONE = "done"
# This company was never searched because it has no job-posting history
# matching a category, so there is no ladder to walk.
STATUS_NO_CATEGORY_MATCH = "no_category_match"


@dataclass
class ContactFindResult:
    status: str
    contacts: list[ContactCandidate] = field(default_factory=list)


def _ladders_for_company(session: Session, company: Company) -> list[tuple[str, list[dict]]]:
    """One (category, ladder) pair per category the company's own job
    history matches — each gets its own capped ladder walk."""
    categories = categories_for_company(session, company.id)
    return [(cat, ROLE_LADDER[cat]) for cat in categories if cat in ROLE_LADDER]


def _gate_by_deliverability(
    people: list[ContactCandidate], company: Company
) -> list[ContactCandidate]:
    """Attach each candidate's derived email and drop the ones with no
    deliverable address. Per issue #20 deliverability is a storage *gate*, not
    a label: a contact whose address is provably undeliverable (no MX, or every
    pattern hard-rejected) or underivable (mononym, no company domain) is
    discarded rather than stored with a wrong or empty address.

    Contacts whose address merely can't be confirmed (catch-all domain,
    probe-blocking server) are kept, carrying that status — dropping those
    would discard reachable people for their mail server's configuration.

    Source-agnostic: keys only off the person's name and the company's
    canonical domain."""
    domain = company.canonical_domain or ""
    kept = []
    for person in people:
        derived = derive_email(person.full_name, domain)
        if derived is None:
            logger.debug(
                "Dropping %s at %s: no deliverable address", person.full_name, company.name
            )
            continue
        person.email_guess = derived.address
        person.email_verification_status = derived.status
        kept.append(person)
    return kept


# One title term per priority tier (hiring_manager, ic, exec_fallback) —
# NOT talent_acquisition, which shows up in results as noise regardless.
# Adding too MANY title terms is worse, not better: an 8-term OR
#    query for a real company returned zero usable profiles, buried under
#    generic job-aggregator pages; the identical intent with 3 terms
#    surfaced real, correctly-tiered employees cleanly (see
#    search_source.MAX_QUERY_TITLE_TERMS). One term per priority tier is the
#    balance that survived live testing.
_PRIORITY_TIERS = ("hiring_manager", "ic", "exec_fallback")


def _query_terms_for_ladder(ladder: list[dict], offset: int = 0) -> list[str]:
    """`offset=0` is the primary query's terms (each tier's top title).
    `offset=1` is the fallback query's terms (each tier's *next* title) —
    genuinely different terms, not a re-ask of the same query, for when the
    primary query under-shoots MIN_CONTACTS_PER_CATEGORY."""
    by_tier = {rung["tier"]: rung["titles"] for rung in ladder}
    terms = []
    for tier in _PRIORITY_TIERS:
        titles = by_tier.get(tier, [])
        if len(titles) > offset:
            terms.append(titles[offset])
    return terms


def _find_via_search_engine(
    company: Company, ladders: list[tuple[str, list[dict]]]
) -> ContactFindResult:
    """No-account path: one broad search-engine query per matched category
    (company name + a spread of that category's ladder titles, restricted to
    public linkedin.com/in profiles), then local tier classification.

    If a category lands under MIN_CONTACTS_PER_CATEGORY, issues one fallback
    query with different title terms (not a repeat of the same query) before
    accepting a thin result — confirmed live that widening a single query
    with MORE terms backfires (buried under job-aggregator pages), so a
    second, differently-worded query is the safer way to add recall."""
    people: list[ContactCandidate] = []
    seen_urls: set[str] = set()
    for category, ladder in ladders:
        profiles = search_profiles(company.name, _query_terms_for_ladder(ladder))
        found = classify_profiles(
            profiles, ladder, company_name=company.name,
            fetch_cap=FETCH_CAP_PER_CATEGORY, seen_urls=seen_urls,
        )

        if len(found) < min(MIN_CONTACTS_PER_CATEGORY, FETCH_CAP_PER_CATEGORY):
            fallback_terms = _query_terms_for_ladder(ladder, offset=1)
            if fallback_terms:
                more_profiles = search_profiles(company.name, fallback_terms)
                found += classify_profiles(
                    more_profiles, ladder, company_name=company.name,
                    fetch_cap=FETCH_CAP_PER_CATEGORY - len(found), seen_urls=seen_urls,
                )

        people.extend(found)

    # Gate before capping, so the cap keeps N *deliverable* contacts rather
    # than N candidates of which some are then dropped as unreachable.
    people = _cap_contacts(_gate_by_deliverability(people, company))
    # An empty completed search still leaves the pending queue.
    return ContactFindResult(status=STATUS_DONE, contacts=people)


def _cap_contacts(people: list[ContactCandidate]) -> list[ContactCandidate]:
    """Truncate to MAX_CONTACTS_PER_COMPANY across all of a company's
    categories combined, keeping the most senior/relevant tiers first
    (hiring_manager > ic > exec_fallback > anything else) rather than
    truncating in whatever order categories happened to be walked."""
    if len(people) <= MAX_CONTACTS_PER_COMPANY:
        return people
    tier_rank = {tier: i for i, tier in enumerate(_PRIORITY_TIERS)}
    ranked = sorted(people, key=lambda p: tier_rank.get(p.seniority_tier, len(_PRIORITY_TIERS)))
    return ranked[:MAX_CONTACTS_PER_COMPANY]


def find_contacts_for_company(company: Company, session: Session) -> ContactFindResult:
    ladders = _ladders_for_company(session, company)
    if not ladders:
        return ContactFindResult(status=STATUS_NO_CATEGORY_MATCH)

    return _find_via_search_engine(company, ladders)
