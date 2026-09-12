"""Durable stage-progress seam for Decision Runs.

Callers report lifecycle intent and reconciled outcomes. This module owns
persistence, status/transition rules, reason invariants, attempt history,
identifier-only drill-down evidence, and stale-work handling.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Callable, Iterable

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models.orm import (
    DecisionRun,
    DecisionRunStage,
    DecisionRunStageAttempt,
    DecisionRunStageRecord,
    DecisionRunStageTransition,
)
from .telemetry import CURRENT_TELEMETRY_VERSION


class StageName(str, Enum):
    FETCH_JOBS = "fetch_jobs"
    COLLECTION_DEDUPLICATION = "collection_deduplication"
    RELEVANCE_STORAGE = "relevance_storage"
    JOB_DEDUPLICATION = "job_deduplication"
    COMPANY_DEDUPLICATION = "company_deduplication"
    JOB_ENRICHMENT = "job_enrichment"
    COMPANY_PHASE_A = "company_phase_a"
    COMPANY_PHASE_B = "company_phase_b"
    SCREENING_RANKING_GROUPING = "screening_ranking_grouping"
    APPROVAL_GATE = "approval_gate"
    RESUME_TAILORING = "resume_tailoring"
    CONTACT_ENRICHMENT = "contact_enrichment"
    DRAFT_PREPARATION = "draft_preparation"
    GMAIL_DRAFT_POSTING = "gmail_draft_posting"
    FINAL_REPORT = "final_report"


class StageStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    COMPLETED = "completed"
    COMPLETED_WITH_ERRORS = "completed_with_errors"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    SKIPPED = "skipped"


class StageOutcome(str, Enum):
    ADVANCED = "advanced"
    DROPPED = "dropped"
    FAILED = "failed"


class FailureDisposition(str, Enum):
    NON_BLOCKING = "non_blocking"
    EXCLUDED = "excluded"


@dataclass(frozen=True)
class FailedRecordDisposition:
    record_type: str
    record_id: str
    disposition: FailureDisposition


REASON_REQUIRED = {
    StageStatus.RUNNING,
    StageStatus.WAITING_FOR_APPROVAL,
    StageStatus.COMPLETED_WITH_ERRORS,
    StageStatus.FAILED,
    StageStatus.INTERRUPTED,
    StageStatus.SKIPPED,
}
ALLOWED_TRANSITIONS = {
    StageStatus.PENDING: {StageStatus.RUNNING, StageStatus.SKIPPED},
    StageStatus.RUNNING: {
        StageStatus.WAITING_FOR_APPROVAL,
        StageStatus.COMPLETED,
        StageStatus.COMPLETED_WITH_ERRORS,
        StageStatus.FAILED,
        StageStatus.INTERRUPTED,
    },
    StageStatus.WAITING_FOR_APPROVAL: {
        StageStatus.RUNNING,
        StageStatus.FAILED,
        StageStatus.SKIPPED,
    },
    StageStatus.FAILED: {StageStatus.RUNNING},
    StageStatus.INTERRUPTED: {StageStatus.RUNNING},
    # A terminal attempt can still contain explicitly recoverable record
    # failures (for example an isolated upsert rejected by a SQL width).  A
    # later attempt may repair those records while preserving attempt history.
    StageStatus.COMPLETED_WITH_ERRORS: {StageStatus.RUNNING},
}

# Typed, intentionally small dependency graph.  Multiple names can be running
# together when their own predecessors have completed (for example job
# enrichment and company Phase B). Approval starts only after the complete
# screening, ranking, and grouping snapshot is ready.
PREDECESSORS: dict[StageName, tuple[StageName, ...]] = {
    StageName.COLLECTION_DEDUPLICATION: (StageName.FETCH_JOBS,),
    StageName.RELEVANCE_STORAGE: (StageName.COLLECTION_DEDUPLICATION,),
    StageName.JOB_DEDUPLICATION: (StageName.RELEVANCE_STORAGE,),
    StageName.COMPANY_DEDUPLICATION: (StageName.JOB_DEDUPLICATION,),
    StageName.JOB_ENRICHMENT: (StageName.JOB_DEDUPLICATION,),
    StageName.COMPANY_PHASE_A: (StageName.COMPANY_DEDUPLICATION,),
    StageName.COMPANY_PHASE_B: (StageName.COMPANY_DEDUPLICATION,),
    StageName.SCREENING_RANKING_GROUPING: (StageName.JOB_ENRICHMENT, StageName.COMPANY_PHASE_B),
    StageName.APPROVAL_GATE: (StageName.SCREENING_RANKING_GROUPING,),
    StageName.RESUME_TAILORING: (StageName.APPROVAL_GATE,),
    StageName.CONTACT_ENRICHMENT: (StageName.APPROVAL_GATE, StageName.COMPANY_PHASE_A),
    StageName.DRAFT_PREPARATION: (StageName.RESUME_TAILORING, StageName.CONTACT_ENRICHMENT),
    StageName.GMAIL_DRAFT_POSTING: (StageName.DRAFT_PREPARATION,),
    StageName.FINAL_REPORT: (StageName.GMAIL_DRAFT_POSTING,),
}


class StageTransitionError(ValueError):
    pass


@dataclass(frozen=True)
class StageCounts:
    input: int
    advanced: int
    dropped: int
    failed: int
    pending: int

    @property
    def processed(self) -> int:
        return self.advanced + self.dropped + self.failed


@dataclass(frozen=True)
class StageRecord:
    record_type: str
    record_id: str
    outcome: str
    reason: str | None = None


@dataclass(frozen=True)
class StageTransition:
    to_status: str
    from_status: str | None
    reason: str | None
    occurred_at: datetime


@dataclass(frozen=True)
class StageAttemptRead:
    attempt_number: int
    status: str
    process_id: int | None
    counts: StageCounts
    reason_counts: dict[str, int]
    reason: str | None
    failed_record_dispositions: tuple[FailedRecordDisposition, ...]
    started_at: datetime
    heartbeat_at: datetime
    ended_at: datetime | None
    records: tuple[StageRecord, ...]


@dataclass(frozen=True)
class StageRead:
    name: str
    status: str
    counts: StageCounts
    reason_counts: dict[str, int]
    reason: str | None
    started_at: datetime | None
    updated_at: datetime
    ended_at: datetime | None
    heartbeat_at: datetime | None
    attempt_number: int
    transitions: tuple[StageTransition, ...]
    attempts: tuple[StageAttemptRead, ...]
    waiting_started_at: datetime | None = None
    waiting_seconds: int = 0
    active_elapsed_seconds: float | None = None
    selected_company_count: int | None = None
    available_company_count: int | None = None
    selected_job_count: int | None = None
    failed_record_dispositions: tuple[FailedRecordDisposition, ...] = ()
    projected_interruption: bool = False

    @property
    def processed_count(self) -> int:
        return self.counts.processed

    @property
    def expected_count(self) -> int:
        return self.counts.input


@dataclass(frozen=True)
class RunProgress:
    run_id: str
    legacy_telemetry: bool
    stages: tuple[StageRead, ...]


def _now(now: datetime | None) -> datetime:
    value = now or datetime.now(timezone.utc)
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _name(value: str | StageName) -> str:
    try:
        return StageName(value).value
    except ValueError as exc:
        raise StageTransitionError(f"unknown Decision Run stage {value!r}") from exc


def _status(value: str | StageStatus) -> StageStatus:
    try:
        return StageStatus(value)
    except ValueError as exc:
        raise StageTransitionError(f"unknown stage status {value!r}") from exc


def _require_reason(status: StageStatus, reason: str | None) -> str | None:
    normalized = reason.strip() if reason else None
    if status in REASON_REQUIRED and not normalized:
        raise StageTransitionError(f"{status.value} requires a reason")
    return normalized


def _validate_counts(counts: StageCounts) -> None:
    values = (counts.input, counts.advanced, counts.dropped, counts.failed, counts.pending)
    if any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in values):
        raise StageTransitionError("stage counts must be non-negative integers")
    if counts.input != counts.advanced + counts.dropped + counts.failed + counts.pending:
        raise StageTransitionError(
            "stage counts must reconcile: input = advanced + dropped + failed + pending"
        )


def _normalize_reason_counts(reason_counts: dict[str, int] | None, counts: StageCounts) -> dict[str, int]:
    normalized = dict(reason_counts or {})
    if any(not key or not isinstance(value, int) or isinstance(value, bool) or value <= 0 for key, value in normalized.items()):
        raise StageTransitionError("reason counts require non-empty keys and positive integer values")
    if sum(normalized.values()) != counts.dropped + counts.failed:
        raise StageTransitionError("reason counts must reconcile with dropped + failed")
    return normalized


def _normalize_failed_record_dispositions(
    failure_dispositions: Iterable[FailedRecordDisposition] | None,
    records: Iterable[StageRecord], *, require_all: bool = False,
) -> tuple[FailedRecordDisposition, ...]:
    failed_keys = {
        (record.record_type, record.record_id)
        for record in records if StageOutcome(record.outcome) is StageOutcome.FAILED
    }
    normalized: list[FailedRecordDisposition] = []
    seen: set[tuple[str, str]] = set()
    for item in (failure_dispositions or ()):
        key = (item.record_type, item.record_id)
        try:
            disposition = FailureDisposition(item.disposition)
        except (TypeError, ValueError) as exc:
            raise StageTransitionError(
                "failed record dispositions must be non_blocking or excluded"
            ) from exc
        if key in seen:
            raise StageTransitionError(f"duplicate failed record disposition {key!r}")
        seen.add(key)
        normalized.append(FailedRecordDisposition(item.record_type, item.record_id, disposition))
    if seen - failed_keys or (require_all and seen != failed_keys):
        raise StageTransitionError(
            "every failed record must be marked non-blocking or excluded"
        )
    return tuple(normalized)


def _serialize_failed_record_dispositions(
    dispositions: Iterable[FailedRecordDisposition],
) -> list[dict[str, str]]:
    return [{
        "record_type": item.record_type,
        "record_id": item.record_id,
        "disposition": FailureDisposition(item.disposition).value,
    } for item in dispositions]


def _deserialize_failed_record_dispositions(value: object) -> tuple[FailedRecordDisposition, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(FailedRecordDisposition(
        str(item["record_type"]), str(item["record_id"]), FailureDisposition(item["disposition"]),
    ) for item in value if isinstance(item, dict))


def _normalize_records(records: Iterable[StageRecord] | None, counts: StageCounts) -> tuple[StageRecord, ...] | None:
    if records is None:
        return None
    normalized = tuple(records)
    seen: set[tuple[str, str]] = set()
    outcome_counts = {outcome.value: 0 for outcome in StageOutcome}
    for record in normalized:
        if not record.record_type.strip() or not record.record_id.strip():
            raise StageTransitionError("stage record identifiers must be non-empty")
        key = (record.record_type, record.record_id)
        if key in seen:
            raise StageTransitionError(f"duplicate stage record identifier {key!r}")
        seen.add(key)
        try:
            outcome = StageOutcome(record.outcome).value
        except ValueError as exc:
            raise StageTransitionError(f"unknown stage record outcome {record.outcome!r}") from exc
        if outcome in {StageOutcome.DROPPED.value, StageOutcome.FAILED.value} and not record.reason:
            raise StageTransitionError(f"{outcome} stage records require a reason")
        outcome_counts[outcome] += 1
    expected = {
        StageOutcome.ADVANCED.value: counts.advanced,
        StageOutcome.DROPPED.value: counts.dropped,
        StageOutcome.FAILED.value: counts.failed,
    }
    if outcome_counts != expected:
        raise StageTransitionError("stage record identifiers must reconcile with advanced/dropped/failed counts")
    return normalized


def _validate_record_reasons(records: tuple[StageRecord, ...] | None,
                             reason_counts: dict[str, int]) -> None:
    if records is None:
        return
    observed: dict[str, int] = {}
    for record in records:
        if record.reason:
            observed[record.reason] = observed.get(record.reason, 0) + 1
    if observed != reason_counts:
        raise StageTransitionError("stage record reasons must reconcile with reason counts")


def _counts_from_row(row: DecisionRunStage | DecisionRunStageAttempt) -> StageCounts:
    return StageCounts(
        input=int(row.expected_count or 0),
        advanced=int(row.advanced_count or 0),
        dropped=int(row.dropped_count or 0),
        failed=int(row.failed_count or 0),
        pending=int(row.pending_count or 0),
    )


def _apply_counts(row: DecisionRunStage | DecisionRunStageAttempt, counts: StageCounts, reason_counts: dict[str, int]) -> None:
    row.expected_count = counts.input
    row.processed_count = counts.processed
    row.advanced_count = counts.advanced
    row.dropped_count = counts.dropped
    row.failed_count = counts.failed
    row.pending_count = counts.pending
    row.reason_counts = reason_counts


def _stage(session: Session, run_id: str, name: str | StageName) -> DecisionRunStage:
    stage_name = _name(name)
    stage = session.execute(select(DecisionRunStage).where(
        DecisionRunStage.run_id == run_id,
        DecisionRunStage.name == stage_name,
    )).scalar_one_or_none()
    if stage is None:
        raise StageTransitionError(f"unknown stage {stage_name!r} for decision run {run_id}")
    return stage


def _predecessors_are_satisfied(session: Session, run_id: str, name: str) -> None:
    for predecessor in PREDECESSORS.get(StageName(name), ()):
        row = session.execute(select(DecisionRunStage).where(
            DecisionRunStage.run_id == run_id, DecisionRunStage.name == predecessor.value,
        )).scalar_one_or_none()
        if row is None:
            raise StageTransitionError(f"stage {name!r} is blocked by predecessor {predecessor.value!r}")
        status = _status(row.status)
        if status in {StageStatus.COMPLETED, StageStatus.SKIPPED}:
            continue
        if status is StageStatus.COMPLETED_WITH_ERRORS:
            attempt = _attempt(session, row)
            records = _records_for_attempt(session, attempt.id)
            try:
                if sum(record.outcome == StageOutcome.FAILED.value for record in records) != row.failed_count:
                    raise StageTransitionError("completed-with-errors requires durable failed record identifiers")
                _normalize_failed_record_dispositions(
                    _deserialize_failed_record_dispositions(row.failed_record_dispositions),
                    records, require_all=True,
                )
            except StageTransitionError as exc:
                raise StageTransitionError(
                    f"stage {name!r} is blocked by predecessor {predecessor.value!r}; "
                    "its failures must be marked non-blocking or excluded"
                ) from exc
            continue
        raise StageTransitionError(f"stage {name!r} is blocked by predecessor {predecessor.value!r} ({status.value})")


def _attempt(session: Session, stage: DecisionRunStage) -> DecisionRunStageAttempt:
    attempt = session.execute(select(DecisionRunStageAttempt).where(
        DecisionRunStageAttempt.stage_id == stage.id,
        DecisionRunStageAttempt.attempt_number == stage.attempt_number,
    )).scalar_one_or_none()
    if attempt is None:
        raise StageTransitionError(f"stage {stage.name!r} has no active attempt")
    return attempt


def _transition(session: Session, stage: DecisionRunStage, *, to_status: str | StageStatus,
                now: datetime, reason: str | None,
                attempt: DecisionRunStageAttempt | None = None) -> None:
    target, source = _status(to_status), _status(stage.status)
    if target not in ALLOWED_TRANSITIONS.get(source, set()):
        raise StageTransitionError(f"cannot transition stage {stage.name!r} from {source.value} to {target.value}")
    normalized_reason = _require_reason(target, reason)
    session.add(DecisionRunStageTransition(
        stage_id=stage.id,
        attempt_id=attempt.id if attempt else None,
        from_status=source.value,
        to_status=target.value,
        reason=normalized_reason,
        occurred_at=now,
    ))
    stage.status = target.value


def declare_pending_stage(session: Session, run_id: str, name: str | StageName, *,
                          expected_count: int, now: datetime | None = None) -> StageCounts:
    """Persist planned work before its dependency graph permits execution.

    This deliberately does not check predecessors: it is a planning record,
    not an attempt to start work.  ``start_stage`` remains the only path that
    can transition it to running and enforces the dependency graph then.
    """
    run = session.get(DecisionRun, run_id)
    if run is None:
        raise StageTransitionError(f"unknown decision run {run_id}")
    run.telemetry_version = run.telemetry_version or CURRENT_TELEMETRY_VERSION
    stage_name, timestamp = _name(name), _now(now)
    initial = StageCounts(expected_count, 0, 0, 0, expected_count)
    _validate_counts(initial)
    stage = session.execute(select(DecisionRunStage).where(
        DecisionRunStage.run_id == run_id,
        DecisionRunStage.name == stage_name,
    )).scalar_one_or_none()
    if stage is None:
        stage = DecisionRunStage(
            run_id=run_id, name=stage_name, status=StageStatus.PENDING.value,
            updated_at=timestamp, reason_counts={},
        )
        _apply_counts(stage, initial, {})
        session.add(stage)
        session.flush()
        return initial
    if stage.expected_count != expected_count:
        raise StageTransitionError("declared stage must preserve the original input count")
    return _counts_from_row(stage)


def start_stage(session: Session, run_id: str, name: str | StageName, *, expected_count: int,
                process_id: int | None = None, process_untracked: bool = False,
                reason: str, now: datetime | None = None) -> StageCounts:
    run = session.get(DecisionRun, run_id)
    if run is None:
        raise StageTransitionError(f"unknown decision run {run_id}")
    run.telemetry_version = run.telemetry_version or CURRENT_TELEMETRY_VERSION
    stage_name, timestamp = _name(name), _now(now)
    _predecessors_are_satisfied(session, run_id, stage_name)
    initial = StageCounts(expected_count, 0, 0, 0, expected_count)
    _validate_counts(initial)
    normalized_reason = _require_reason(StageStatus.RUNNING, reason)
    stage = session.execute(select(DecisionRunStage).where(
        DecisionRunStage.run_id == run_id,
        DecisionRunStage.name == stage_name,
    )).scalar_one_or_none()
    if stage is None:
        stage = DecisionRunStage(run_id=run_id, name=stage_name, status=StageStatus.PENDING.value,
                                 updated_at=timestamp, reason_counts={})
        _apply_counts(stage, initial, {})
        session.add(stage)
        session.flush()
    source = _status(stage.status)
    if StageStatus.RUNNING not in ALLOWED_TRANSITIONS.get(source, set()):
        raise StageTransitionError(f"cannot start stage {stage_name!r} from {source.value}")
    if stage.expected_count != expected_count:
        raise StageTransitionError("retry must preserve the original input count")
    counts = _counts_from_row(stage)
    if source is StageStatus.WAITING_FOR_APPROVAL:
        _attempt(session, stage).ended_at = timestamp
        if stage.waiting_started_at:
            stage.waiting_seconds += max(0, int((timestamp - _now(stage.waiting_started_at)).total_seconds()))
            stage.waiting_started_at = None
    stage.attempt_number += 1
    stage.reason = normalized_reason
    stage.started_at = stage.started_at or timestamp
    stage.updated_at = stage.heartbeat_at = timestamp
    stage.ended_at = None
    attempt = DecisionRunStageAttempt(
        stage_id=stage.id,
        attempt_number=stage.attempt_number,
        status=StageStatus.RUNNING.value,
        process_id=None if process_untracked else (process_id if process_id is not None else os.getpid()),
        reason=normalized_reason,
        started_at=timestamp,
        heartbeat_at=timestamp,
    )
    _apply_counts(attempt, counts, dict(stage.reason_counts or {}))
    session.add(attempt)
    session.flush()
    _transition(session, stage, to_status=StageStatus.RUNNING, now=timestamp,
                reason=normalized_reason, attempt=attempt)
    return counts


def _replace_records(session: Session, stage: DecisionRunStage,
                     attempt: DecisionRunStageAttempt, records: tuple[StageRecord, ...]) -> None:
    session.execute(delete(DecisionRunStageRecord).where(DecisionRunStageRecord.attempt_id == attempt.id))
    session.add_all([
        DecisionRunStageRecord(stage_id=stage.id, attempt_id=attempt.id,
                               record_type=record.record_type, record_id=record.record_id,
                               outcome=StageOutcome(record.outcome).value, reason=record.reason)
        for record in records
    ])


def update_stage(session: Session, run_id: str, name: str | StageName, *, counts: StageCounts,
                 reason_counts: dict[str, int] | None = None,
                 records: Iterable[StageRecord] | None = None,
                 reason: str | None = None, now: datetime | None = None) -> None:
    stage = _stage(session, run_id, name)
    if _status(stage.status) is not StageStatus.RUNNING:
        raise StageTransitionError(f"cannot update stage {stage.name!r} from {stage.status}")
    _validate_counts(counts)
    if counts.input != stage.expected_count:
        raise StageTransitionError("stage update must preserve the original input count")
    normalized_reasons = _normalize_reason_counts(reason_counts, counts)
    normalized_records = _normalize_records(records, counts)
    _validate_record_reasons(normalized_records, normalized_reasons)
    timestamp, attempt = _now(now), _attempt(session, stage)
    _apply_counts(stage, counts, normalized_reasons)
    _apply_counts(attempt, counts, normalized_reasons)
    stage.reason = reason or stage.reason
    stage.updated_at = stage.heartbeat_at = attempt.heartbeat_at = timestamp
    if normalized_records is not None:
        _replace_records(session, stage, attempt, normalized_records)


def heartbeat_stage(session: Session, run_id: str, name: str | StageName, *,
                    reason: str | None = None, now: datetime | None = None) -> None:
    """Refresh liveness without asking a caller to restate durable counts."""
    stage = _stage(session, run_id, name)
    if _status(stage.status) is not StageStatus.RUNNING:
        raise StageTransitionError(f"cannot heartbeat stage {stage.name!r} from {stage.status}")
    timestamp, attempt = _now(now), _attempt(session, stage)
    stage.reason = reason or stage.reason
    stage.updated_at = stage.heartbeat_at = attempt.heartbeat_at = timestamp


def _finish(session: Session, run_id: str, name: str | StageName, *, status: StageStatus,
            counts: StageCounts | None, reason_counts: dict[str, int] | None,
            records: Iterable[StageRecord] | None, reason: str | None,
            now: datetime | None) -> None:
    stage = _stage(session, run_id, name)
    source = _status(stage.status)
    allowed_sources = {StageStatus.RUNNING}
    if status is StageStatus.FAILED:
        allowed_sources.add(StageStatus.WAITING_FOR_APPROVAL)
    if source not in allowed_sources:
        raise StageTransitionError(f"cannot finish stage {stage.name!r} from {stage.status}")
    final_counts = counts or _counts_from_row(stage)
    _validate_counts(final_counts)
    if final_counts.input != stage.expected_count:
        raise StageTransitionError("stage finish must preserve the original input count")
    normalized_reasons = _normalize_reason_counts(
        reason_counts if reason_counts is not None else dict(stage.reason_counts or {}),
        final_counts,
    )
    normalized_records = _normalize_records(records, final_counts)
    _validate_record_reasons(normalized_records, normalized_reasons)
    if status is StageStatus.COMPLETED and (final_counts.failed or final_counts.pending):
        raise StageTransitionError("completed stage cannot have failed or pending records")
    if status is StageStatus.COMPLETED_WITH_ERRORS and (not final_counts.failed or final_counts.pending):
        raise StageTransitionError("completed-with-errors requires failed > 0 and pending = 0")
    timestamp, attempt = _now(now), _attempt(session, stage)
    _apply_counts(stage, final_counts, normalized_reasons)
    _apply_counts(attempt, final_counts, normalized_reasons)
    stage.reason = attempt.reason = _require_reason(status, reason or stage.reason)
    stage.updated_at = stage.heartbeat_at = attempt.heartbeat_at = timestamp
    stage.ended_at = attempt.ended_at = timestamp
    attempt.status = status.value
    if normalized_records is not None:
        _replace_records(session, stage, attempt, normalized_records)
    _transition(session, stage, to_status=status, now=timestamp, reason=stage.reason, attempt=attempt)


def finish_stage(session: Session, run_id: str, name: str | StageName, *, counts: StageCounts | None = None,
                 reason_counts: dict[str, int] | None = None,
                 records: Iterable[StageRecord] | None = None,
                 reason: str | None = None, now: datetime | None = None) -> None:
    _finish(session, run_id, name, status=StageStatus.COMPLETED, counts=counts,
            reason_counts=reason_counts, records=records, reason=reason, now=now)


def complete_with_errors_stage(session: Session, run_id: str, name: str | StageName, *,
                               counts: StageCounts | None = None,
                               reason_counts: dict[str, int] | None = None,
                               records: Iterable[StageRecord] | None = None,
                               failure_dispositions: Iterable[FailedRecordDisposition] | None = None,
                               reason: str, now: datetime | None = None) -> None:
    stage = _stage(session, run_id, name)
    attempt = _attempt(session, stage)
    normalized_records = tuple(records) if records is not None else _records_for_attempt(session, attempt.id)
    dispositions = _normalize_failed_record_dispositions(
        failure_dispositions, normalized_records,
    )
    _finish(session, run_id, name, status=StageStatus.COMPLETED_WITH_ERRORS, counts=counts,
            reason_counts=reason_counts, records=normalized_records if records is not None else None,
            reason=reason, now=now)
    serialized = _serialize_failed_record_dispositions(dispositions)
    stage.failed_record_dispositions = serialized
    _attempt(session, stage).failed_record_dispositions = serialized


def begin_approval_preparation(
    session: Session, run_id: str, *,
    now: datetime | None = None,
) -> None:
    """Start active work on the decision snapshot and review artifact."""
    run = session.get(DecisionRun, run_id)
    if run is None:
        raise StageTransitionError(f"unknown decision run {run_id}")
    timestamp = _now(now)
    start_stage(
        session, run_id, StageName.APPROVAL_GATE,
        expected_count=1,
        reason="building approval artifact", now=timestamp,
    )


def mark_approval_artifact_ready(
    session: Session, run_id: str, *, available_company_count: int,
    gmail_message_id: str | None = None, gmail_thread_id: str | None = None,
    google_spreadsheet_id: str | None = None,
    google_spreadsheet_url: str | None = None,
    now: datetime | None = None,
) -> None:
    """Start human waiting only after the review artifact is ready/delivered."""
    run = session.get(DecisionRun, run_id)
    if run is None:
        raise StageTransitionError(f"unknown decision run {run_id}")
    timestamp = _now(now)
    stage = _stage(session, run_id, StageName.APPROVAL_GATE)
    if _status(stage.status) is not StageStatus.RUNNING:
        raise StageTransitionError("approval artifact can become ready only during active preparation")
    stage.selection_counts = {
        "available_companies": available_company_count,
        **({"approval_gmail_message_id": gmail_message_id} if gmail_message_id else {}),
        **({"approval_gmail_thread_id": gmail_thread_id} if gmail_thread_id else {}),
        **({"approval_google_spreadsheet_id": google_spreadsheet_id} if google_spreadsheet_id else {}),
        **({"approval_google_spreadsheet_url": google_spreadsheet_url} if google_spreadsheet_url else {}),
    }
    wait_for_approval(
        session, run_id, StageName.APPROVAL_GATE,
        reason="awaiting named approval", now=timestamp,
    )
    run.state = "awaiting_approval"
    run.prepared_at = timestamp


def approval_artifact_thread_id(session: Session, run_id: str) -> str:
    """Return the Gmail thread recorded for the delivered approval Sheet."""
    stage = _stage(session, run_id, StageName.APPROVAL_GATE)
    thread_id = (stage.selection_counts or {}).get("approval_gmail_thread_id")
    if not thread_id:
        raise StageTransitionError(
            "approval Sheet delivery has no durable Gmail thread id"
        )
    return str(thread_id)


def approval_artifact_spreadsheet(session: Session, run_id: str) -> tuple[str, str]:
    """Return the exact native Sheet ID and provider-observed URL for a run."""
    stage = _stage(session, run_id, StageName.APPROVAL_GATE)
    metadata = stage.selection_counts or {}
    spreadsheet_id = metadata.get("approval_google_spreadsheet_id")
    spreadsheet_url = metadata.get("approval_google_spreadsheet_url")
    if not spreadsheet_id or not spreadsheet_url:
        raise StageTransitionError(
            "approval delivery has no durable Google Sheet ID/link"
        )
    return str(spreadsheet_id), str(spreadsheet_url)


def replace_approval_artifact_delivery(
    session: Session, run_id: str, *, gmail_message_id: str,
    gmail_thread_id: str, google_spreadsheet_id: str,
    google_spreadsheet_url: str,
) -> None:
    """Make a corrected Sheet/email pair authoritative while still waiting."""
    stage = _stage(session, run_id, StageName.APPROVAL_GATE)
    if _status(stage.status) is not StageStatus.WAITING_FOR_APPROVAL:
        raise StageTransitionError(
            "approval artifact can be replaced only while waiting for approval"
        )
    metadata = dict(stage.selection_counts or {})
    metadata.update({
        "approval_gmail_message_id": gmail_message_id,
        "approval_gmail_thread_id": gmail_thread_id,
        "approval_google_spreadsheet_id": google_spreadsheet_id,
        "approval_google_spreadsheet_url": google_spreadsheet_url,
    })
    stage.selection_counts = metadata


def resume_approval_processing(
    session: Session, run_id: str, *, now: datetime | None = None,
) -> None:
    """Stop human waiting as soon as named approval input is received."""
    start_stage(
        session, run_id, StageName.APPROVAL_GATE, expected_count=1,
        reason="validating named approval", now=now,
    )


def complete_approval_gate(
    session: Session, run_id: str, *, available_company_count: int,
    selected_company_count: int, selected_job_count: int,
    now: datetime | None = None,
) -> None:
    """Atomically persist named selection, close waiting, and approve the run."""
    run = session.get(DecisionRun, run_id)
    if run is None:
        raise StageTransitionError(f"unknown decision run {run_id}")
    if not 0 <= selected_company_count <= available_company_count:
        raise StageTransitionError("selected company count must be within available company count")
    if selected_job_count < 0:
        raise StageTransitionError("selected job count must be non-negative")
    timestamp = _now(now)
    stage = _stage(session, run_id, StageName.APPROVAL_GATE)
    if _status(stage.status) is not StageStatus.RUNNING:
        raise StageTransitionError("approval completion requires received input under active validation")
    counts = StageCounts(1, 1, 0, 0, 0)
    update_stage(
        session, run_id, StageName.APPROVAL_GATE, counts=counts,
        reason_counts={}, reason="named approval recorded", now=timestamp,
    )
    stage.selection_counts = {
        "available_companies": available_company_count,
        "selected_companies": selected_company_count,
        "selected_jobs": selected_job_count,
    }
    finish_stage(
        session, run_id, StageName.APPROVAL_GATE,
        counts=counts, reason_counts={},
        reason="named approval recorded", now=timestamp,
    )
    run.state = "approved"
    run.approved_at = timestamp


def open_approval_gate(
    session: Session, run_id: str, *, available_company_count: int,
    now: datetime | None = None,
) -> None:
    """Compatibility convenience: preparation and readiness at one instant."""
    begin_approval_preparation(session, run_id, now=now)
    mark_approval_artifact_ready(
        session, run_id, available_company_count=available_company_count, now=now,
    )


def fail_stage(session: Session, run_id: str, name: str | StageName, *,
               counts: StageCounts | None = None,
               reason_counts: dict[str, int] | None = None,
               records: Iterable[StageRecord] | None = None,
               reason: str, now: datetime | None = None) -> None:
    _finish(session, run_id, name, status=StageStatus.FAILED, counts=counts,
            reason_counts=reason_counts, records=records, reason=reason, now=now)


def interrupt_stage(session: Session, run_id: str, name: str | StageName, *, reason: str,
                    now: datetime | None = None) -> None:
    _finish(session, run_id, name, status=StageStatus.INTERRUPTED, counts=None,
            reason_counts=None, records=None, reason=reason, now=now)


def wait_for_approval(session: Session, run_id: str, name: str | StageName, *, reason: str,
                      now: datetime | None = None) -> None:
    stage = _stage(session, run_id, name)
    if _status(stage.status) is not StageStatus.RUNNING:
        raise StageTransitionError(f"cannot wait stage {stage.name!r} from {stage.status}")
    timestamp, attempt = _now(now), _attempt(session, stage)
    normalized_reason = _require_reason(StageStatus.WAITING_FOR_APPROVAL, reason)
    stage.reason = attempt.reason = normalized_reason
    stage.waiting_started_at = timestamp
    stage.updated_at = stage.heartbeat_at = attempt.heartbeat_at = timestamp
    attempt.status = StageStatus.WAITING_FOR_APPROVAL.value
    _transition(session, stage, to_status=StageStatus.WAITING_FOR_APPROVAL,
                now=timestamp, reason=normalized_reason, attempt=attempt)


def skip_stage(session: Session, run_id: str, name: str | StageName, *, reason: str,
               expected_count: int = 0, now: datetime | None = None) -> None:
    if session.get(DecisionRun, run_id) is None:
        raise StageTransitionError(f"unknown decision run {run_id}")
    stage_name, timestamp = _name(name), _now(now)
    stage = session.execute(select(DecisionRunStage).where(
        DecisionRunStage.run_id == run_id, DecisionRunStage.name == stage_name,
    )).scalar_one_or_none()
    if stage is None:
        counts = StageCounts(expected_count, 0, 0, 0, expected_count)
        _validate_counts(counts)
        stage = DecisionRunStage(run_id=run_id, name=stage_name, status=StageStatus.PENDING.value,
                                 updated_at=timestamp, reason_counts={})
        _apply_counts(stage, counts, {})
        session.add(stage)
        session.flush()
    source = _status(stage.status)
    if StageStatus.SKIPPED not in ALLOWED_TRANSITIONS.get(source, set()):
        raise StageTransitionError(f"cannot skip stage {stage.name!r} from {source.value}")
    normalized_reason = _require_reason(StageStatus.SKIPPED, reason)
    stage.reason = normalized_reason
    stage.updated_at = stage.ended_at = timestamp
    attempt = _attempt(session, stage) if source is StageStatus.WAITING_FOR_APPROVAL else None
    if attempt is not None:
        attempt.status = StageStatus.SKIPPED.value
        attempt.reason = normalized_reason
        attempt.ended_at = timestamp
    _transition(session, stage, to_status=StageStatus.SKIPPED, now=timestamp,
                reason=normalized_reason, attempt=attempt)


def _default_process_is_alive(process_id: int | None) -> bool:
    if not process_id:
        return False
    try:
        os.kill(process_id, 0)
    except OSError:
        return False
    return True


def _is_stale(stage: DecisionRunStage, attempt: DecisionRunStageAttempt, *, now: datetime,
              stale_after: timedelta,
              process_is_alive: Callable[[int | None], bool]) -> bool:
    heartbeat = _now(stage.heartbeat_at or stage.updated_at)
    if attempt.process_id is None:
        return now - heartbeat > stale_after
    return not process_is_alive(attempt.process_id) or now - heartbeat > stale_after


def reconcile_stale_stages(session: Session, run_id: str, *, now: datetime | None = None,
                           stale_after: timedelta = timedelta(minutes=5),
                           process_is_alive: Callable[[int | None], bool] = _default_process_is_alive) -> int:
    """Explicit writer seam for durable stale-work reconciliation."""
    timestamp, reconciled = _now(now), 0
    stages = list(session.execute(select(DecisionRunStage).where(
        DecisionRunStage.run_id == run_id,
        DecisionRunStage.status == StageStatus.RUNNING.value,
    )).scalars())
    for stage in stages:
        attempt = _attempt(session, stage)
        if _is_stale(stage, attempt, now=timestamp, stale_after=stale_after,
                     process_is_alive=process_is_alive):
            interrupt_stage(session, run_id, stage.name,
                            reason="process_missing_or_heartbeat_stale", now=timestamp)
            reconciled += 1
    return reconciled


def _records_for_attempt(session: Session, attempt_id: int) -> tuple[StageRecord, ...]:
    return tuple(StageRecord(row.record_type, row.record_id, row.outcome, row.reason)
                 for row in session.execute(select(DecisionRunStageRecord).where(
                     DecisionRunStageRecord.attempt_id == attempt_id,
                 ).order_by(DecisionRunStageRecord.id)).scalars())


def read_run_progress(session: Session, run_id: str, *, now: datetime | None = None,
                      stale_after: timedelta = timedelta(minutes=5),
                      process_is_alive: Callable[[int | None], bool] = _default_process_is_alive,
                      project_interruptions: bool = True) -> RunProgress:
    """Pure read seam; stale interruption is projected and never persisted here."""
    run = session.get(DecisionRun, run_id)
    if run is None:
        raise StageTransitionError(f"unknown decision run {run_id}")
    timestamp = _now(now)
    stages = list(session.execute(select(DecisionRunStage).where(
        DecisionRunStage.run_id == run_id,
    ).order_by(DecisionRunStage.id)).scalars())

    def as_read(stage: DecisionRunStage) -> StageRead:
        attempt_rows = list(session.execute(select(DecisionRunStageAttempt).where(
            DecisionRunStageAttempt.stage_id == stage.id,
        ).order_by(DecisionRunStageAttempt.attempt_number)).scalars())
        attempts = tuple(StageAttemptRead(
            attempt_number=row.attempt_number,
            status=row.status,
            process_id=row.process_id,
            counts=_counts_from_row(row),
            reason_counts=dict(row.reason_counts or {}),
            reason=row.reason,
            failed_record_dispositions=_deserialize_failed_record_dispositions(
                row.failed_record_dispositions
            ),
            started_at=_now(row.started_at),
            heartbeat_at=_now(row.heartbeat_at),
            ended_at=_now(row.ended_at) if row.ended_at else None,
            records=_records_for_attempt(session, row.id),
        ) for row in attempt_rows)
        projected = False
        display_status, display_reason = stage.status, stage.reason
        if project_interruptions and stage.status == StageStatus.RUNNING.value and attempt_rows:
            projected = _is_stale(stage, attempt_rows[-1], now=timestamp,
                                  stale_after=stale_after, process_is_alive=process_is_alive)
            if projected:
                display_status = StageStatus.INTERRUPTED.value
                display_reason = "process_missing_or_heartbeat_stale"
        transitions = tuple(StageTransition(row.to_status, row.from_status, row.reason,
                                            _now(row.occurred_at))
                            for row in session.execute(select(DecisionRunStageTransition).where(
                                DecisionRunStageTransition.stage_id == stage.id,
                            ).order_by(DecisionRunStageTransition.id)).scalars())
        waiting_seconds = int(stage.waiting_seconds or 0)
        waiting_started_at = _now(stage.waiting_started_at) if stage.waiting_started_at else None
        if display_status == StageStatus.WAITING_FOR_APPROVAL.value and waiting_started_at:
            waiting_seconds += max(0, int((timestamp - waiting_started_at).total_seconds()))
        active_end = _now(stage.ended_at) if stage.ended_at else timestamp
        active_started = _now(stage.started_at) if stage.started_at else None
        active_elapsed = max(0.0, (active_end - active_started).total_seconds() - waiting_seconds) if active_started else None
        selection_counts = dict(stage.selection_counts or {})
        return StageRead(
            name=stage.name,
            status=display_status,
            counts=_counts_from_row(stage),
            reason_counts=dict(stage.reason_counts or {}),
            reason=display_reason,
            started_at=_now(stage.started_at) if stage.started_at else None,
            updated_at=_now(stage.updated_at),
            ended_at=_now(stage.ended_at) if stage.ended_at else None,
            heartbeat_at=_now(stage.heartbeat_at) if stage.heartbeat_at else None,
            attempt_number=stage.attempt_number,
            transitions=transitions,
            attempts=attempts,
            waiting_started_at=waiting_started_at,
            waiting_seconds=waiting_seconds,
            active_elapsed_seconds=active_elapsed,
            selected_company_count=selection_counts.get("selected_companies"),
            available_company_count=selection_counts.get("available_companies"),
            selected_job_count=selection_counts.get("selected_jobs"),
            failed_record_dispositions=_deserialize_failed_record_dispositions(
                stage.failed_record_dispositions
            ),
            projected_interruption=projected,
        )

    return RunProgress(run_id, legacy_telemetry=run.telemetry_version is None,
                       stages=tuple(as_read(stage) for stage in stages))
