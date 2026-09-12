"""Run-scoped progress adapter for the agent-driven resume-tailoring skill.

It freezes the approved posting-version IDs once.  The skill supplies honest
tailoring judgement; this module owns only durable outcomes and shared stage
progress, so a standalone backlog render is never attributed to a run.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.orm import (
    DecisionRun, DecisionRunResumeTailoringManifest, DecisionRunSelectedJob,
    JobPostingVersion, TailoredResume,
)
from .progress import (
    FailedRecordDisposition, FailureDisposition, StageCounts, StageName,
    StageRecord, StageTransitionError, complete_with_errors_stage, finish_stage,
    StageStatus, read_run_progress, start_stage, update_stage,
)
from .telemetry import is_legacy_telemetry


class TailoringOutcome(str, Enum):
    TAILORED = "tailored"
    DROPPED = "dropped"
    FAILED = "failed"


class TailoringDropReason(str, Enum):
    """Approved exceptional drop; implementation problems remain failures."""
    APPROVAL_REVOKED = "approval_revoked_for_posting"


@dataclass(frozen=True)
class ResumeManifestItem:
    posting_version_id: int
    job_id: int


@dataclass(frozen=True)
class ResumeTailoringManifest:
    run_id: str
    items: tuple[ResumeManifestItem, ...]

    @property
    def posting_version_ids(self) -> tuple[int, ...]:
        return tuple(item.posting_version_id for item in self.items)


@dataclass(frozen=True)
class TailoringResult:
    posting_version_id: int
    outcome: TailoringOutcome | str
    reason: str | None = None
    disposition: FailureDisposition | str | None = None


def _rows(session: Session, run_id: str) -> list[DecisionRunResumeTailoringManifest]:
    return list(session.execute(select(DecisionRunResumeTailoringManifest).where(
        DecisionRunResumeTailoringManifest.run_id == run_id,
    ).order_by(DecisionRunResumeTailoringManifest.id)).scalars())


def resume_tailoring_manifest(session: Session, run_id: str) -> ResumeTailoringManifest:
    return ResumeTailoringManifest(run_id, tuple(
        ResumeManifestItem(row.posting_version_id, row.job_id) for row in _rows(session, run_id)
    ))


def versions_needing_tailoring(session: Session, run_id: str) -> list[JobPostingVersion]:
    """The unfinished exact scope; reused and terminal IDs are not re-rendered."""
    rows = _rows(session, run_id)
    ids = [row.posting_version_id for row in rows if row.outcome is None]
    if not ids:
        return []
    return list(session.execute(select(JobPostingVersion).where(
        JobPostingVersion.id.in_(ids),
    ).order_by(JobPostingVersion.id)).scalars())


def job_requirements_for_approved_version(
    session: Session, run_id: str, posting_version_id: int,
) -> dict:
    """Canonical extraction plus immutable posting-version input fields."""
    row = next((item for item in _rows(session, run_id)
                if item.posting_version_id == posting_version_id), None)
    if row is None:
        raise StageTransitionError("posting version is outside the approved resume manifest")
    version = session.get(JobPostingVersion, posting_version_id)
    if version is None:
        raise StageTransitionError("approved posting version no longer exists")
    snapshot = version.snapshot if isinstance(version.snapshot, dict) else {}
    # Enrichment columns and JobSkill rows are mutable current-state facts and
    # are therefore intentionally absent unless a future posting snapshot
    # explicitly freezes them.  The agent may decompose the frozen JD in its
    # reasoning, but this adapter never substitutes today's live enrichment.
    return {
        "job_id": version.job_id,
        "posting_version_id": version.id,
        "material_content_hash": version.material_content_hash,
        "title": snapshot.get("title"),
        "company": snapshot.get("company_name"),
        "location": snapshot.get("location_raw"),
        "seniority": snapshot.get("seniority"),
        "employment_type": snapshot.get("employment_type"),
        "experience_min_years": snapshot.get("experience_min_years"),
        "experience_max_years": snapshot.get("experience_max_years"),
        "education_requirement": snapshot.get("education_requirement"),
        "qualification_other": snapshot.get("qualification_other"),
        "hard_skills": list(snapshot.get("hard_skills") or []),
        "soft_skills": list(snapshot.get("soft_skills") or []),
        "description_raw": snapshot.get("description_raw"),
        "job_url": snapshot.get("job_url") or snapshot.get("apply_url"),
    }


def approved_version_for_job(session: Session, run_id: str, job_id: int) -> JobPostingVersion:
    """Resolve one job to its one exact approved resume-manifest version."""
    matches = [item for item in _rows(session, run_id) if item.job_id == job_id]
    if len(matches) != 1:
        raise StageTransitionError("saved resume job is outside or ambiguous in the approved run manifest")
    version = session.get(JobPostingVersion, matches[0].posting_version_id)
    if version is None:
        raise StageTransitionError("approved posting version no longer exists")
    return version


def freeze_resume_tailoring_manifest(session: Session, run_id: str) -> ResumeTailoringManifest:
    """Freeze the selected approved IDs; existing valid rows count as reused."""
    existing = _rows(session, run_id)
    if existing:
        stage = next((item for item in read_run_progress(
            session, run_id, project_interruptions=False,
        ).stages if item.name == StageName.RESUME_TAILORING.value), None)
        if stage is not None and stage.status in {
            StageStatus.INTERRUPTED.value, StageStatus.FAILED.value,
        }:
            start_stage(session, run_id, StageName.RESUME_TAILORING,
                        expected_count=len(existing), reason="resuming exact approved resume-tailoring manifest")
        return resume_tailoring_manifest(session, run_id)
    run = session.get(DecisionRun, run_id)
    if run is None or is_legacy_telemetry(run.telemetry_version):
        raise StageTransitionError("resume tailoring requires a current approved Decision Run")
    selected = list(session.execute(select(DecisionRunSelectedJob, JobPostingVersion).join(
        JobPostingVersion, DecisionRunSelectedJob.posting_version_id == JobPostingVersion.id,
    ).where(DecisionRunSelectedJob.run_id == run_id).order_by(DecisionRunSelectedJob.id)).all())
    if run.state not in {"approved", "processing", "completed"}:
        raise StageTransitionError("resume tailoring requires named approval")
    job_ids = [version.job_id for _, version in selected]
    resumes = {resume.job_id: resume for resume in session.execute(select(TailoredResume).where(
        TailoredResume.job_id.in_(job_ids), TailoredResume.artifact_dir.is_not(None),
    )).scalars()} if job_ids else {}
    from app.resume.tailor import jd_hash
    reusable_versions = set()
    for _, version in selected:
        resume = resumes.get(version.job_id)
        frozen_description = (version.snapshot or {}).get("description_raw")
        accepted_hashes = {jd_hash(frozen_description), version.material_content_hash}
        if (resume is not None and resume.jd_snapshot == frozen_description
                and resume.jd_hash in accepted_hashes):
            reusable_versions.add(version.id)
    session.add_all(DecisionRunResumeTailoringManifest(
        run_id=run_id, posting_version_id=selected_job.posting_version_id, job_id=version.job_id,
        outcome=TailoringOutcome.TAILORED.value if version.id in reusable_versions else None,
        reason="reused_existing_resume" if version.id in reusable_versions else None,
    ) for selected_job, version in selected)
    session.flush()
    start_stage(session, run_id, StageName.RESUME_TAILORING, expected_count=len(selected),
                reason="exact approved posting-version manifest frozen for resume tailoring")
    _publish(session, run_id)
    return resume_tailoring_manifest(session, run_id)


def _reason(result: TailoringResult) -> str:
    if not result.reason or not result.reason.strip():
        raise StageTransitionError(f"{result.outcome} tailoring results require a reason")
    return result.reason.strip()


def _disposition(result: TailoringResult) -> str | None:
    if result.disposition is None:
        raise StageTransitionError(
            "failed tailoring results require an explicit disposition: non_blocking or excluded"
        )
    try:
        return FailureDisposition(result.disposition).value
    except (TypeError, ValueError) as exc:
        raise StageTransitionError("tailoring failure disposition must be non_blocking or excluded") from exc


def report_resume_tailoring(session: Session, run_id: str, results: Iterable[TailoringResult]) -> None:
    """Record only exact-manifest outcomes; omitted IDs remain pending."""
    manifest = resume_tailoring_manifest(session, run_id)
    stages = read_run_progress(session, run_id, project_interruptions=False).stages
    if not any(stage.name == StageName.RESUME_TAILORING.value for stage in stages):
        raise StageTransitionError("resume tailoring requires a frozen approved manifest")
    supplied_items = tuple(results)
    supplied = {item.posting_version_id: item for item in supplied_items}
    if len(supplied) != len(supplied_items) or set(supplied) - set(manifest.posting_version_ids):
        raise StageTransitionError("tailoring results must name each frozen posting version at most once")
    rows = {row.posting_version_id: row for row in _rows(session, run_id)}
    for version_id, result in supplied.items():
        row = rows[version_id]
        try:
            outcome = TailoringOutcome(result.outcome)
        except (TypeError, ValueError) as exc:
            raise StageTransitionError(f"unknown tailoring outcome {result.outcome!r}") from exc
        if outcome is TailoringOutcome.TAILORED:
            row.outcome, row.reason, row.failure_disposition = outcome.value, None, None
        elif outcome is TailoringOutcome.DROPPED:
            reason = _reason(result)
            try:
                reason = TailoringDropReason(reason).value
            except (TypeError, ValueError) as exc:
                raise StageTransitionError(
                    "dropped tailoring requires reason approval_revoked_for_posting; "
                    "render and validation problems are failures"
                ) from exc
            row.outcome, row.reason, row.failure_disposition = outcome.value, reason, None
        else:
            reason, disposition = _reason(result), _disposition(result)
            row.outcome, row.reason, row.failure_disposition = outcome.value, reason, disposition
    _publish(session, run_id)


def report_resume_tailoring_failure(
    session: Session, run_id: str, posting_version_id: int, *, reason: str,
    disposition: FailureDisposition | str,
) -> None:
    """Live terminal failure seam; disposition is mandatory for successors."""
    report_resume_tailoring(session, run_id, [TailoringResult(
        posting_version_id, TailoringOutcome.FAILED, reason, disposition,
    )])


def report_resume_tailoring_drop(
    session: Session, run_id: str, posting_version_id: int, *,
    reason: TailoringDropReason | str,
) -> None:
    """Record an explicit post-approval revocation, never an implementation error."""
    report_resume_tailoring(session, run_id, [TailoringResult(
        posting_version_id, TailoringOutcome.DROPPED, reason,
    )])


def report_saved_tailored_resume(session: Session, *, run_id: str | None, job_id: int) -> None:
    """Production persistence handoff. No explicit run ID means standalone work."""
    if run_id is None:
        return
    version = approved_version_for_job(session, run_id, job_id)
    report_resume_tailoring(session, run_id, [TailoringResult(version.id, TailoringOutcome.TAILORED)])


def _publish(session: Session, run_id: str) -> None:
    manifest = resume_tailoring_manifest(session, run_id)
    rows = {row.posting_version_id: row for row in _rows(session, run_id)}
    records: list[StageRecord] = []
    reasons: dict[str, int] = {}
    advanced = dropped = failed = 0
    for item in manifest.items:
        row = rows[item.posting_version_id]
        if row.outcome == TailoringOutcome.TAILORED.value:
            records.append(StageRecord("posting_version", str(item.posting_version_id), "advanced")); advanced += 1
        elif row.outcome == TailoringOutcome.DROPPED.value:
            records.append(StageRecord("posting_version", str(item.posting_version_id), "dropped", row.reason)); dropped += 1
            reasons[row.reason] = reasons.get(row.reason, 0) + 1
        elif row.outcome == TailoringOutcome.FAILED.value:
            records.append(StageRecord("posting_version", str(item.posting_version_id), "failed", row.reason)); failed += 1
            reasons[row.reason] = reasons.get(row.reason, 0) + 1
    counts = StageCounts(len(manifest.items), advanced, dropped, failed, len(manifest.items) - advanced - dropped - failed)
    if counts.pending:
        update_stage(session, run_id, StageName.RESUME_TAILORING, counts=counts,
                     reason_counts=reasons, records=records)
        return
    if failed:
        dispositions = tuple(FailedRecordDisposition("posting_version", str(row.posting_version_id), FailureDisposition(row.failure_disposition))
            for row in rows.values() if row.outcome == TailoringOutcome.FAILED.value and row.failure_disposition)
        complete_with_errors_stage(session, run_id, StageName.RESUME_TAILORING, counts=counts, reason_counts=reasons,
            records=records, failure_dispositions=dispositions, reason="resume tailoring finished with explicitly recorded failures")
    else:
        finish_stage(session, run_id, StageName.RESUME_TAILORING, counts=counts, reason_counts=reasons,
            records=records, reason="resume tailoring completed for exact approved manifest")
