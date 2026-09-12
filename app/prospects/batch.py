"""
Mechanical support for the `/find-prospects` skill: thesis selection, dedup,
budget enforcement, persistence, evidence file, batch report.

**Nothing in this module decides whether a company is a good prospect.** That
judgement — does this company have a real data function, is the India signal
genuine, which ranking band does it land in — is the agent's, made against
live search results and recorded here verbatim. This module owns only the
parts that erode silently under pace: the search budget, the dedup key, and
the requirement that a qualified row carry its evidence.

That split is deliberate and copied from `app/contacts/agentic_batch.py`,
whose docstring makes the same promise ("Nothing in this module accepts or
rejects a candidate"). The lesson it encodes: find-contacts' per-company
search spend eroded from 3.1 to 2.0 calls while the cap lived only in the
skill's prose, and only stopped eroding when the cap moved into code.
"""
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.companies.naming import is_placeholder, normalize
from app.config.prospect_theses import (
    THESES,
    TARGET_QUALIFIED_PER_THESIS,
    Thesis,
    get_thesis,
)
from app.models.orm import Company, Prospect, utcnow

# The time cap. Free search costs no money, so the scarce resource is the
# session itself: a 200-candidate pool with uncapped qualification is a run
# that never finishes. Three searches is enough to answer a binary "does
# anyone in scope work here?" — it is not enough to enumerate a team, and it
# is not meant to be (that is find-contacts' job, on the billed transport).
#
# The single WebFetch of a careers/about page is deliberately NOT counted:
# it is the cheapest and most informative single action available, and
# charging for it would push the agent toward three vaguer searches instead.
MAX_SEARCHES_PER_CANDIDATE = 3

STATUS_CANDIDATE = "candidate"
STATUS_QUALIFIED = "qualified"
STATUS_UNQUALIFIED = "unqualified"
STATUS_DUPLICATE = "duplicate"

REASON_NO_FUNCTION = "no_function"
REASON_NO_INDIA_SIGNAL = "no_india_signal"
REASON_CAP_REACHED = "cap_reached"
REASON_STAFFING = "staffing"
REASON_ALREADY_IN_COMPANIES = "already_in_companies"

VALID_REASONS = frozenset({
    REASON_NO_FUNCTION, REASON_NO_INDIA_SIGNAL, REASON_CAP_REACHED,
    REASON_STAFFING, REASON_ALREADY_IN_COMPANIES,
})

EVIDENCE_DIR = Path(__file__).resolve().parents[2] / "prospect_batches"


class BudgetExceeded(Exception):
    """Raised when a candidate has already spent MAX_SEARCHES_PER_CANDIDATE.
    The caller is expected to disqualify it `cap_reached` and move on, not to
    keep digging."""


class MissingEvidence(Exception):
    """Raised when a caller tries to qualify a prospect without the verbatim
    evidence that justifies it. A qualified row whose evidence can't be
    checked against the live web defeats the entire trial gate."""


# ---------------------------------------------------------------- thesis ---

def qualified_count(session: Session, thesis_slug: str) -> int:
    return session.execute(
        select(func.count(Prospect.id)).where(
            Prospect.thesis == thesis_slug,
            Prospect.status == STATUS_QUALIFIED,
        )
    ).scalar_one()


def next_thesis(session: Session) -> Thesis | None:
    """The highest-priority thesis that hasn't hit its qualified target.
    None when every thesis is exhausted — which is a real answer, not an
    error: it means the checked-in list needs extending, and that is a
    decision for the user rather than something the agent should improvise."""
    for thesis in THESES:
        if qualified_count(session, thesis.slug) < TARGET_QUALIFIED_PER_THESIS:
            return thesis
    return None


def thesis_progress(session: Session) -> list[tuple[Thesis, int]]:
    return [(t, qualified_count(session, t.slug)) for t in THESES]


# ----------------------------------------------------------------- dedup ---

@dataclass
class DedupResult:
    """What a harvested batch of names split into. `new` is the only bucket
    worth spending qualification budget on."""
    new: list[str] = field(default_factory=list)
    already_in_companies: list[str] = field(default_factory=list)
    already_a_prospect: list[str] = field(default_factory=list)
    placeholder: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{len(self.new)} new, "
            f"{len(self.already_in_companies)} already in companies, "
            f"{len(self.already_a_prospect)} already a prospect, "
            f"{len(self.placeholder)} placeholder/unusable"
        )


