"""
Company-name normalization — the single rule for deciding whether two
strings name the same real-world company.

Extracted from scripts/dedup_companies.py, where it originally lived, once
prospect discovery needed to ask the same question from the other side:
"is this name I just harvested off a directory page already in `companies`?"
Dedup correctness depends on BOTH sides applying the identical rule — a
prospect normalized by a slightly different function would silently
re-introduce the duplicate class the dedup script exists to collapse. One
rule, one module, imported by both.

Normalization strips parentheticals and punctuation, then repeatedly peels
trailing legal-entity suffixes (Pvt Ltd, LLC, Private Limited, ...) and
regional/generic qualifiers (India, Global, Services, North America, ...)
TOKEN BY TOKEN from the end, so multi-suffix names ("NTT DATA Americas,
Inc" -> strip "Inc" -> strip "Americas" -> "ntt data") normalize the same
as their bare counterpart. Only TRAILING strips are applied — never leading
or mid-name — so a company's actual distinguishing brand word is never
clipped merely because it matches a qualifier ("Global Logic" keeps its
"Global"; only ever peel qualifiers off the end).
"""
import re

# Longest-first so "private limited" strips as one unit before a lone
# "limited" would strip and leave a dangling "private".
_LEGAL_SUFFIX_PHRASES: tuple[tuple[str, ...], ...] = (
    ("private", "limited"), ("pvt", "ltd"), ("pte", "ltd"), ("p", "ltd"),
    ("l", "l", "c"), ("north", "america"), ("asia", "pacific"),
)
_LEGAL_SUFFIX_SINGLE = frozenset({
    "limited", "incorporated", "corporation", "llc", "inc", "ltd", "corp",
    "gmbh", "bv", "sa", "llp", "plc", "co",
})
_TRAILING_QUALIFIERS = frozenset({
    "india", "global", "group", "services", "americas", "usa", "uk",
    "worldwide", "international", "technologies", "solutions", "consulting",
    "consultancy", "company", "companies", "holdings", "ventures",
})
_ALL_TRAILING_PHRASES = _LEGAL_SUFFIX_PHRASES + tuple((w,) for w in _LEGAL_SUFFIX_SINGLE | _TRAILING_QUALIFIERS)
# Longest phrases first, so multi-token qualifiers match before a
# single-token prefix of them could.
_ALL_TRAILING_PHRASES = tuple(sorted(_ALL_TRAILING_PHRASES, key=len, reverse=True))


# Confirmed live: "Confidential" / "confidential company" / "Confidential
# Jobs" normalize identically and would merge as if they were one company —
# but each is how a DIFFERENT job posting marks its employer as undisclosed,
# so merging them fuses unrelated real companies into one fake canonical
# row. Anything that means "employer withheld" must never be a merge key,
# no matter how many rows share the literal text.
#
# Prospect discovery reuses this list for a second reason: a harvested
# directory row reading "Stealth Startup" or "Various" is not a company you
# can research or email, so it must never become a prospect either.
PLACEHOLDER_NORMS = frozenset({
    "confidential", "confidential company", "confidential jobs",
    "unknown", "undisclosed", "anonymous", "various", "multiple",
    "na", "tbd", "stealth", "stealth startup", "name",
    "consulting firm", "us mnc",
})


def normalize(name: str) -> str:
    """Strip parentheticals/punctuation, then repeatedly peel a trailing
    legal-suffix or qualifier phrase off the end until none match. Never
    strips down to nothing — a name that's ENTIRELY qualifier words (rare,
    but possible for a garbage row) is left as its last remaining token
    rather than emptied, so it can't accidentally collide with every other
    stripped-to-nothing row."""
    lowered = name.lower()
    lowered = re.sub(r"\([^)]*\)", " ", lowered)
    lowered = re.sub(r"[^a-z0-9\s]", " ", lowered)
    tokens = lowered.split()
    if not tokens:
        return ""
    changed = True
    while changed and len(tokens) > 1:
        changed = False
        for phrase in _ALL_TRAILING_PHRASES:
            n = len(phrase)
            if len(tokens) > n and tuple(tokens[-n:]) == phrase:
                tokens = tokens[:-n]
                changed = True
                break
    return " ".join(tokens)


def is_placeholder(name: str) -> bool:
    """True when a name means "employer withheld" or is otherwise not a real,
    researchable company. Callers should skip these rather than store them."""
    return normalize(name) in PLACEHOLDER_NORMS
