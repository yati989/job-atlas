"""
Mechanical support for the `/find-contacts` skill (issue #73): batch
selection, per-company context, email derivation, persistence, the evidence
file, and the batch report.

Everything requiring *judgement* — is this a real person, are they at this
company now, which tier do they occupy — stays in the skill, done by the
agent (#68). Nothing in this module accepts or rejects a candidate. Its job
is to hand the agent a batch, then take back what the agent decided and make
it durable.

Reuses unchanged, per #73's acceptance criteria:
  - `email_resolution.derive_email` / `_get_mx_host` (email + MX guard)
  - `upsert.upsert_contact` (contact dedup on company + profile URL)
  - `company_queue.get_company_queue` (#72 ordering)
"""
import json
import re
from dataclasses import replace
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.contacts import ladder as ladder_cfg
from app.contacts.company_profile import (
    ContactCompanyProfileMissing,
    InvalidContactCompanyProfile,
    stored_search_groups,
)
from app.contacts.company_queue import get_company_queue
from app.contacts.email_resolution import candidate_addresses, derive_email
from app.contacts.people_search import ContactCandidate
from app.contacts.upsert import upsert_contact
from app.models.orm import (
    Company,
    Contact,
    LinkedInProfileLink,
    ProfileDiscoveryAuthorization,
    PublicSelectionScopeItem,
)

BATCH_SIZE = 10

STATUS_DONE = "done"
STATUS_PARTIAL = "partial"
STATUS_NO_CATEGORY_MATCH = "no_category_match"

EVIDENCE_DIR = Path(__file__).resolve().parents[2] / "contact_batches"
_EMAIL_TEXT_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)


def _redact_email_text(value: str | None) -> str | None:
    return _EMAIL_TEXT_RE.sub("[email removed]", value) if value else value


def _canonical_linkedin_profile_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    host = parsed.netloc.lower().split(":", 1)[0]
    if host == "linkedin.com" or host.endswith(".linkedin.com"):
        pieces = [piece for piece in parsed.path.split("/") if piece]
        if len(pieces) >= 2 and pieces[0].lower() == "in":
            return f"https://www.linkedin.com/in/{pieces[1]}"
    raise ValueError("profile link must be a canonicalizable LinkedIn /in/ URL")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class CompanyContext:
    """Everything the agent needs to work one company, resolved up front."""
    company_id: int
    name: str
    company_type: str
    canonical_domain: str | None
    industry: str | None
    hq_location: str | None
    search_groups: list[str]
    ladder: dict[str, dict[str, list[str]]]
    quotas: dict[str, int]


@dataclass
class JudgedContact:
    """One contact the agent judged. `tier_rationale` and `judged_text` are
    the audit trail — they are written to the evidence file (never to the DB,
    per #68's out-of-scope list) so each decision can be checked against the
    live profile.

    `name_collision_risk` is set by the agent when the judging search turned
    up more than one profile with this contact's exact full name at this
    company. It IS persisted (unlike the rationale/judged-text audit trail)
    because it changes how the stored contact should be used afterwards: a
    wrong pattern-guessed email bounces and self-corrects, but an email that
    collides with a same-named stranger delivers successfully to the wrong
    person and never surfaces as an error. `collision_note` records what was
    seen (e.g. "3 other <name> profiles matched <company>")."""
    full_name: str
    title: str
    profile_url: str
    tier: str
    search_group: str
    tier_rationale: str
    judged_text: str
    query: str
    email: str | None = None
    email_confidence: str | None = None
    name_collision_risk: bool = False
    collision_note: str | None = None


def get_batch(session: Session, limit: int = BATCH_SIZE, pinned: list[str] | None = None) -> list[CompanyContext]:
    """The next `limit` companies to work, already resolved into per-company
    ladders and quotas. Ordering is #72's; this only adds contact-finding
    context on top."""
    companies = get_company_queue(session, limit=limit, pinned=pinned)
    return [_context_for(session, company) for company in companies]


def context_for_company_id(session: Session, company_id: int) -> CompanyContext:
    """Public single-company lookup, for callers that already know which
    company they're working (the brightdata_query CLI, ad-hoc scripts) rather
    than pulling the next N off the queue. Thin wrapper over `_context_for` so
    those callers don't reach into a private function."""
    company = session.get(Company, company_id)
    if company is None:
        raise ValueError(f"no company with id={company_id}")
    return _context_for(session, company)