def dedup_candidates(session: Session, names: list[str]) -> DedupResult:
    """Split harvested names against `companies` and existing `prospects`,
    on the shared normalized key (app.companies.naming.normalize).

    Both sides matter. Colliding with `companies` means we already scrape jobs
    from them, so they are not a cold-email prospect. Colliding with
    `prospects` means a previous thesis already researched them — including
    the ones that came back `unqualified`, which is exactly why unqualified
    rows are kept rather than deleted.

    Names that normalize identically WITHIN the input batch collapse to one:
    directory pages routinely list "Razorpay" and "Razorpay Software Pvt Ltd"
    as separate rows.
    """
    result = DedupResult()
    seen_in_batch: set[str] = set()

    company_norms = {
        normalize(name) for (name,) in session.execute(select(Company.name)).all()
    }
    prospect_norms = {
        norm for (norm,) in session.execute(select(Prospect.normalized_name)).all()
    }

    for raw in names:
        name = raw.strip()
        if not name:
            continue
        norm = normalize(name)
        if not norm or is_placeholder(name):
            result.placeholder.append(name)
            continue
        if norm in seen_in_batch:
            continue
        seen_in_batch.add(norm)

        if norm in company_norms:
            result.already_in_companies.append(name)
        elif norm in prospect_norms:
            result.already_a_prospect.append(name)
        else:
            result.new.append(name)
    return result


# ----------------------------------------------------------- persistence ---

def record_candidate(
    session: Session,
    name: str,
    *,
    thesis: str,
    source_query: str | None = None,
    source_url: str | None = None,
) -> Prospect:
    """Create (or return) the `candidate` row for a harvested name.

    Idempotent on `normalized_name`: re-running a thesis over the same
    directory updates provenance and touches `last_checked_at`, but never
    creates a second row and never resets a status the agent already reached.
    """
    get_thesis(thesis)  # fail loudly on a typo'd slug before writing anything
    norm = normalize(name)
    if not norm or is_placeholder(name):
        raise ValueError(f"{name!r} is a placeholder or empty after normalization")

    existing = session.execute(
        select(Prospect).where(Prospect.normalized_name == norm)
    ).scalar_one_or_none()
    if existing is not None:
        existing.last_checked_at = utcnow()
        if source_query and not existing.source_query:
            existing.source_query = source_query
        if source_url and not existing.source_url:
            existing.source_url = source_url
        session.flush()
        return existing

    prospect = Prospect(
        name=name,
        normalized_name=norm,
        thesis=thesis,
        source_query=source_query,
        source_url=source_url,
        status=STATUS_CANDIDATE,
    )
    session.add(prospect)
    session.flush()
    return prospect


def spend_search(session: Session, prospect: Prospect) -> int:
    """Charge one search against a candidate's budget, returning the new
    total. Raises BudgetExceeded when the cap is already spent — the check
    happens BEFORE the increment, so a refused call costs nothing, mirroring
    `bright_data_profiles`' budget check happening before it bills."""
    if prospect.searches_spent >= MAX_SEARCHES_PER_CANDIDATE:
        raise BudgetExceeded(
            f"{prospect.name!r} has spent {prospect.searches_spent}/"
            f"{MAX_SEARCHES_PER_CANDIDATE} searches — disqualify it "
            f"{REASON_CAP_REACHED} rather than digging further"
        )
    prospect.searches_spent += 1
    prospect.last_checked_at = utcnow()
    session.flush()
    return prospect.searches_spent


def spend_ok(prospect: Prospect) -> bool:
    """Whether another search is allowed. Read-only companion to
    spend_search, for callers that want to branch rather than catch."""
    return prospect.searches_spent < MAX_SEARCHES_PER_CANDIDATE


