"""Run-scoped dashboard projection for the local public-workflow database."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.orm import (
    Company,
    Contact,
    DecisionRun,
    GuidedRunPlan,
    GuidedSourceRun,
    Job,
    JobPostingVersion,
    JobSkill,
    LinkedInProfileLink,
    OutreachDeliveryAttempt,
    OutreachMessage,
    ProfileDiscoveryAuthorization,
    ProfiledSearchOutcome,
    PublicJobPhaseAEvidence,
    PublicSelectionScope,
    PublicSelectionScopeItem,
    PublicTailoredResume,
    PublicWorkflowStage,
)


@dataclass(frozen=True)
class PublicDashboardFilters:
    source: str | None = None
    enrichment_status: str | None = None
    company: str | None = None
    skill: str | None = None
    remote: bool | None = None
    selected_only: bool = False
    minimum_glassdoor_overall_rating: float | None = None
    minimum_glassdoor_wlb_rating: float | None = None
    minimum_estimated_salary_lpa: float | None = None
    include_blank_glassdoor_overall_rating: bool = True
    include_blank_glassdoor_wlb_rating: bool = True
    include_blank_estimated_salary_lpa: bool = True


def filter_kept_jobs(
    jobs: tuple[dict, ...], filters: PublicDashboardFilters,
) -> tuple[dict, ...]:
    """Apply display-only filters to one run's eligible jobs."""
    thresholds = (
        (
            "glassdoor_overall_rating",
            filters.minimum_glassdoor_overall_rating,
            filters.include_blank_glassdoor_overall_rating,
        ),
        (
            "glassdoor_wlb_rating",
            filters.minimum_glassdoor_wlb_rating,
            filters.include_blank_glassdoor_wlb_rating,
        ),
        (
            "estimated_salary_lpa",
            filters.minimum_estimated_salary_lpa,
            filters.include_blank_estimated_salary_lpa,
        ),
    )

    def visible(job: dict) -> bool:
        if filters.source and job.get("source") != filters.source:
            return False
        if filters.enrichment_status and job.get("enrichment_status") != filters.enrichment_status:
            return False
        if filters.company and filters.company.casefold() not in (job.get("company") or "").casefold():
            return False
        if filters.skill:
            skills = (*job.get("hard_skills", ()), *job.get("soft_skills", ()))
            if filters.skill.casefold() not in {item.casefold() for item in skills}:
                return False
        if filters.remote is not None and job.get("is_remote") is not filters.remote:
            return False
        if filters.selected_only and not job.get("selected"):
            return False
        return all(
            minimum is None
            or (job.get(field) is None and include_blank)
            or (job.get(field) is not None and job[field] >= minimum)
            for field, minimum, include_blank in thresholds
        )

    return tuple(job for job in jobs if visible(job))


@dataclass(frozen=True)
class PublicDashboardSnapshot:
    run_id: str
    run: dict
    source_statuses: tuple[dict, ...]
    stage_statuses: tuple[dict, ...]
    outcome_counts: dict[str, int]
    kept_jobs: tuple[dict, ...]
    companies: tuple[dict, ...]
    latest_scope: dict | None
    profile_links: tuple[dict, ...]
    resumes: tuple[dict, ...]
    outreach_drafts: tuple[dict, ...]
    workbook_path: str | None
    collected_count: int
    enriched_job_count: int


