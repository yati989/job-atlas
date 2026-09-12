"""
Pure title classifiers for the triage layer (ADR-0007). No I/O, no DB, no
`ProfileCandidate` — everything here takes and returns plain data so it can
be unit-tested against real stored titles without a session. `triage.py`
composes these with company-identity results from `profile_relevance.py`;
this module never imports that one (the dependency runs the other way, if
at all).

Two axes, on purpose kept in separate functions with separate return types
(`SeniorityVerdict`, `FunctionSignals`) rather than a single fused score —
see `app/config/contact_function.py`'s module docstring for why the fused
`RapidFuzzTierScorer` approach produces false positives (a 1-2 word marker's
tokens are trivially a subset of most titles under `token_set_ratio`).
"""
import re
from dataclasses import dataclass, field

from app.config.contact_function import (
    AGENCY_MARKERS, FUNCTION_IN_SCOPE, FUNCTION_MANAGER_PREFIXES,
    FUNCTION_OUT_OF_SCOPE, FUNCTION_RECRUITING, L1_IC, L2_MODIFIER_MARKERS,
    L2_SENIOR_IC, L3_MANAGER, L4_HEAD, L5_EXEC, OUT_OF_SCOPE_ARMED,
    PRINCIPAL_MARKERS, SENIORITY_DOWNGRADE_MODIFIERS, SENIORITY_MARKERS,
    UNQUALIFIED_CHIEF_MARKERS, VP_MARKERS,
)

# Reuse the same normalization the company-identity gate already uses, so a
# title is tokenized identically everywhere in this project — including the
# camelCase-boundary recovery Bright Data's glued-together text needs.
from app.contacts.profile_relevance import _tokenize  # noqa: E402

_COMPANY_BOUNDARY_RE = re.compile(r"\s+at\s+|@", re.I)


# ---------------------------------------------------------------------------
# Headline resolution
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Headline:
    """The text the classifiers below actually read.

    `trusted=False` means the resolution is a guess (no reliable "Name - "
    boundary was found) — callers must never let an untrusted headline drive
    AUTO_ACCEPT or AUTO_REJECT, only ESCALATE-track annotation."""
    text: str
    trusted: bool


def resolve_headline(title: str) -> Headline:
    """Real Bright Data SERP titles are the LinkedIn headline in the form
    'First Last - Role text ...' (confirmed live across every batch this
    project has run — 'Shrikant Hebbar - Senior Director, Insights & Data
    at...'). This strips the leading name segment (first ' - ' split) and
    then truncates at the first COMPANY boundary (' at '/'@') so a
    company's own name can't leak stray vocabulary into the classifiers —
    e.g. a hypothetical '... @ Marketing Solutions Inc' must not make a
    Data Scientist look like a marketing hit.

    This is a deliberately CONSERVATIVE truncation: text after the company
    boundary is discarded even when it's genuine function signal ('Executive
    Director @ PwC India | Data and Analytics' loses 'Data and Analytics').
    That's an accepted trade — losing signal only ever pushes a row from
    AUTO_ACCEPT to ESCALATE (safe direction), never the reverse.

    Does NOT use `ProfileCandidate.full_name` — at the one real call site
    (`brightdata_query._print_rows_gated`), candidates are constructed with
    `full_name=""` because the agent extracts the real name during judging,
    after the gate has already run. Position-based splitting on the first
    ' - ' is the only signal available at gate time, and it's reliable
    because Bright Data's title shape is structural, not free prose (see
    `profile_relevance._title_company_segments`'s docstring for the same
    "headline is structured" premise).

    ONLY for a RAW SERP title. The already-name-stripped titles stored in
    `contacts.title` (the agent writes those by hand at judging time, e.g.
    'Director - AI' not 'Vijayender Rathore - Director - AI') must go
    through `truncate_at_company_boundary` directly instead — confirmed by
    the backtest the hard way: this function, run on the ALREADY-clean
    stored title 'Executive Director - Data and Analytics at PwC India',
    stripped 'Executive Director' off as if it were a person's name."""
    parts = title.split(" - ", 1)
    if len(parts) < 2 or not parts[1].strip():
        # No 'Name - ' boundary found at all — atypical shape, don't guess.
        return Headline(text=title, trusted=False)

    return Headline(text=truncate_at_company_boundary(parts[1]), trusted=True)


