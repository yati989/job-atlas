"""Durable title-first role review for the guided public pipeline."""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.orm import (
    DecisionRun,
    GuidedRunPlan,
    GuidedSourceRun,
    ProfiledSearchOutcome,
    PublicRoleReviewCandidate,
)
from app.models.schemas import NormalizedJob
from app.pipeline.canonical_dedup import apply_cross_source_job_dedup
from app.pipeline.profiled_outcomes import record_profiled_outcomes
from app.pipeline.relevance import RelevancePolicy, evaluate_after_semantic_role
from app.workflows.planning import SearchProfile
from app.workflows.public_progress import record_public_stage


def _json_fingerprint(value: object) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _candidate_snapshot(job: NormalizedJob) -> dict:
    snapshot = job.model_dump(mode="json")
    snapshot["raw_payload"] = {}
    return snapshot


def _compact_evidence(job: NormalizedJob) -> dict:
    return {
        "title": job.title,
        "company_name_raw": job.company_name_raw,
        "location_raw": job.location_raw,
        "is_remote": job.is_remote,
        "remote_scope": job.remote_scope,
        "employment_type": job.employment_type,
        "seniority": job.seniority,
        "posted_at": job.posted_at.isoformat() if job.posted_at else None,
    }


def record_role_review_candidates(
    session: Session,
    *,
    run_id: str,
    profile_fingerprint: str,
    policy: RelevancePolicy,
    recency_cutoff: datetime,
    jobs: list[NormalizedJob],
) -> dict[str, int]:
    """Reject clear factual mismatches, then queue plausible roles."""
    recorded = 0
    rejected = 0
    for job in jobs:
        factual = evaluate_after_semantic_role(
            job, policy, cutoff_at=recency_cutoff,
        )
        if factual.outcome == "rejected":
            result = record_profiled_outcomes(
                session,
                run_id=run_id,
                profile_fingerprint=profile_fingerprint,
                policy=policy,
                jobs=[job],
                role_preapproved=True,
                recency_cutoff=recency_cutoff,
            )
            rejected += result.counts["rejected"]
            continue
        snapshot = _candidate_snapshot(job)
        existing = session.scalar(select(PublicRoleReviewCandidate).where(
            PublicRoleReviewCandidate.run_id == run_id,
            PublicRoleReviewCandidate.source == job.source,
            PublicRoleReviewCandidate.external_job_id == job.external_job_id,
        ))
        if existing is not None:
            if existing.profile_fingerprint != profile_fingerprint:
                raise ValueError("role candidate belongs to a different profile")
            if existing.candidate_snapshot != snapshot:
                raise ValueError("role candidate changed while retrying an immutable run")
            continue
        session.add(PublicRoleReviewCandidate(
            run_id=run_id,
            source=job.source,
            external_job_id=job.external_job_id,
            profile_fingerprint=profile_fingerprint,
            candidate_snapshot=snapshot,
            status="pending",
        ))
        session.flush()
        recorded += 1
    return {"queued": recorded, "rejected": rejected}


def _context_body(session: Session, run_id: str, mode: str) -> dict:
    if mode not in {"titles", "details"}:
        raise ValueError("role review mode must be 'titles' or 'details'")
    run = session.get(DecisionRun, run_id)
    plan = session.scalar(select(GuidedRunPlan).where(GuidedRunPlan.run_id == run_id))
    if run is None or plan is None:
        raise ValueError(f"unknown guided run: {run_id}")
    if run.state != "collected":
        raise ValueError("role review requires completed source collection")
    profile = SearchProfile.model_validate(plan.profile_snapshot)
    status = "pending" if mode == "titles" else "uncertain"
    rows = list(session.scalars(
        select(PublicRoleReviewCandidate)
        .where(
            PublicRoleReviewCandidate.run_id == run_id,
            PublicRoleReviewCandidate.status == status,
        )
        .order_by(PublicRoleReviewCandidate.id)
    ))
    candidates = []
    for row in rows:
        snapshot = row.candidate_snapshot
        item = {
            "candidate_id": row.id,
            "title": snapshot["title"],
            "company": snapshot["company_name_raw"],
            "location": snapshot.get("location_raw"),
            "is_remote": snapshot.get("is_remote"),
            "remote_scope": snapshot.get("remote_scope"),
            "employment_type": snapshot.get("employment_type"),
            "seniority": snapshot.get("seniority"),
            "description_available": bool(snapshot.get("description_raw")),
        }
        if mode == "details":
            item["description"] = snapshot.get("description_raw")
        candidates.append(item)
    return {
        "run_id": run_id,
        "mode": mode,
        "profile_fingerprint": plan.profile_fingerprint,
        "intent": {
            "professions": list(profile.professions),
            "search_terms": list(profile.search_terms),
            "countries": list(profile.countries),
            "arrangements": list(profile.arrangements),
            "include_internships": profile.include_internships,
            "include_part_time": profile.include_part_time,
            "hard_rejects": list(profile.hard_rejects),
        },
        "candidates": candidates,
    }


