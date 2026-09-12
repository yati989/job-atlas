"""
The profile relevance gate (ADR-0006) — structural pre-screening for search
results the agent judges, mirroring `app/pipeline/relevance.py`'s pattern
(one predicate per axis, ordered `AXIS_CHECKS`, first-failure attribution).

WHY THIS EXISTS, AND WHY IT IS NOT A REVERSAL OF ADR-0005. ADR-0005 replaced
this project's old heuristic contact matcher (`people_search._title_matches`,
`search_source._mentions_company`) because word-overlap string matching
"demonstrably accepted a person at one bank for a search against a
*different* bank" — a single shared word standing in for a specific match.
That failure class gets WORSE under semantic similarity, not better:
embeddings would rate "ICICI Bank" highly similar to "ICICI Securities" —
they genuinely are similar; they are simply different employers. So this
module draws a hard line:

    axis            | technique              | may this axis DROP a candidate?
    ----------------|-------------------------|----------------------------------
    profile shape   | structural (URL)        | yes
    company identity| exact/structural only   | yes
    tier fit        | semantic, advisory      | NEVER
    domain distance | semantic, advisory      | NEVER

Only `AXIS_CHECKS` below (shape, company) can cause `first_failing_axis` to
reject anyone. Tier and domain scoring (`TierScorer`, `domain_note`) return
suggestions the agent reads and can overrule — they can never remove a
candidate from what the agent sees. This is the same "structural filtering
only, judgement stays with the agent" boundary the ADR-0005 addendum already
draws for `bright_data_profiles`'s URL-shape filter; this module just adds
one more structural axis (company identity) rather than a semantic one.

CONTACTS DIFFER FROM JOBS ON ONE DESIGN AXIS: drop-at-ingest vs
store-and-flag. `filter_relevant` here returns a 4-tuple, not the jobs gate's
3-tuple — the extra element is `flags`, company-identity matches that are
AMBIGUOUS rather than wrong (a sister entity, or no structured field to check
at all). Jobs are cheap and plentiful, so ADR-0002 drops anything uncertain.
Contacts are expensive to find and the two real ambiguous cases hit live
(2026-08-03) were judged OPPOSITE ways by a human — "Capgemini Invent" kept
(same real org), "Binance.US" dropped (a separately regulated entity) — so
this module cannot and does not decide those; it surfaces them.
"""
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from app.config.contact_matching import (
    EXEC_FALLBACK, FORMER_ROLE_MARKERS, HEAD, HIRING_MANAGER, IC,
    KNOWN_COMPANY_ALIASES, LEGAL_SUFFIXES, SISTER_ENTITY_SUFFIXES,
    TALENT_ACQUISITION, TIER_DEMOTION_MARKERS, TIER_TITLE_MARKERS,
)

_PROFILE_URL_RE = re.compile(r"^https?://([a-z]{2,3}\.)?linkedin\.com/in/[^/]+", re.I)

# Bright Data's snippet HTML glues the structured company field directly onto
# the following free-text prose with NO separator — confirmed live,
# 2026-08-03: "CapgeminiCurrently working in Capgemini as Data Science
# Manager", "Gainwell TechnologiesI head the Analytics vertical at Gainwell
# Technologies, India". A naive word-split treats the whole run-on blob as
# one non-matching token. This regex recovers the boundary the missing space
# should have been at (a lowercase letter directly followed by an uppercase
# one), the same trick as camelCase-splitting an identifier.
#
# 2026-08-03 (second gap, same root cause): the lowercase->uppercase rule
# above misses a run-on that starts with an ALL-CAPS acronym glued directly
# to the next capitalized word — "USTAkanksha Bhatnagar", "USTGraphic",
# "USTIndira Gandhi..." (confirmed live: UST's snippets glue the company
# acronym to whatever text follows it with no space, systematically enough
# that it dropped ~14 of ~15 genuine UST candidates as `wrong_company` in a
# 2026-08-03 audit). There is no lowercase letter for the first rule to
# anchor on — "UST" is all-caps — so a second boundary is needed: an
# uppercase letter directly followed by (uppercase, then lowercase), the
# same rule that splits "HTTPRequest" -> "HTTP Request".
_CASE_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")