def stage_message(stage: dict) -> str:
    """Explain durable workflow progress, including a stored blocker."""
    detail = stage.get("detail") if isinstance(stage.get("detail"), dict) else {}
    name = stage["name"]
    if name == "role_review" and detail:
        return (
            f"Reviewed {stage['completed_count']:,} jobs: "
            f"{detail.get('relevant', 0):,} looked relevant and "
            f"{detail.get('irrelevant', 0):,} did not."
        )
    if name == "location_review" and detail:
        return (
            f"Checked {stage['completed_count']:,} unclear locations: "
            f"{detail.get('eligible', 0):,} eligible and "
            f"{detail.get('ineligible', 0):,} outside the search."
        )
    if name == "phase_a" and stage["status"] == "completed":
        return "Job requirements and company facts are ready."
    if name == "phase_b" and stage["status"] in {"not_requested", "skipped"}:
        return "Skipped for this search."
    if name == "selection" and stage["status"] == "completed":
        return f"Saved {stage['completed_count']:,} jobs in the final shortlist."
    if name == "export" and stage["status"] == "completed":
        return "The Excel workbook is ready to download."
    blocker = detail.get("blocker")
    if blocker and stage["status"] in {"partial", "failed"}:
        reason = str(blocker).strip().rstrip(".")
        prefix = "Partly completed" if stage["status"] == "partial" else "Could not complete"
        return f"{prefix}: {reason}."
    return {
        "not_requested": "Not requested for this search.",
        "awaiting_confirmation": "Waiting for your approval.",
        "running": "Currently running.",
        "partial": "Partly completed; some records still need attention.",
        "failed": "This step failed. Check the run output for the reason.",
        "completed": "Completed.",
        "skipped": "Skipped for this search.",
    }.get(stage["status"], stage["status"].replace("_", " ").title())


def profile_link_output_message(stages: tuple[dict, ...]) -> str:
    """Explain an empty profile-link table without hiding completed search work."""
    stage = next((item for item in stages if item["name"] == "profile_links"), None)
    if stage is None or stage["status"] == "not_requested":
        return "No LinkedIn profile-link search was requested for this selection."
    if stage["status"] == "completed":
        return "The LinkedIn profile-link search completed, but no usable profile links were found."

    detail = stage.get("detail") if isinstance(stage.get("detail"), dict) else {}
    progress = (
        f"{stage['completed_count']:,} of {stage['expected_count']:,} planned "
        "profile searches completed"
    )
    limit = detail.get("maximum_calls")
    if isinstance(limit, int):
        progress += f" (approved limit: {limit:,})"
    return f"{stage_message(stage)} {progress}; no profile links were found."


def outreach_draft_caption(drafts: tuple[dict, ...]) -> str:
    """Describe whether the displayed delivery attempts remain unsent drafts."""
    if all(draft.get("sent_at") is None for draft in drafts):
        return "These drafts were created in Gmail and were not sent."
    return "Some delivery attempts have been sent; review each row's state and sent time."


def friendly_search_label(profile: dict, fallback: str = "Saved search") -> str:
    """Describe a saved search in plain language without exposing its ID or slug."""
    professions = [str(item).strip() for item in profile.get("professions") or () if str(item).strip()]
    cities = [str(item).strip() for item in profile.get("cities") or () if str(item).strip()]
    countries = [str(item).strip() for item in profile.get("countries") or () if str(item).strip()]
    country_names = [{"IN": "India"}.get(item.upper(), item) for item in countries]
    arrangements = {str(item).casefold() for item in profile.get("arrangements") or ()}
    role = " or ".join(professions) if professions else fallback
    locations = list(cities)
    if "remote" in arrangements:
        locations.append("remote")
    elif not locations and country_names:
        locations.extend(country_names)
    if not locations:
        return role[:1].upper() + role[1:]
    if locations == ["remote"]:
        country = country_names[0] if country_names else None
        suffix = f" in {country}" if country else ""
        return f"{role[:1].upper() + role[1:]} for remote work{suffix}"
    if len(locations) == 1:
        location = locations[0]
    else:
        location = ", ".join(locations[:-1]) + f" or {locations[-1]}"
    return f"{role[:1].upper() + role[1:]} in {location}"


def dated_search_label(
    profile: dict,
    created_at: datetime,
    timezone_name: str,
    fallback: str = "Saved search",
) -> str:
    """Build a compact role/location/date label for the search dropdown."""
    professions = [
        str(item).strip() for item in profile.get("professions") or ()
        if str(item).strip()
    ]
    cities = [
        str(item).strip() for item in profile.get("cities") or ()
        if str(item).strip()
    ]
    arrangements = {str(item).casefold() for item in profile.get("arrangements") or ()}
    role = " / ".join(professions) if professions else fallback
    places = list(cities)
    if "remote" in arrangements:
        places.append("Remote")
    location = " / ".join(places) if places else "India"
    moment = created_at
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    try:
        moment = moment.astimezone(ZoneInfo(timezone_name))
    except (KeyError, ValueError):
        moment = moment.astimezone(timezone.utc)
    timestamp = moment.strftime("%d %b %Y · %I:%M %p").lstrip("0")
    return f"{timestamp} · {role[:1].upper() + role[1:]} · {location}"