def role_review_context(session: Session, run_id: str, mode: str = "titles") -> dict:
    """Return a frozen compact title batch or descriptions for uncertain rows."""
    body = _context_body(session, run_id, mode)
    return {**body, "context_fingerprint": _json_fingerprint(body)}


def _record_irrelevant(
    session: Session,
    candidate: PublicRoleReviewCandidate,
    reason: str,
) -> None:
    existing = session.scalar(select(ProfiledSearchOutcome).where(
        ProfiledSearchOutcome.run_id == candidate.run_id,
        ProfiledSearchOutcome.source == candidate.source,
        ProfiledSearchOutcome.external_job_id == candidate.external_job_id,
    ))
    if existing is not None:
        raise ValueError("role candidate already has a terminal outcome")
    job = NormalizedJob.model_validate(candidate.candidate_snapshot)
    session.add(ProfiledSearchOutcome(
        run_id=candidate.run_id,
        source=candidate.source,
        external_job_id=candidate.external_job_id,
        outcome="rejected",
        first_failed_axis="role",
        reason=reason,
        profile_fingerprint=candidate.profile_fingerprint,
        job_url=job.job_url,
        apply_url=job.apply_url,
        evidence=_compact_evidence(job),
    ))


def apply_role_review_prefilters(
    session: Session, run_id: str, policy: RelevancePolicy,
    recency_cutoff: datetime,
) -> int:
    """Reconcile pending rows collected before or outside the factual prefilter."""
    rejected = 0
    for candidate in session.scalars(select(PublicRoleReviewCandidate).where(
        PublicRoleReviewCandidate.run_id == run_id,
        PublicRoleReviewCandidate.status == "pending",
    )):
        job = NormalizedJob.model_validate(candidate.candidate_snapshot)
        decision = evaluate_after_semantic_role(
            job, policy, cutoff_at=recency_cutoff,
        )
        if decision.outcome != "rejected":
            continue
        record_profiled_outcomes(
            session,
            run_id=run_id,
            profile_fingerprint=candidate.profile_fingerprint,
            policy=policy,
            jobs=[job],
            role_preapproved=True,
            recency_cutoff=recency_cutoff,
        )
        candidate.status = "irrelevant"
        candidate.reason = f"factual prefilter: {decision.reason}"
        candidate.reviewed_at = datetime.now(timezone.utc)
        rejected += 1
        session.flush()
    return rejected