_SISTER_ENTITY_SUFFIXES_SET = frozenset(SISTER_ENTITY_SUFFIXES)

# Reasons `_company_match_reason` can return. Only these two ever fail the
# company axis — everything else passes (possibly flagged).
_COMPANY_REJECT_REASONS = frozenset({"wrong_company", "former"})


@dataclass(frozen=True)
class ProfileCandidate:
    """One /in/ search result, already carrying the company it's being
    judged against — unlike a job, a profile doesn't self-declare its
    target, so the target travels with the candidate rather than being a
    property the gate infers."""
    full_name: str
    title: str          # SerpResult.title, e.g. "Shrikant Hebbar - Senior Director, Insights & Data at ..."
    url: str
    snippet: str        # SerpResult.snippet — may embed the structured Location·Title·Company field
    target_company: str
    tier_hint: str | None = None  # which tier's query surfaced this, if known


@dataclass(frozen=True)
class StructuredField:
    """The `Location · Title · Company` rich-snippet fields Bright Data's
    Google SERP embeds at the front of a profile snippet, when present.
    `company_run_on` is NOT isolated to just the company name — Google's
    markup runs it straight into free-text prose with no separator, e.g.
    'Capgemini' + 'Senior Director, Insights and Data · Capgemini · Business...'
    all lands in one field. Callers test whether it STARTS WITH a known
    target's normalized form rather than trying to find where it "ends"."""
    location: str
    title: str
    company_run_on: str


def parse_structured_field(snippet: str) -> StructuredField | None:
    """Split a snippet on the `·` separator Bright Data's rich-snippet markup
    uses. Returns None when fewer than 3 segments exist — i.e. the structured
    field simply wasn't present in this result, which is common and not an
    error (see `no_signal` handling below).

    NOTE: kept for callers that want the canonical Location-Title-Company
    reading, but `_company_match_reason` does NOT rely on segment position —
    see `_candidate_company_segments` below for why. Confirmed live
    2026-08-03: Bright Data returns more than one snippet shape. Most
    profiles read 'Location·Title·CompanyProse...' (segment 2 is the
    company), but some read 'Title at Company· Experience: Company ·
    Location: X...' (the company is embedded in segment 0 after " at ", and
    repeated after "Experience:" in segment 1) — trusting a fixed segment
    index silently mis-parsed real Highbrow Technologies employees as
    wrong-company."""
    parts = snippet.split("·")  # '·' MIDDLE DOT
    if len(parts) < 3:
        return None
    return StructuredField(
        location=parts[0].strip(),
        title=parts[1].strip(),
        company_run_on=parts[2],
    )


def _candidate_company_segments(snippet: str) -> list[str]:
    """Every text span the snippet PRESENTS as a company/employer name —
    structural markers only, never a blind full-text search (that would
    reintroduce the substring false-accept class ADR-0005 removed). Requires
    at least one real `·` (Bright Data's rich-snippet marker) to consider the
    snippet structured at all — a plain unstructured blob with zero `·`
    returns [] (no_signal), regardless of what words it happens to contain.

    Given a structured snippet, four markers, all confirmed live:
      - each `·`-split segment (the common Location·Title·Company shape),
      - the text after the LAST ' at ' in the FIRST segment only ('Title at
        Company' — the alternate shape Highbrow Technologies profiles used).
        Deliberately restricted to segment 0: extracting " at " from every
        segment, including free-text prose deeper in the snippet, let an
        unrelated PAST-employer mention ('...Technical IT Recruiter at
        Trail Blazer Consulting LLC' inside a Prosperity Travels bio)
        masquerade as the current company — exactly the former-employer
        trap this axis exists to catch.
      - the text after the LAST '@' in the FIRST segment only, same rule and
        same restriction as ' at ' above — 'Title @Company' is the identical
        convention with the other common LinkedIn headline separator.
        Confirmed live 2026-08-03: 'Sr. Data Scientist @Danaher' was
        structurally dropped as wrong_company because only ' at ' was
        recognised, not '@'.
      - the text after an 'Experience:' label on ANY segment (an explicit,
        unambiguous marker, e.g. '...· Experience: Highbrow Technology Inc
        ·...' — safe to check everywhere since it never occurs by accident).
    """
    raw_parts = snippet.split("·")  # '·' MIDDLE DOT
    if len(raw_parts) < 2:
        return []

    parts = [p.strip() for p in raw_parts if p.strip()]
    segments: list[str] = list(parts)

    if parts:
        first_lower = parts[0].lower()
        at_idx = first_lower.rfind(" at ")
        if at_idx != -1:
            segments.append(parts[0][at_idx + 4:])
        at_symbol_idx = parts[0].rfind("@")
        if at_symbol_idx != -1:
            segments.append(parts[0][at_symbol_idx + 1:])

    for part in parts:
        if part.lower().startswith("experience:"):
            segments.append(part[len("experience:"):])

    return segments


