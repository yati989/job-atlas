"""Persist source observations, then explicitly calculate combined values."""

from math import isfinite
import re
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.orm import Company, utcnow


_ACTIVE_SOURCES = {"glassdoor", "ambitionbox"}
_SALARY_ROLE_ALIASES = {
    "data science": "data scientist",
    "applied scientist": "data scientist",
    "analytics": "data analyst",
    "bi analyst": "data analyst",
    "business intelligence analyst": "data analyst",
    "data engineering": "data engineer",
    "analytics engineer": "data engineer",
    "software development engineer": "software engineer",
    "software developer": "software engineer",
    "ml engineer": "machine learning engineer",
    "mlops engineer": "machine learning engineer",
    "ml ops": "machine learning engineer",
    "mlops": "machine learning engineer",
    "ml engr": "machine learning engineer",
    "machine learning engr": "machine learning engineer",
    "computer vision": "machine learning engineer",
    "ai ml": "machine learning engineer",
    "artificial intelligence engineer": "ai engineer",
    "applied ai": "ai engineer",
    "generative ai": "ai engineer",
    "genai developer": "ai engineer",
    "gen ai developer": "ai engineer",
    "generative ai developer": "ai engineer",
    "genai engineer": "ai engineer",
    "gen ai engineer": "ai engineer",
    "generative ai engineer": "ai engineer",
    "genai": "ai engineer",
    "gen ai": "ai engineer",
    "llm": "ai engineer",
    "agentic ai": "ai engineer",
    "ai engr": "ai engineer",
    "risk analyst": "credit risk",
    "risk analytics": "credit risk",
    "credit analyst": "credit risk",
    "fraud risk": "credit risk",
    "fraud analyst": "credit risk",
    "model risk": "credit risk",
    "underwriting": "credit risk",
    "underwriter": "credit risk",
}


def _optional_number(
    value: float | None,
    *,
    label: str,
    minimum: float,
    maximum: float | None = None,
) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a number or null")
    number = float(value)
    if not isfinite(number):
        raise ValueError(f"{label} must be finite")
    if number < minimum or (maximum is not None and number > maximum):
        limit = f"between {minimum:g} and {maximum:g}" if maximum else f"at least {minimum:g}"
        raise ValueError(f"{label} must be {limit}")
    return number


def _rating(value: float | None, label: str) -> float | None:
    return _optional_number(value, label=label, minimum=1, maximum=5)


def _salary(value: float | None, label: str) -> float | None:
    return _optional_number(value, label=label, minimum=0.000_001)