def refresh_role_review_progress(session: Session, run_id: str) -> dict:
    candidates = list(session.scalars(select(PublicRoleReviewCandidate).where(
        PublicRoleReviewCandidate.run_id == run_id,
    )))
    candidate_counts = Counter(row.status for row in candidates)
    outcomes = list(session.scalars(select(ProfiledSearchOutcome).where(
        ProfiledSearchOutcome.run_id == run_id,
    )))
    for source_run in session.scalars(select(GuidedSourceRun).where(
        GuidedSourceRun.run_id == run_id,
    )):
        source_counts = Counter(
            row.outcome for row in outcomes if row.source == source_run.source
        )
        awaiting = sum(
            1 for row in candidates
            if row.source == source_run.source and row.status in {"pending", "uncertain"}
        )
        source_run.outcome_counts = {
            "kept": source_counts["kept"],
            "rejected": source_counts["rejected"],
            "needs_review": source_counts["needs_review"],
            "awaiting_role_review": awaiting,
        }

    unresolved = candidate_counts["pending"] + candidate_counts["uncertain"]
    detail = {
        "pending_titles": candidate_counts["pending"],
        "uncertain_details": candidate_counts["uncertain"],
        "relevant": candidate_counts["relevant"],
        "irrelevant": candidate_counts["irrelevant"],
    }
    if unresolved:
        record_public_stage(
            session, run_id, "role_review", "awaiting_confirmation",
            expected_count=len(candidates),
            completed_count=len(candidates) - unresolved,
            detail=detail,
        )
    else:
        record_public_stage(
            session, run_id, "role_review", "completed",
            expected_count=len(candidates), completed_count=len(candidates), detail=detail,
        )
        location_review_count = session.scalar(select(func.count(ProfiledSearchOutcome.id)).where(
            ProfiledSearchOutcome.run_id == run_id,
            ProfiledSearchOutcome.outcome == "needs_review",
            ProfiledSearchOutcome.first_failed_axis == "location",
        )) or 0
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
        if location_review_count:
            record_public_stage(
                session, run_id, "location_review", "awaiting_confirmation",
                expected_count=location_review_count,
            )
            record_public_stage(session, run_id, "phase_a", "not_requested")
        else:
            record_public_stage(
                session, run_id, "location_review", "completed",
                expected_count=0, completed_count=0,
            )
            record_public_stage(
                session, run_id, "phase_a", "awaiting_confirmation",
                expected_count=phase_a_count,
            )
        apply_cross_source_job_dedup(session)
    session.flush()
    return {"unresolved": unresolved, **detail}


def apply_role_review(session: Session, run_id: str, payload: dict) -> dict:
    """Apply one complete, fingerprint-bound agent judgment batch."""
    mode = payload.get("mode")
    context = role_review_context(session, run_id, mode)
    if payload.get("context_fingerprint") != context["context_fingerprint"]:
        raise ValueError("role review context is stale or belongs to another run")
    judgments = payload.get("judgments")
    if not isinstance(judgments, list):
        raise ValueError("role review judgments must be a list")
    expected_ids = {item["candidate_id"] for item in context["candidates"]}
    received_ids = [item.get("candidate_id") for item in judgments if isinstance(item, dict)]
    if len(received_ids) != len(set(received_ids)) or set(received_ids) != expected_ids:
        raise ValueError("role review must contain exactly one judgment per candidate")

    allowed = {"relevant", "irrelevant", "uncertain"} if mode == "titles" else {
        "relevant", "irrelevant",
    }
    plan = session.scalar(select(GuidedRunPlan).where(GuidedRunPlan.run_id == run_id))
    run = session.get(DecisionRun, run_id)
    profile = SearchProfile.model_validate(plan.profile_snapshot)
    policy = RelevancePolicy.from_profile(profile)
    current_status = "pending" if mode == "titles" else "uncertain"
    for judgment in judgments:
        decision = judgment.get("decision")
        reason = str(judgment.get("reason") or "").strip()
        if decision not in allowed or not reason:
            raise ValueError("each role judgment needs an allowed decision and reason")
        candidate = session.get(PublicRoleReviewCandidate, judgment["candidate_id"])
        if candidate is None or candidate.run_id != run_id or candidate.status != current_status:
            raise ValueError("role review candidate is no longer awaiting this judgment")
        if decision == "uncertain":
            candidate.status = "uncertain"
            candidate.reason = reason
            continue
        job = NormalizedJob.model_validate(candidate.candidate_snapshot)
        if decision == "relevant":
            record_profiled_outcomes(
                session,
                run_id=run_id,
                profile_fingerprint=candidate.profile_fingerprint,
                policy=policy,
                jobs=[job],
                role_preapproved=True,
                recency_cutoff=run.since_at,
            )
        else:
            _record_irrelevant(session, candidate, reason)
        candidate.status = decision
        candidate.reason = reason
        candidate.reviewed_at = datetime.now(timezone.utc)
        session.flush()

    progress = refresh_role_review_progress(session, run_id)
    return {
        "run_id": run_id,
        "mode": mode,
        "judgments_recorded": len(judgments),
        **progress,
    }