def _title_company_segments(title: str) -> list[str]:
    """Company names the SERP *title* presents, via the same two structural
    markers trusted in the snippet's segment 0 (' at ' and '@').

    2026-08-04: the company axis previously read the snippet alone, but the
    title is the LinkedIn *headline* — a structured field, not free prose —
    and it routinely names the employer when the snippet body doesn't.
    Confirmed live: 'Evelyn Ryan - Data Scientist at CrowdStrike',
    'Himanshu Patel - Sr Data Engineer at Crowdstrike' and 'Dharti Patel -
    Sr Talent Acquisition Partner @Crowdstrike' were all structurally dropped
    as `wrong_company` on 2026-08-03 while naming the target company in plain
    text, because their snippets carried `·` markers but no company field.

    Unlike the snippet path this takes EVERY marker occurrence, not just the
    last: multi-company headlines ('Data Scientist @AuthMind | Ex @CrowdStrike')
    are normal, and taking only the last would read the target off a trailing
    `Ex-` mention. Taking all of them means a genuine current employer is
    always among the candidates, and the past-employer case stays the job of
    `_former_or_clean`, which reads title+snippet and fires on the `Ex `
    sitting immediately before the mention.

    2026-08-06: each segment is bounded at the next `|` (LinkedIn headlines'
    own conventional clause separator — 'Talent Advisor @ Oracle | Tech
    Recruitment Certified Professional', 'Data Analyst @ KATBOTZ | CS
    Graduate'), not run to the end of the title. Confirmed live: 'Senior
    Data Scientist @ EPAM | Ex-A23.com | Ex-Tiger Analytics' against target
    'Epam Systems' was structurally dropped as wrong_company — the
    unbounded segment 'EPAM | Ex-A23.com | Ex-Tiger Analytics' tokenizes to
    7 words, and neither `_target_prefix_len`'s forward check (target
    'epam systems' isn't a prefix of it) nor its reverse "shorter root"
    check (which requires the WHOLE segment to be the shorter name, not
    just its first word) can match a single-word company mention with
    unrelated trailing prose glued on. Bounding at `|` makes the segment
    just 'EPAM', which the existing reverse-root check already handles
    correctly — this is a segment-EXTRACTION fix, not a new matching rule."""
    segments: list[str] = []
    lowered = title.lower()

    def _bounded(rest: str) -> str:
        pipe_idx = rest.find("|")
        return rest[:pipe_idx] if pipe_idx != -1 else rest

    start = 0
    while (idx := lowered.find(" at ", start)) != -1:
        segments.append(_bounded(title[idx + 4:]))
        start = idx + 4

    start = 0
    while (idx := title.find("@", start)) != -1:
        segments.append(_bounded(title[idx + 1:]))
        start = idx + 1

    return segments


def _tokenize(text: str) -> list[str]:
    """Lowercase word tokens, with case-transition boundaries inserted first
    (see `_CASE_BOUNDARY_RE`) so a glued-together run-on blob splits the same
    way a properly spaced string would."""
    spaced = _CASE_BOUNDARY_RE.sub(" ", text)
    cleaned = re.sub(r"[^a-z0-9\s]", " ", spaced.lower())
    return cleaned.split()


def _strip_trailing_legal_suffix(words: list[str]) -> list[str]:
    words = list(words)
    suffix_set = set(LEGAL_SUFFIXES)
    while words and words[-1] in suffix_set:
        words.pop()
    return words


