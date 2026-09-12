"""
The per-company search worklist, expanded from the ladder in code rather than
improvised per run.

WHY THIS EXISTS (2026-08-03). The per-tier search budget used to live only as
prose in `.claude/skills/find-contacts/SKILL.md` ("run seed 1, judge, if short
run seed 2, then stop"), executed by the agent's own discipline. Nothing in the
codebase referenced `ladder.TIER_SEARCH_ORDER` or `ladder.MAX_SEARCHES_EXEC_FALLBACK`
at all, and `MAX_SEARCHES_PER_TIER` was referenced exactly once — inside
`agentic_batch.batch_report`, computing a display ceiling *after* the money was
already spent. So the budget could be silently under-spent, and was: across a
50-company pilot, calls/company drifted 3.1 -> 2.0 while head-tier quota fill
collapsed from 120% to 8%. Binance received 1 billed call across 4 tiers;
Vinsari and Highbrow 1 each. Those companies were then written to the DB as
`partial`, which in the batch report is indistinguishable from a company that
was searched properly and genuinely has nobody.

The fix is to invert control. This module expands the complete, mandatory
worklist for a company up front; the agent works *through* that list instead of
deciding what to run. `coverage_for_company` then reads the call ledger back and
names any mandatory query that was never issued, which turns a silent
under-spend into a visible, blocking one (see `agentic_batch.record_company`'s
`strict` flag).

This module deliberately contains NO judgement. It decides which queries are
*required*, never which candidates are acceptable — that stays with the agent
per ADR 0005.
"""
from collections.abc import Sequence
from dataclasses import asdict, dataclass, replace

from app.contacts import ladder as ladder_cfg
from app.contacts.agentic_batch import CompanyContext

# The query shape is fixed here rather than in the skill prose so every issued
# call is reproducible from its plan entry. Unquoted, one title, regional
# subdomain — see references/search-tactics.md for the measured reasoning.
QUERY_TEMPLATE = "{seed} at {company} site:in.linkedin.com/in"


def _query_for_seed(seed: str, company: str) -> str:
    """Build one query from a seed title or a small recruiter title family.

    Product and leadership seeds are exactly one title. The only multi-title
    seeds are the two recruiter families in the public product-manager ladder:
    related hiring titles share one query, but each retains its own complete
    ``<title> at <company>`` clause so company association is not ambiguous.
    """
    alternatives = seed.split(" OR ")
    if len(alternatives) == 1:
        return QUERY_TEMPLATE.format(seed=seed, company=company)
    clauses = " OR ".join(
        f"{title} at {company}" for title in alternatives
    )
    return f"site:in.linkedin.com/in ({clauses})"


@dataclass(frozen=True)
class PlannedQuery:
    """One billed call the plan says may be made.

    `conditional` is the load-bearing field: a False entry is *mandatory* (it
    must be issued before the company can be judged complete), while a True
    entry is a top-up that is only issued when the tier is still short after
    its mandatory query. Coverage checking counts only mandatory entries, so a
    tier that filled on its first call is never reported as under-searched."""
    company_id: int
    company_name: str
    search_group: str
    tier: str
    attempt: int
    seed_title: str
    query: str
    conditional: bool

    @property
    def key(self) -> tuple[int, str, str]:
        """The ledger key this query bills against."""
        return (self.company_id, self.search_group, self.tier)

    def as_dict(self) -> dict[str, object]:
        """Stable JSON-ready snapshot for reviewed public authorizations."""
        return asdict(self)

    def with_query(self, query: str) -> "PlannedQuery":
        return replace(self, query=query)


def _max_attempts(tier: str) -> int:
    """exec_fallback gets one call, not two — a small company has no title
    variance for a second query to catch (ladder.MAX_SEARCHES_EXEC_FALLBACK)."""
    if tier == ladder_cfg.EXEC_FALLBACK:
        return ladder_cfg.MAX_SEARCHES_EXEC_FALLBACK
    return ladder_cfg.MAX_SEARCHES_PER_TIER


