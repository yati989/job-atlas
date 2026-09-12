"""Durable evidence review for semantically relevant jobs with unclear location."""
from __future__ import annotations

import hashlib
import json

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.orm import (
    ProfiledSearchOutcome,
    PublicRoleReviewCandidate,
)
from app.models.schemas import NormalizedJob
from app.pipeline.canonical_dedup import apply_cross_source_job_dedup
from app.pipeline.upsert import upsert_job
from app.workflows.public_progress import record_public_stage


def _fingerprint(value: object) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def location_review_context(session: Session, run_id: str) -> dict:
    outcomes = list(session.scalars(select(ProfiledSearchOutcome).where(
        ProfiledSearchOutcome.run_id == run_id,
        ProfiledSearchOutcome.outcome == "needs_review",
        ProfiledSearchOutcome.first_failed_axis == "location",
    ).order_by(ProfiledSearchOutcome.id)))
    items = []
    for outcome in outcomes:
        candidate = session.scalar(select(PublicRoleReviewCandidate).where(
            PublicRoleReviewCandidate.run_id == run_id,
            PublicRoleReviewCandidate.source == outcome.source,
            PublicRoleReviewCandidate.external_job_id == outcome.external_job_id,
            PublicRoleReviewCandidate.status == "relevant",
        ))
        if candidate is None:
            raise ValueError("location review is missing its accepted role snapshot")
        snapshot = candidate.candidate_snapshot
        items.append({
            "outcome_id": outcome.id,
            "source": outcome.source,
            "title": snapshot["title"],
            "company": snapshot["company_name_raw"],
            "location": snapshot.get("location_raw"),
            "is_remote": snapshot.get("is_remote"),
            "remote_scope": snapshot.get("remote_scope"),
            "description": snapshot.get("description_raw"),
            "job_url": snapshot.get("job_url"),
        })
    body = {"run_id": run_id, "items": items}
    record_public_stage(
        session, run_id, "location_review",
        "running" if items else "completed",
        expected_count=len(items), completed_count=0 if items else len(items),
    )
    return {**body, "context_fingerprint": _fingerprint(body)}


def apply_location_review(session: Session, run_id: str, payload: dict) -> dict:
    context = location_review_context(session, run_id)
    if payload.get("context_fingerprint") != context["context_fingerprint"]:
        raise ValueError("location review context is stale or belongs to another run")
    judgments = payload.get("judgments")
    if not isinstance(judgments, list):
        raise ValueError("location review judgments must be a list")
    expected = {item["outcome_id"] for item in context["items"]}
    received = [item.get("outcome_id") for item in judgments if isinstance(item, dict)]
    if len(received) != len(set(received)) or set(received) != expected:
        raise ValueError("location review must contain exactly one judgment per outcome")

    counts = {"eligible": 0, "ineligible": 0}
    for judgment in judgments:
        decision = judgment.get("decision")
        reason = str(judgment.get("reason") or "").strip()
        if decision not in counts or not reason:
            raise ValueError("each location judgment needs eligible/ineligible and a reason")
        outcome = session.get(ProfiledSearchOutcome, judgment["outcome_id"])
        if (
            outcome is None or outcome.run_id != run_id
            or outcome.outcome != "needs_review"
            or outcome.first_failed_axis != "location"
        ):
            raise ValueError("location outcome is no longer awaiting judgment")
        candidate = session.scalar(select(PublicRoleReviewCandidate).where(
            PublicRoleReviewCandidate.run_id == run_id,
            PublicRoleReviewCandidate.source == outcome.source,
            PublicRoleReviewCandidate.external_job_id == outcome.external_job_id,
        ))
        if decision == "eligible":
            job = NormalizedJob.model_validate(candidate.candidate_snapshot)
            stored = upsert_job(session, job)
            outcome.job_id = stored.id
            outcome.posting_version_id = stored.posting_versions[-1].id
            outcome.outcome = "kept"
            outcome.first_failed_axis = None
        else:
            outcome.outcome = "rejected"
        outcome.reason = reason
        outcome.evidence = {**(outcome.evidence or {}), "location_review": reason}
        counts[decision] += 1
        session.flush()

    apply_cross_source_job_dedup(session)
    record_public_stage(
        session, run_id, "location_review", "completed",
        expected_count=len(judgments), completed_count=len(judgments),
        detail=counts,
    )
    phase_a_count = session.scalar(select(func.count(ProfiledSearchOutcome.id)).where(
        ProfiledSearchOutcome.run_id == run_id,
        (
            (ProfiledSearchOutcome.outcome == "kept")
            | (
                (ProfiledSearchOutcome.outcome == "needs_review")
                & (ProfiledSearchOutcome.first_failed_axis == "experience")
            )
        ),
    )) or 0
    record_public_stage(
        session, run_id, "phase_a", "awaiting_confirmation",
        expected_count=phase_a_count,
    )
    return {"run_id": run_id, **counts, "phase_a_count": phase_a_count}