def _singularize(word: str) -> str:
    """Minimal, deliberately narrow stemming — NOT general fuzzy matching
    (which would recreate the ADR-0005 false-accept class). Handles exactly
    the real gap found live: a company's own self-reported name varies
    ('Highbrow Technology Inc') from the DB's stored name ('Highbrow
    Technologies') by simple pluralization, not by being a different word."""
    if word.endswith("ies") and len(word) > 3:
        return word[:-3] + "y"
    if word.endswith("s") and not word.endswith("ss") and len(word) > 1:
        return word[:-1]
    return word


def _words_match(a: list[str], b: list[str]) -> bool:
    """Word-for-word equality tolerating only singular/plural variance per
    word, never reordering or dropping words."""
    return len(a) == len(b) and all(_singularize(x) == _singularize(y) for x, y in zip(a, b))


def _normalize_company(name: str) -> str:
    """'Ace Technologies, Inc.' and 'Ace Technologies' must normalize
    identically; 'ICICI Bank' and 'ICICI Securities' must NOT — which is
    exactly why this strips known LEGAL suffixes only, never
    business-descriptor words like 'bank' or 'securities' (those are real
    parts of a company's identity, not boilerplate)."""
    return " ".join(_strip_trailing_legal_suffix(_tokenize(name)))


def _matches_sister_suffix(words: list[str], start: int) -> bool:
    """True if a known sister-entity suffix (`.US`, `Invent`, `Global
    Services`, ...) begins at `words[start]` — checked as a whole phrase
    since some entries ('global services') are two words."""
    for suffix in SISTER_ENTITY_SUFFIXES:
        suffix_words = suffix.split()
        end = start + len(suffix_words)
        if words[start:end] == suffix_words:
            return True
    return False


def _known_alias_word_lists(target_company: str) -> list[list[str]]:
    """This target's own agent-confirmed delivery-center/GCC aliases (see
    KNOWN_COMPANY_ALIASES), pre-tokenized. Empty for every company without a
    confirmed entry — this only ever narrows what already matches as
    'sister_entity' down to 'clean' for a specific, previously-verified
    name; it can never newly cause a rejection."""
    aliases = KNOWN_COMPANY_ALIASES.get(_normalize_company(target_company), [])
    return [_tokenize(alias) for alias in aliases]


def _matches_known_alias(words: list[str], alias_word_lists: list[list[str]]) -> bool:
    """True if the segment begins with one of the target's confirmed known
    aliases — e.g. 'PwC Acceleration Center' for target 'PWC'. Unlike
    SISTER_ENTITY_SUFFIXES (a general suffix shape, always flagged), this is
    a specific business fact already verified for THIS company, so it may
    resolve straight to 'clean' — the same structural-fact standard
    ADR-0006 already applies to company identity, just extended to a name
    the agent has personally confirmed rather than the target's literal
    string.

    Needs the same word-vs-concatenation double check as
    `_target_prefix_len`: the config's alias is written plain ('pwc
    acceleration center') but `_tokenize` splits the real snippet's intercap
    'PwC' into ['pw', 'c'] at the internal capital — confirmed live, this
    test's own PwC case, the identical CrowdStrike/DataKrew mismatch
    `_target_prefix_len` already exists to handle."""
    for alias_words in alias_word_lists:
        n = len(alias_words)
        if _words_match(words[:n], alias_words):
            return True
        alias_joined = "".join(alias_words)
        acc = ""
        for word in words:
            acc += word
            if acc == alias_joined:
                return True
            if len(acc) >= len(alias_joined):
                break
    return False


def _target_reappears(words: list[str], target_words: list[str], after: int, window: int = 20) -> bool:
    """True if `target_words` reoccurs as a contiguous run within `window`
    tokens after position `after`. This is the real, common signal that a
    prefix match is genuinely the same company with prose glued on rather
    than a coincidental prefix: a bio about someone actually AT the target
    company almost always names it again describing their own role —
    'CapgeminiCurrently working in Capgemini as...', 'Gainwell
    Technologies...I head the Analytics vertical at Gainwell Technologies,
    India' were both observed live, 2026-08-03."""
    n = len(target_words)
    end = min(len(words), after + window)
    for i in range(after, end - n + 1):
        if _words_match(words[i:i + n], target_words):
            return True
    return False


