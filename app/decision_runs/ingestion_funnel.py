"""Durable Decision Run telemetry for the ingestion job funnel."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from sqlalchemy import delete, select, tuple_
from sqlalchemy.orm import Session

from app.models.orm import (
    DecisionRun,
    DecisionRunCanonicalJob,
    DecisionRunIngestionObservation,
    Job,
    JobPostingVersion,
)
from app.pipeline.runner import GateOutcome, IngestionGateFacts

from .progress import (
    StageCounts,
    StageName,
    StageRecord,
    StageTransitionError,
    complete_with_errors_stage,
    finish_stage,
    read_run_progress,
    start_stage,
)
from .telemetry import CURRENT_TELEMETRY_VERSION


@dataclass(frozen=True)
class IngestionObservation:
    """Ingestion-owned immutable value; generic progress only stores its ID."""

    source: str
    external_job_id: str
    connector_instance: int
    connector_dimensions: dict[str, object]
    fetch_attempt: int
    occurrence_ordinal: int
    job_url: str | None
    apply_url: str | None

    @classmethod
    def from_gate_outcome(cls, fact: GateOutcome) -> "IngestionObservation":
        job = fact.observation.job
        return cls(
            source=job.source,
            external_job_id=job.external_job_id,
            connector_instance=fact.source_instance,
            connector_dimensions=dict(fact.connector_dimensions),
            fetch_attempt=fact.observation.attempt,
            occurrence_ordinal=fact.observation.ordinal,
            job_url=job.job_url,
            apply_url=job.apply_url,
        )

    @classmethod
    def from_row(cls, row: DecisionRunIngestionObservation) -> "IngestionObservation":
        return cls(
            source=row.source,
            external_job_id=row.external_job_id,
            connector_instance=row.connector_instance,
            connector_dimensions=dict(row.connector_dimensions or {}),
            fetch_attempt=row.fetch_attempt,
            occurrence_ordinal=row.occurrence_ordinal,
            job_url=row.job_url,
            apply_url=row.apply_url,
        )

    @property
    def identity(self) -> tuple[int, int, int]:
        return self.connector_instance, self.fetch_attempt, self.occurrence_ordinal

    def to_row(self, run_id: str) -> DecisionRunIngestionObservation:
        return DecisionRunIngestionObservation(
            run_id=run_id,
            source=self.source,
            external_job_id=self.external_job_id,
            connector_instance=self.connector_instance,
            connector_dimensions=self.connector_dimensions,
            fetch_attempt=self.fetch_attempt,
            occurrence_ordinal=self.occurrence_ordinal,
            job_url=self.job_url,
            apply_url=self.apply_url,
        )

    def to_stage_record(
        self, observation_id: int, *, outcome: str, reason: str | None,
    ) -> StageRecord:
        return StageRecord("ingestion_observation", str(observation_id), outcome, reason)


@dataclass(frozen=True)
class CanonicalJobReference:
    observation_id: int


def load_canonical_job_manifest(
    session: Session, run_id: str,
) -> tuple[CanonicalJobReference, ...]:
    """Recover the exact relevance-kept manifest after a stage-boundary crash."""
    relevance = next((stage for stage in read_run_progress(
        session, run_id, project_interruptions=False,
    ).stages if stage.name == StageName.RELEVANCE_STORAGE.value), None)
    if relevance is None or relevance.status not in {"completed", "completed_with_errors"}:
        raise StageTransitionError("canonical manifest requires terminal relevance storage")
    records = relevance.attempts[-1].records if relevance.attempts else ()
    return tuple(
        CanonicalJobReference(int(record.record_id))
        for record in records
        if record.record_type == "ingestion_observation" and record.outcome == "advanced"
    )


def _persist_observations(
    session: Session, run_id: str, facts: tuple[GateOutcome, ...],
) -> dict[tuple[int, int, int], DecisionRunIngestionObservation]:
    rows: dict[tuple[int, int, int], DecisionRunIngestionObservation] = {}
    for fact in facts:
        value = IngestionObservation.from_gate_outcome(fact)
        if value.identity in rows:
            continue
        row = value.to_row(run_id)
        session.add(row)
        rows[value.identity] = row
    session.flush()
    return rows


def _finish_facts_stage(
    session: Session,
    run_id: str,
    stage: StageName,
    facts: tuple[GateOutcome, ...],
    observations: dict[tuple[int, int, int], DecisionRunIngestionObservation],
    *,
    reason: str,
) -> None:
    advanced = sum(fact.outcome == "advanced" for fact in facts)
    dropped = sum(fact.outcome == "dropped" for fact in facts)
    failed = sum(fact.outcome == "failed" for fact in facts)
    reasons: dict[str, int] = {}
    records = []
    for fact in facts:
        if fact.reason:
            reasons[fact.reason] = reasons.get(fact.reason, 0) + 1
        value = IngestionObservation.from_gate_outcome(fact)
        records.append(value.to_stage_record(
            observations[value.identity].id,
            outcome=fact.outcome,
            reason=fact.reason,
        ))
    start_stage(session, run_id, stage, expected_count=len(facts), reason=reason)
    finisher = complete_with_errors_stage if failed else finish_stage
    finisher(
        session,
        run_id,
        stage,
        counts=StageCounts(len(facts), advanced, dropped, failed, 0),
        reason_counts=reasons,
        records=records,
        reason=reason,
    )


def report_ingestion_funnel(
    session: Session, run_id: str, facts: IngestionGateFacts,
) -> tuple[CanonicalJobReference, ...]:
    """Persist facts emitted by the canonical gate without re-gating."""
    observations = _persist_observations(session, run_id, facts.raw)
    _finish_facts_stage(
        session, run_id, StageName.COLLECTION_DEDUPLICATION,
        facts.collection, observations, reason="collection uniqueness recorded",
    )
    _finish_facts_stage(
        session, run_id, StageName.RELEVANCE_STORAGE,
        facts.relevance, observations, reason="central relevance gate completed",
    )
    return tuple(
        CanonicalJobReference(
            observations[IngestionObservation.from_gate_outcome(fact).identity].id
        )
        for fact in facts.relevance
        if fact.outcome == "advanced"
    )


def report_canonical_jobs(
    session: Session, run_id: str, manifest: Iterable[CanonicalJobReference],
) -> None:
    """Close canonicalisation from durable relevance-kept observations."""
    refs = tuple(manifest)
    observation_ids = [ref.observation_id for ref in refs]
    observation_rows = list(session.execute(select(DecisionRunIngestionObservation).where(
        DecisionRunIngestionObservation.run_id == run_id,
        DecisionRunIngestionObservation.id.in_(observation_ids),
    )).scalars()) if observation_ids else []
    observations = {row.id: row for row in observation_rows}
    identities = {(row.source, row.external_job_id) for row in observation_rows}
    jobs = list(session.execute(select(Job).where(
        tuple_(Job.source, Job.external_job_id).in_(identities)
    )).scalars()) if identities else []
    jobs_by_identity = {(job.source, job.external_job_id): job for job in jobs}

    existing_stage = next((stage for stage in read_run_progress(
        session, run_id, project_interruptions=False,
    ).stages if stage.name == StageName.JOB_DEDUPLICATION.value), None)
    if existing_stage is not None and existing_stage.status == "completed_with_errors":
        session.execute(delete(DecisionRunCanonicalJob).where(
            DecisionRunCanonicalJob.run_id == run_id,
        ))

    records: list[StageRecord] = []
    canonical_rows: list[DecisionRunCanonicalJob] = []
    for ref in refs:
        observation = observations[ref.observation_id]
        job = jobs_by_identity.get((observation.source, observation.external_job_id))
        if job is None:
            records.append(IngestionObservation.from_row(observation).to_stage_record(
                observation.id, outcome="failed", reason="upsert_missing",
            ))
        elif job.duplicate_of_job_id is not None:
            records.append(IngestionObservation.from_row(observation).to_stage_record(
                observation.id, outcome="dropped", reason="canonical_duplicate",
            ))
        else:
            version = session.execute(select(JobPostingVersion).where(
                JobPostingVersion.job_id == job.id,
            ).order_by(JobPostingVersion.id.desc())).scalars().first()
            if version is None:
                records.append(IngestionObservation.from_row(observation).to_stage_record(
                    observation.id, outcome="failed", reason="posting_version_missing",
                ))
                continue
            records.append(IngestionObservation.from_row(observation).to_stage_record(
                observation.id, outcome="advanced", reason=None,
            ))
            canonical_rows.append(DecisionRunCanonicalJob(
                run_id=run_id,
                ingestion_observation_id=observation.id,
                job_id=job.id,
                posting_version_id=version.id,
            ))

    session.add_all(canonical_rows)
    run = session.get(DecisionRun, run_id)
    if run is not None:
        run.telemetry_version = CURRENT_TELEMETRY_VERSION

    advanced = sum(record.outcome == "advanced" for record in records)
    dropped = sum(record.outcome == "dropped" for record in records)
    failed = sum(record.outcome == "failed" for record in records)
    reasons: dict[str, int] = {}
    for record in records:
        if record.reason:
            reasons[record.reason] = reasons.get(record.reason, 0) + 1
    start_stage(session, run_id, StageName.JOB_DEDUPLICATION, expected_count=len(refs),
                reason="checking cross-source canonical jobs")
    finisher = complete_with_errors_stage if failed else finish_stage
    finisher(
        session,
        run_id,
        StageName.JOB_DEDUPLICATION,
        counts=StageCounts(len(refs), advanced, dropped, failed, 0),
        reason_counts=reasons,
        records=records,
        reason="some kept jobs were not stored" if failed else "cross-source canonicalisation completed",
    )
