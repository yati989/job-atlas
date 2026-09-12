"""
The prioritised company queue (issue #72): "show me the next N companies to
work" for contact-finding. The backlog (~3,800 companies) is explicitly
assumed never to be exhausted, so this ordering — not any cap — determines
what actually gets worked. Companies are ranked, never filtered out, by:

    pinned > work mode (any remote job first) > role rank (best matching
    job) > recency > posting count > company id (stable tiebreak)

Company type (employer/staffing, #71) deliberately does NOT affect ordering
— it selects which ladder a company gets in #73, not queue position.

Work mode and role rank are computed here at query time from each company's
own Job rows. Company type and target contact search groups are prerequisites
stored by ``enrich-companies`` before a row may enter this queue.
"""
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config.categories import CATEGORY_KEYWORDS
from app.models.orm import Company, Job
from app.pipeline.relevance import _word_match

# Role-rank precedence (issue #72): AI engineer, then ML engineer, then data
# scientist, then credit risk, then analytics — lower index = higher
# priority. This is a FINER split than CATEGORY_KEYWORDS: "ai_ml_engineering"
# there is one undifferentiated category, but the queue needs AI engineer and
# ML engineer as separate rungs. The marker lists below are carved out of
# CATEGORY_KEYWORDS["ai_ml_engineering"] and CATEGORY_KEYWORDS["data_science"]
# by hand (AI-flavoured terms vs ML-flavoured terms vs neither), not an
# independent vocabulary — so a title that matches the gate still ranks
# consistently with why it matched.
_AI_ENGINEER_MARKERS = [
    "ai engineer", "artificial intelligence", "applied ai", "generative ai",
    "genai", "gen ai", "llm", "agentic ai", "ai engr",
]
_ML_ENGINEER_MARKERS = [
    "machine learning engineer", "ml engineer", "mlops engineer", "ml ops",
    "mlops", "ml engr", "machine learning engr", "computer vision",
    "ai/ml", "ml/ai", "ai ml",
]
_DATA_SCIENTIST_MARKERS = [
    m for m in CATEGORY_KEYWORDS["data_science"] if m not in ("machine learning",)
]
_CREDIT_RISK_MARKERS = CATEGORY_KEYWORDS["credit_risk"]
_ANALYTICS_MARKERS = CATEGORY_KEYWORDS["analytics"]

# Rank order is the list order itself — index 0 is the highest-priority role.
_ROLE_RANK_MARKERS: list[tuple[str, list[str]]] = [
    ("ai_engineer", _AI_ENGINEER_MARKERS),
    ("ml_engineer", _ML_ENGINEER_MARKERS),
    ("data_scientist", _DATA_SCIENTIST_MARKERS),
    ("credit_risk", _CREDIT_RISK_MARKERS),
    ("analytics", _ANALYTICS_MARKERS),
]
# Sentinel rank for a job that matches *some* target category (so the
# company is still eligible) but none of the five ranked roles — e.g.
# data_engineering or quant_decision_science. Sorts after every ranked role,
# ahead of nothing (there is no lower tier).
_UNRANKED = len(_ROLE_RANK_MARKERS)


def _job_role_rank(title: str) -> int:
    """Best (lowest) role rank this title matches, or _UNRANKED if it
    matches a target category but none of the five ranked roles."""
    title_lower = title.lower()
    for rank, (_name, markers) in enumerate(_ROLE_RANK_MARKERS):
        if any(_word_match(m, title_lower) for m in markers):
            return rank
    return _UNRANKED


def _matches_any_category(title: str) -> bool:
    title_lower = title.lower()
    return any(
        _word_match(kw, title_lower)
        for keywords in CATEGORY_KEYWORDS.values()
        for kw in keywords
    )