def _target_prefix_len(seg_words: list[str], target_words: list[str]) -> int | None:
    """How many of `seg_words` the target occupies when the segment STARTS
    with it, or None when it doesn't.

    Normally that is just `len(target_words)`. The extra pass exists because
    the two sides can tokenize the same name differently: `_CASE_BOUNDARY_RE`
    splits an internal capital, so the real spelling 'CrowdStrike' becomes
    ['crowd', 'strike'] while the DB's 'Crowdstrike' stays ['crowdstrike'] —
    and the word-for-word comparison then reports a genuine employee as
    wrong_company (confirmed live on every CrowdStrike profile, 2026-08-04).
    Any company written with an intercap is exposed to this: PowerToFly,
    DataKrew, LinkedIn.

    Falling back to concatenation equality is exact, not fuzzy — it accepts
    only when the same letters in the same order sit at a token boundary, so
    it adds no substring false-accept surface (ADR-0005). Word-for-word is
    still tried first, since it carries the singular/plural tolerance that
    concatenation cannot ('Technologies' vs 'Technology')."""
    n = len(target_words)
    if _words_match(seg_words[:n], target_words):
        return n

    target_joined = "".join(target_words)
    acc = ""
    for k, word in enumerate(seg_words, start=1):
        acc += word
        if acc == target_joined:
            return k
        if len(acc) >= len(target_joined):
            break
    return None


def _segment_match_reason(
    seg_words: list[str], target_words: list[str], alias_word_lists: list[list[str]] = (),
) -> str | None:
    """Tests ONE segment against the target; returns a reason if this
    segment settles the question (prefix match either direction), or None if
    this segment says nothing about the target at all (caller tries the
    next segment before falling back to 'wrong_company')."""
    if _matches_known_alias(seg_words, alias_word_lists):
        return "clean"

    n = _target_prefix_len(seg_words, target_words)
    if n is not None:
        trailing = seg_words[n:]
        # A trailing legal-entity suffix ("Inc", "Ltd", "Pvt Ltd") is
        # boilerplate, not a distinguishing continuation — 'Highbrow
        # Technology Inc' must read as the SAME company as 'Highbrow
        # Technologies', not a flagged sister entity, once the suffix is
        # stripped away and nothing real is left over.
        if not _strip_trailing_legal_suffix(trailing):
            return "clean"
        if _matches_sister_suffix(seg_words, n):
            return "sister_entity"
        if _target_reappears(seg_words, target_words, n):
            return "clean"
        # Trailing content that neither matches a known sister suffix nor
        # restates the target company — the "Weave" vs "Weave Design"
        # ambiguity string shape alone cannot resolve. Flagged, never
        # silently accepted or rejected.
        return "sister_entity"

    # Reverse direction: this segment IS a shorter root of a longer target
    # name (e.g. candidate field says just "Bosch" while the target company
    # is recorded as "Bosch Group").
    seg_norm = _strip_trailing_legal_suffix(seg_words)
    m = len(seg_norm)
    if m and _words_match(target_words[:m], seg_norm):
        return "sister_entity"

    # Neither direction is a strict prefix of the other, but they share a
    # genuine leading run of whole words before diverging — the "ICICI
    # Bank" vs "ICICI Securities" shape ADR-0006 names explicitly as the
    # ambiguity this gate must FLAG, never silently decide either way
    # ("a prefix relationship that isn't confirmed same-company... passes
    # the gate and carries a flag"). Confirmed live, 2026-08-06: 'KPMG
    # Global Services' (segment) vs 'KPMG India' (target) was falling
    # through this whole function to 'wrong_company' — silently
    # auto-REJECTING a real, human-accepted contact (Somil Jain) — because
    # neither the forward check above (target is a prefix of segment) nor
    # the reverse check just above (segment is a prefix of target) applies
    # when both sides extend PAST a shared prefix in different directions.
    common = 0
    for target_word, seg_word in zip(target_words, seg_norm):
        if not _words_match([target_word], [seg_word]):
            break
        common += 1
    if common and common < len(target_words) and common < len(seg_norm):
        return "sister_entity"

    return None