def truncate_at_company_boundary(text: str) -> str:
    """Keep only the text before the first company-boundary marker (' at '/
    '@'). Safe to call on ANY headline-shaped text regardless of source —
    unlike the name-prefix strip above, there's no name-vs-role ambiguity
    here, just 'stop before a company name might start'. See
    `resolve_headline`'s docstring for the conservative-truncation trade
    this makes (losing genuine function signal after the boundary is
    accepted, because it only ever pushes AUTO_ACCEPT to ESCALATE, never
    the reverse)."""
    boundary = _COMPANY_BOUNDARY_RE.search(text)
    role_text = text[: boundary.start()] if boundary else text
    return role_text.strip()


# ---------------------------------------------------------------------------
# Axis A: seniority
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SeniorityVerdict:
    level: int
    matched: tuple[str, ...] = field(default_factory=tuple)
    ambiguous: bool = True
    notes: tuple[str, ...] = field(default_factory=tuple)


def _phrase_in_tokens(tokens: list[str], phrase: str) -> bool:
    """True if `phrase` (space-separated, possibly multi-word) occurs as a
    contiguous run in `tokens` — the same whole-phrase, word-boundary
    standard `profile_relevance._matches_sister_suffix`/
    `_matches_known_alias` already use, deliberately never substring or
    fuzzy."""
    phrase_tokens = phrase.split()
    n = len(phrase_tokens)
    return any(tokens[i:i + n] == phrase_tokens for i in range(len(tokens) - n + 1))


def classify_seniority(text: str) -> SeniorityVerdict:
    """Highest matched ANCHOR level wins — an anchor is an explicit L1 IC
    noun, L3 "manager", L4 "director"/"head", or L5 CxO phrase
    (`SENIORITY_MARKERS`). `ambiguous=True` whenever the result can't be
    trusted at face value — see the individual checks below. Only an
    `ambiguous=False` verdict may ever drive AUTO_ACCEPT (triage.py).

    L2 (senior/sr/principal/staff, `L2_MODIFIER_MARKERS`) is deliberately a
    MODIFIER, never an anchor on its own — the backtest against all 782
    stored contacts is why: 'Senior AVP, Fraud & Credit Risk' (stored
    `head`) and 'Senior Technical Product Owner' (stored `hiring_manager`)
    both have "senior" as their ONLY matched word, and letting it arm alone
    would have silently produced `ic` for both. When an L2 modifier
    co-occurs with a real anchor ('Senior Data Scientist' = L2 + L1
    "scientist"), nothing is lost by this restriction — L1 and L2 map to
    the same tier in `triage._tier_for` anyway."""
    tokens = _tokenize(text)
    if not tokens:
        return SeniorityVerdict(level=L1_IC, matched=(), ambiguous=True, notes=("no_text",))

    matched_levels: dict[int, list[str]] = {}
    for level, markers in SENIORITY_MARKERS.items():
        hits = [m for m in markers if _phrase_in_tokens(tokens, m)]
        if hits:
            matched_levels[level] = hits

    notes: list[str] = []
    modifiers: list[str] = [m for m in L2_MODIFIER_MARKERS if _phrase_in_tokens(tokens, m)]

    # "lead" is POSITION-dependent (contact_function.py's measured split:
    # prefix "Lead Data Scientist" was 87% ic on n=15, but the backtest
    # still found real anchor-co-occurring disagreement — 'Lead Data
    # Scientist/DATA & AI' stored hiring_manager despite an L1 "scientist"
    # anchor present). Fully demoted: identified, but ALWAYS forces
    # ambiguous regardless of what else matched, unlike senior/sr/
    # principal/staff which only fail to arm when they're the sole signal.
    if tokens[0] == "lead" and len(tokens) > 1:
        notes.append("lead_prefix_unarmed")
    elif "lead" in tokens:
        notes.append("lead_head_noun_unarmed")

    if any(_phrase_in_tokens(tokens, m) for m in VP_MARKERS):
        notes.append("vp_family_unarmed")
    if any(_phrase_in_tokens(tokens, m) for m in UNQUALIFIED_CHIEF_MARKERS):
        notes.append("chief_unqualified_unarmed")
    if any(_phrase_in_tokens(tokens, m) for m in PRINCIPAL_MARKERS):
        # Unlike senior/sr/staff, "principal" disagreed with a real stored
        # contact even alongside a co-occurring L1 anchor — see
        # contact_function.py's PRINCIPAL_MARKERS docstring.
        notes.append("principal_unarmed")

    if not matched_levels:
        # No anchor at all — a bare modifier ("Senior AVP...") or nothing
        # recognized. Never confidently L1; see the docstring above for the
        # two real stored contradictions that made this non-negotiable.
        level = L2_SENIOR_IC if modifiers else L1_IC
        return SeniorityVerdict(level=level, matched=tuple(modifiers), ambiguous=True, notes=tuple(notes))

    best_level = max(matched_levels)
    best_matched = tuple(matched_levels[best_level]) + tuple(modifiers)

    ambiguous = bool(notes)
    if best_level == L5_EXEC:
        # Never row-decidable — see contact_function.py's L5 comment
        # (2/8 head, 4/8 exec_fallback, 2/8 talent_acquisition on real
        # stored data, AND exec_fallback is a batch-level, not row-level,
        # fact).
        ambiguous = True
        notes.append("l5_never_armed")
    if len(matched_levels) > 1:
        # Competing levels (e.g. both "director" and "manager" present) —
        # surfaced, not silently resolved by taking the max.
        ambiguous = True
        notes.append("competing_levels")
    if best_level in (L3_MANAGER, L4_HEAD):
        preceding_downgrade = any(
            i > 0 and tokens[i - 1] in SENIORITY_DOWNGRADE_MODIFIERS
            for i, tok in enumerate(tokens)
            if tok in ("director", "manager")
        )
        if preceding_downgrade:
            ambiguous = True
            notes.append("downgrade_modifier")
    if best_level == L3_MANAGER:
        preceding_function_manager = any(
            i > 0 and tokens[i - 1] in FUNCTION_MANAGER_PREFIXES
            for i, tok in enumerate(tokens)
            if tok == "manager"
        )
        if preceding_function_manager:
            ambiguous = True
            notes.append("function_manager_prefix")

    return SeniorityVerdict(level=best_level, matched=best_matched, ambiguous=ambiguous, notes=tuple(notes))


