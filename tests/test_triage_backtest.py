"""
Backtest: replay the rule-based triage layer (ADR-0007) over every REAL
contact this project has ever stored — the leak-rate-audit analogue for
`triage.py`, mirroring `test_profile_gate_backtest.py`'s role for the
company-identity gate and `scripts/audit_leak_rate.py`'s role for the jobs
gate.

Queries the LIVE `contacts` table rather than a frozen fixture on purpose:
this corpus grows with every batch, and a stale snapshot would silently stop
catching new contradictions. Skips gracefully if the DB isn't reachable in
this checkout — same soft-skip convention `test_profile_gate_backtest.py`
already uses for missing evidence files, just for a missing DB instead of a
missing directory.

Ground truth caveat, argued honestly rather than assumed: `contacts.title` is
the raw stored headline (not the evidence file's paraphrase), so this is a
STRONGER backtest than the 131-contact evidence-file one — but it's still
only as good as the human judgement that produced `seniority_tier` in the
first place, and it says nothing about company identity (every row here
already passed that axis by construction, which is why `triage()` is called
with `company_reason="clean"` uniformly)."""
import pytest
from sqlalchemy import select

from app.contacts.title_classification import Headline, truncate_at_company_boundary
from app.contacts.triage import AUTO_ACCEPT, AUTO_REJECT, triage_from_headline
from app.config.contact_function import FUNCTION_OUT_OF_SCOPE


def _triage_stored_title(title: str, *, company_type: str, company_reason: str):
    """`contacts.title` is already name-stripped (the agent cleaned it by
    hand at judging time, e.g. 'Director - AI' not 'Vijayender Rathore -
    Director - AI') — feeding it through `triage()`'s `resolve_headline`
    would wrongly treat the first word of the ROLE as a person's name (this
    is exactly what happened on the first run of this backtest: 'Executive
    Director - Data and Analytics at PwC India' got 'Executive Director'
    stripped away, dropping it from `head` to `ic`). This still runs the
    company-boundary truncation, since some stored titles DO carry an
    inline company mention ('... at PwC India') from sessions that wrote
    titles less strictly — only the name-prefix guess is skipped."""
    return triage_from_headline(
        Headline(text=truncate_at_company_boundary(title), trusted=True),
        company_type=company_type, company_reason=company_reason,
    )


def _load_corpus() -> list[tuple[str, str, str]]:
    """Returns (title, stored_tier, company_type) for every contact with a
    real title. Import is local so a missing DB config doesn't fail
    collection for the whole test session."""
    from app.db.session import get_session
    from app.models.orm import Company, Contact

    with get_session() as session:
        rows = session.execute(
            select(Contact.title, Contact.seniority_tier, Company.company_type)
            .join(Company, Contact.company_id == Company.id)
            .where(Contact.title.is_not(None))
        ).all()
    return [(title, tier, company_type or "employer") for title, tier, company_type in rows]


def _try_load_corpus() -> list[tuple[str, str, str]] | None:
    try:
        return _load_corpus()
    except Exception:
        return None


_CORPUS = _try_load_corpus()


@pytest.mark.skipif(_CORPUS is None, reason="DB not reachable in this checkout")
def test_backtest_corpus_is_large_enough_to_trust():
    """Guards against a broken query silently making the invariants below
    vacuously true — 782 stored contacts at the time this test was written,
    so 500 is a safe floor with headroom for the corpus shrinking slightly
    (dedup) without breaking CI on a technicality."""
    assert len(_CORPUS) > 500, f"only loaded {len(_CORPUS)} contacts — query likely broken"


@pytest.mark.skipif(_CORPUS is None, reason="DB not reachable in this checkout")
def test_triage_never_auto_rejects_a_real_stored_contact(monkeypatch):
    """THE HARD INVARIANT. Run with every candidate out-of-scope word ARMED
    (not just today's empty OUT_OF_SCOPE_ARMED) — otherwise this test would
    be vacuous, since nothing can reject while the armed set is empty. A
    failure here is blocking: it means some real, human-accepted contact's
    title contains a word this design considers unambiguously wrong."""
    import app.contacts.title_classification as tc
    monkeypatch.setattr(tc, "OUT_OF_SCOPE_ARMED", frozenset(FUNCTION_OUT_OF_SCOPE))

    rejected = []
    for title, tier, company_type in _CORPUS:
        v = _triage_stored_title(title, company_type=company_type, company_reason="clean")
        if v.decision == AUTO_REJECT:
            rejected.append((title, tier, v.reasons))

    assert rejected == [], (
        f"triage would have auto-rejected {len(rejected)} real stored contact(s): "
        + "; ".join(f"{t!r} (stored {tier}, {r})" for t, tier, r in rejected[:10])
    )


# One named, accepted exception, not a threshold. This is what remained
# after fixing every disagreement the first backtest runs found — each fix
# is documented in contact_function.py (explicit L1 IC-noun list instead of
# a "nothing else matched" default; L2 demoted to modifier-only; "lead"/
# "principal" fully demoted after they disagreed even alongside a real
# anchor; "avp"/"cio"/"chief" added to the never-arm identification lists).
# "Engineer, Data.Analytics.AI" (stored `head`) is bare "Engineer" with NO
# other recognized marker — demoting "engineer" to fix this one case would
# break 201 of its other 209 correct occurrences (96% measured purity, the
# single strongest signal in the whole vocabulary). Named here so a NEW
# disagreement still fails loudly and isn't silently swallowed by a
# percentage threshold — only this specific, already-investigated title is
# exempt.
_KNOWN_TIER_DISAGREEMENTS = {"Engineer, Data.Analytics.AI"}


@pytest.mark.skipif(_CORPUS is None, reason="DB not reachable in this checkout")
def test_triage_tier_agreement_on_auto_accepted_is_total():
    """100%, minus the one named exception above — not a threshold/rate. A
    NEW disagreement here is a silent mis-store on the exact axis
    judging-and-evidence.md says a reviewer cannot easily re-check ('what
    must never be fudged is the seniority claim')."""
    disagreements = []
    accepted = 0
    for title, tier, company_type in _CORPUS:
        v = _triage_stored_title(title, company_type=company_type, company_reason="clean")
        if v.decision == AUTO_ACCEPT:
            accepted += 1
            if v.tier != tier and title not in _KNOWN_TIER_DISAGREEMENTS:
                disagreements.append((title, tier, v.tier))

    assert disagreements == [], (
        f"{len(disagreements)} of {accepted} AUTO_ACCEPTed contacts disagree with their stored tier "
        f"(beyond the {len(_KNOWN_TIER_DISAGREEMENTS)} named, already-investigated exception(s)): "
        + "; ".join(f"{t!r} stored={s} triage={g}" for t, s, g in disagreements[:10])
    )


@pytest.mark.skipif(_CORPUS is None, reason="DB not reachable in this checkout")
def test_auto_accept_rate_is_nonzero():
    """Guards the failure mode where preconditions tighten over time until
    nothing ever auto-accepts and every other invariant in this file passes
    trivially. Not a target rate, just a liveness check."""
    accepted = sum(
        1 for title, _, company_type in _CORPUS
        if _triage_stored_title(title, company_type=company_type, company_reason="clean").decision == AUTO_ACCEPT
    )
    assert accepted > 0, "triage never auto-accepted a single real stored contact — layer is inert"