def plan_for_company(context: CompanyContext) -> list[PlannedQuery]:
    """Every query the ladder authorises for this company, in issue order.

    The first pass follows `TIER_SEARCH_ORDER` (head -> hiring_manager -> ic
    -> talent_acquisition), with one mandatory query per level. Only then do
    alternate-title top-ups appear, in the same order. An approved cap can
    therefore cover every level instead of being consumed by variants for the
    first one.

    Tiers in `PER_COMPANY_TIERS` (ic, talent_acquisition) are planned ONCE for
    the company even when two search groups matched — mirroring how their quota
    is counted, and why a dual-group company costs 12 calls rather than 16.

    `exec_fallback` is excluded entirely: it is conditional on both head and
    hiring_manager coming back empty AND the company being small enough for a
    founder to be reachable, and that second condition is a judgement call the
    agent makes (see references/search-tactics.md). Planning it unconditionally
    would fire it at 600,000-person enterprises, which #75 specifically caught.
    """
    planned: list[PlannedQuery] = []
    seen_per_company: set[tuple[str, int]] = set()

    for tier in ladder_cfg.TIER_SEARCH_ORDER:
        for group in context.search_groups:
            seeds = context.ladder.get(group, {}).get(tier)
            if not seeds:
                continue

            per_company = tier in ladder_cfg.PER_COMPANY_TIERS
            for attempt in range(1, _max_attempts(tier) + 1):
                if attempt > len(seeds):
                    break
                # ic/talent_acquisition are searched once for the whole company,
                # not once per matched search group.
                if per_company:
                    if (tier, attempt) in seen_per_company:
                        continue
                    seen_per_company.add((tier, attempt))

                seed = seeds[attempt - 1]
                planned.append(PlannedQuery(
                    company_id=context.company_id,
                    company_name=context.name,
                    search_group=group,
                    tier=tier,
                    attempt=attempt,
                    seed_title=seed,
                    query=_query_for_seed(seed, context.name),
                    conditional=attempt > 1,
                ))

    # Spend the initial query for every level before any alternate title. A
    # tight provider cap then still covers department head, manager/lead,
    # senior/product IC, and recruiting instead of exhausting its budget on
    # title variants for the first level.
    planned.sort(key=lambda p: (
        p.conditional,
        ladder_cfg.TIER_SEARCH_ORDER.index(p.tier),
        p.attempt,
        p.search_group,
    ))
    return planned


def mandatory_queries(context: CompanyContext) -> list[PlannedQuery]:
    """The subset that must be issued before a company's status can be trusted."""
    return [p for p in plan_for_company(context) if not p.conditional]


def coverage_for_company(
    context: CompanyContext,
    contacts: Sequence[object] | None = None,
) -> list[PlannedQuery]:
    """Mandatory queries that were never issued AND are still worth issuing.

    Empty list == the company was searched to plan. A non-empty list is the
    under-spend signal: whatever `partial` status the company carries was
    reached without actually looking, and re-running those queries is likely to
    change it. Reads the billed-call ledger, so it survives across sessions and
    cannot be fooled by an agent's own account of what it ran.

    `contacts` (2026-08-04) makes the check quota-aware, which the harvest
    rules require. Since a query aimed at one tier routinely returns real
    people belonging to *other* tiers ("Head of Data Science at X" surfacing
    three ICs is the common case, not the exception), those people are now
    kept and counted — so a later tier's quota can already be full by the time
    the plan reaches it. Billing that tier's own query anyway buys nothing.
    Passing the judged contacts here lets a filled tier's unissued query count
    as satisfied rather than outstanding.

    The guard's real purpose is unchanged: catch a `partial` written without
    looking. "Quota is already full" is not that — it means we looked, found
    enough, and stopped. A tier that is still SHORT and never searched remains
    outstanding exactly as before, so the Binance/Highbrow/Vinsari failure this
    was built for still raises. Omit `contacts` (the CLI `coverage`
    subcommand) for the strict ledger-only reading."""
    from app.contacts.agentic_batch import tier_quota_filled
    from app.contacts.search_source import spent_for

    outstanding = []
    for planned in mandatory_queries(context):
        if spent_for(*planned.key) >= planned.attempt:
            continue
        if contacts and tier_quota_filled(
            context, contacts, planned.tier, planned.search_group
        ):
            continue
        outstanding.append(planned)
    return outstanding


def format_plan(planned: list[PlannedQuery]) -> str:
    """Human-readable worklist for the agent to follow."""
    if not planned:
        return "(no queries planned — company has no matching search group)"
    lines = []
    for p in planned:
        marker = "  optional" if p.conditional else "MANDATORY"
        lines.append(
            f"[{marker}] {p.tier:20s} {p.search_group:12s} attempt {p.attempt}  {p.query}"
        )
    return "\n".join(lines)
