"""Durable start/resume coordinator for the public guided collection stage."""
from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.orm import (
    DecisionRun,
    GuidedRunPlan,
    GuidedSourceAttempt,
    GuidedSourceRun,
    ProfiledSearchOutcome,
)
from app.models.schemas import NormalizedJob
from app.pipeline.profiled_outcomes import record_profiled_outcomes
from app.pipeline.canonical_dedup import apply_cross_source_job_dedup
from app.pipeline.relevance import RelevancePolicy
from app.pipeline.runner import deduplicate_normalized_jobs
from app.workflows.planning import RunPreview, SearchProfile
from app.workflows.public_progress import initialize_public_stages, record_public_stage
from app.workflows.public_role_review import (
    apply_role_review_prefilters,
    record_role_review_candidates,
    refresh_role_review_progress,
)


FetchSource = Callable[[SearchProfile], Sequence[NormalizedJob]]


@dataclass(frozen=True)
class StartedGuidedRun:
    run_id: str
    state: str


@dataclass(frozen=True)
class GuidedCollectionResult:
    run_id: str
    state: str
    source_counts: dict[str, int]
    canonical_dedup: dict[str, int]


def start_guided_run(
    session: Session,
    *,
    preview: RunPreview,
    profile_fingerprint: str,
    confirmed: bool,
    now: datetime | None = None,
    run_id: str | None = None,
) -> StartedGuidedRun:
    """Freeze the reviewed preview; source execution cannot expand it."""
    if not confirmed:
        raise ValueError("explicit preview confirmation is required")
    now = now or datetime.now(timezone.utc)
    run_id = run_id or uuid.uuid4().hex
    existing = session.get(DecisionRun, run_id)
    if existing is not None:
        plan = session.scalar(select(GuidedRunPlan).where(GuidedRunPlan.run_id == run_id))
        if plan is None or plan.profile_fingerprint != profile_fingerprint:
            raise ValueError("run id already belongs to a different plan")
        return StartedGuidedRun(run_id, existing.state)

    profile = preview.profile
    run = DecisionRun(
        id=run_id,
        since_at=now - timedelta(days=profile.collection_window_days),
        cutoff_at=now,
        input_timezone="UTC",
        state="collecting",
        policy_snapshot=profile.model_dump(mode="json"),
        target_count=0,
        prepared_at=now,
        approved_at=now,
        telemetry_version="public-guided-v1",
    )
    session.add(run)
    session.flush()
    session.add(GuidedRunPlan(
        run_id=run_id,
        profile_fingerprint=profile_fingerprint,
        profile_snapshot=profile.model_dump(mode="json"),
        selected_sources=[source.name for source in preview.selected_sources],
        source_plan=[asdict(source) for source in preview.selected_sources],
        source_count=preview.source_count,
        query_instance_count=preview.query_instance_count,
    ))
    session.add_all([
        GuidedSourceRun(run_id=run_id, source=source.name, status="pending")
        for source in preview.selected_sources
    ])
    initialize_public_stages(session, run_id)
    session.flush()
    return StartedGuidedRun(run_id, run.state)


def collect_guided_run(
    session: Session,
    *,
    run_id: str,
    fetchers: Mapping[str, FetchSource],
    semantic_role_review: bool = False,
) -> GuidedCollectionResult:
    """Run only frozen sources and retry only incomplete ones on resume."""
    run = session.get(DecisionRun, run_id)
    plan = session.scalar(select(GuidedRunPlan).where(GuidedRunPlan.run_id == run_id))
    if run is None or plan is None:
        raise ValueError(f"unknown guided run: {run_id}")
    profile = SearchProfile.model_validate(plan.profile_snapshot)
    policy = RelevancePolicy.from_profile(profile)
    rows = list(session.scalars(
        select(GuidedSourceRun)
        .where(GuidedSourceRun.run_id == run_id)
        .order_by(GuidedSourceRun.id)
    ))
    for source_run in rows:
        if source_run.status == "completed":
            continue
        fetcher = fetchers.get(source_run.source)
        if fetcher is None:
            raise ValueError(f"no fetcher configured for selected source {source_run.source}")
        source_run.attempt_count += 1
        source_run.status = "running"
        source_run.started_at = datetime.now(timezone.utc)
        source_run.failure_detail = None
        attempt = GuidedSourceAttempt(
            source_run_id=source_run.id,
            attempt_number=source_run.attempt_count,
            status="running",
            started_at=source_run.started_at,
        )
        session.add(attempt)
        session.flush()
        # Make the running transition visible to dashboard readers before the
        # potentially long network/browser fetch begins.
        session.commit()
        try:
            jobs, _duplicate_count = deduplicate_normalized_jobs(list(fetcher(profile)))
            wrong_source = next((job.source for job in jobs if job.source != source_run.source), None)
            if wrong_source is not None:
                raise ValueError(
                    f"fetcher {source_run.source} returned observation for {wrong_source}"
                )
            with session.begin_nested():
                if semantic_role_review:
                    role_counts = record_role_review_candidates(
                        session,
                        run_id=run_id,
                        profile_fingerprint=plan.profile_fingerprint,
                        policy=policy,
                        recency_cutoff=run.since_at,
                        jobs=jobs,
                    )
                    source_run.outcome_counts = {
                        "kept": 0,
                        "rejected": role_counts["rejected"],
                        "needs_review": 0,
                        "awaiting_role_review": role_counts["queued"],
                    }
                else:
                    result = record_profiled_outcomes(
                        session,
                        run_id=run_id,
                        profile_fingerprint=plan.profile_fingerprint,
                        policy=policy,
                        jobs=jobs,
                        recency_cutoff=run.since_at,
                    )
                    source_run.outcome_counts = result.counts
            source_run.status = "completed"
            source_run.completed_at = datetime.now(timezone.utc)
            attempt.status = "completed"
            attempt.completed_at = source_run.completed_at
        except Exception as exc:
            source_run.status = "failed"
            source_run.failure_detail = str(exc)
            source_run.completed_at = datetime.now(timezone.utc)
            attempt.status = "failed"
            attempt.failure_detail = str(exc)
            attempt.completed_at = source_run.completed_at
        # Each source is a durable progress boundary. This keeps completed or
        # failed work visible during the rest of collection and resumable if a
        # later attended source pauses or the process stops.
        session.commit()

    statuses = [row.status for row in rows]
    state = "collected" if statuses and all(value == "completed" for value in statuses) else "partial"
    if not statuses:
        state = "collected"
    run.state = state
    canonical_dedup = (
        {"scanned": 0, "flagged": 0, "review_candidates": 0, "skipped_groups": 0}
        if semantic_role_review
        else apply_cross_source_job_dedup(session)
    )
    if state == "collected":
        if semantic_role_review:
            apply_role_review_prefilters(
                session, run_id, policy, run.since_at,
            )
            refresh_role_review_progress(session, run_id)
        else:
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
    session.commit()
    source_counts = {
        status: statuses.count(status)
        for status in ("completed", "failed", "pending", "running")
        if statuses.count(status)
    }
    return GuidedCollectionResult(run_id, state, source_counts, canonical_dedup)