def qualify(
    session: Session,
    prospect: Prospect,
    *,
    rank_band: int,
    function_evidence: str,
    india_signal: str,
    industry: str | None = None,
    hq_location: str | None = None,
    remote_posture: str | None = None,
    canonical_domain: str | None = None,
    website: str | None = None,
    linkedin_company_url: str | None = None,
) -> Prospect:
    """Mark a prospect qualified. Both evidence fields are REQUIRED and must
    be the verbatim text they were read from, not a paraphrase — the trial
    gate is a human re-checking them against the live web, which a summary
    makes impossible."""
    if rank_band not in (1, 2, 3):
        raise ValueError(f"rank_band must be 1, 2 or 3; got {rank_band!r}")
    if not (function_evidence or "").strip():
        raise MissingEvidence(f"{prospect.name!r}: function_evidence is required to qualify")
    if not (india_signal or "").strip():
        raise MissingEvidence(f"{prospect.name!r}: india_signal is required to qualify")

    prospect.status = STATUS_QUALIFIED
    prospect.unqualified_reason = None
    prospect.rank_band = rank_band
    prospect.function_evidence = function_evidence
    prospect.india_signal = india_signal
    prospect.industry = industry
    prospect.hq_location = hq_location
    prospect.remote_posture = remote_posture
    prospect.canonical_domain = canonical_domain
    prospect.website = website
    prospect.linkedin_company_url = linkedin_company_url
    prospect.last_checked_at = utcnow()
    session.flush()
    return prospect


def disqualify(session: Session, prospect: Prospect, reason: str, *, note: str | None = None) -> Prospect:
    """Mark a prospect unqualified. The row is KEPT — free search produces
    real false negatives, so a stored negative is both re-checkable later and
    protection against re-researching the same dead end next thesis."""
    if reason not in VALID_REASONS:
        raise ValueError(f"unknown reason {reason!r}; valid: {sorted(VALID_REASONS)}")
    prospect.status = STATUS_UNQUALIFIED
    prospect.unqualified_reason = reason
    prospect.rank_band = None
    if note:
        prospect.function_evidence = note
    prospect.last_checked_at = utcnow()
    session.flush()
    return prospect


def record_excluded(session: Session, names: list[str], *, thesis: str, reason: str) -> int:
    """Persist names that dedup already ruled out (typically
    `already_in_companies`) as unqualified rows, so the next run of any thesis
    skips them without re-deriving why. Returns how many rows were written."""
    written = 0
    for name in names:
        try:
            prospect = record_candidate(session, name, thesis=thesis)
        except ValueError:
            continue
        if prospect.status == STATUS_CANDIDATE:
            disqualify(session, prospect, reason)
            written += 1
    return written


# -------------------------------------------------------------- evidence ---

def prospects_for_thesis(session: Session, thesis_slug: str) -> list[Prospect]:
    """Every row for a thesis, qualified first and strongest band first —
    the order a reviewer wants to read them in."""
    rows = session.execute(
        select(Prospect).where(Prospect.thesis == thesis_slug)
    ).scalars().all()
    status_rank = {STATUS_QUALIFIED: 0, STATUS_CANDIDATE: 1, STATUS_UNQUALIFIED: 2, STATUS_DUPLICATE: 3}
    return sorted(rows, key=lambda p: (status_rank.get(p.status, 9), p.rank_band or 9, p.name.lower()))


