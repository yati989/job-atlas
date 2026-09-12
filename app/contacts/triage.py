"""
Composition layer for the rule-based tier/function triage (ADR-0007). Takes
the company-identity result `profile_relevance.filter_relevant` already
computed plus the two axis classifiers in `title_classification.py`, and
produces one of three verdicts per kept candidate — never gates
`filter_relevant` itself, never widens its 4-tuple (see that module's
docstring and `tests/test_profile_gate.py::test_filter_relevant_still_returns_four_tuple`).

WHY THIS STOPS SHORT OF AUTO-STORING, EVEN AT `TRIAGE_MODE=gate`: an
AUTO_ACCEPT verdict still requires the agent to write the `JudgedContact` and
run the name-collision check (`judging-and-evidence.md`'s step 4.4) — that
failure mode (a guessed email silently delivering to a same-named stranger)
is invisible after the fact and code cannot see it from a title string. See
the module-level `AUTO_ACCEPT`/`ESCALATE`/`AUTO_REJECT` docstrings below.
"""
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from app.contacts.ladder import HEAD, HIRING_MANAGER, IC, TALENT_ACQUISITION
from app.contacts.title_classification import (
    FunctionSignals, Headline, SeniorityVerdict, classify_function,
    classify_seniority, has_agency_marker, resolve_headline,
)
from app.config.contact_function import L3_MANAGER, L4_HEAD

AUTO_ACCEPT = "AUTO_ACCEPT"   # eligible to become a machine-drafted JudgedContact
ESCALATE = "ESCALATE"          # the agent reads and judges it, exactly as today
AUTO_REJECT = "AUTO_REJECT"    # moves to the structurally-dropped section (gate mode only)


@dataclass(frozen=True)
class TriageVerdict:
    decision: str
    tier: str | None
    headline: Headline
    seniority: SeniorityVerdict
    function: FunctionSignals
    reasons: tuple[str, ...] = field(default_factory=tuple)


def _tier_for(fn: FunctionSignals, sen: SeniorityVerdict) -> str:
    """Function DOMINATES seniority — real stored confirmation: 'Head of
    Talent Acquisition, India', 'Director, Talent Acquisition' (x2), 'Branch
    Head - Recruitment' are ALL stored `talent_acquisition`, never `head`,
    despite each carrying a strong L4 seniority word.

    Never returns EXEC_FALLBACK: 0 of 782 stored contacts were classified
    from a title alone into it, and it is conditional on a BATCH-level fact
    (head + hiring_manager both empty for this company) no row-level
    classifier can see — see contact_function.py's L5 comment."""
    if fn.verdict == "recruiting":
        return TALENT_ACQUISITION
    if sen.level >= L4_HEAD:
        return HEAD
    if sen.level == L3_MANAGER:
        return HIRING_MANAGER
    return IC


def _auto_acceptable(hl: Headline, fn: FunctionSignals, sen: SeniorityVerdict, company_type: str) -> bool:
    if not hl.trusted:
        return False
    if fn.verdict not in ("in_scope", "recruiting"):
        return False
    # Seniority ambiguity only matters when the FINAL TIER actually depends
    # on it. `_tier_for` ignores seniority entirely once function is
    # "recruiting" (function dominates — see its docstring), so gating a
    # bare 'Recruiter at Oracle' on seniority ambiguity would block the
    # single most common, highest-confidence AUTO_ACCEPT shape for no
    # reason: "recruiter" itself isn't a seniority word, so classify_
    # seniority correctly reports it as having no anchor at all, which
    # (post-backtest-fix) is unconditionally ambiguous — that ambiguity is
    # real but irrelevant here.
    if sen.ambiguous and fn.verdict != "recruiting":
        return False
    if has_agency_marker(hl.text) and company_type != "staffing":
        return False
    if company_type == "staffing" and fn.verdict != "recruiting":
        # STAFFING_LADDER (ladder.py) only ever plans talent_acquisition
        # queries for a staffing firm — a non-recruiting hit there is
        # structurally unexpected, not confidently classifiable.
        return False
    return True