def contexts_for_approved_companies(session: Session, approved_scope) -> list[CompanyContext]:
    """Exact manifest adapter; never falls through to the generic queue."""
    from app.decision_runs.contact_funnel import freeze_contact_enrichment_manifest
    from app.decision_runs.manifests import NO_CATEGORY_MATCH, NO_CONTACT_SELECTION
    from app.models.orm import DecisionRun
    # Legacy approval work remains readable but is intentionally never given
    # invented telemetry.  Current runs freeze on this production seam.
    run = session.get(DecisionRun, approved_scope.run_id)
    if run is not None and run.telemetry_version is not None:
        manifest = freeze_contact_enrichment_manifest(session, approved_scope.run_id)
        frozen_groups = {}
        for row in manifest:
            frozen_groups.setdefault(row.company_id, []).append(row.search_group)
    else:
        from app.decision_runs.manifests import approved_contact_search_groups
        frozen_groups = {
            company_id: approved_contact_search_groups(
                session, approved_scope.run_id, company_id,
            ) for company_id in approved_scope.company_ids
        }
    contexts = []
    for company_id in approved_scope.company_ids:
        groups = [group for group in frozen_groups.get(company_id, [])
                  if group not in (NO_CONTACT_SELECTION, NO_CATEGORY_MATCH)]
        if not groups:
            continue
        context = context_for_company_id(session, company_id)
        contexts.append(replace(
            context, search_groups=groups,
            ladder={group: ladder_cfg.ladder_for(context.company_type, group) for group in groups},
        ))
    return contexts


def contexts_for_public_scope(
    session: Session, scope,
) -> list[CompanyContext]:
    """Build existing contact-search contexts for a public links-only scope."""
    company_ids = list(dict.fromkeys(session.scalars(
        select(PublicSelectionScopeItem.company_id)
        .where(PublicSelectionScopeItem.scope_id == scope.id)
        .order_by(PublicSelectionScopeItem.company_id)
    )))
    contexts: list[CompanyContext] = []
    missing: list[str] = []
    for company_id in company_ids:
        company = session.get(Company, company_id)
        try:
            context = context_for_company_id(session, company_id)
        except (ContactCompanyProfileMissing, InvalidContactCompanyProfile) as exc:
            missing.append(f"{company_id}:{company.name if company else 'unknown'} ({exc})")
            continue
        if not context.search_groups:
            missing.append(
                f"{company_id}:{context.name} (no supported function evidence)"
            )
            continue
        contexts.append(context)
    if missing:
        raise ValueError(
            f"profile-search prerequisites missing for {len(missing)}/{len(company_ids)} "
            f"selected companies: {'; '.join(missing)}"
        )
    return contexts


def _context_for(session: Session, company: Company) -> CompanyContext:
    groups = stored_search_groups(company)
    company_type = company.company_type or "employer"
    return CompanyContext(
        company_id=company.id,
        name=company.name,
        company_type=company_type,
        canonical_domain=company.canonical_domain,
        industry=company.industry,
        hq_location=company.hq_location,
        search_groups=groups,
        ladder={g: ladder_cfg.ladder_for(company_type, g) for g in groups},
        quotas=ladder_cfg.quotas_for(company_type),
    )


def attach_email(contact: JudgedContact, domain: str | None) -> JudgedContact:
    """Derive the default-guess address for one judged contact. Deliberately
    never drops: per #68 deliverability is a stored *attribute*, not a gate,
    reversing #20 — a verified-real hiring manager behind a probe-blocking
    mail server is still a lead, reachable on the platform even if the
    address guess fails. `derive_email` returns None for the underivable
    case and we simply store that as an empty email.

    Stores only `candidates[0]`; the other 3 shapes are computed at
    evidence-file render time (see `write_evidence`) rather than stored,
    since they're cheap to recompute from `full_name`/domain on demand."""
    if not domain:
        return contact
    derived = derive_email(contact.full_name, domain)
    if derived is not None:
        contact.email = derived.address
        contact.email_confidence = derived.status
    return contact