def _salary_evidence_text(value: object) -> str | None:
    """Turn structured Phase A salary evidence into a compact display value."""
    if not isinstance(value, dict) or not value:
        return None
    parts = [str(value["evidence"])] if value.get("evidence") else []
    minimum = value.get("guaranteed_min_lpa")
    maximum = value.get("guaranteed_max_lpa")
    if minimum is not None and maximum is not None:
        parts.append(f"{minimum}-{maximum} LPA")
    elif maximum is not None:
        parts.append(f"up to {maximum} LPA")
    elif minimum is not None:
        parts.append(f"from {minimum} LPA")
    if value.get("unusable_reason"):
        parts.append(str(value["unusable_reason"]).replace("_", " "))
    return " · ".join(parts) or None


def load_public_snapshot(session: Session, run_id: str) -> PublicDashboardSnapshot:
    decision_run = session.get(DecisionRun, run_id)
    if decision_run is None:
        raise ValueError(f"unknown guided run: {run_id}")
    plan = session.scalar(select(GuidedRunPlan).where(GuidedRunPlan.run_id == run_id))
    profile = plan.profile_snapshot if plan and isinstance(plan.profile_snapshot, dict) else {}
    search_label = friendly_search_label(profile)
    run = {
        "name": profile.get("name") or search_label,
        "label": search_label,
        "state": decision_run.state,
        "created_at": decision_run.created_at,
        "search_terms": tuple(profile.get("search_terms") or ()),
        "countries": tuple(profile.get("countries") or ()),
        "cities": tuple(profile.get("cities") or ()),
        "arrangements": tuple(profile.get("arrangements") or ()),
        "collection_window_days": profile.get("collection_window_days"),
    }
    outcome_by_source: dict[str, dict[str, int]] = {}
    for source, outcome, count in session.execute(
        select(
            ProfiledSearchOutcome.source,
            ProfiledSearchOutcome.outcome,
            func.count(ProfiledSearchOutcome.id),
        )
        .where(ProfiledSearchOutcome.run_id == run_id)
        .group_by(ProfiledSearchOutcome.source, ProfiledSearchOutcome.outcome)
    ):
        outcome_by_source.setdefault(source, {})[outcome] = count
    sources = tuple({
        "source": row.source, "status": row.status,
        "attempt_count": row.attempt_count,
        "collected": sum((row.outcome_counts or {}).values()),
        "kept": outcome_by_source.get(row.source, {}).get("kept", 0),
        "rejected": outcome_by_source.get(row.source, {}).get("rejected", 0),
        "needs_review": outcome_by_source.get(row.source, {}).get("needs_review", 0),
        "failure_detail": row.failure_detail,
    } for row in session.scalars(
        select(GuidedSourceRun).where(GuidedSourceRun.run_id == run_id)
        .order_by(GuidedSourceRun.source)
    ))
    stages = tuple({
        "name": row.name, "status": row.status,
        "expected_count": row.expected_count,
        "completed_count": row.completed_count,
        "failed_count": row.failed_count,
        "detail": row.detail,
    } for row in session.scalars(
        select(PublicWorkflowStage).where(PublicWorkflowStage.run_id == run_id)
        .order_by(PublicWorkflowStage.id)
    ))
    counts = dict(session.execute(
        select(ProfiledSearchOutcome.outcome, func.count(ProfiledSearchOutcome.id))
        .where(ProfiledSearchOutcome.run_id == run_id)
        .group_by(ProfiledSearchOutcome.outcome)
    ).all())
    scope = session.scalar(
        select(PublicSelectionScope).where(
            PublicSelectionScope.run_id == run_id,
            PublicSelectionScope.purpose == "selection",
        )
        .order_by(PublicSelectionScope.revision.desc()).limit(1)
    )
    latest_scope = None if scope is None else {
        "revision": scope.revision, "mode": scope.mode, "purpose": scope.purpose,
        "job_count": scope.job_count, "company_count": scope.company_count,
    }
    selected_versions: set[int] = set()
    selected_companies: set[int] = set()
    if scope is not None:
        selected_versions = set(session.scalars(select(
            PublicSelectionScopeItem.posting_version_id,
        ).where(PublicSelectionScopeItem.scope_id == scope.id)))
        selected_companies = set(session.scalars(select(
            PublicSelectionScopeItem.company_id,
        ).where(PublicSelectionScopeItem.scope_id == scope.id)))

    rows = list(session.execute(
        select(ProfiledSearchOutcome, Job, Company)
        .join(Job, ProfiledSearchOutcome.job_id == Job.id)
        .outerjoin(Company, Job.company_id == Company.id)
        .where(
            ProfiledSearchOutcome.run_id == run_id,
            ProfiledSearchOutcome.outcome == "kept",
            Job.duplicate_of_job_id.is_(None),
        ).order_by(Job.id)
    ))
    job_ids = [job.id for _outcome, job, _company in rows]
    skills_by_job: dict[int, dict[str, list[str]]] = {}
    if job_ids:
        for skill in session.scalars(
            select(JobSkill).where(JobSkill.job_id.in_(job_ids)).order_by(JobSkill.id)
        ):
            skills_by_job.setdefault(skill.job_id, {"hard": [], "soft": []})[
                skill.skill_type
            ].append(skill.skill)
    version_ids = [outcome.posting_version_id for outcome, _job, _company in rows if outcome.posting_version_id]
    phase_a_by_version = {}
    if version_ids:
        phase_a_by_version = {
            evidence.posting_version_id: evidence
            for evidence in session.scalars(select(PublicJobPhaseAEvidence).where(
                PublicJobPhaseAEvidence.posting_version_id.in_(version_ids)
            ))
        }
    jobs = tuple({
        "job_id": job.id,
        "run_id": run_id,
        "search": search_label,
        "posting_version_id": outcome.posting_version_id,
        "external_job_id": outcome.external_job_id,
        "selected": outcome.posting_version_id in selected_versions,
        "source": job.source,
        "title": job.title,
        "company_id": job.company_id,
        "company": (company.name if company else job.company_name_raw),
        "location": job.location_raw,
        "work_mode": "Remote" if job.is_remote else "On-site / unspecified",
        "is_remote": job.is_remote,
        "posted_at": job.posted_at,
        "employment_type": job.employment_type,
        "seniority": job.seniority,
        "salary": job.salary_raw,
        "salary_evidence": _salary_evidence_text(
            (phase_a_by_version.get(outcome.posting_version_id).evidence or {}).get("salary")
            if phase_a_by_version.get(outcome.posting_version_id) else None
        ),
        "experience_min_years": job.experience_min_years,
        "experience_max_years": job.experience_max_years,
        "education": job.education_requirement,
        "other_qualifications": job.qualification_other,
        "hard_skills": tuple(skills_by_job.get(job.id, {}).get("hard", ())),
        "soft_skills": tuple(skills_by_job.get(job.id, {}).get("soft", ())),
        "enrichment_status": job.enrichment_status,
        "job_url": job.job_url,
        "apply_url": job.apply_url,
        "glassdoor_overall_rating": company.glassdoor_overall_rating if company else None,
        "glassdoor_wlb_rating": company.glassdoor_wlb_rating if company else None,
        "estimated_salary_lpa": company.estimated_salary_lpa if company else None,
    } for outcome, job, company in rows)

    job_count_by_company: dict[int, int] = {}
    for _outcome, job, _company in rows:
        if job.company_id is not None:
            job_count_by_company[job.company_id] = job_count_by_company.get(job.company_id, 0) + 1
    companies = tuple({
        "company_id": company.id,
        "searches": (search_label,),
        "selected": company.id in selected_companies,
        "company": company.name,
        "company_type": company.company_type,
        "industry": company.industry,
        "employee_count": company.employee_count_range,
        "founded": company.founding_year,
        "description": company.description,
        "pain_points": company.pain_points,
        "search_groups": ", ".join(company.contact_search_groups or ()),
        "enrichment_status": company.enrichment_status,
        "jobs": job_count_by_company[company.id],
        "overall_rating": company.overall_rating,
        "wlb_rating": company.wlb_rating,
        "estimated_salary_lpa": company.estimated_salary_lpa,
        "company_url": f"https://{company.canonical_domain}" if company.canonical_domain else None,
    } for company in session.scalars(
        select(Company).where(Company.id.in_(job_count_by_company)).order_by(Company.name)
    )) if job_count_by_company else ()

    links: tuple[dict, ...] = ()
    if scope is not None:
        selected_company_ids = select(PublicSelectionScopeItem.company_id).where(
            PublicSelectionScopeItem.scope_id == scope.id,
        )
        links = tuple({
            "company": company.name,
            "name": link.full_name,
            "headline": link.headline,
            "linkedin_url": link.linkedin_url,
            "evidence": link.evidence,
        } for link, company in session.execute(
            select(LinkedInProfileLink, Company)
            .join(
                ProfileDiscoveryAuthorization,
                LinkedInProfileLink.authorization_id == ProfileDiscoveryAuthorization.id,
            )
            .join(Company, LinkedInProfileLink.company_id == Company.id)
            .where(
                ProfileDiscoveryAuthorization.run_id == run_id,
                LinkedInProfileLink.company_id.in_(selected_company_ids),
            )
            .order_by(LinkedInProfileLink.id)
        ))
    resumes: tuple[dict, ...] = ()
    if scope is not None:
        resumes = tuple({
            "posting_version_id": resume.posting_version_id,
            "title": job.title,
            "company": job.company_name_raw,
            "score": resume.score,
            "artifact_dir": resume.artifact_dir,
        } for resume, job in session.execute(
            select(PublicTailoredResume, Job)
            .join(
                PublicSelectionScopeItem,
                PublicTailoredResume.posting_version_id
                == PublicSelectionScopeItem.posting_version_id,
            )
            .join(
                JobPostingVersion,
                PublicTailoredResume.posting_version_id == JobPostingVersion.id,
            )
            .join(Job, JobPostingVersion.job_id == Job.id)
            .where(PublicSelectionScopeItem.scope_id == scope.id)
            .order_by(PublicTailoredResume.posting_version_id)
        ))
    outreach_drafts_by_message: dict[int, dict] = {}
    for attempt, message, company, contact in session.execute(
        select(OutreachDeliveryAttempt, OutreachMessage, Company, Contact)
        .join(OutreachMessage, OutreachDeliveryAttempt.message_id == OutreachMessage.id)
        .join(Company, OutreachMessage.company_id == Company.id)
        .outerjoin(Contact, OutreachMessage.contact_id == Contact.id)
        .where(
            OutreachMessage.run_id == run_id,
            OutreachMessage.message_kind == "initial",
        )
        .order_by(
            OutreachMessage.id,
            OutreachDeliveryAttempt.sequence_number.desc(),
            OutreachDeliveryAttempt.id.desc(),
        )
    ):
        if message.id in outreach_drafts_by_message:
            continue
        evidence = attempt.evidence if isinstance(attempt.evidence, dict) else {}
        retargeted_from = evidence.get("retargeted_from")
        prior_evidence = (
            retargeted_from.get("recipient_evidence")
            if isinstance(retargeted_from, dict) else None
        )
        source_url = evidence.get("linkedin_url")
        if not isinstance(source_url, str):
            source_url = evidence.get("source_url")
        if not isinstance(source_url, str) and isinstance(prior_evidence, dict):
            source_url = prior_evidence.get("source_url")
        outreach_drafts_by_message[message.id] = {
            "company": company.name,
            "contact": contact.full_name if contact else None,
            "to_email": attempt.to_email,
            "state": attempt.state,
            "sent_at": attempt.sent_at,
            "gmail_draft_id": attempt.gmail_draft_id,
            "evidence_source_url": source_url if isinstance(source_url, str) else None,
        }
    outreach_drafts = tuple(outreach_drafts_by_message.values())
    workbook_path = next((
        stage["detail"].get("workbook")
        for stage in reversed(stages)
        if stage["name"] == "export" and isinstance(stage["detail"], dict)
        and stage["detail"].get("workbook")
    ), None)
    collected_count = sum(source["collected"] for source in sources)
    enriched_job_count = sum(job["enrichment_status"] == "done" for job in jobs)
    return PublicDashboardSnapshot(
        run_id, run, sources, stages, counts, jobs, companies,
        latest_scope, links, resumes, outreach_drafts, workbook_path,
        collected_count, enriched_job_count,
    )