def triage(
    title: str,
    *,
    company_type: str,
    company_reason: str,
) -> TriageVerdict:
    """`company_reason` is `profile_relevance._company_match_reason`'s
    output for this same candidate — passed in rather than recomputed so
    this module never needs to import `profile_relevance` (the layering
    stays one-directional).

    `title` here is the RAW SERP title/headline ('Name - Role text ...') —
    this calls `resolve_headline` to strip the name. Do not call this with
    an already-name-stripped title (e.g. `contacts.title` as stored in the
    DB, which the agent already cleaned by hand at judging time) — use
    `triage_from_headline` for that, or `resolve_headline` will wrongly
    treat the first word of the role itself as a name (confirmed live by
    the backtest: 'Executive Director - Data and Analytics at PwC India'
    got 'Executive Director' stripped as if it were a person's name)."""
    return triage_from_headline(resolve_headline(title), company_type=company_type, company_reason=company_reason)


def triage_from_headline(
    hl: Headline,
    *,
    company_type: str,
    company_reason: str,
) -> TriageVerdict:
    """The composition logic on an ALREADY-RESOLVED headline — the seam
    `triage()` and the backtest (`tests/test_triage_backtest.py`) both call,
    so a stored, already-name-stripped `contacts.title` can be classified
    honestly (`Headline(text=title, trusted=True)`) without double-stripping
    it through `resolve_headline`'s name-boundary guess."""
    fn = classify_function(hl.text)
    sen = classify_seniority(hl.text)

    reasons: list[str] = []

    if fn.verdict == "out_of_scope" and fn.armed_out_of_scope and hl.trusted:
        reasons.append(f"out_of_scope:{','.join(fn.armed_out_of_scope)}")
        return TriageVerdict(AUTO_REJECT, tier=None, headline=hl, seniority=sen, function=fn, reasons=tuple(reasons))

    if company_reason == "clean" and _auto_acceptable(hl, fn, sen, company_type):
        tier = _tier_for(fn, sen)
        reasons.append(f"sen:L{sen.level}->{tier}")
        reasons.append(f"fn:{fn.verdict}")
        return TriageVerdict(AUTO_ACCEPT, tier=tier, headline=hl, seniority=sen, function=fn, reasons=tuple(reasons))

    if not hl.trusted:
        reasons.append("headline_untrusted")
    if company_reason != "clean":
        reasons.append(f"company:{company_reason}")
    if fn.verdict not in ("in_scope", "recruiting"):
        reasons.append(f"fn:{fn.verdict}")
    if sen.ambiguous:
        reasons.append("seniority_ambiguous")
        reasons.extend(sen.notes)
    return TriageVerdict(ESCALATE, tier=None, headline=hl, seniority=sen, function=fn, reasons=tuple(reasons))


# ---------------------------------------------------------------------------
# Shadow logging — one line PER CANDIDATE, unlike
# `profile_relevance.log_gate_verdict`'s aggregate-per-call shape, because
# arming an OUT_OF_SCOPE marker (contact_function.py) and building the
# confusion matrix (scripts/audit_triage.py) both need the individual
# headline text, not just a count.
# ---------------------------------------------------------------------------
TRIAGE_SHADOW_LOG = Path(__file__).resolve().parents[2] / "logs" / "triage_shadow.jsonl"
_EMAIL_TEXT_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)


def log_triage_shadow(
    verdicts: dict[str, TriageVerdict],
    *,
    company_id: int | None,
    search_group: str | None,
    tier_searched: str | None,
    attempt: int | None,
    company_reasons: dict[str, str],
    redact_emails: bool = False,
) -> None:
    try:
        TRIAGE_SHADOW_LOG.parent.mkdir(parents=True, exist_ok=True)
        with TRIAGE_SHADOW_LOG.open("a", encoding="utf-8") as fh:
            for url, v in verdicts.items():
                fh.write(json.dumps({
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "company_id": company_id,
                    "search_group": search_group,
                    "tier_searched": tier_searched,
                    "attempt": attempt,
                    "url": url,
                    "headline_text": (
                        _EMAIL_TEXT_RE.sub("[email removed]", v.headline.text)
                        if redact_emails
                        else v.headline.text
                    ),
                    "headline_trusted": v.headline.trusted,
                    "company_reason": company_reasons.get(url),
                    "decision": v.decision,
                    "tier": v.tier,
                    "seniority_level": v.seniority.level,
                    "seniority_matched": list(v.seniority.matched),
                    "seniority_ambiguous": v.seniority.ambiguous,
                    "fn_in_scope": list(v.function.in_scope),
                    "fn_recruiting": list(v.function.recruiting),
                    "fn_out_of_scope": list(v.function.out_of_scope),
                    "reasons": list(v.reasons),
                }) + "\n")
    except OSError:
        # Never let bookkeeping break a run mid-batch — same convention as
        # search_source._log_bright_data_call / profile_relevance.log_gate_verdict.
        pass