def _company_match_reason(candidate: ProfileCandidate) -> str:
    """One of: 'clean' (confident same-company match), 'sister_entity'
    (shares a word-boundary-aligned prefix with the target but the
    remainder can't be confirmed as the same organisation — the
    Capgemini/Capgemini-Invent, Binance/Binance.US shape), 'no_signal' (no
    structural company marker found at all), 'wrong_company' (one or more
    structural markers exist and NONE of them begin with the target), or
    'former' (a segment matches, but an explicit past-role marker sits next
    to the mention). Only 'wrong_company' and 'former' fail the axis — see
    `company_ok`.

    Tries every structural company marker the snippet offers (see
    `_candidate_company_segments`), not a single fixed position — Bright
    Data's snippet shape varies (confirmed live: a plain
    'Location·Title·Company' shape and a 'Title at Company·Experience:
    Company·Location' shape both occur), and trusting one fixed segment
    index silently mis-parsed real Highbrow Technologies employees as
    wrong-company on 2026-08-03.

    The SERP title is mined too (`_title_company_segments`): it is the
    LinkedIn headline, and a profile whose snippet body is pure prose often
    names its employer only there."""
    target_words = _strip_trailing_legal_suffix(_tokenize(candidate.target_company))
    if not target_words:
        return "no_signal"

    segments = (
        _candidate_company_segments(candidate.snippet)
        + _title_company_segments(candidate.title)
    )
    if not segments:
        return "no_signal"

    alias_word_lists = _known_alias_word_lists(candidate.target_company)
    for seg in segments:
        reason = _segment_match_reason(_tokenize(seg), target_words, alias_word_lists)
        if reason == "clean":
            return _former_or_clean(candidate, target_words)
        if reason == "sister_entity":
            return "sister_entity"
        # None -> this segment said nothing about the target; try the next.

    return "wrong_company"


def _former_or_clean(candidate: ProfileCandidate, target_words: list[str]) -> str:
    """Called only when the structured company field IS confirmed the
    target — so the only way this becomes 'former' is an EXPLICIT past-role
    marker (FORMER_ROLE_MARKERS) sitting near the mention, never an
    inference from a missing 'Present'. A current employer stated in the
    structured field is authoritative per judging-and-evidence.md.

    A marker BEFORE the target mention always counts ('Ex-Samsara',
    'Former ... at Samsara', 'previously at Samsara' — it can only be
    describing the mention it precedes). A marker AFTER the mention counts
    only if the target's own name reappears again shortly past the marker
    too ('CapgeminiFormer Capgemini employee...' — the repeated 'Capgemini'
    confirms the marker is about the target). Without that reappearance, a
    trailing marker is describing a DIFFERENT, later company instead —
    confirmed live 2026-08-03: 'Data and AI at Samsara | Ex - HP Inc' was
    misread as Arup Kumar Maiti having left Samsara, when 'Ex-' actually
    describes his prior employer HP, named right after it; nothing repeats
    'Samsara' after the marker, which is exactly the tell."""
    if not target_words:
        return "clean"
    haystack = f"{candidate.title} {candidate.snippet}".lower()
    target_pos = haystack.find(target_words[0])
    if target_pos == -1:
        return "clean"
    target_len = len(target_words[0])
    before = haystack[max(0, target_pos - 60):target_pos]
    if any(marker in before for marker in FORMER_ROLE_MARKERS):
        return "former"
    after = haystack[target_pos + target_len:target_pos + target_len + 60]
    for marker in FORMER_ROLE_MARKERS:
        idx = after.find(marker)
        if idx == -1:
            continue
        tail = after[idx + len(marker):idx + len(marker) + 40]
        if target_words[0] in tail:
            return "former"
    return "clean"


def shape_ok(candidate: ProfileCandidate) -> bool:
    """Structural URL-shape re-check — the same test `bright_data_profiles`
    already applies upstream. Re-validated here so this gate is safe to run
    standalone (backtests, a future non-Bright-Data source) without silently
    trusting an already-filtered list."""
    url = candidate.url.split("?")[0].split("#")[0].rstrip("/")
    return _PROFILE_URL_RE.match(url) is not None