def combine_public_snapshots(
    snapshots: tuple[PublicDashboardSnapshot, ...],
) -> PublicDashboardSnapshot:
    """Combine saved searches into one deduplicated, filterable dashboard view."""
    if not snapshots:
        raise ValueError("at least one public dashboard snapshot is required")
    jobs_by_key: dict[tuple[str, str], dict] = {}
    for snapshot in snapshots:
        for source_job in snapshot.kept_jobs:
            key = (source_job["source"], source_job["external_job_id"])
            if key not in jobs_by_key:
                job = dict(source_job)
                job["search"] = (source_job["search"],)
                jobs_by_key[key] = job
                continue
            job = jobs_by_key[key]
            job["selected"] = bool(job["selected"] or source_job["selected"])
            job["search"] = tuple(dict.fromkeys((*job["search"], source_job["search"])))
    for job in jobs_by_key.values():
        if isinstance(job["search"], tuple):
            job["search"] = "; ".join(job["search"])

    companies_by_id: dict[int, dict] = {}
    for snapshot in snapshots:
        for source_company in snapshot.companies:
            company_id = source_company["company_id"]
            if company_id not in companies_by_id:
                companies_by_id[company_id] = dict(source_company)
                continue
            company = companies_by_id[company_id]
            company["selected"] = bool(company["selected"] or source_company["selected"])
            company["searches"] = tuple(dict.fromkeys(
                (*company["searches"], *source_company["searches"])
            ))
    cumulative_job_counts: dict[int, int] = {}
    for job in jobs_by_key.values():
        company_id = job.get("company_id")
        if company_id is not None:
            cumulative_job_counts[company_id] = cumulative_job_counts.get(company_id, 0) + 1
    for company in companies_by_id.values():
        company["jobs"] = cumulative_job_counts.get(company["company_id"], 0)
        company["searches"] = "; ".join(company["searches"])

    counts: dict[str, int] = {}
    for snapshot in snapshots:
        for outcome, count in snapshot.outcome_counts.items():
            counts[outcome] = counts.get(outcome, 0) + count
    links_by_url = {
        link["linkedin_url"]: link
        for snapshot in snapshots for link in snapshot.profile_links
    }
    resumes_by_version = {
        resume["posting_version_id"]: resume
        for snapshot in snapshots for resume in snapshot.resumes
    }
    return PublicDashboardSnapshot(
        run_id="all",
        run={
            "name": "All saved searches",
            "label": "All searches",
            "state": "cumulative",
            "created_at": None,
            "search_terms": (),
            "countries": (),
            "cities": (),
            "arrangements": (),
            "collection_window_days": None,
        },
        source_statuses=(),
        stage_statuses=(),
        outcome_counts=counts,
        kept_jobs=tuple(jobs_by_key.values()),
        companies=tuple(companies_by_id.values()),
        latest_scope=None,
        profile_links=tuple(links_by_url.values()),
        resumes=tuple(resumes_by_version.values()),
        outreach_drafts=(),
        workbook_path=None,
        collected_count=sum(snapshot.collected_count for snapshot in snapshots),
        enriched_job_count=sum(
            job["enrichment_status"] == "done" for job in jobs_by_key.values()
        ),
    )