def record_profile_links(
    session: Session,
    *,
    authorization_id: int,
    context: CompanyContext,
    contacts: Iterable[JudgedContact],
) -> list[LinkedInProfileLink]:
    """Persist the links-only exit of the existing judged-contact workflow.

    Search planning, provider calls, relevance judgement, tiering, coverage,
    and retry accounting happen through the normal ``find-contacts`` flow.
    This seam deliberately stops before ``attach_email``/``record_company``.
    """
    authorization = session.get(ProfileDiscoveryAuthorization, authorization_id)
    if authorization is None:
        raise ValueError(f"unknown profile discovery authorization {authorization_id}")
    company_in_scope = session.scalar(
        select(PublicSelectionScopeItem.id).where(
            PublicSelectionScopeItem.scope_id == authorization.scope_id,
            PublicSelectionScopeItem.company_id == context.company_id,
        ).limit(1)
    )
    if company_in_scope is None:
        raise ValueError("company is outside the approved profile-link scope")

    stored: list[LinkedInProfileLink] = []
    for contact in contacts:
        if contact.search_group not in context.search_groups:
            raise ValueError("judged contact uses a search group outside company evidence")
        profile_url = _canonical_linkedin_profile_url(contact.profile_url)
        existing = session.scalar(select(LinkedInProfileLink).join(
            ProfileDiscoveryAuthorization,
            LinkedInProfileLink.authorization_id == ProfileDiscoveryAuthorization.id,
        ).where(
            ProfileDiscoveryAuthorization.run_id == authorization.run_id,
            LinkedInProfileLink.company_id == context.company_id,
            LinkedInProfileLink.linkedin_url == profile_url,
        ))
        if existing is not None:
            stored.append(existing)
            continue
        row = LinkedInProfileLink(
            authorization_id=authorization_id,
            company_id=context.company_id,
            linkedin_url=profile_url,
            full_name=_redact_email_text(contact.full_name),
            headline=_redact_email_text(contact.title),
            search_group=contact.search_group,
            evidence={
                "tier": contact.tier,
                "search_group": contact.search_group,
                "tier_rationale": _redact_email_text(contact.tier_rationale),
            },
        )
        session.add(row)
        session.flush()
        stored.append(row)
    return stored


def tier_quota_filled(
    context: CompanyContext,
    contacts: Iterable[JudgedContact],
    tier: str,
    search_group: str,
) -> bool:
    """Is `tier`'s quota already met for `search_group`?

    Extracted from `_status_for` so the SAME rule answers two questions that
    must never disagree: "is this company done?" and "is this tier's remaining
    query still worth billing?" (`search_plan.coverage_for_company`). If these
    two drifted apart, a tier could be simultaneously full enough to stop
    searching and reported as under-searched — the exact contradiction the
    2026-08-04 harvest rules would otherwise create.

    Counts only contacts with an email, matching the quota rule in
    `_status_for`: a stored contact with no derivable address is still a real
    lead, it just cannot advance a tier."""
    quota = context.quotas.get(tier)
    if not quota:
        return False
    if tier in ladder_cfg.PER_COMPANY_TIERS:
        filled = sum(1 for c in contacts if c.tier == tier and c.email)
    else:
        filled = sum(
            1 for c in contacts
            if c.tier == tier and c.search_group == search_group and c.email
        )
    return filled >= quota


def _status_for(contacts: list[JudgedContact], context: CompanyContext) -> str:
    """`done` only when every applicable tier quota is filled; `partial`
    otherwise — the agent only stops short once its search budget is spent,
    so "under quota after searching" and "budget exhausted while short" are
    the same state. Partial is terminal by default (#68): a small company
    with one data scientist should not be revisited forever chasing a quota
    it can never fill.

    Quota counts only contacts that HAVE an email (#68 user story 9), which
    is why `c.email` is required here — a stored contact with no derivable
    address still lands in the DB, it just doesn't count toward the quota.

    Tiers in `ladder.PER_COMPANY_TIERS` (ic, talent_acquisition) are counted
    once across the whole company; the rest are counted per search group.
    That mirrors how search budget is actually spent — see the comment on
    PER_COMPANY_TIERS."""
    if not context.search_groups:
        return STATUS_NO_CATEGORY_MATCH

    for tier, quota in context.quotas.items():
        if tier in ladder_cfg.CONDITIONAL_TIERS:
            continue
        applicable_groups = [
            g for g in context.search_groups if tier in context.ladder.get(g, {})
        ]
        if not applicable_groups:
            continue

        if tier in ladder_cfg.PER_COMPANY_TIERS:
            if not tier_quota_filled(context, contacts, tier, applicable_groups[0]):
                return STATUS_PARTIAL
            continue

        for group in applicable_groups:
            if not tier_quota_filled(context, contacts, tier, group):
                return STATUS_PARTIAL
    return STATUS_DONE


