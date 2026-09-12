"""
Classify public LinkedIn profile search results against a company's role
ladder (see `ROLE_LADDER` in `app/config/categories.py`).
"""
import re
from dataclasses import dataclass
from app.contacts.search_source import RawProfile


_STOPWORDS = {"of", "the", "and", "for", "a", "an", "at", "to", "in"}

# Generic seniority/role words ("Manager", "Head", "Director"...) overlap
# with unrelated titles too easily — confirmed live: "Data Science Manager"
# matched a "Product/Project Manager" purely on the word "manager". Domain
# matching must land on the domain-specific words instead; the seniority
# words are used separately, as the hiring_manager tier's leadership gate.
_GENERIC_ROLE_WORDS = {"head", "manager", "director", "vp", "senior", "lead", "chief"}

# Words that mark a candidate as a recruiter/TA person rather than a
# functional hiring manager or IC — confirmed live that these leak into
# hiring_manager-tier results ("Lead Talent Acquisition Specialist |
# Data & Analytics, AI/ML Hiring" matched on "Analytics"). A recruiterish
# title is only a valid match for the talent_acquisition rung, and is
# *required* there.
_RECRUITER_WORDS = {"recruiter", "recruitment", "recruiting", "talent", "hiring", "sourcing", "staffing"}

# Words that signal the candidate actually holds a leadership role, required
# for the hiring_manager tier — confirmed live that domain overlap alone let
# ICs through ("Data Scientist II", "Sr. Data Engineer" tagged
# hiring_manager, matched on "Data" from "Head of Data Science").
_LEADERSHIP_WORDS = {
    "head", "manager", "director", "vp", "vice", "chief", "president",
    "leader", "officer", "founder", "ceo", "cto", "cxo",
}


# LinkedIn headlines are segment-lists ("Sales Director | Pharma & Life
# Sciences | Driving Data-to-Value Transformation"), so requiring the
# query's words anywhere in the whole headline is too loose — confirmed
# live: that exact headline passed for "Data Science Manager" because
# "data" and "science" each appeared in a *different* segment. The domain
# words must co-occur within one segment. Split on the separators people
# actually use between segments (pipe, middle dot, bullet, comma, spaced
# dash) — NOT bare hyphens ("Data-to-Value") or slashes ("Analyst/BI
# Developer"), which join alternatives inside a single role rather than
# separating segments.
_SEGMENT_SEPARATORS = re.compile(r"[|·•,]|\s-\s")


def _normalize_title(text: str) -> list[str]:
    return re.sub(r"[^a-z0-9 ]", " ", text.lower()).split()


def _title_matches(title_query: str, candidate_title: str, tier: str) -> bool:
    """Public profile search results are relevance matches, not title-filtered
    records. Validate each result against its intended ladder tier:

    - Domain: ALL non-generic words of the searched title must appear
      together within one headline segment (substring per word, so
      "science" matches "scientist"). Any-word matching let "Global
      Strategic Leader ... Data-Driven Decision-Making" pass for "Head of
      Data Science" on the word "data" alone; whole-headline all-words
      matching still let "Sales Director | Pharma & Life Sciences |
      Data-to-Value" pass via words in different segments.
    - hiring_manager additionally requires a leadership word, and rejects
      recruiterish titles (those belong on the talent_acquisition rung).
    - ic rejects recruiterish titles but needs no leadership word.
    - talent_acquisition requires a recruiterish title.
    """
    candidate_words = _normalize_title(candidate_title)

    is_recruiterish = any(w in candidate_words for w in _RECRUITER_WORDS)
    if tier == "talent_acquisition":
        return is_recruiterish
    if is_recruiterish:
        return False

    query_words = [w for w in _normalize_title(title_query) if w not in _STOPWORDS and w not in _GENERIC_ROLE_WORDS]
    if not query_words:
        # Titles that are ONLY generic/seniority words (shouldn't happen
        # with this repo's ROLE_LADDER, but stay permissive rather than
        # reject everything if it ever does).
        query_words = [w for w in _normalize_title(title_query) if w not in _STOPWORDS]

    # Segments describing PAST roles don't count — confirmed live:
    # "Associate Enterprise Project Manager @ Canonical | former Business
    # Intelligence Analyst at Ausenco" passed for "Head of Business
    # Intelligence" via the "former ..." segment.
    segments = [" ".join(_normalize_title(s)) for s in _SEGMENT_SEPARATORS.split(candidate_title)]
    segments = [s for s in segments if s and s.split()[0] not in ("former", "ex", "previously", "past")]
    if not any(all(w in segment for w in query_words) for segment in segments):
        return False

    if tier == "hiring_manager":
        return any(w in candidate_words for w in _LEADERSHIP_WORDS)
    return True


@dataclass
class ContactCandidate:
    full_name: str
    title: str
    linkedin_url: str
    seniority_tier: str
    email_guess: str | None = None
    email_verification_status: str | None = None
    name_collision_risk: bool = False


def _mentions_company(company_name: str, candidate_title: str) -> bool:
    """True if any word of the company's name appears in the candidate's
    headline. Used only for the exec_fallback rung, where generic founder or
    CEO results can otherwise be attributed to the wrong company. Requiring
    the company mention loses genuine CEOs whose
    headline is just "CEO", but per issue #6 an ambiguous exec match should
    be missed/flagged, not guessed wrong."""
    company_words = set(_normalize_title(company_name)) - _STOPWORDS
    candidate_words = set(_normalize_title(candidate_title))
    return bool(company_words & candidate_words)


def _best_tier_for_profile(headline: str, ladder: list[dict], company_name: str) -> str | None:
    """Assign a profile to the highest-priority ladder rung whose tier its
    headline satisfies, or None if it matches no rung. Walks rungs in ladder
    order (hiring_manager first, exec_fallback last) and returns the first
    rung whose tier `_title_matches` accepts — so someone who qualifies as
    both a manager and an IC is labelled the more senior tier."""
    for rung in ladder:
        tier = rung["tier"]
        if not any(_title_matches(title, headline, tier) for title in rung["titles"]):
            continue
        if tier == "exec_fallback" and company_name and not _mentions_company(company_name, headline):
            continue
        return tier
    return None


def classify_profiles(
    profiles: list[RawProfile],
    ladder: list[dict],
    company_name: str = "",
    fetch_cap: int = 4,
    seen_urls: set[str] | None = None,
) -> list[ContactCandidate]:
    """Validate and label already-discovered profiles from
    `search_source.search_profiles`, returning up to `fetch_cap`
    `ContactCandidate`s deduplicated by profile URL. `seen_urls` is shared
    and mutated across a company's categories."""
    if seen_urls is None:
        seen_urls = set()
    found: list[ContactCandidate] = []
    for profile in profiles:
        if len(found) >= fetch_cap:
            break
        if profile.linkedin_url in seen_urls:
            continue
        tier = _best_tier_for_profile(profile.headline, ladder, company_name)
        if not tier:
            continue
        seen_urls.add(profile.linkedin_url)
        found.append(
            ContactCandidate(
                full_name=profile.full_name,
                title=profile.headline,
                linkedin_url=profile.linkedin_url,
                seniority_tier=tier,
            )
        )
    return found