def write_evidence(session: Session, thesis_slug: str, *, batch_name: str | None = None) -> Path:
    """Write the reviewable record of a run to prospect_batches/.

    Every qualified prospect renders with the verbatim evidence that justified
    it and the query that found it, so the trial-gate reviewer can re-check
    each claim without re-running anything.
    """
    thesis = get_thesis(thesis_slug)
    rows = prospects_for_thesis(session, thesis_slug)
    stamp = batch_name or datetime.now().strftime("%Y%m%d-%H%M%S")
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE_DIR / f"prospects-{thesis_slug}-{stamp}.md"

    qualified = [p for p in rows if p.status == STATUS_QUALIFIED]
    others = [p for p in rows if p.status != STATUS_QUALIFIED]

    lines: list[str] = [
        f"# Prospect batch — {thesis.label}",
        "",
        f"Thesis `{thesis_slug}` · generated {utcnow().isoformat()} · "
        f"{len(qualified)} qualified of {len(rows)} researched",
        "",
        f"_{thesis.rationale}_",
        "",
        "Review each qualified prospect against the live web: does the company "
        "exist, does it actually have an in-scope data function, is the India "
        "signal real, and is the ranking band right?",
        "",
    ]

    if not qualified:
        lines += ["## Qualified", "", "_None._", ""]
    else:
        lines += ["## Qualified", ""]
        for p in qualified:
            lines += [
                f"### {p.name}  (band {p.rank_band})",
                "",
                f"- **Industry:** {p.industry or '—'}",
                f"- **HQ:** {p.hq_location or '—'}  ·  **Remote posture:** {p.remote_posture or 'unknown'}",
                f"- **Domain:** {('`' + p.canonical_domain + '`') if p.canonical_domain else '—'}"
                f"  ·  **Site:** {p.website or '—'}",
                f"- **LinkedIn:** {p.linkedin_company_url or '—'}",
                f"- **Function evidence:** {p.function_evidence}",
                f"- **India signal:** {p.india_signal}",
                f"- **Found via:** `{p.source_query or '—'}`",
                f"- **Source page:** {p.source_url or '—'}",
                f"- **Searches spent:** {p.searches_spent}",
                "",
            ]

    lines += ["## Not qualified", ""]
    if not others:
        lines += ["_None._", ""]
    else:
        lines += ["| company | status | reason | searches |", "|---|---|---|---|"]
        for p in others:
            lines.append(
                f"| {p.name} | {p.status} | {p.unqualified_reason or '—'} | {p.searches_spent} |"
            )
        lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def batch_report(session: Session, thesis_slug: str) -> str:
    """Terminal summary of a run. Deliberately surfaces the two ways a run can
    look successful while being wrong: a pool that was never really searched
    (candidates left unresearched), and qualification so cheap it suggests the
    gate was waved through rather than applied."""
    thesis = get_thesis(thesis_slug)
    rows = prospects_for_thesis(session, thesis_slug)
    qualified = [p for p in rows if p.status == STATUS_QUALIFIED]
    unqualified = [p for p in rows if p.status == STATUS_UNQUALIFIED]
    unresearched = [p for p in rows if p.status == STATUS_CANDIDATE]

    lines = [
        f"=== {thesis.label}  [{thesis_slug}] ===",
        f"  qualified   : {len(qualified)} / target {TARGET_QUALIFIED_PER_THESIS}",
        f"  unqualified : {len(unqualified)}",
        f"  unresearched: {len(unresearched)}",
    ]

    if qualified:
        bands: dict[int, int] = {}
        for p in qualified:
            bands[p.rank_band or 0] = bands.get(p.rank_band or 0, 0) + 1
        band_str = ", ".join(f"band {b}: {n}" for b, n in sorted(bands.items()))
        lines.append(f"  bands       : {band_str}")

    if unqualified:
        reasons: dict[str, int] = {}
        for p in unqualified:
            key = p.unqualified_reason or "—"
            reasons[key] = reasons.get(key, 0) + 1
        reason_str = ", ".join(f"{k}: {v}" for k, v in sorted(reasons.items(), key=lambda kv: -kv[1]))
        lines.append(f"  reasons     : {reason_str}")

    # Rows excluded by dedup were never candidates for Stage B, so they spend
    # zero searches BY DESIGN. Counting them here drags the effort average
    # down and fires the under-search warning on a run that actually searched
    # properly — observed on the first live run (8 of 41 rows, avg 0.98 vs a
    # true 1.2). The average must describe only what was actually researched.
    researched = [
        p for p in qualified + unqualified
        if p.unqualified_reason != REASON_ALREADY_IN_COMPANIES
    ]
    if researched:
        avg = sum(p.searches_spent for p in researched) / len(researched)
        lines.append(f"  avg searches: {avg:.1f} / {MAX_SEARCHES_PER_CANDIDATE} per researched candidate")
        if avg < 1.0:
            lines.append(
                "  ⚠️  under 1 search per candidate — the function gate cannot "
                "have been applied to most of these. Re-check before trusting."
            )

    missing_evidence = [p for p in qualified if not (p.function_evidence or "").strip()]
    if missing_evidence:
        lines.append(f"  ⚠️  {len(missing_evidence)} qualified rows have no function evidence (bug)")

    if unresearched:
        lines.append(
            f"  ⚠️  {len(unresearched)} candidates were harvested but never "
            f"researched — the pool is not exhausted, the run just stopped."
        )

    unreviewed = [p for p in qualified if not p.reviewed]
    if unreviewed:
        lines.append(f"  {len(unreviewed)} qualified prospects await human review (trial gate)")

    return "\n".join(lines)