# Company names that no amount of searching can resolve to a real employer.
# All pre-existing ingestion debt the queue surfaced, not something #72 or
# #73 created — but a contact search needs a company name to search *for*,
# so these must not consume batch slots.
#
#   "Unknown"  — 1,147 unattributed postings from paywalled/parse-failed
#                sources merged into one row by `get_or_create_company`'s
#                exact-name matching. Not one company; ~1,147 different ones.
#   "name"     — a himalayas parse bug that captured a literal placeholder;
#                its jobs' URLs point at several *different* real companies
#                (/companies/jeeves/, /companies/wealth-enhancement/), so it
#                is a merged bucket too.
#   "X..."     — some historical source rows truncated long employer names to
#                a single letter plus an ellipsis ("H...", "R...", "P...").
#   "Premium"  — naukri's listing BADGE, not an employer, captured as the
#                company name (found 2026-08-03 when it reached the top of the
#                contact queue). Its 5 jobs carry 4 different titles spread
#                across Bengaluru/Mumbai/Gurugram/Pune/Chennai and all come
#                from naukri — a merged bucket of several unrelated employers,
#                exactly like "Unknown". There is nothing to search for.
#
# Measured 2026-08-02: 40 rows / 1,408 jobs total. Matching is deliberately
# narrow — a bare length check would wrongly exclude real short names like
# "EY".
_PLACEHOLDER_NAMES = ("Unknown", "name", "N/A", "null", "None", "company", "Premium")


def _searchable_name_filters():
    """Filters excluding company rows whose name can't be searched for."""
    return [
        Company.name.notin_(_PLACEHOLDER_NAMES),
        ~Company.name.like("%..."),
    ]


@dataclass
class QueuedCompany:
    company: Company
    is_remote: bool
    best_role_rank: int
    latest_posted_at: datetime | None
    matching_job_count: int


def _sort_key(pinned_ids: set[int]):
    def key(q: QueuedCompany):
        return (
            0 if q.company.id in pinned_ids else 1,
            0 if q.is_remote else 1,
            q.best_role_rank,
            # Descending recency/count via negation; None sorts last within
            # its tier by treating it as the oldest possible timestamp.
            -(q.latest_posted_at.timestamp() if q.latest_posted_at else float("-inf")),
            -q.matching_job_count,
            q.company.id,
        )
    return key


def get_company_queue(
    session: Session, limit: int, pinned: list[str] | None = None
) -> list[Company]:
    """The next `limit` companies to work, in priority order.

    Only companies still `pending` contact enrichment and matching at least
    one target category (any CATEGORY_KEYWORDS family, not just the five
    ranked roles) are eligible. `pinned` is a list of company names to force
    ahead of every heuristic tiebreak — for a company the user is applying
    to this week. Pinned companies are still ordered against each other by
    the same heuristics; pinning only lifts them above the unpinned group.
    """
    companies = (
        session.execute(
            select(Company).where(
                Company.contact_enrichment_status == "pending",
                Company.contact_search_groups.is_not(None),
                *_searchable_name_filters(),
            )
        )
        .scalars()
        .all()
    )
    company_by_id = {c.id: c for c in companies}
    if not company_by_id:
        return []

    jobs = session.execute(
        select(Job.company_id, Job.title, Job.is_remote, Job.posted_at).where(
            Job.company_id.in_(company_by_id.keys())
        )
    ).all()

    per_company: dict[int, dict] = {
        cid: {"remote": False, "rank": _UNRANKED, "latest": None, "count": 0}
        for cid in company_by_id
    }
    for company_id, title, is_remote, posted_at in jobs:
        if not title or not _matches_any_category(title):
            continue
        agg = per_company[company_id]
        agg["count"] += 1
        if is_remote:
            agg["remote"] = True
        rank = _job_role_rank(title)
        if rank < agg["rank"]:
            agg["rank"] = rank
        if posted_at and (agg["latest"] is None or posted_at > agg["latest"]):
            agg["latest"] = posted_at

    queued = [
        QueuedCompany(
            company=company_by_id[cid],
            is_remote=agg["remote"],
            best_role_rank=agg["rank"],
            latest_posted_at=agg["latest"],
            matching_job_count=agg["count"],
        )
        for cid, agg in per_company.items()
        if agg["count"] > 0  # matched at least one target-category job
    ]

    pinned_names = {name.lower() for name in (pinned or [])}
    pinned_ids = {q.company.id for q in queued if q.company.name.lower() in pinned_names}

    queued.sort(key=_sort_key(pinned_ids))
    return [q.company for q in queued[:limit]]
