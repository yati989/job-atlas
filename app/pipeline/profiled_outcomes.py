"""Persist profile-based relevance outcomes at the canonical-storage seam."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.orm import ProfiledSearchOutcome
from app.models.schemas import NormalizedJob
from app.pipeline.relevance import (
    RelevancePolicy,
    evaluate_after_semantic_role,
    evaluate_relevance,
)
from app.pipeline.upsert import upsert_job


@dataclass(frozen=True)
class ProfiledOutcomeResult:
    counts: dict[str, int]


def _evidence(job: NormalizedJob) -> dict:
    """Return a compact decision snapshot without retaining provider payloads."""
    return {
        "title": job.title,
        "company_name_raw": job.company_name_raw,
        "location_raw": job.location_raw,
        "is_remote": job.is_remote,
        "remote_scope": job.remote_scope,
        "seniority": job.seniority,
        "posted_at": job.posted_at.isoformat() if job.posted_at else None,
    }


def record_profiled_outcomes(
    session: Session,
    *,
    run_id: str,
    profile_fingerprint: str,
    policy: RelevancePolicy,
    jobs: Iterable[NormalizedJob],
    role_preapproved: bool = False,
    recency_cutoff: datetime | None = None,
) -> ProfiledOutcomeResult:
    """Classify deduplicated observations and persist one immutable run result.

    Only kept observations cross into the canonical ``jobs`` table. Repeating
    the same accepted profile/run is safe; changing the profile behind an
    existing run is rejected rather than silently rewriting its evidence.
    """
    counts = {"kept": 0, "rejected": 0, "needs_review": 0}
    for item in jobs:
        decision = (
            evaluate_after_semantic_role(item, policy, cutoff_at=recency_cutoff)
            if role_preapproved
            else evaluate_relevance(item, policy, cutoff_at=recency_cutoff)
        )
        counts[decision.outcome] += 1
        existing = session.scalar(
            select(ProfiledSearchOutcome).where(
                ProfiledSearchOutcome.run_id == run_id,
                ProfiledSearchOutcome.source == item.source,
                ProfiledSearchOutcome.external_job_id == item.external_job_id,
            )
        )
        if existing is not None:
            if existing.profile_fingerprint != profile_fingerprint:
                raise ValueError(
                    "cannot record a different profile against an existing run outcome"
                )
            if existing.outcome != decision.outcome:
                raise ValueError(
                    "relevance outcome changed while retrying an immutable run"
                )
            continue

        stored_job = None
        posting_version_id = None
        if decision.outcome == "kept" or (
            decision.outcome == "needs_review" and decision.axis == "experience"
        ):
            stored_job = upsert_job(session, item)
            posting_version_id = stored_job.posting_versions[-1].id

        session.add(
            ProfiledSearchOutcome(
                run_id=run_id,
                source=item.source,
                external_job_id=item.external_job_id,
                outcome=decision.outcome,
                first_failed_axis=decision.axis,
                reason=decision.reason,
                profile_fingerprint=profile_fingerprint,
                job_url=item.job_url,
                apply_url=item.apply_url,
                evidence=_evidence(item),
                job_id=stored_job.id if stored_job else None,
                posting_version_id=posting_version_id,
            )
        )
        session.flush()
    return ProfiledOutcomeResult(counts=counts)
