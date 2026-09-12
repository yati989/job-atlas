"""
Vocabulary for the profile relevance gate (`app/contacts/profile_relevance.py`,
ADR-0006). Same split as `categories.py`: vocabulary lives here as data, the
matching logic lives in the gate module. Nothing in this file decides
anything by itself.

**This is a MATCHING vocabulary, not `ladder.py`'s SEARCH vocabulary — do not
conflate them, same rule CLAUDE.md already states for SEARCH_TERMS vs
ROLE_TITLE_KEYWORDS.** `ladder.py`'s two seeds per tier exist to shape a
*query*; they are deliberately narrow (an 8-term query returned zero usable
profiles per ADR 0001). `TIER_TITLE_MARKERS` below exists to *score a title
Bright Data already returned*, so it must be wide enough to recognise every
real title variant a search could drag in — "Global Delivery Head for
Analytics", "Director - R&D", "Enterprise Analytics Practice Manager" are all
real function-owner titles found live that no fixed seed list would predict
(references/search-tactics.md).

CRITICAL: none of these markers ever reject a candidate. Tier/domain scoring
is advisory only (see `profile_relevance.TierScorer`) — ADR-0005 already
established that a word-overlap accept/reject class is the exact failure this
project removed, and semantic scoring recreates that risk if it's allowed to
drop anyone. Company IDENTITY is the only axis that may drop, and it is
exact/structural, never scored against these lists.
"""
from app.contacts import ladder as _ladder

HEAD = _ladder.HEAD
HIRING_MANAGER = _ladder.HIRING_MANAGER
IC = _ladder.IC
TALENT_ACQUISITION = _ladder.TALENT_ACQUISITION
EXEC_FALLBACK = _ladder.EXEC_FALLBACK

# --- Tier matching vocabulary ------------------------------------------------
# Deliberately broad and overlapping across tiers — e.g. "manager" appears
# under both hiring_manager and ic-adjacent titles like "engineering manager"
# people sometimes hold as an IC-equivalent grade. Overlap is fine: the
# scorer returns every tier's score, not a forced single label, and the agent
# reads the ranked list rather than trusting one winner blindly.
TIER_TITLE_MARKERS: dict[str, list[str]] = {
    HEAD: [
        "head of", "head", "chief", "vp", "vice president", "director",
        "global head", "practice head", "delivery head", "principal architect",
        "senior director",
    ],
    HIRING_MANAGER: [
        "manager", "team lead", "engineering lead", "group manager",
        "senior manager", "associate director", "practice lead",
    ],
    IC: [
        "scientist", "engineer", "analyst", "consultant", "architect",
        "developer", "specialist",
    ],
    TALENT_ACQUISITION: [
        "recruiter", "recruitment", "talent acquisition", "sourcing",
        "staffing", "hiring", "hr", "human resources",
    ],
    EXEC_FALLBACK: [
        "founder", "co-founder", "ceo", "cto", "coo", "chief executive",
        "chief technology officer", "managing director",
    ],
}

# Titles that MUST NOT score into head/hiring_manager regardless of how close
# a fuzzy match otherwise looks — this is the rule human review established
# the hard way (#75): a Program Manager and a BU Data Manager were both
# stored as `head` on "closest available" reasoning and rejected on review.
# The scorer floors head/hiring_manager to 0 for any title containing one of
# these, forcing the agent to look at what tier the title actually earned
# instead of a near-miss on "manager".
TIER_DEMOTION_MARKERS: list[str] = [
    "program manager", "project manager", "delivery manager",
    "product manager", "account manager", "operations manager",
]

# --- Company-identity matching ----------------------------------------------
# Legal-entity suffixes stripped before comparing two company names, so
# "Ace Technologies, Inc." and "Ace Technologies" normalize identically.
LEGAL_SUFFIXES: list[str] = [
    "inc", "incorporated", "ltd", "limited", "llc", "llp", "corp",
    "corporation", "co", "company", "pvt", "pte", "private", "plc",
]

# Real, confirmed suffix patterns that turn an exact match into a SISTER
# entity rather than the same one — documented here for evidence-file
# messaging and tests, not as a gating list: the gate detects the
# prefix-relationship structurally (see profile_relevance._company_match_reason)
# and treats ANY non-exact prefix relationship as "sister_entity", flagged
# for the agent to resolve rather than auto-accepted or auto-rejected. Two
# real, DIFFERENTLY-judged cases from the 2026-08-03 pilot: "Capgemini
# Invent" was kept (same real org, its consulting arm); "Binance.US" was
# dropped (a separately regulated legal entity) — the gate cannot and must
# not make that call itself.
#
# "group" added 2026-08-03: without it in this list, a trailing "Group" fell
# through to the segment matcher's target-reappears fallback, which silently
# resolved to "clean" with NO flag at all whenever the bio happened to repeat
# the company mention (a common pattern — "...HR operations in SAMSARA
# Group" restates the employer in prose). "Samsara Group" (a Mumbai
# shipping/logistics firm) was accepted twice with zero flag against a
# search for Samsara Inc. (the Bay-Area IoT company) this way — the more
# dangerous failure direction, since nothing marked it as needing review.
# Being IN this list intercepts the check earlier (a direct suffix match),
# before that reappearance heuristic ever runs.
SISTER_ENTITY_SUFFIXES: list[str] = [
    "us", "usa", "invent", "securities", "global services", "technologies",
    "labs", "consulting", "india", "engineering", "group",
]

# Confirmed, PER-COMPANY aliases: a specific target company's own known
# delivery-center/GCC brand names, verified by the agent (not guessed) to be
# the SAME real employer — same email domain, same HR/recruiting org — not a
# genuinely separate business. Deliberately NOT a general suffix rule like
# SISTER_ENTITY_SUFFIXES above: "Acceleration Center" being safe for PwC
# doesn't imply anything about some other company's "X Acceleration Center",
# so this only auto-clears a match for the exact target company it was
# verified against (keyed by `_normalize_company(target_company)` — see
# profile_relevance.KNOWN_ALIASES). This is the narrow exception ADR-0006
# allows: company identity may only be auto-decided on a STRUCTURAL fact,
# and an agent-confirmed alias list for one specific company is exactly that
# (the same kind of fact recorded when a subsidiary was assigned its
# parent's `canonical_domain` during 2026-08-04's domain-resolution pass) —
# not a semantic guess about suffixes in general.
#
# Found live, 2026-08-05, first find-contacts batch after the gate-verdict
# logging landed: PwC, Ecolab and TEKsystems each contributed several
# `sister_entity`-flagged contacts that were, on judgement, obviously the
# same company under its own delivery-center branding.
KNOWN_COMPANY_ALIASES: dict[str, list[str]] = {
    "pwc": [
        "pwc acceleration center", "pwc acceleration centers",
        "pwc sdc", "pwc service delivery center", "pwc india",
    ],
    "ecolab": ["ecolab digital center"],
    "teksystems": [
        "teksystems global services", "teksystems global services in india",
    ],
}

# Structural, conservative markers of a PAST role — reused idea from the
# dormant people_search.py's segment-stripping, but applied narrowly (see
# profile_relevance.py): only fires on an explicit textual marker, never on
# the mere absence of "Present".
FORMER_ROLE_MARKERS: list[str] = ["former", "ex-", "ex ", "previously", "past"]