class UnderSearched(Exception):
    """Raised by `record_company(..., strict=True)` when the company is about
    to be written `partial` but mandatory queries from `search_plan` were
    never issued. Confirmed live 2026-08-03: a `partial` reached this way is
    not evidence of scarcity, it's evidence of not having looked — Binance
    (1 call across 4 tiers), Highbrow Technologies (1 call), and Vinsari (no
    seed-2 top-up) all read identically to genuine scarcity in the batch
    report until the coverage gap was checked by hand. `strict=True` makes
    that check happen every time, not only when someone remembers to run it."""


def record_company(
    session: Session,
    context: CompanyContext,
    contacts: list[JudgedContact],
    *,
    strict: bool = False,
    replace_existing: bool = False,
    run_id: str | None = None,
    function_results: Iterable | None = None,
) -> str:
    """Upsert this company's judged contacts and set its status. Returns the
    status written. Re-running a company updates rather than duplicates —
    `upsert_contact` dedups on (company_id, profile URL).

    `strict=True` raises `UnderSearched` instead of writing `partial` when
    `search_plan.coverage_for_company` reports outstanding mandatory queries —
    i.e. it refuses to let an under-searched company masquerade as a
    genuinely-scarce one. `replace_existing=True` makes the submitted review
    authoritative: contacts omitted from it are deleted before accepted rows
    are upserted. Callers that already know they're intentionally
    stopping early (a spend cap hit mid-batch, a company the user asked to
    skip) should pass `strict=False` and record that reason themselves; this
    is not a universal gate, it's a guard against the specific silent failure
    mode measured in the 2026-08-03 pilot."""
    if (run_id is None) != (function_results is None):
        raise ValueError("approved-run persistence requires both run_id and exact function_results")
    exact_function_results = tuple(function_results or ())
    if any(result.company_id != context.company_id for result in exact_function_results):
        raise ValueError("every function result must belong to the same company as its context")
    if run_id is not None:
        from app.decision_runs.contact_funnel import frozen_real_contact_functions
        from app.decision_runs.manifests import contact_function
        try:
            reported_functions = tuple(contact_function(result.search_group)
                                       for result in exact_function_results)
            judged_functions = tuple(contact_function(contact.search_group)
                                     for contact in contacts)
        except ValueError as exc:
            raise ValueError(
                "approved contact persistence accepts only real contact-search functions"
            ) from exc
        if len(set(reported_functions)) != len(reported_functions):
            raise ValueError("each exact function may have only one function result")
        frozen_functions = frozen_real_contact_functions(
            session, run_id, context.company_id,
        )
        if not set(reported_functions).issubset(frozen_functions):
            raise ValueError("every function result must match a frozen real-function obligation")
        if not set(judged_functions).issubset(set(reported_functions)):
            raise ValueError(
                "every judged contact function requires its own exact function result"
            )
        if any(result.new_contact_ids for result in exact_function_results):
            raise ValueError("record_company assigns new contact IDs from matching stored groups")
    status = _status_for(contacts, context)
    if strict and status == STATUS_PARTIAL:
        from app.contacts.search_plan import coverage_for_company, format_plan
        outstanding = coverage_for_company(context, contacts)
        if outstanding:
            raise UnderSearched(
                f"{context.name} would be recorded `partial`, but "
                f"{len(outstanding)} mandatory quer{'y' if len(outstanding) == 1 else 'ies'} "
                f"were never issued:\n{format_plan(outstanding)}\n"
                "Issue these (see `python -m app.contacts.brightdata_query search ...`) "
                "before trusting this company's status, or call record_company(strict=False) "
                "if the batch is intentionally stopping early."
            )

    company = session.get(Company, context.company_id)
    if replace_existing:
        accepted_urls = [contact.profile_url for contact in contacts]
        stale = delete(Contact).where(Contact.company_id == context.company_id)
        if accepted_urls:
            stale = stale.where(Contact.linkedin_url.not_in(accepted_urls))
        session.execute(stale)

    stored_by_group: dict[str, list[tuple[int, JudgedContact]]] = {}
    for contact in contacts:
        stored = upsert_contact(session, context.company_id, ContactCandidate(
            full_name=contact.full_name,
            title=contact.title,
            linkedin_url=contact.profile_url,
            seniority_tier=contact.tier,
            email_guess=contact.email,
            email_verification_status=contact.email_confidence,
            name_collision_risk=contact.name_collision_risk,
        ))
        session.flush()
        stored_by_group.setdefault(contact.search_group, []).append((stored.id, contact))

    company.contact_enrichment_status = status
    company.contact_enriched_at = utcnow()
    if run_id is not None:
        from app.decision_runs.contact_funnel import report_contact_enrichment
        exact_results = []
        for result in exact_function_results:
            stored = stored_by_group.get(result.search_group, [])
            coverage = dict(result.coverage or {})
            coverage["new_contact_evidence"] = [{
                "contact_id": contact_id,
                "search_group": result.search_group,
                "profile_url": contact.profile_url,
                "judged_text": contact.judged_text,
                "tier_rationale": contact.tier_rationale,
                "query": contact.query,
            } for contact_id, contact in stored]
            exact_results.append(replace(
                result,
                new_contact_ids=tuple(contact_id for contact_id, _ in stored),
                coverage=coverage,
            ))
        report_contact_enrichment(session, run_id, exact_results)
    return status


