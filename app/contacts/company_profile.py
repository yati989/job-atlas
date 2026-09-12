"""Persist the company-level prerequisites for agentic contact search.

The ``enrich-companies`` skill owns the judgement: domain identity,
employer-vs-staffing type, and target hiring functions. This module is the
small persistence interface at that seam. ``find-contacts`` consumes the
stored profile and does not repeat company research during people search.
"""
from __future__ import annotations

from datetime import datetime, timezone
import re

from typing import Literal

from sqlalchemy.orm import Session

from app.contacts import ladder
from app.contacts.company_categories import categories_for_company
from app.models.orm import Company


VALID_COMPANY_TYPES = frozenset({"employer", "staffing"})
VALID_DOMAIN_STATUSES = frozenset({"done", "unresolvable"})
SEARCH_GROUP_ORDER = (ladder.DATA_AI, ladder.CREDIT_RISK)
_SEARCH_GROUP_RE = re.compile(r"^[a-z0-9][a-z0-9_]{0,63}$")


class InvalidContactCompanyProfile(ValueError):
    """Raised when a profile cannot safely drive contact search."""


class ContactCompanyProfileMissing(ValueError):
    """Raised when contact search is attempted before company enrichment."""


CanonicalDomainWriteOutcome = Literal["saved", "unchanged", "conflict"]


def record_resolved_canonical_domain(
    session: Session,
    company_id: int,
    *,
    canonical_domain: str,
    mx_host: str,
) -> CanonicalDomainWriteOutcome:
    """Persist an identity-confirmed, MX-valid domain without clobbering one.

    LinkedIn's pre-relevance official-careers pass resolves the same company
    fact as ``enrich-companies``.  This deliberately narrow seam lets that
    pass retain its useful result without pretending the rest of the contact
    company profile has also been completed.
    """
    company = session.get(Company, company_id)
    if company is None:
        raise ValueError(f"no company with id={company_id}")

    domain = canonical_domain.strip().lower().rstrip(".")
    mx = mx_host.strip().lower().rstrip(".")
    if not domain or not mx:
        raise InvalidContactCompanyProfile(
            "a resolved canonical domain requires canonical_domain and mx_host"
        )
    if any(token in domain for token in ("://", "/", "@", " ")):
        raise InvalidContactCompanyProfile(
            "canonical_domain must be a bare domain, not a URL or email"
        )

    existing = (company.canonical_domain or "").strip().lower().rstrip(".")
    if existing and existing != domain:
        return "conflict"
    if existing == domain and company.domain_resolution_status == "done":
        return "unchanged"

    company.canonical_domain = domain
    company.domain_resolution_status = "done"
    return "saved"


def suggested_search_groups(session: Session, company_id: int) -> list[str]:
    """Return the posting-derived starting point for agent review."""
    categories = categories_for_company(session, company_id)
    return ladder.search_groups_for_categories(categories)


def _normalise_search_groups(search_groups: list[str]) -> list[str]:
    normalized = [str(group).strip().casefold() for group in search_groups]
    invalid = sorted({group for group in normalized if not _SEARCH_GROUP_RE.fullmatch(group)})
    if invalid:
        raise InvalidContactCompanyProfile(
            "contact search groups must be lowercase slugs: " + ", ".join(invalid)
        )
    unique = list(dict.fromkeys(normalized))
    builtins = [group for group in SEARCH_GROUP_ORDER if group in unique]
    custom = sorted(group for group in unique if group not in SEARCH_GROUP_ORDER)
    return builtins + custom


def search_group_for_profession(profession: str) -> str:
    """Create a stable public profile-search group from an accepted profession."""
    slug = re.sub(r"[^a-z0-9]+", "_", profession.strip().casefold()).strip("_")
    if not slug or not _SEARCH_GROUP_RE.fullmatch(slug):
        raise InvalidContactCompanyProfile(
            f"profession cannot form a safe profile-search group: {profession!r}"
        )
    return slug


def stored_search_groups(company: Company) -> list[str]:
    """Return a validated stored classification for contact planning."""
    if company.contact_search_groups is None:
        raise ContactCompanyProfileMissing(
            f"{company.name} has no contact company profile; run "
            "enrich-companies before find-contacts"
        )
    if not isinstance(company.contact_search_groups, list):
        raise InvalidContactCompanyProfile(
            f"{company.name} contact_search_groups must be a JSON list"
        )
    return _normalise_search_groups(company.contact_search_groups)


def record_contact_company_profile(
    session: Session,
    company_id: int,
    *,
    canonical_domain: str | None,
    domain_resolution_status: str,
    company_type: str,
    search_groups: list[str],
) -> Company:
    """Validate and atomically persist all contact-search prerequisites."""
    company = session.get(Company, company_id)
    if company is None:
        raise ValueError(f"no company with id={company_id}")
    if company_type not in VALID_COMPANY_TYPES:
        raise InvalidContactCompanyProfile(
            f"company_type must be one of {sorted(VALID_COMPANY_TYPES)}"
        )
    if domain_resolution_status not in VALID_DOMAIN_STATUSES:
        raise InvalidContactCompanyProfile(
            f"domain_resolution_status must be one of {sorted(VALID_DOMAIN_STATUSES)}"
        )
    domain = (canonical_domain or "").strip().lower() or None
    if domain_resolution_status == "done" and domain is None:
        raise InvalidContactCompanyProfile(
            "a done domain resolution requires canonical_domain"
        )
    if domain_resolution_status == "unresolvable" and domain is not None:
        raise InvalidContactCompanyProfile(
            "an unresolvable domain must not store canonical_domain"
        )

    company.canonical_domain = domain
    company.domain_resolution_status = domain_resolution_status
    company.company_type = company_type
    company.contact_search_groups = _normalise_search_groups(search_groups)
    company.contact_profile_enriched_at = datetime.now(timezone.utc)
    return company


def record_profile_discovery_company_profile(
    session: Session,
    company_id: int,
    *,
    company_type: str,
    search_groups: list[str],
    canonical_domain: str | None = None,
) -> Company:
    """Persist links-only Phase A prerequisites without an email/MX gate.

    The caller must have confirmed company identity from job and official
    company evidence. A canonical website is useful identity evidence but is
    optional here and is never tested for mail-server capability.
    """
    company = session.get(Company, company_id)
    if company is None:
        raise ValueError(f"no company with id={company_id}")
    if company_type not in VALID_COMPANY_TYPES:
        raise InvalidContactCompanyProfile(
            f"company_type must be one of {sorted(VALID_COMPANY_TYPES)}"
        )
    domain = (canonical_domain or "").strip().lower().rstrip(".") or None
    if domain and any(token in domain for token in ("://", "/", "@", " ")):
        raise InvalidContactCompanyProfile(
            "canonical_domain must be a bare domain, not a URL or email"
        )
    company.company_type = company_type
    company.contact_search_groups = _normalise_search_groups(search_groups)
    company.contact_profile_enriched_at = datetime.now(timezone.utc)
    if domain:
        company.canonical_domain = domain
    return company