def _review_count(value: int | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("Glassdoor review count must be a non-negative integer or null")
    return value


def _company(session: Session, company_id: int) -> Company:
    # Glassdoor and AmbitionBox are intentionally collected in parallel. Lock
    # and refresh the company before either source performs its JSON
    # read-modify-write so both source observations survive concurrent commits.
    company = session.execute(
        select(Company).where(Company.id == company_id).with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if company is None:
        raise LookupError(f"company {company_id} was not found")
    return company


def _normalized_role(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(re.findall(r"[a-z0-9]+", value.lower()))
    if not normalized:
        return None
    return _SALARY_ROLE_ALIASES.get(normalized, normalized)


def _ambitionbox_salary(company: Company) -> tuple[str | None, list[float], list[str]]:
    """Use AmbitionBox as the sole active salary-estimation source."""
    evidence = company.market_profile_evidence
    evidence = evidence if isinstance(evidence, Mapping) else {}
    sources = evidence.get("sources")
    sources = sources if isinstance(sources, Mapping) else {}

    salary = _salary(
        company.ambitionbox_estimated_salary_lpa,
        "AmbitionBox salary estimate",
    )
    source_evidence = sources.get("ambitionbox")
    role = (
        source_evidence.get("selected_role")
        if isinstance(source_evidence, Mapping)
        else None
    )
    selected_role = _normalized_role(role)
    if salary is None or selected_role is None:
        return None, [], []
    return selected_role, [salary], ["ambitionbox"]


def _completion_status(evidence: Mapping[str, Any]) -> str:
    sources = evidence.get("sources")
    if not isinstance(sources, Mapping) or not _ACTIVE_SOURCES <= set(sources):
        return "partial"
    statuses = {
        sources[source].get("status")
        if isinstance(sources[source], Mapping)
        else None
        for source in _ACTIVE_SOURCES
    }
    return "done" if statuses <= {"ok", "missing"} else "partial"


def save_source_market_profile(
    session: Session,
    company_id: int,
    *,
    glassdoor_overall_rating: float | None,
    glassdoor_wlb_rating: float | None,
    ambitionbox_overall_rating: float | None,
    ambitionbox_wlb_rating: float | None,
    ambitionbox_estimated_salary_lpa: float | None,
    levels_fyi_estimated_salary_lpa: float | None,
    evidence: Mapping[str, Any],
    glassdoor_review_count: int | None = None,
    employee_count_range: str | None = None,
    ownership_type: str | None = None,
    revenue: str | None = None,
) -> Company:
    """Replace and flush source-specific values without changing combined fields."""
    values = {
        "glassdoor_overall_rating": _rating(
            glassdoor_overall_rating, "Glassdoor overall rating",
        ),
        "glassdoor_wlb_rating": _rating(
            glassdoor_wlb_rating, "Glassdoor WLB rating",
        ),
        "ambitionbox_overall_rating": _rating(
            ambitionbox_overall_rating, "AmbitionBox overall rating",
        ),
        "ambitionbox_wlb_rating": _rating(
            ambitionbox_wlb_rating, "AmbitionBox WLB rating",
        ),
        "ambitionbox_estimated_salary_lpa": _salary(
            ambitionbox_estimated_salary_lpa, "AmbitionBox salary estimate",
        ),
        "levels_fyi_estimated_salary_lpa": _salary(
            levels_fyi_estimated_salary_lpa, "Levels.fyi salary estimate",
        ),
    }
    company = _company(session, company_id)
    for field, value in values.items():
        setattr(company, field, value)
    sources = evidence.get("sources")
    glassdoor_source = (
        sources.get("glassdoor") if isinstance(sources, Mapping) else None
    )
    if (
        isinstance(glassdoor_source, Mapping)
        and glassdoor_source.get("status") == "ok"
    ):
        company.glassdoor_review_count = _review_count(glassdoor_review_count)
        company.employee_count_range = employee_count_range
        company.ownership_type = ownership_type
        company.revenue = revenue
    company.market_profile_evidence = dict(evidence)
    company.market_profile_status = _completion_status(evidence)
    company.market_profile_updated_at = utcnow()
    session.flush()
    return company


def save_ambitionbox_market_profile(
    session: Session,
    company_id: int,
    *,
    overall_rating: float | None,
    wlb_rating: float | None,
    salary_lpa: float | None,
    evidence: Mapping[str, Any],
) -> Company:
    """Update only AmbitionBox state, preserving every other source.

    A transient refresh failure never discards a prior successful observation.
    The failed refresh remains inspectable under ``last_refresh`` so a later
    source-only run can retry it without touching Glassdoor or Levels.fyi.
    """
    company = _company(session, company_id)
    profile_evidence = company.market_profile_evidence
    profile_evidence = (
        dict(profile_evidence) if isinstance(profile_evidence, Mapping) else {}
    )
    sources = profile_evidence.get("sources")
    sources = dict(sources) if isinstance(sources, Mapping) else {}
    previous = sources.get("ambitionbox")
    previous = dict(previous) if isinstance(previous, Mapping) else {}
    source_evidence = dict(evidence)
    status = source_evidence.get("status")
    if status not in {"ok", "missing", "error", "deferred", "not_attempted"}:
        raise ValueError(f"unsupported AmbitionBox status {status!r}")

    transient_failure = status in {"error", "deferred", "not_attempted"}
    if previous.get("status") == "ok" and transient_failure:
        previous["last_refresh"] = source_evidence
        sources["ambitionbox"] = previous
    else:
        company.ambitionbox_overall_rating = _rating(
            overall_rating, "AmbitionBox overall rating",
        )
        company.ambitionbox_wlb_rating = _rating(
            wlb_rating, "AmbitionBox WLB rating",
        )
        company.ambitionbox_estimated_salary_lpa = _salary(
            salary_lpa, "AmbitionBox salary estimate",
        )
        sources["ambitionbox"] = source_evidence

    profile_evidence["sources"] = sources
    company.market_profile_evidence = profile_evidence
    company.market_profile_status = _completion_status(profile_evidence)
    company.market_profile_updated_at = utcnow()
    session.flush()
    return company


def save_glassdoor_market_profile(
    session: Session,
    company_id: int,
    *,
    overall_rating: float | None,
    wlb_rating: float | None,
    evidence: Mapping[str, Any],
    review_count: int | None = None,
    employee_count_range: str | None = None,
    ownership_type: str | None = None,
    revenue: str | None = None,
) -> Company:
    """Update only Glassdoor state, preserving every other source."""
    company = _company(session, company_id)
    profile_evidence = company.market_profile_evidence
    profile_evidence = (
        dict(profile_evidence) if isinstance(profile_evidence, Mapping) else {}
    )
    sources = profile_evidence.get("sources")
    sources = dict(sources) if isinstance(sources, Mapping) else {}
    source_evidence = dict(evidence)
    status = source_evidence.get("status")
    if status not in {"ok", "missing", "error", "deferred", "not_attempted"}:
        raise ValueError(f"unsupported Glassdoor status {status!r}")

    company.glassdoor_overall_rating = _rating(
        overall_rating, "Glassdoor overall rating",
    )
    company.glassdoor_wlb_rating = _rating(
        wlb_rating, "Glassdoor WLB rating",
    )
    if status == "ok":
        company.glassdoor_review_count = _review_count(review_count)
        company.employee_count_range = employee_count_range
        company.ownership_type = ownership_type
        company.revenue = revenue
    sources["glassdoor"] = source_evidence
    profile_evidence["sources"] = sources
    company.market_profile_evidence = profile_evidence
    company.market_profile_status = _completion_status(profile_evidence)
    company.market_profile_updated_at = utcnow()
    session.flush()
    return company


def save_levels_fyi_market_profile(
    session: Session,
    company_id: int,
    *,
    salary_lpa: float | None,
    evidence: Mapping[str, Any],
    requested_salary_role: str,
    requested_seniority: str,
    retrieved_at: str,
) -> Company:
    """Update only Levels.fyi state, preserving every other source."""
    company = _company(session, company_id)
    profile_evidence = company.market_profile_evidence
    profile_evidence = (
        dict(profile_evidence) if isinstance(profile_evidence, Mapping) else {}
    )
    sources = profile_evidence.get("sources")
    sources = dict(sources) if isinstance(sources, Mapping) else {}
    source_evidence = dict(evidence)
    status = source_evidence.get("status")
    if status not in {"ok", "missing", "error", "deferred", "not_attempted"}:
        raise ValueError(f"unsupported Levels.fyi status {status!r}")

    company.levels_fyi_estimated_salary_lpa = _salary(
        salary_lpa, "Levels.fyi salary estimate",
    )
    sources["levels_fyi"] = source_evidence
    profile_evidence.update(
        {
            "requested_salary_role": requested_salary_role,
            "requested_seniority": requested_seniority,
            "retrieved_at": retrieved_at,
            "sources": sources,
        }
    )
    company.market_profile_evidence = profile_evidence
    company.market_profile_status = _completion_status(profile_evidence)
    company.market_profile_updated_at = utcnow()
    session.flush()
    return company

def calculate_market_profile(
    session: Session,
    company_id: int,
) -> Company:
    """Derive combined values in a separate, explicitly invoked stage."""
    company = _company(session, company_id)
    glassdoor_wlb = _rating(
        company.glassdoor_wlb_rating,
        "Glassdoor WLB rating",
    )
    selected_salary_role, salaries, salary_sources = _ambitionbox_salary(company)
    overall = [
        value for value in (
            _rating(company.glassdoor_overall_rating, "Glassdoor overall rating"),
            _rating(company.ambitionbox_overall_rating, "AmbitionBox overall rating"),
        )
        if value is not None
    ]

    company.wlb_rating = glassdoor_wlb
    company.estimated_salary_lpa = (
        round(salaries[0], 1) if salaries else None
    )
    company.overall_rating = (
        round(sum(overall) / len(overall), 1) if overall else None
    )
    evidence = company.market_profile_evidence
    evidence = dict(evidence) if isinstance(evidence, Mapping) else {}
    combined = evidence.get("combined")
    combined = dict(combined) if isinstance(combined, Mapping) else {}
    salary_method = "single_source" if salaries else "none"
    combined["salary"] = {
        "selected_role": selected_salary_role,
        "sources": salary_sources,
        "method": salary_method,
    }
    combined["wlb"] = {
        "source": "glassdoor" if glassdoor_wlb is not None else None,
        "method": "single_source" if glassdoor_wlb is not None else "none",
    }
    evidence["combined"] = combined
    company.market_profile_evidence = evidence
    session.flush()
    return company