def write_evidence(batch: list[tuple[CompanyContext, list[JudgedContact], str]], batch_name: str | None = None) -> Path:
    """Write the per-batch reviewable evidence file (#68 user stories 32-33):
    every contact with its profile link, the text judged, the tier and why,
    the search that surfaced it, and the derived email. Lives on disk, not in
    the DB, and outlives the session so the user can work through it beside a
    browser."""
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    name = batch_name or utcnow().strftime("%Y%m%d-%H%M%S")
    path = EVIDENCE_DIR / f"batch-{name}.md"

    lines = [
        f"# Contact batch {name}",
        "",
        f"Generated {utcnow().isoformat()} · {len(batch)} companies",
        "",
        "Review each contact against the live profile: is this the right person "
        "at the right company, and is the tier right?",
        "",
    ]
    for context, contacts, status in batch:
        lines += [
            f"## {context.name}  ({context.company_type}, status: {status})",
            "",
            f"- Domain: `{context.canonical_domain or '—'}`",
            f"- Search groups: {', '.join(context.search_groups) or '—'}",
            "",
        ]
        if not contacts:
            lines += ["_No contacts found._", ""]
            continue
        for c in contacts:
            lines += [
                f"### {c.full_name} — {c.title}",
                f"- **Profile:** {c.profile_url}",
                f"- **Tier:** `{c.tier}` ({c.search_group}) — {c.tier_rationale}",
                f"- **Email (stored guess):** `{c.email or '—'}` ({c.email_confidence or 'none'})",
                f"- **Found via:** `{c.query}`",
                f"- **Text judged:** {c.judged_text}",
            ]
            if context.canonical_domain:
                alternates = [
                    a for a in candidate_addresses(c.full_name, context.canonical_domain)
                    if a != c.email
                ]
                if alternates:
                    lines.append(f"- **Other candidate shapes:** {', '.join(f'`{a}`' for a in alternates)}")
            if c.name_collision_risk:
                lines.append(
                    f"- ⚠️ **Name-collision risk** — {c.collision_note or 'multiple same-name profiles matched this company'}. "
                    "The email above is a pattern guess against a shared name; it can deliver to a different person of the "
                    "same name and won't bounce if it does. **Contact via the LinkedIn profile above, not this email.**"
                )
            lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def calls_spent_since(started_at: datetime) -> int:
    """How many BILLED Bright Data calls were made since `started_at`, read
    back from the transport's call log.

    The per-tier cap lives in the find-contacts skill as prose the agent
    follows; this is what makes it auditable rather than aspirational. Record
    `utcnow()` before working a batch and pass it here afterwards to see what
    the batch actually cost, and to reconcile against the Bright Data
    dashboard."""
    from app.contacts.search_source import BRIGHT_DATA_CALL_LOG

    if not BRIGHT_DATA_CALL_LOG.exists():
        return 0
    count = 0
    with BRIGHT_DATA_CALL_LOG.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                if datetime.fromisoformat(entry["ts"]) >= started_at:
                    count += 1
            except (ValueError, KeyError):
                continue
    return count


