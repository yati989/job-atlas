"""Classify companies for contact search as employers or staffing firms.

Staffing firms use a recruiter-only ladder. Classification prefers exact,
verified identity overrides over enriched job text because agency postings
often describe the client's industry rather than the agency's own business.
"""
import re

EMPLOYER = "employer"
STAFFING = "staffing"

# Deliberately narrow. Broader terms such as ``talent`` and ``workforce``
# produce HR-product and workforce-analytics false positives.
_STAFFING_MARKERS = [
    "staffing",
    "recruit",
    "consultancy",
    "headhunt",
    "executive search",
    "job board",
    "job portal",
    "hr consulting",
    "hr consultancy",
    "hr services",
]
_INDUSTRY_MARKER_RE = re.compile("|".join(re.escape(marker) for marker in _STAFFING_MARKERS))

# Descriptions often mention that a normal employer is recruiting, so only
# agency-defining phrases are safe here. Bare "recruit"/"consultancy" remain
# valid for the curated industry field but would be far too broad in prose.
_DESCRIPTION_MARKERS = [
    "staffing firm",
    "staffing agency",
    "recruitment firm",
    "recruitment agency",
    "recruiting firm",
    "recruiting agency",
    "staff augmentation",
    "talent acquisition services",
    "executive search firm",
    "employment agency",
]
_DESCRIPTION_MARKER_RE = re.compile(
    "|".join(re.escape(marker) for marker in _DESCRIPTION_MARKERS)
)

# Exact normalized names only. Fuzzy identity matching here could wrongly
# collapse a real technology employer to the recruiter-only ladder.
_VERIFIED_STAFFING_NAMES = {
    "braintree technology solutions",
    "robert half",
    "viraaj hr solutions",
}


def _normalize_name(name: str | None) -> str:
    value = re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()
    return re.sub(
        r"\s+(?:(?:private|pvt)\s+(?:limited|ltd)|"
        r"llc|llp|inc|limited|ltd|private|pvt)$",
        "",
        value,
    ).strip()


def classify(
    industry: str | None,
    *,
    company_name: str | None = None,
    description: str | None = None,
) -> str:
    """Return ``STAFFING`` for a verified agency or staffing-marked text.

    Unknown companies default to ``EMPLOYER``: wasting a full ladder is safer
    than a false staffing classification that hides every functional contact.
    """
    if _normalize_name(company_name) in _VERIFIED_STAFFING_NAMES:
        return STAFFING
    if industry and _INDUSTRY_MARKER_RE.search(industry.lower()):
        return STAFFING
    if description and _DESCRIPTION_MARKER_RE.search(description.lower()):
        return STAFFING
    return EMPLOYER
