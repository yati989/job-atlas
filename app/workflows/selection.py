"""Immutable downstream selection scopes for the public guided pipeline."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from collections.abc import Mapping, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.orm import (
    Company, Job, JobPostingVersion, ProfiledSearchOutcome,
    PublicJobPhaseAEvidence, PublicSelectionScope, PublicSelectionScopeItem,
)


_MODES = {"all_eligible", "manual", "filtered"}
_ARRANGEMENTS = {"remote", "hybrid", "onsite"}


class SelectionFilterGroup(BaseModel):
    """One named conjunction of supported stored-evidence criteria.

    Groups are deliberately declarative rather than SQL-like.  They can be
    shown back to a user, validated before execution, and re-evaluated against
    the same persisted run without granting arbitrary database access.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1, max_length=80)
    sources: tuple[str, ...] = ()
    title_terms: tuple[str, ...] = ()
    company_names: tuple[str, ...] = ()
    arrangements: tuple[str, ...] = ()
    seniority: tuple[str, ...] = ()
    posted_on_or_after: date | None = None
    posted_on_or_before: date | None = None
    maximum_experience_required_years: float | None = Field(default=None, ge=0)
    minimum_company_wlb_rating: float | None = Field(default=None, ge=0, le=5)
    minimum_company_wlb_review_count: int | None = Field(default=None, ge=1)
    minimum_company_estimated_salary_lpa: float | None = Field(default=None, ge=0)

    @field_validator("sources", "title_terms", "company_names", "seniority")
    @classmethod
    def nonblank_unique_text(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(value.strip() for value in values)
        if any(not value for value in normalized):
            raise ValueError("values cannot be blank")
        if len({value.casefold() for value in normalized}) != len(normalized):
            raise ValueError("values must be unique")
        return normalized

    @field_validator("arrangements")
    @classmethod
    def valid_arrangements(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(value.strip().lower() for value in values)
        invalid = sorted(set(normalized) - _ARRANGEMENTS)
        if invalid:
            raise ValueError(f"unsupported work arrangements: {', '.join(invalid)}")
        if len(set(normalized)) != len(normalized):
            raise ValueError("arrangements must be unique")
        return normalized

    @model_validator(mode="after")
    def ordered_dates(self) -> "SelectionFilterGroup":
        if (
            self.posted_on_or_after is not None
            and self.posted_on_or_before is not None
            and self.posted_on_or_after > self.posted_on_or_before
        ):
            raise ValueError("posted_on_or_after cannot be after posted_on_or_before")
        if (
            self.minimum_company_wlb_rating is None
            and self.minimum_company_wlb_review_count is not None
        ):
            raise ValueError(
                "minimum_company_wlb_review_count requires minimum_company_wlb_rating"
            )
        if (
            self.minimum_company_wlb_rating is not None
            and self.minimum_company_wlb_review_count is None
        ):
            raise ValueError(
                "minimum_company_wlb_rating requires minimum_company_wlb_review_count"
            )
        return self


class SelectionExclusions(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    posting_version_ids: tuple[int, ...] = ()
    company_ids: tuple[int, ...] = ()
    sources: tuple[str, ...] = ()

    @field_validator("posting_version_ids", "company_ids")
    @classmethod
    def positive_unique_ids(cls, values: tuple[int, ...]) -> tuple[int, ...]:
        if any(value <= 0 for value in values):
            raise ValueError("IDs must be positive")
        if len(set(values)) != len(values):
            raise ValueError("IDs must be unique")
        return values

    @field_validator("sources")
    @classmethod
    def nonblank_unique_sources(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(value.strip() for value in values)
        if any(not value for value in normalized):
            raise ValueError("sources cannot be blank")
        if len({value.casefold() for value in normalized}) != len(normalized):
            raise ValueError("sources must be unique")
        return normalized


class SelectionFilterConfig(BaseModel):
    """Validated saved-filter contract for one immutable selection revision."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: int = Field(default=1, ge=1, le=1)
    groups: tuple[SelectionFilterGroup, ...] = ()
    explicit_posting_version_ids: tuple[int, ...] = ()
    exclusions: SelectionExclusions = Field(default_factory=SelectionExclusions)

    @field_validator("explicit_posting_version_ids")
    @classmethod
    def positive_unique_explicit_ids(cls, values: tuple[int, ...]) -> tuple[int, ...]:
        if any(value <= 0 for value in values):
            raise ValueError("posting version IDs must be positive")
        if len(set(values)) != len(values):
            raise ValueError("posting version IDs must be unique")
        return values

    @model_validator(mode="after")
    def has_a_selection_rule(self) -> "SelectionFilterConfig":
        if not self.groups and not self.explicit_posting_version_ids:
            raise ValueError("filters require at least one group or explicit posting version")
        names = [group.name.casefold() for group in self.groups]
        if len(set(names)) != len(names):
            raise ValueError("group names must be unique")
        return self


@dataclass(frozen=True)
class SelectionPreviewItem:
    posting_version_id: int
    company_id: int
    outcome: str
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class FilteredSelectionPreview:
    """Reviewable result of applying a filter contract to one stored run."""

    run_id: str
    filter_snapshot: dict[str, object]
    items: tuple[SelectionPreviewItem, ...]

    @property
    def selected_posting_version_ids(self) -> tuple[int, ...]:
        return tuple(item.posting_version_id for item in self.items if item.outcome == "retained")

    @property
    def company_ids(self) -> dict[int, int]:
        return {
            item.posting_version_id: item.company_id
            for item in self.items if item.outcome == "retained"
        }

    @property
    def retained_job_count(self) -> int:
        return len(self.selected_posting_version_ids)

    @property
    def retained_company_count(self) -> int:
        return len(set(self.company_ids.values()))

    @property
    def excluded_job_count(self) -> int:
        return sum(item.outcome == "excluded" for item in self.items)

    @property
    def unresolved_job_count(self) -> int:
        return sum(item.outcome == "unresolved" for item in self.items)


def _arrangement(job: Job) -> str:
    location = " ".join(filter(None, (job.location_raw, job.remote_scope))).casefold()
    if "hybrid" in location:
        return "hybrid"
    if job.is_remote or "remote" in location:
        return "remote"
    return "onsite"


def _group_match_reasons(
    group: SelectionFilterGroup, job: Job, company: Company | None,
) -> tuple[bool, tuple[str, ...], bool]:
    """Return match, failed criteria, and whether a failed criterion is unknown."""
    failures: list[str] = []
    unknown_failures: list[str] = []
    title = job.title.casefold()
    if group.sources and job.source.casefold() not in {value.casefold() for value in group.sources}:
        failures.append("source")
    if group.title_terms and not any(value.casefold() in title for value in group.title_terms):
        failures.append("title_terms")
    if group.company_names:
        name = (company.name if company is not None else job.company_name_raw or "").casefold()
        if not any(value.casefold() in name for value in group.company_names):
            failures.append("company_names")
    if group.arrangements and _arrangement(job) not in group.arrangements:
        failures.append("arrangements")
    if group.seniority:
        if not (job.seniority or "").strip():
            failures.append("seniority")
            unknown_failures.append("seniority")
        elif job.seniority.casefold() not in {
            value.casefold() for value in group.seniority
        }:
            failures.append("seniority")
    posted = job.posted_at.date() if job.posted_at is not None else None
    if group.posted_on_or_after is not None:
        if posted is None:
            failures.append("posted_on_or_after")
            unknown_failures.append("posted_on_or_after")
        elif posted < group.posted_on_or_after:
            failures.append("posted_on_or_after")
    if group.posted_on_or_before is not None:
        if posted is None:
            failures.append("posted_on_or_before")
            unknown_failures.append("posted_on_or_before")
        elif posted > group.posted_on_or_before:
            failures.append("posted_on_or_before")
    if group.maximum_experience_required_years is not None:
        if job.experience_min_years is None:
            failures.append("maximum_experience_required_years")
            unknown_failures.append("maximum_experience_required_years")
        elif job.experience_min_years > group.maximum_experience_required_years:
            failures.append("maximum_experience_required_years")
    if group.minimum_company_wlb_rating is not None:
        rating = company.wlb_rating if company is not None else None
        if rating is None:
            failures.append("minimum_company_wlb_rating")
            unknown_failures.append("minimum_company_wlb_rating")
        elif rating < group.minimum_company_wlb_rating:
            failures.append("minimum_company_wlb_rating")
        review_count = company.glassdoor_review_count if company is not None else None
        if review_count is None or review_count < group.minimum_company_wlb_review_count:
            failures.append("minimum_company_wlb_review_count")
            unknown_failures.append("minimum_company_wlb_review_count")
    if group.minimum_company_estimated_salary_lpa is not None:
        salary = company.estimated_salary_lpa if company is not None else None
        if salary is None:
            failures.append("minimum_company_estimated_salary_lpa")
            unknown_failures.append("minimum_company_estimated_salary_lpa")
        elif salary < group.minimum_company_estimated_salary_lpa:
            failures.append("minimum_company_estimated_salary_lpa")
    # A missing value is unresolved only when it is the sole reason this group
    # did not match.  A known mismatch (for example an onsite role in a remote
    # group) stays excluded even if an unrelated threshold is also unknown.
    return not failures, tuple(failures), bool(unknown_failures) and len(unknown_failures) == len(failures)


def preview_filtered_selection(
    session: Session, *, run_id: str, configuration: SelectionFilterConfig | Mapping[str, object],
) -> FilteredSelectionPreview:
    """Evaluate a filter only against kept posting versions from the exact run.

    Groups form an OR; each group's criteria form an AND.  Explicit posting
    picks are another inclusion route, but exclusions always win.  Missing
    evidence for an otherwise viable threshold is surfaced as ``unresolved``.
    """
    config = (
        configuration
        if isinstance(configuration, SelectionFilterConfig)
        else SelectionFilterConfig.model_validate(configuration)
    )
    rows = session.execute(
        select(JobPostingVersion, Job, Company)
        .join(Job, JobPostingVersion.job_id == Job.id)
        .outerjoin(Company, Job.company_id == Company.id)
        .join(
            ProfiledSearchOutcome,
            ProfiledSearchOutcome.posting_version_id == JobPostingVersion.id,
        )
        .where(
            ProfiledSearchOutcome.run_id == run_id,
            ProfiledSearchOutcome.outcome == "kept",
            Job.duplicate_of_job_id.is_(None),
        )
        .order_by(JobPostingVersion.id)
    ).all()
    explicit = set(config.explicit_posting_version_ids)
    excluded_versions = set(config.exclusions.posting_version_ids)
    excluded_companies = set(config.exclusions.company_ids)
    excluded_sources = {source.casefold() for source in config.exclusions.sources}
    available_posting_versions = {posting.id for posting, _job, _company in rows}
    unknown_explicit = sorted(explicit - available_posting_versions)
    if unknown_explicit:
        raise ValueError(
            "explicit posting versions are not kept results for run "
            f"{run_id}: {unknown_explicit}"
        )
    unknown_excluded = sorted(excluded_versions - available_posting_versions)
    if unknown_excluded:
        raise ValueError(
            "excluded posting versions are not kept results for run "
            f"{run_id}: {unknown_excluded}"
        )
    items: list[SelectionPreviewItem] = []
    for posting, job, company in rows:
        company_id = job.company_id
        if company_id is None:
            # A kept job without a canonical company cannot be a downstream
            # company scope.  It remains visible rather than being fabricated.
            items.append(SelectionPreviewItem(posting.id, 0, "unresolved", ("company_identity",)))
            continue
        exclusion_reasons = []
        if posting.id in excluded_versions:
            exclusion_reasons.append("posting_version")
        if company_id in excluded_companies:
            exclusion_reasons.append("company")
        if job.source.casefold() in excluded_sources:
            exclusion_reasons.append("source")
        if exclusion_reasons:
            items.append(SelectionPreviewItem(posting.id, company_id, "excluded", tuple(exclusion_reasons)))
            continue
        if posting.id in explicit:
            items.append(SelectionPreviewItem(posting.id, company_id, "retained", ("explicit_pick",)))
            continue
        matches: list[str] = []
        unknown_reasons: list[str] = []
        for group in config.groups:
            matched, failures, unknown = _group_match_reasons(group, job, company)
            if matched:
                matches.append(group.name)
            elif unknown:
                unknown_reasons.extend(f"{group.name}:{reason}" for reason in failures)
        if matches:
            items.append(SelectionPreviewItem(posting.id, company_id, "retained", tuple(matches)))
        elif unknown_reasons:
            items.append(SelectionPreviewItem(posting.id, company_id, "unresolved", tuple(unknown_reasons)))
        else:
            items.append(SelectionPreviewItem(posting.id, company_id, "excluded", ("no_matching_group",)))
    return FilteredSelectionPreview(
        run_id=run_id,
        filter_snapshot=config.model_dump(mode="json"),
        items=tuple(items),
    )


def freeze_filtered_selection_scope(
    session: Session, *, run_id: str, configuration: SelectionFilterConfig | Mapping[str, object],
) -> PublicSelectionScope:
    """Freeze the currently resolved kept-only manifest for a saved filter."""
    preview = preview_filtered_selection(session, run_id=run_id, configuration=configuration)
    return freeze_selection_scope(
        session,
        run_id=run_id,
        mode="filtered",
        posting_version_ids=preview.selected_posting_version_ids,
        company_ids=preview.company_ids,
        filter_snapshot=preview.filter_snapshot,
    )


def freeze_selection_scope(
    session: Session,
    *,
    run_id: str,
    mode: str,
    posting_version_ids: Sequence[int],
    company_ids: Mapping[int, int],
    filter_snapshot: Mapping[str, object] | None = None,
    purpose: str = "selection",
) -> PublicSelectionScope:
    """Create or reuse an exact posting-version/company scope revision."""
    if mode not in _MODES:
        raise ValueError(f"unsupported selection mode: {mode}")
    if purpose not in {"selection", "phase_b_research"}:
        raise ValueError(f"unsupported scope purpose: {purpose}")
    if purpose == "phase_b_research" and mode != "all_eligible":
        raise ValueError("Phase B research scope must use all_eligible mode")
    ordered = tuple(sorted(set(posting_version_ids)))
    missing = [posting_id for posting_id in ordered if posting_id not in company_ids]
    if missing:
        raise ValueError(f"company identity is missing for posting versions: {missing}")
    for posting_id in ordered:
        outcome = session.scalar(select(ProfiledSearchOutcome).where(
            ProfiledSearchOutcome.run_id == run_id,
            ProfiledSearchOutcome.posting_version_id == posting_id,
            ProfiledSearchOutcome.outcome == "kept",
        ))
        if outcome is None:
            raise ValueError(
                f"posting version {posting_id} is not an eligible kept result for run {run_id}"
            )
        actual_company = session.execute(
            select(Job.company_id, Job.duplicate_of_job_id)
            .join(JobPostingVersion, JobPostingVersion.job_id == Job.id)
            .where(JobPostingVersion.id == posting_id)
        ).one_or_none()
        if actual_company is None or actual_company.duplicate_of_job_id is not None:
            raise ValueError(
                f"posting version {posting_id} is not a canonical eligible result"
            )
        actual_company_id = actual_company.company_id
        if actual_company_id is None or actual_company_id != company_ids[posting_id]:
            raise ValueError(
                f"posting version {posting_id} does not belong to company "
                f"{company_ids[posting_id]}"
            )
    phase_a_versions = set(session.scalars(select(
        PublicJobPhaseAEvidence.posting_version_id
    ).where(PublicJobPhaseAEvidence.posting_version_id.in_(ordered))))
    missing_job_evidence = sorted(set(ordered) - phase_a_versions)
    if missing_job_evidence:
        raise ValueError(
            "Phase A job evidence is incomplete for posting versions: "
            f"{missing_job_evidence}"
        )
    selected_company_ids = {company_ids[posting_id] for posting_id in ordered}
    company_newest_posting = dict(session.execute(
        select(Job.company_id, func.max(Job.last_seen_at))
        .join(JobPostingVersion, JobPostingVersion.job_id == Job.id)
        .where(JobPostingVersion.id.in_(ordered))
        .group_by(Job.company_id)
    ).all())
    companies = {
        company.id: company
        for company in session.scalars(select(Company).where(Company.id.in_(selected_company_ids)))
    }
    missing_company_evidence = sorted(
        company_id
        for company_id in selected_company_ids
        if (
            (company := companies.get(company_id)) is None
            or company.enrichment_status != "done"
            or company.contact_search_groups is None
            or company.enriched_at is None
            or (
                company_newest_posting.get(company_id) is not None
                and company.enriched_at < company_newest_posting[company_id]
            )
        )
    )
    if missing_company_evidence:
        raise ValueError(
            "Phase A company classification is incomplete for companies: "
            f"{missing_company_evidence}"
        )
    if mode == "filtered" and filter_snapshot is None:
        raise ValueError("filtered selection requires a saved filter snapshot")
    if mode != "filtered" and filter_snapshot is not None:
        raise ValueError("only filtered selections can include a filter snapshot")
    snapshot = {
        "mode": mode,
        "purpose": purpose,
        "items": [[posting_id, company_ids[posting_id]] for posting_id in ordered],
        "filter": filter_snapshot,
    }
    fingerprint = hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    existing = session.scalar(select(PublicSelectionScope).where(
        PublicSelectionScope.run_id == run_id,
        PublicSelectionScope.fingerprint == fingerprint,
    ))
    if existing is not None:
        return existing

    revision = (session.scalar(select(func.max(PublicSelectionScope.revision)).where(
        PublicSelectionScope.run_id == run_id,
    )) or 0) + 1
    scope = PublicSelectionScope(
        run_id=run_id,
        revision=revision,
        mode=mode,
        purpose=purpose,
        fingerprint=fingerprint,
        filter_snapshot=dict(filter_snapshot) if filter_snapshot is not None else None,
        job_count=len(ordered),
        company_count=len({company_ids[posting_id] for posting_id in ordered}),
    )
    session.add(scope)
    session.flush()
    session.add_all([
        PublicSelectionScopeItem(
            scope_id=scope.id,
            posting_version_id=posting_id,
            company_id=company_ids[posting_id],
        )
        for posting_id in ordered
    ])
    session.flush()
    return scope