def company_ok(candidate: ProfileCandidate) -> bool:
    """The only axis doing the bulk of what used to be manual reading: is
    the structured company field the target, a plausible sister entity, or
    genuinely unrelated? Only 'wrong_company' and 'former' return False —
    'sister_entity' and 'no_signal' PASS this axis (they are not evidence of
    a wrong company, only of uncertainty) and are surfaced as flags instead,
    see `filter_relevant`."""
    return _company_match_reason(candidate) not in _COMPANY_REJECT_REASONS


# The two axes that may reject a candidate, in check order. Company is
# checked second (after the cheap structural shape check) since it's the
# more expensive/interesting one to inspect when reading a failure.
AXIS_CHECKS: list[tuple[str, Callable[[ProfileCandidate], bool]]] = [
    ("shape", shape_ok),
    ("company", company_ok),
]

AXIS_NAMES: list[str] = [name for name, _ in AXIS_CHECKS]

# Reasons that pass the company axis but are still worth a flag on a kept
# candidate — the store-and-flag half of the drop-at-ingest/store-and-flag
# split this module's docstring explains.
_COMPANY_FLAG_REASONS = frozenset({"sister_entity", "no_signal"})


def first_failing_axis(candidate: ProfileCandidate) -> str | None:
    for axis_name, check in AXIS_CHECKS:
        if not check(candidate):
            return axis_name
    return None


def filter_relevant(
    candidates: list[ProfileCandidate],
) -> tuple[list[ProfileCandidate], dict[str, int], list[dict[str, Any]], list[dict[str, Any]]]:
    """Apply the two structural axes. Returns (kept, drop_counts,
    drop_details, flags):
      - `kept` — candidates the agent should read and judge.
      - `drop_counts` — pre-seeded to all axis names, like the jobs gate.
      - `drop_details` — one dict per DROPPED candidate: {"axis":...,
        "company_reason": "wrong_company"|"former"} for axis=="company".
      - `flags` — one dict per KEPT candidate whose company match was
        'sister_entity' or 'no_signal': {"candidate": ..., "reason": ...}.
        This is the extra element vs. the jobs gate's 3-tuple — see module
        docstring for why contacts need store-and-flag, not drop-at-ingest,
        for the ambiguous case."""
    drop_counts: dict[str, int] = {name: 0 for name in AXIS_NAMES}
    kept: list[ProfileCandidate] = []
    drop_details: list[dict[str, Any]] = []
    flags: list[dict[str, Any]] = []

    for candidate in candidates:
        axis = first_failing_axis(candidate)
        if axis is not None:
            drop_counts[axis] += 1
            detail: dict[str, Any] = {"axis": axis, "candidate": candidate}
            if axis == "company":
                detail["company_reason"] = _company_match_reason(candidate)
            drop_details.append(detail)
            continue

        kept.append(candidate)
        reason = _company_match_reason(candidate)
        if reason in _COMPANY_FLAG_REASONS:
            flags.append({"candidate": candidate, "reason": reason})

    return kept, drop_counts, drop_details, flags


# Where verdict counts are logged, one line per `filter_relevant` call — see
# `log_gate_verdict`. Deliberately a separate file from
# `search_source.BRIGHT_DATA_CALL_LOG`: that ledger answers "did we spend
# the call", this one answers "what did the gate decide", and conflating them
# would mean every billing-ledger reader has to skip rows it doesn't
# understand. Exists so the "how often does a kept contact actually need a
# judgement call, vs. being an unambiguous clean structural match" question
# (raised 2026-08-05, no historical data existed to answer it — the call
# ledger only logs a result *count*, and evidence-file contacts are
# paraphrased text that can't be re-gated) can be answered for free off the
# next real batch instead of costing a dedicated measurement run.
GATE_VERDICT_LOG = Path(__file__).resolve().parents[2] / "logs" / "gate_verdicts.jsonl"


