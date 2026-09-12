"""Pure classification/ranking helpers; no database knowledge.

The immutable ranking plan is the single policy implementation used by both
legacy and instrumented Decision Runs.  Persistence happens separately, so
ranking never depends on mutable ``Job`` rows or SQLAlchemy identity state.
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

ROLE_PRIORITY = ("ml_ai", "data_science", "credit_risk", "analytics", "data_engineering", "other")

def classify_role(title: str | None, description: str | None = None) -> str:
    title_l = (title or "").lower()
    patterns = {
        "ml_ai": ("ai engineer", "ml engineer", "machine learning", "mlops", "genai", "llm", "nlp", "computer vision"),
        "data_science": ("data scientist", "applied scientist", "decision scientist"),
        "credit_risk": ("credit", "underwriting", "fraud", "collections", "model risk", "portfolio risk"),
        "analytics": ("data analyst", "business intelligence", "bi analyst", "business analyst", "product analytics", "marketing analytics"),
        "data_engineering": ("data engineer", "analytics engineer", "etl", "data warehouse", "data platform"),
    }
    for text in (title_l, (description or "").lower()):
        for family in ROLE_PRIORITY:
            if family != "other" and any(word in text for word in patterns[family]):
                return family
    return "other"

def qualified_wlb(wlb: float | None, reviews: int | None, minimum_reviews: int) -> float | None:
    return wlb if wlb is not None and (reviews or 0) >= minimum_reviews else None

def group_for(is_remote: bool | None, effective_salary: float | None) -> int:
    remote = is_remote is True
    high_or_unknown = effective_salary is None or effective_salary >= 20
    return 1 if remote and high_or_unknown else 2 if high_or_unknown else 3 if remote else 4

@dataclass(frozen=True)
class RankableJob:
    run_job_id: int
    job_id: int
    company_id: int
    role_family: str
    effective_salary_lpa: float | None
    qualified_wlb: float | None
    posted_at: datetime | None
    is_remote: bool


@dataclass(frozen=True)
class RankedJob:
    run_job_id: int
    group_number: int
    within_company_rank: int
    is_primary: bool


@dataclass(frozen=True)
class RankedCompany:
    company_id: int
    group_number: int
    rank: int
    primary_run_job_id: int


@dataclass(frozen=True)
class RankingPlan:
    jobs: tuple[RankedJob, ...]
    companies: tuple[RankedCompany, ...]


def job_sort_key(row: RankableJob) -> tuple:
    """The established Decision Run ordering policy.

    Keep this key stable: it controls primary-job selection, within-company
    ordering, and company ordering inside each group.
    """
    salary = row.effective_salary_lpa
    wlb = row.qualified_wlb
    posted = row.posted_at
    return (salary is None, ROLE_PRIORITY.index(row.role_family), wlb is None,
            -(wlb or 0), -(salary or 0), -(posted.timestamp() if posted else 0),
            row.job_id, row.company_id)


def build_ranking_plan(jobs: Iterable[RankableJob]) -> RankingPlan:
    """Return deterministic immutable placements for eligible jobs/companies."""
    by_company: dict[int, list[RankableJob]] = {}
    for job in jobs:
        by_company.setdefault(job.company_id, []).append(job)

    job_placements: list[RankedJob] = []
    primary_by_group: dict[int, list[RankableJob]] = {group: [] for group in range(1, 5)}
    for company_jobs in by_company.values():
        groups = {
            job.run_job_id: group_for(job.is_remote, job.effective_salary_lpa)
            for job in company_jobs
        }
        winning_group = min(groups.values())
        primary = min(
            (job for job in company_jobs if groups[job.run_job_id] == winning_group),
            key=job_sort_key,
        )
        primary_by_group[winning_group].append(primary)
        for position, job in enumerate(sorted(company_jobs, key=job_sort_key), 1):
            job_placements.append(RankedJob(
                run_job_id=job.run_job_id,
                group_number=groups[job.run_job_id],
                within_company_rank=position,
                is_primary=job.run_job_id == primary.run_job_id,
            ))

    company_placements: list[RankedCompany] = []
    for group, primaries in primary_by_group.items():
        for rank, primary in enumerate(sorted(primaries, key=job_sort_key), 1):
            company_placements.append(RankedCompany(
                company_id=primary.company_id,
                group_number=group,
                rank=rank,
                primary_run_job_id=primary.run_job_id,
            ))

    return RankingPlan(
        jobs=tuple(sorted(job_placements, key=lambda item: item.run_job_id)),
        companies=tuple(company_placements),
    )