def batch_report(
    batch: list[tuple[CompanyContext, list[JudgedContact], str]],
    started_at: datetime | None = None,
) -> str:
    """Per-tier quota fill rates and the email-confidence mix (#68 user story
    35), so a company whose contacts are all unconfirmed guesses is visible
    rather than counted as a success."""
    tier_found: dict[str, int] = {}
    tier_quota: dict[str, int] = {}
    confidence: dict[str, int] = {}
    no_email = 0
    collision_risk = 0

    for context, contacts, _status in batch:
        for tier, quota in context.quotas.items():
            if tier in ladder_cfg.CONDITIONAL_TIERS:
                continue
            applicable_groups = [
                g for g in context.search_groups if tier in context.ladder.get(g, {})
            ]
            if not applicable_groups:
                continue
            # Per-company tiers count their quota once no matter how many
            # search groups matched; per-group tiers count it per group.
            multiplier = 1 if tier in ladder_cfg.PER_COMPANY_TIERS else len(applicable_groups)
            tier_quota[tier] = tier_quota.get(tier, 0) + quota * multiplier
        for c in contacts:
            if c.email:
                tier_found[c.tier] = tier_found.get(c.tier, 0) + 1
                confidence[c.email_confidence or "unknown"] = (
                    confidence.get(c.email_confidence or "unknown", 0) + 1
                )
            else:
                no_email += 1
            if c.name_collision_risk:
                collision_risk += 1

    lines = ["Per-tier quota fill:"]
    for tier in ladder_cfg.QUOTAS:
        quota = tier_quota.get(tier, 0)
        found = tier_found.get(tier, 0)
        if quota == 0 and found == 0:
            continue
        pct = f"{100.0 * found / quota:.0f}%" if quota else "n/a"
        lines.append(f"  {tier:20s} {found:3d} / {quota:3d}  {pct}")

    lines.append("Email confidence mix:")
    for status, count in sorted(confidence.items(), key=lambda kv: -kv[1]):
        lines.append(f"  {status:20s} {count:3d}")
    lines.append(f"  {'(no email)':20s} {no_email:3d}")
    if collision_risk:
        lines.append(f"⚠️  {collision_risk} contact(s) flagged name-collision risk — see evidence file, prefer LinkedIn outreach for these")

    statuses: dict[str, int] = {}
    for _c, _k, status in batch:
        statuses[status] = statuses.get(status, 0) + 1
    lines.append("Company status: " + ", ".join(f"{k}={v}" for k, v in sorted(statuses.items())))

    # Under-spend detector (2026-08-03): a company can be `partial` because it
    # was genuinely searched and came up short, or because a mandatory query
    # was simply never issued — the batch report used to make those look
    # identical. This section makes the second case loud. Checked for every
    # `partial` company regardless of `started_at`, since coverage reads the
    # ledger directly rather than a time window.
    undercovered: list[tuple[str, int]] = []
    for context, _contacts, status in batch:
        if status != STATUS_PARTIAL:
            continue
        from app.contacts.search_plan import coverage_for_company
        outstanding = coverage_for_company(context, _contacts)
        if outstanding:
            undercovered.append((context.name, len(outstanding)))
    if undercovered:
        lines.append(
            f"⚠️  {len(undercovered)} `partial` compan{'y' if len(undercovered) == 1 else 'ies'} "
            "have MANDATORY QUERIES NEVER ISSUED — this partial status is not yet "
            "trustworthy, re-search before accepting it as scarcity:"
        )
        for name, n in undercovered:
            lines.append(f"    {name}: {n} outstanding")

    if started_at is not None:
        spent = calls_spent_since(started_at)
        companies = len(batch) or 1
        # Ceiling per company: 4 tiers x 2 calls, doubled for the two
        # leadership tiers when a second search group applies (staffing firms
        # run one tier only). exec_fallback adds at most 1 more, and rarely.
        ceiling = 0
        for context, _contacts, _status in batch:
            tiers = {t for g in context.search_groups for t in context.ladder.get(g, {})}
            tiers -= set(ladder_cfg.CONDITIONAL_TIERS)
            per_group = [t for t in tiers if t not in ladder_cfg.PER_COMPANY_TIERS]
            extra_groups = max(len(context.search_groups) - 1, 0)
            ceiling += (len(tiers) + len(per_group) * extra_groups) * ladder_cfg.MAX_SEARCHES_PER_TIER
        lines.append(
            f"Bright Data calls: {spent} spent / {ceiling} cap "
            f"({spent / companies:.1f} per company)"
        )
        if spent > ceiling:
            lines.append(f"⚠️  OVER CAP by {spent - ceiling} call(s) — check the query log")
    return "\n".join(lines)