# ---------------------------------------------------------------------------
# Axis B: function
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class FunctionSignals:
    in_scope: tuple[str, ...] = field(default_factory=tuple)
    recruiting: tuple[str, ...] = field(default_factory=tuple)
    out_of_scope: tuple[str, ...] = field(default_factory=tuple)
    armed_out_of_scope: tuple[str, ...] = field(default_factory=tuple)

    @property
    def verdict(self) -> str:
        """One of: 'in_scope', 'recruiting', 'mixed' (positive AND negative
        both present), 'out_of_scope' (negative only), 'unknown' (nothing
        matched — NEVER treated as evidence of anything, see
        `contact_function.py`'s FUNCTION_IN_SCOPE docstring)."""
        positive = self.in_scope or self.recruiting
        if positive and self.out_of_scope:
            return "mixed"
        if positive:
            return "recruiting" if self.recruiting else "in_scope"
        if self.out_of_scope:
            return "out_of_scope"
        return "unknown"


def classify_function(text: str) -> FunctionSignals:
    tokens = _tokenize(text)
    if not tokens:
        return FunctionSignals()

    in_scope = tuple(m for m in FUNCTION_IN_SCOPE if _phrase_in_tokens(tokens, m))
    recruiting = tuple(m for m in FUNCTION_RECRUITING if _phrase_in_tokens(tokens, m))
    out_of_scope = tuple(m for m in FUNCTION_OUT_OF_SCOPE if _phrase_in_tokens(tokens, m))
    armed = tuple(m for m in out_of_scope if m in OUT_OF_SCOPE_ARMED)

    return FunctionSignals(
        in_scope=in_scope, recruiting=recruiting,
        out_of_scope=out_of_scope, armed_out_of_scope=armed,
    )


def has_agency_marker(text: str) -> bool:
    """RPO/agency-embedded-recruiter language — never rejects on its own,
    only blocks AUTO_ACCEPT (see AGENCY_MARKERS' docstring for the real
    stored counter-example at a staffing firm)."""
    lowered = f" {text.lower()} "
    return any(marker in lowered for marker in AGENCY_MARKERS)
