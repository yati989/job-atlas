"""Decision Run-scoped telemetry adapters for the agent-driven enrich-jobs stage.

This module intentionally records scope and outcomes only.  The enrich-jobs
skill remains responsible for reading descriptions and extracting facts; it
uses these exact posting-version IDs instead of a global backlog query.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.orm import (
    DecisionRunFinding,
    DecisionRun,
    DecisionRunCanonicalJob,
    DecisionRunJob,
    DecisionRunJobEnrichmentManifest,
    Job,
    JobPostingVersion,
)

from .progress import (
    FailedRecordDisposition,
    FailureDisposition,
    StageCounts,
    StageName,
    StageRecord,
    StageStatus,
    StageTransitionError,
    complete_with_errors_stage,
    finish_stage,
    read_run_progress,
    start_stage,
    update_stage,
)
from .telemetry import is_legacy_telemetry


class TerminalJobOutcome(str, Enum):
    ELIGIBLE = "eligible"
    ANOMALY = "anomaly"
    REJECTED = "rejected"


def terminal_job_outcome(decision_outcome: str) -> TerminalJobOutcome:
    if decision_outcome == "eligible":
        return TerminalJobOutcome.ELIGIBLE
    if decision_outcome == "anomaly":
        return TerminalJobOutcome.ANOMALY
    return TerminalJobOutcome.REJECTED


@dataclass(frozen=True)
class EnrichmentManifestJob:
    posting_version_id: int
    job_id: int


@dataclass(frozen=True)
class EnrichmentManifest:
    run_id: str
    jobs: tuple[EnrichmentManifestJob, ...]

    @property
    def posting_version_ids(self) -> tuple[int, ...]:
        return tuple(row.posting_version_id for row in self.jobs)


@dataclass(frozen=True)
class EnrichmentResult:
    """One completed agent outcome; `failed` needs an explicit reason.

    A failed item only permits screening to continue if it is deliberately
    non-blocking (or excluded) and records why.  Pending items are inferred
    from frozen manifest IDs omitted from this call.
    """
    posting_version_id: int
    outcome: str  # enriched | failed
    reason: str | None = None
    disposition: FailureDisposition | str | None = None


def _manifest_rows(session: Session, run_id: str) -> list[DecisionRunJobEnrichmentManifest]:
    return list(session.execute(select(DecisionRunJobEnrichmentManifest).where(
        DecisionRunJobEnrichmentManifest.run_id == run_id,
    ).order_by(DecisionRunJobEnrichmentManifest.id)).scalars())


def job_enrichment_manifest(session: Session, run_id: str) -> EnrichmentManifest:
    return EnrichmentManifest(run_id, tuple(
        EnrichmentManifestJob(row.posting_version_id, row.job_id)
        for row in _manifest_rows(session, run_id)
    ))


def freeze_job_enrichment_manifest(session: Session, run_id: str) -> EnrichmentManifest:
    """Copy exact canonical versions; never reconstruct from mutable Jobs."""
    existing = _manifest_rows(session, run_id)
    if existing:
        return job_enrichment_manifest(session, run_id)
    run = session.get(DecisionRun, run_id)
    if run is None or is_legacy_telemetry(run.telemetry_version):
        raise StageTransitionError("job enrichment requires current canonical telemetry")
    canonical = list(session.execute(select(DecisionRunCanonicalJob).where(
        DecisionRunCanonicalJob.run_id == run_id,
    ).order_by(DecisionRunCanonicalJob.id)).scalars())
    canonical_stage = next((stage for stage in read_run_progress(
        session, run_id, project_interruptions=False,
    ).stages if stage.name == StageName.JOB_DEDUPLICATION.value), None)
    if canonical_stage is None or canonical_stage.counts.advanced != len(canonical):
        raise StageTransitionError("current Decision Run is missing its canonical posting-version manifest")
    jobs = {job.id: job for job in session.execute(select(Job).where(
        Job.id.in_([item.job_id for item in canonical]),
    )).scalars()}
    session.add_all(DecisionRunJobEnrichmentManifest(
        run_id=run_id,
        posting_version_id=item.posting_version_id,
        job_id=item.job_id,
        outcome="enriched" if jobs[item.job_id].enrichment_status in ("done", "no_description") else None,
    ) for item in canonical)
    session.flush()
    start_stage(
        session, run_id, StageName.JOB_ENRICHMENT, expected_count=len(canonical),
        reason="exact canonical posting-version manifest frozen for enrich-jobs",
    )
    return job_enrichment_manifest(session, run_id)


def _reason_for(result: EnrichmentResult) -> str:
    if not result.reason or not result.reason.strip():
        raise StageTransitionError("failed enrichment results require a reason")
    return result.reason.strip()


def _disposition_for(result: EnrichmentResult) -> str | None:
    if result.disposition is None:
        return None
    try:
        return FailureDisposition(result.disposition).value
    except ValueError as exc:
        raise StageTransitionError("failure disposition must be non_blocking or excluded") from exc


def report_job_enrichment(
    session: Session, run_id: str, results: Iterable[EnrichmentResult],
) -> None:
    """Publish agent outcomes for the frozen manifest without touching jobs.

    Existing ``done`` and ``no_description`` rows are reusable completed work;
    both advance under the single dashboard label *Enriched*.
    """
    manifest = job_enrichment_manifest(session, run_id)
    progress = read_run_progress(session, run_id, project_interruptions=False)
    if not any(stage.name == StageName.JOB_ENRICHMENT.value for stage in progress.stages):
        raise StageTransitionError("job enrichment requires a frozen Decision Run manifest")
    ids = set(manifest.posting_version_ids)
    supplied_results = tuple(results)
    supplied = {result.posting_version_id: result for result in supplied_results}
    if len(supplied) != len(supplied_results):
        raise StageTransitionError("each frozen posting version may have one enrichment result")
    unknown = set(supplied) - ids
    if unknown:
        raise StageTransitionError("enrichment result is outside the Decision Run manifest")
    rows = {row.posting_version_id: row for row in _manifest_rows(session, run_id)}
    for version_id, result in supplied.items():
        row = rows[version_id]
        if result.outcome == "enriched":
            row.outcome, row.reason, row.failure_disposition = "enriched", None, None
        elif result.outcome == "failed":
            row.outcome = "failed"
            row.reason = _reason_for(result)
            row.failure_disposition = _disposition_for(result)
        else:
            raise StageTransitionError(f"unknown enrichment outcome {result.outcome!r}")
    records: list[StageRecord] = []
    reasons: dict[str, int] = {}
    advanced = failed = 0
    for row in manifest.jobs:
        durable = rows[row.posting_version_id]
        if durable.outcome == "enriched":
            records.append(StageRecord("posting_version", str(row.posting_version_id), "advanced"))
            advanced += 1
        elif durable.outcome == "failed":
            reason = durable.reason
            records.append(StageRecord("posting_version", str(row.posting_version_id), "failed", reason))
            reasons[reason] = reasons.get(reason, 0) + 1
            failed += 1
    pending = len(manifest.jobs) - advanced - failed
    counts = StageCounts(len(manifest.jobs), advanced, 0, failed, pending)
    if pending:
        update_stage(session, run_id, StageName.JOB_ENRICHMENT, counts=counts,
                     reason_counts=reasons, records=records)
        return
    if failed:
        failure_dispositions = tuple(
            FailedRecordDisposition(
                "posting_version", str(row.posting_version_id),
                FailureDisposition(row.failure_disposition),
            )
            for row in rows.values()
            if row.outcome == "failed" and row.failure_disposition is not None
        )
        complete_with_errors_stage(
            session, run_id, StageName.JOB_ENRICHMENT, counts=counts,
            reason_counts=reasons, records=records,
            failure_dispositions=failure_dispositions,
            reason="enrichment finished with explicitly recorded failures",
        )
    else:
        finish_stage(
            session, run_id, StageName.JOB_ENRICHMENT, counts=counts,
            reason_counts={}, records=records, reason="enrichment completed for exact manifest",
        )


def screening_may_start(session: Session, run_id: str) -> None:
    """Reject implicit advancement across unresolved enrichment failures."""
    run = session.get(DecisionRun, run_id)
    if run is None:
        raise StageTransitionError(f"unknown decision run {run_id}")
    if is_legacy_telemetry(run.telemetry_version):
        return
    manifest = _manifest_rows(session, run_id)
    stage = next((item for item in read_run_progress(session, run_id, project_interruptions=False).stages
                  if item.name == StageName.JOB_ENRICHMENT.value), None)
    if stage is None:
        raise StageTransitionError("current Decision Run is missing enrichment manifest/stage telemetry")
    if stage.counts.input != len(manifest):
        raise StageTransitionError("current Decision Run is missing enrichment manifest/stage telemetry")
    if stage.status == StageStatus.COMPLETED.value:
        return
    if stage.status != StageStatus.COMPLETED_WITH_ERRORS.value:
        raise StageTransitionError("screening requires completed Decision Run job enrichment")
    failures = [row for row in manifest if row.outcome == "failed"]
    if not failures or not all(row.failure_disposition in {
        FailureDisposition.NON_BLOCKING.value, FailureDisposition.EXCLUDED.value,
    } for row in failures):
        raise StageTransitionError("screening requires enrichment failures to be explicitly non-blocking or excluded")


def report_screening_outcomes(session: Session, run_id: str) -> None:
    """Publish exclusive eligible/anomaly/rejected terminals from frozen snapshots."""
    rows = list(session.execute(select(DecisionRunJob).where(
        DecisionRunJob.run_id == run_id,
    ).order_by(DecisionRunJob.id)).scalars())
    findings = list(session.execute(select(DecisionRunFinding).join(
        DecisionRunJob, DecisionRunFinding.decision_run_job_id == DecisionRunJob.id,
    ).where(DecisionRunJob.run_id == run_id)).scalars())
    reason_by_run_job: dict[int, str] = {}
    for finding in findings:
        reason_by_run_job.setdefault(finding.decision_run_job_id, finding.reason_code)
    records: list[StageRecord] = []
    reasons: dict[str, int] = {}
    eligible = rejected = anomalies = 0
    for row in rows:
        terminal = terminal_job_outcome(row.outcome)
        if terminal is TerminalJobOutcome.ELIGIBLE:
            records.append(StageRecord("posting_version", str(row.posting_version_id), "advanced"))
            eligible += 1
            continue
        reason = reason_by_run_job.get(row.id) or (row.snapshot or {}).get("anomaly") or row.outcome
        records.append(StageRecord("posting_version", str(row.posting_version_id), "dropped", reason))
        reasons[reason] = reasons.get(reason, 0) + 1
        if terminal is TerminalJobOutcome.ANOMALY:
            anomalies += 1
        else:
            rejected += 1
    counts = StageCounts(len(rows), eligible, anomalies + rejected, 0, 0)
    start_stage(session, run_id, StageName.SCREENING_RANKING_GROUPING,
                expected_count=len(rows), reason="screening frozen enriched manifest")
    finish_stage(session, run_id, StageName.SCREENING_RANKING_GROUPING,
                 counts=counts, reason_counts=reasons, records=records,
                 reason="eligible, anomaly, and rejected outcomes recorded")