def log_gate_verdict(
    kept: list[ProfileCandidate],
    drop_counts: dict[str, int],
    flags: list[dict[str, Any]],
    *,
    company_id: int | None = None,
    search_group: str | None = None,
    tier: str | None = None,
    attempt: int | None = None,
) -> None:
    """Append one aggregate line per gate run. `clean` is `len(kept)` minus
    the flagged ones — the count that could, in principle, be auto-stored
    without an agent judgement call if the "auto-accept clean matches, only
    escalate flags" idea (2026-08-05) is built. Counts only, never candidate
    text — this log's job is a ratio, not a second evidence trail."""
    flagged_reasons: dict[str, int] = {}
    for f in flags:
        flagged_reasons[f["reason"]] = flagged_reasons.get(f["reason"], 0) + 1
    clean = len(kept) - len(flags)

    try:
        GATE_VERDICT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with GATE_VERDICT_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(),
                "company_id": company_id,
                "search_group": search_group,
                "tier": tier,
                "attempt": attempt,
                "kept_clean": clean,
                "kept_flagged": flagged_reasons,
                "dropped_shape": drop_counts.get("shape", 0),
                "dropped_company": drop_counts.get("company", 0),
            }) + "\n")
    except OSError:
        # Never let bookkeeping break a run mid-batch — same convention as
        # search_source._log_bright_data_call.
        pass


# ---------------------------------------------------------------------------
# Advisory-only tier/domain scoring. NOTHING below this line may drop a
# candidate — see module docstring for why that boundary is load-bearing.
# ---------------------------------------------------------------------------


class TierScorer(Protocol):
    """A pluggable tier-fit scorer. Swappable so a future embedding backend
    can drop in without touching any caller — see RapidFuzzTierScorer for why
    the first implementation deliberately isn't one (no torch dependency
    added for a component that can only ever be advisory)."""
    def score(self, title: str) -> dict[str, float]: ...


@dataclass(frozen=True)
class TierSuggestion:
    """`competing` lets a close call be visibly close rather than silently
    resolved — e.g. a title that scores nearly equally for `head` and
    `hiring_manager` should show both, not just the winner."""
    tier: str | None
    score: float
    competing: list[tuple[str, float]]


class RapidFuzzTierScorer:
    """`rapidfuzz.token_set_ratio` of a title against `TIER_TITLE_MARKERS`.
    Chosen as the first implementation because `rapidfuzz` is already a
    declared dependency (used by scripts/dedup_jobs.py) and pulls no new
    heavy dependency — an embedding model would add `torch`, this project's
    first ~1GB dependency, for a component whose output can only ever be a
    suggestion. If the vocabulary approach demonstrably falls short, the
    `TierScorer` protocol means an embedding backend replaces this without
    any caller changing."""

    def score(self, title: str) -> dict[str, float]:
        from rapidfuzz import fuzz

        title_lower = title.lower()
        demoted = any(marker in title_lower for marker in TIER_DEMOTION_MARKERS)

        scores: dict[str, float] = {}
        for tier, markers in TIER_TITLE_MARKERS.items():
            if demoted and tier in (HEAD, HIRING_MANAGER):
                # Floored to 0, not merely lowered — a Program Manager must
                # never win head/hiring_manager on a fuzzy near-miss. This is
                # the #75 human-review rule encoded, not discovered by score.
                scores[tier] = 0.0
                continue
            scores[tier] = max(
                (fuzz.token_set_ratio(title_lower, marker) for marker in markers),
                default=0.0,
            )
        return scores


DEFAULT_TIER_SCORER: TierScorer = RapidFuzzTierScorer()


def suggest_tier(title: str, scorer: TierScorer = DEFAULT_TIER_SCORER) -> TierSuggestion:
    """Advisory tier suggestion for a title — NEVER used to accept or reject
    a candidate, only to help the agent triage a batch of results faster.
    The agent's own judgement (SKILL.md step 4) remains the actual tier
    decision recorded on a `JudgedContact`."""
    scores = scorer.score(title)
    if not scores:
        return TierSuggestion(tier=None, score=0.0, competing=[])
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    best_tier, best_score = ranked[0]
    if best_score <= 0:
        return TierSuggestion(tier=None, score=0.0, competing=ranked)
    return TierSuggestion(tier=best_tier, score=best_score, competing=ranked)
