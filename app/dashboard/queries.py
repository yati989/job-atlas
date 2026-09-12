"""
Query layer for the analysis dashboard (issue #46).

Pure functions only — no Streamlit imports here. Each function takes a
`DashboardFilters` instance plus a SQLAlchemy `Session` and returns a pandas
`DataFrame`. `app.py` renders these; `scripts/verify_dashboard_queries.py`
exercises them directly against the real DB.

Queries derived from enrichment-only fields (skills, seniority, experience
years, industry) filter to rows where that field is non-null rather than
silently computing over the full (mostly-unenriched) dataset. Those
DataFrames carry the row count that contributed via `df.attrs["n"]`, so
callers (namely `app.py`) can surface sample size next to the chart.
"""
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import pandas as pd
from sqlalchemy import func, cast, Date, select, tuple_
from sqlalchemy.orm import Session

from app.config.categories import CATEGORY_KEYWORDS, classify_title
from app.models.orm import (
    Company, Contact, DecisionRun, DecisionRunIngestionObservation,
    DecisionRunFinding, DecisionRunJob, DecisionRunJobEnrichmentManifest,
    DecisionRunCompanyFunnelManifest, DecisionRunCompanyPhaseBEvidence,
    DecisionRunContactEnrichmentManifest, DecisionRunResumeTailoringManifest,
    DecisionRunDraftPreparationManifest, DecisionRunGmailDraftManifest,
    Job, JobPostingVersion, JobSkill,
)
from app.decision_runs.progress import StageName, read_run_progress
from app.decision_runs.enrichment_funnel import TerminalJobOutcome, terminal_job_outcome
from app.decision_runs.read_models import decision_run_progress


def decision_run_options(session: Session) -> pd.DataFrame:
    """Durable run-selector read model; legacy runs intentionally remain selectable."""
    runs = list(session.execute(select(DecisionRun).order_by(DecisionRun.created_at.desc())).scalars())
    rows = [{
        "run_id": run.id,
        "state": run.state,
        "since_at": run.since_at,
        "cutoff_at": run.cutoff_at,
        "started_at": run.created_at,
        "created_at": run.created_at,
        "telemetry": (
            "incomplete legacy telemetry" if run.telemetry_version is None else "complete"
        ),
    } for run in runs]
    result = pd.DataFrame(rows, columns=[
        "run_id", "state", "since_at", "cutoff_at", "started_at", "created_at", "telemetry",
    ])
    return result


def default_decision_run_id(options: pd.DataFrame) -> str | None:
    """Choose the newest active run, falling back to the newest durable run."""
    if options.empty:
        return None
    active = options[options["state"].isin(("preparing", "awaiting_approval", "approved", "processing"))]
    return str((active.iloc[0] if not active.empty else options.iloc[0])["run_id"])


_JOB_FUNNEL_STAGES = (
    (StageName.COLLECTION_DEDUPLICATION.value, "Raw fetched", "Collection-unique"),
    (StageName.RELEVANCE_STORAGE.value, "Collection-unique", "Relevance-kept"),
    (StageName.JOB_DEDUPLICATION.value, "Relevance-kept", "Canonical jobs"),
    (StageName.JOB_ENRICHMENT.value, "Canonical jobs", "Enriched"),
)

# PostgreSQL can exceed its parser stack depth when a decision-run drilldown
# expands tens of thousands of composite ``(source, external_job_id)`` values
# into one ``IN`` expression.  Keep each hydration query comfortably small;
# the dashboard combines the results in memory.
_DRILLDOWN_IDENTITY_CHUNK_SIZE = 500


def _chunks(values: list[tuple[str, str]], size: int | None = None):
    size = size or _DRILLDOWN_IDENTITY_CHUNK_SIZE
    for start in range(0, len(values), size):
        yield values[start:start + size]


def decision_run_job_funnel(session: Session, run_id: str) -> pd.DataFrame:
    """Canonical funnel totals; dashboard filters never enter this query."""
    stages = {stage.name: stage for stage in read_run_progress(session, run_id).stages}
    rows = []
    for stage_name, input_label, output_label in _JOB_FUNNEL_STAGES:
        stage = stages.get(stage_name)
        rows.append({
            "stage": stage_name,
            "from": input_label,
            "to": output_label,
            "input": stage.counts.input if stage else None,
            "advanced": stage.counts.advanced if stage else None,
            "dropped": stage.counts.dropped if stage else None,
            "failed": stage.counts.failed if stage else None,
            "pending": stage.counts.pending if stage else None,
            "available": stage is not None,
        })
    screening = stages.get(StageName.SCREENING_RANKING_GROUPING.value)
    outcomes = list(session.execute(select(DecisionRunJob).where(
        DecisionRunJob.run_id == run_id,
    )).scalars())
    if screening is None:
        eligible = sum(terminal_job_outcome(item.outcome) is TerminalJobOutcome.ELIGIBLE for item in outcomes)
        anomalies = sum(terminal_job_outcome(item.outcome) is TerminalJobOutcome.ANOMALY for item in outcomes)
        rejected = sum(terminal_job_outcome(item.outcome) is TerminalJobOutcome.REJECTED for item in outcomes)
        rows.append({
            "stage": StageName.SCREENING_RANKING_GROUPING.value,
            "from": "Enriched", "to": "Eligible / Anomalies / Rejected",
            "input": len(outcomes) if outcomes else None,
            "advanced": None, "dropped": None, "failed": None,
            "pending": len(outcomes) if outcomes else None,
            "eligible": eligible if outcomes else None,
            "anomalies": anomalies if outcomes else None,
            "rejected": rejected if outcomes else None,
            "available": False, "outcomes_available": bool(outcomes),
        })
    else:
        rows.append({
            "stage": StageName.SCREENING_RANKING_GROUPING.value,
            "from": "Enriched", "to": "Eligible / Anomalies / Rejected",
            "input": screening.counts.input,
            "advanced": screening.counts.advanced,
            "dropped": screening.counts.dropped,
            "failed": screening.counts.failed,
            "pending": screening.counts.pending,
            "eligible": sum(terminal_job_outcome(item.outcome) is TerminalJobOutcome.ELIGIBLE for item in outcomes),
            "anomalies": sum(terminal_job_outcome(item.outcome) is TerminalJobOutcome.ANOMALY for item in outcomes),
            "rejected": sum(terminal_job_outcome(item.outcome) is TerminalJobOutcome.REJECTED for item in outcomes),
            "available": True,
            "outcomes_available": True,
        })
    return pd.DataFrame(rows)


def decision_run_resume_tailoring_funnel(session: Session, run_id: str) -> pd.DataFrame:
    """One immutable approved-scope tailoring transition, separate from jobs."""
    stage = next((item for item in read_run_progress(session, run_id).stages
                  if item.name == StageName.RESUME_TAILORING.value), None)
    return pd.DataFrame([{
        "stage": StageName.RESUME_TAILORING.value,
        "from": "Approved posting versions", "to": "Tailored",
        "input": stage.counts.input if stage else None,
        "tailored": stage.counts.advanced if stage else None,
        "dropped": stage.counts.dropped if stage else None,
        "failed": stage.counts.failed if stage else None,
        "pending": stage.counts.pending if stage else None,
        "available": stage is not None,
    }])


def decision_run_resume_tailoring_drilldown(
    session: Session, run_id: str, *, reason: str | None = None,
) -> pd.DataFrame:
    """Exact approved records with source URLs; this never changes the total."""
    rows = list(session.execute(select(
        DecisionRunResumeTailoringManifest, Job, JobPostingVersion,
    ).join(Job, Job.id == DecisionRunResumeTailoringManifest.job_id).join(
        JobPostingVersion, JobPostingVersion.id == DecisionRunResumeTailoringManifest.posting_version_id,
    ).where(DecisionRunResumeTailoringManifest.run_id == run_id).order_by(
        DecisionRunResumeTailoringManifest.id,
    )).all())
    result = []
    for item, job, version in rows:
        if reason is not None and item.reason != reason:
            continue
        snapshot = version.snapshot if isinstance(version.snapshot, dict) else {}
        result.append({
            "posting_version_id": item.posting_version_id, "job_id": item.job_id,
            "outcome": item.outcome or "pending", "reason": item.reason,
            "failure_disposition": item.failure_disposition, "source": job.source,
            "external_job_id": job.external_job_id,
            "job_url": snapshot.get("job_url") or snapshot.get("apply_url") or job.job_url or job.apply_url,
            "apply_url": snapshot.get("apply_url") or snapshot.get("job_url") or job.apply_url or job.job_url,
            "snapshot": version.snapshot,
        })
    return pd.DataFrame(result, columns=[
        "posting_version_id", "job_id", "outcome", "reason", "failure_disposition",
        "source", "external_job_id", "job_url", "apply_url", "snapshot",
    ])


def decision_run_outreach_funnel(session: Session, run_id: str) -> pd.DataFrame:
    """Frozen draft and Gmail-Draft counts; never reads the global backlog."""
    stages = {item.name: item for item in read_run_progress(session, run_id).stages}
    draft = stages.get(StageName.DRAFT_PREPARATION.value)
    gmail = stages.get(StageName.GMAIL_DRAFT_POSTING.value)
    return pd.DataFrame([
        {"stage": StageName.DRAFT_PREPARATION.value, "from": "Approved contacts / pairings", "to": "Copy prepared",
         "input": draft.counts.input if draft else None, "prepared": draft.counts.advanced if draft else None,
         "dropped": draft.counts.dropped if draft else None, "failed": draft.counts.failed if draft else None,
         "pending": draft.counts.pending if draft else None, "available": draft is not None},
        {"stage": StageName.GMAIL_DRAFT_POSTING.value, "from": "Copy prepared", "to": "Gmail Draft exists",
         "input": gmail.counts.input if gmail else None, "posted": gmail.counts.advanced if gmail else None,
         "dropped": gmail.counts.dropped if gmail else None, "failed": gmail.counts.failed if gmail else None,
         "pending": gmail.counts.pending if gmail else None, "available": gmail is not None},
    ])


def decision_run_outreach_funnel_drilldown(session: Session, run_id: str, *, stage_name: str) -> pd.DataFrame:
    """Recipient/job identifiers and source links for the selected funnel stage."""
    columns = ["stage", "contact_id", "company_id", "outreach_draft_id", "outcome", "reason",
               "failure_disposition", "candidate_job_ids", "email", "job_url", "apply_url"]
    if stage_name == StageName.DRAFT_PREPARATION.value:
        rows = _draft_rows = list(session.execute(select(DecisionRunDraftPreparationManifest).where(
            DecisionRunDraftPreparationManifest.run_id == run_id)).scalars())
        result = []
        for row in rows:
            contact = session.get(Contact, row.contact_id)
            links = _outreach_job_links(session, row.posting_version_id, row.candidate_job_ids or [])
            result.append({"stage": stage_name, "contact_id": row.contact_id, "company_id": row.company_id,
                "outreach_draft_id": row.outreach_draft_id, "outcome": row.outcome or "pending", "reason": row.reason,
                "failure_disposition": row.failure_disposition, "candidate_job_ids": row.candidate_job_ids,
                "email": contact.email_guess if contact else None, **links})
        return pd.DataFrame(result, columns=columns)
    if stage_name == StageName.GMAIL_DRAFT_POSTING.value:
        rows = list(session.execute(select(DecisionRunGmailDraftManifest, DecisionRunDraftPreparationManifest).join(
            DecisionRunDraftPreparationManifest, DecisionRunGmailDraftManifest.draft_preparation_manifest_id == DecisionRunDraftPreparationManifest.id
        ).where(DecisionRunGmailDraftManifest.run_id == run_id)).all())
        result=[]
        for gmail, draft in rows:
            contact=session.get(Contact,draft.contact_id); links=_outreach_job_links(session,draft.posting_version_id,draft.candidate_job_ids or [])
            result.append({"stage":stage_name,"contact_id":draft.contact_id,"company_id":draft.company_id,
                "outreach_draft_id":gmail.outreach_draft_id,"outcome":gmail.outcome or "pending","reason":gmail.reason,
                "failure_disposition":gmail.failure_disposition,"candidate_job_ids":draft.candidate_job_ids,
                "email":contact.email_guess if contact else None,**links})
        return pd.DataFrame(result,columns=columns)
    return pd.DataFrame(columns=columns)


def _outreach_job_links(session: Session, posting_version_id: int | None, job_ids: list[int]) -> dict:
    version = session.get(JobPostingVersion, posting_version_id) if posting_version_id else None
    snapshot = version.snapshot if version is not None and isinstance(version.snapshot, dict) else {}
    job = session.get(Job, job_ids[0]) if job_ids else None
    return {"job_url": snapshot.get("job_url") or snapshot.get("apply_url") or (job.job_url if job else None),
            "apply_url": snapshot.get("apply_url") or snapshot.get("job_url") or (job.apply_url if job else None)}


def decision_run_job_funnel_drilldown(
    session: Session, run_id: str, *, stage_name: str,
    source: str | None = None, reason: str | None = None,
) -> pd.DataFrame:
    """Return a subset beside the canonical funnel, never a filtered total."""
    stage = next((item for item in read_run_progress(session, run_id).stages if item.name == stage_name), None)
    columns = [
        "stage", "record_type", "record_id", "outcome", "reason", "source",
        "external_job_id", "connector_instance", "connector_dimensions",
        "fetch_attempt", "occurrence_ordinal", "posting_version_id",
        "failure_disposition", "job_url", "apply_url",
    ]
    if stage_name == StageName.SCREENING_RANKING_GROUPING.value:
        run_jobs = list(session.execute(select(DecisionRunJob).where(
            DecisionRunJob.run_id == run_id,
        ).order_by(DecisionRunJob.id)).scalars())
        findings = list(session.execute(select(DecisionRunFinding).join(
            DecisionRunJob, DecisionRunFinding.decision_run_job_id == DecisionRunJob.id,
        ).where(DecisionRunJob.run_id == run_id).order_by(DecisionRunFinding.id)).scalars())
        findings_by_job: dict[int, list[DecisionRunFinding]] = {}
        for finding in findings:
            findings_by_job.setdefault(finding.decision_run_job_id, []).append(finding)
        rows = []
        for run_job in run_jobs:
            snapshot = run_job.snapshot if isinstance(run_job.snapshot, dict) else {}
            terminal = terminal_job_outcome(run_job.outcome).value
            item_findings = findings_by_job.get(run_job.id) or [None]
            for finding in item_findings:
                item_reason = finding.reason_code if finding else snapshot.get("anomaly")
                item_source = snapshot.get("source")
                if source is not None and item_source != source:
                    continue
                if reason is not None and item_reason != reason:
                    continue
                rows.append({
                    "stage": stage_name, "record_type": "posting_version",
                    "record_id": str(run_job.posting_version_id), "outcome": terminal,
                    "reason": item_reason, "source": item_source,
                    "external_job_id": snapshot.get("external_job_id"),
                    "connector_instance": None, "connector_dimensions": None,
                    "fetch_attempt": None, "occurrence_ordinal": None,
                    "posting_version_id": run_job.posting_version_id,
                    "failure_disposition": None,
                    "job_url": snapshot.get("job_url"), "apply_url": snapshot.get("apply_url"),
                })
        return pd.DataFrame(rows, columns=columns)
    if stage is None or not stage.attempts:
        return pd.DataFrame(columns=columns)
    latest = stage.attempts[-1]
    job_ids = [int(record.record_id) for record in latest.records if record.record_type == "job" and record.record_id.isdigit()]
    jobs = {str(job.id): job for job in session.execute(select(Job).where(Job.id.in_(job_ids))).scalars()} if job_ids else {}
    observation_ids = [
        int(record.record_id) for record in latest.records
        if record.record_type == "ingestion_observation" and record.record_id.isdigit()
    ]
    observations = {
        str(item.id): item for item in session.execute(
            select(DecisionRunIngestionObservation).where(
                DecisionRunIngestionObservation.run_id == run_id,
                DecisionRunIngestionObservation.id.in_(observation_ids),
            )
        ).scalars()
    } if observation_ids else {}
    observation_identities = {
        (item.source, item.external_job_id) for item in observations.values()
    }
    observation_jobs = {}
    # Do not compile every collection identity into one SQL tuple-IN clause.
    # Large attended runs can hold tens of thousands of observations.
    for identity_chunk in _chunks(sorted(observation_identities)):
        observation_jobs.update({
            (job.source, job.external_job_id): job
            for job in session.execute(select(Job).where(
                tuple_(Job.source, Job.external_job_id).in_(identity_chunk)
            )).scalars()
        })
    posting_version_ids = [
        int(record.record_id) for record in latest.records
        if record.record_type == "posting_version" and record.record_id.isdigit()
    ]
    pending_manifest = []
    if stage_name == StageName.JOB_ENRICHMENT.value:
        pending_manifest = list(session.execute(
            select(DecisionRunJobEnrichmentManifest).where(
                DecisionRunJobEnrichmentManifest.run_id == run_id,
                DecisionRunJobEnrichmentManifest.outcome.is_(None),
            ).order_by(DecisionRunJobEnrichmentManifest.id)
        ).scalars())
        posting_version_ids.extend(item.posting_version_id for item in pending_manifest)
    versions = {
        str(version.id): version for version in session.execute(select(JobPostingVersion).where(
            JobPostingVersion.id.in_(posting_version_ids),
        )).scalars()
    } if posting_version_ids else {}
    enrichment_manifest = {
        str(item.posting_version_id): item for item in session.execute(
            select(DecisionRunJobEnrichmentManifest).where(
                DecisionRunJobEnrichmentManifest.run_id == run_id,
                DecisionRunJobEnrichmentManifest.posting_version_id.in_(posting_version_ids),
            )
        ).scalars()
    } if posting_version_ids else {}
    version_jobs = {
        version.id: session.get(Job, version.job_id) for version in versions.values()
    }
    rows = []
    for record in latest.records:
        job = jobs.get(record.record_id)
        observation = observations.get(record.record_id)
        version = versions.get(record.record_id)
        if observation is not None:
            job = observation_jobs.get((observation.source, observation.external_job_id))
        elif version is not None:
            job = version_jobs.get(version.id)
        record_source = observation.source if observation else (job.source if job else None)
        if source is not None and record_source != source:
            continue
        if reason is not None and record.reason != reason:
            continue
        rows.append({
            "stage": stage_name,
            "record_type": record.record_type,
            "record_id": record.record_id,
            "outcome": record.outcome,
            "reason": record.reason,
            "source": record_source,
            "external_job_id": observation.external_job_id if observation else (job.external_job_id if job else None),
            "connector_instance": observation.connector_instance if observation else None,
            "connector_dimensions": observation.connector_dimensions if observation else None,
            "fetch_attempt": observation.fetch_attempt if observation else None,
            "occurrence_ordinal": observation.occurrence_ordinal if observation else None,
            "posting_version_id": version.id if version else None,
            "failure_disposition": (
                enrichment_manifest[record.record_id].failure_disposition
                if record.record_id in enrichment_manifest else None
            ),
            "job_url": ((observation.job_url if observation else None)
                        or (version.snapshot.get("job_url") if version and isinstance(version.snapshot, dict) else None)
                        or (job.job_url if job else None)),
            "apply_url": (observation.apply_url if observation else None) or (job.apply_url if job else None),
        })
    if reason is None:
        for item in pending_manifest:
            version = versions.get(str(item.posting_version_id))
            job = version_jobs.get(version.id) if version is not None else None
            record_source = job.source if job else None
            if source is not None and record_source != source:
                continue
            snapshot = version.snapshot if version is not None and isinstance(version.snapshot, dict) else {}
            rows.append({
                "stage": stage_name, "record_type": "posting_version",
                "record_id": str(item.posting_version_id), "outcome": "pending",
                "reason": None, "source": record_source,
                "external_job_id": job.external_job_id if job else None,
                "connector_instance": None, "connector_dimensions": None,
                "fetch_attempt": None, "occurrence_ordinal": None,
                "posting_version_id": item.posting_version_id,
                "failure_disposition": None,
                "job_url": snapshot.get("job_url") or (job.job_url if job else None),
                "apply_url": snapshot.get("apply_url") or (job.apply_url if job else None),
            })
    return pd.DataFrame(rows, columns=columns)


def decision_run_company_funnel(session: Session, run_id: str) -> pd.DataFrame:
    """Canonical company totals; never accepts dashboard filters."""
    manifest = list(session.execute(select(DecisionRunCompanyFunnelManifest).where(
        DecisionRunCompanyFunnelManifest.run_id == run_id,
    ).order_by(DecisionRunCompanyFunnelManifest.company_id)).scalars())
    if not manifest:
        return pd.DataFrame(columns=["stage", "from", "to", "companies", "eligible_jobs", "advanced", "dropped", "failed", "pending", "duplicates", "found", "confirmed_missing", "source_error", "group_1", "group_2", "group_3", "group_4", "ungroupable", "available"])
    stages = {item.name: item for item in read_run_progress(session, run_id).stages}
    jobs = sum(row.eligible_job_count for row in manifest)
    def stage_row(stage_name, label, output, outcome_field):
        stage = stages.get(stage_name)
        return {"stage": stage_name, "from": label, "to": output,
                "companies": len(manifest), "eligible_jobs": jobs,
                "advanced": stage.counts.advanced if stage else None,
                "dropped": stage.counts.dropped if stage else None,
                "failed": stage.counts.failed if stage else None,
                "pending": stage.counts.pending if stage else None,
                "available": stage is not None,
                "found": sum(getattr(row, "phase_b_evidence_outcome", None) == "found" for row in manifest) if outcome_field == "phase_b_outcome" else None,
                "confirmed_missing": sum(getattr(row, "phase_b_evidence_outcome", None) == "confirmed_missing" for row in manifest) if outcome_field == "phase_b_outcome" else None,
                "source_error": sum(getattr(row, "phase_b_evidence_outcome", None) == "source_error" for row in manifest) if outcome_field == "phase_b_outcome" else None}
    dedup = stages.get(StageName.COMPANY_DEDUPLICATION.value)
    rows = [{"stage": StageName.COMPANY_DEDUPLICATION.value, "from": "Eligible jobs", "to": "Unique companies",
             "companies": len(manifest), "eligible_jobs": jobs,
             "advanced": dedup.counts.advanced if dedup else None, "dropped": 0 if dedup else None,
             "failed": dedup.counts.failed if dedup else None, "pending": dedup.counts.pending if dedup else None,
             "duplicates": jobs - len(manifest), "available": dedup is not None}]
    rows.append(stage_row(StageName.COMPANY_PHASE_A.value, "Unique companies", "Phase A enriched", "phase_a_outcome"))
    rows.append(stage_row(StageName.COMPANY_PHASE_B.value, "Phase A enriched", "Phase B enriched", "phase_b_outcome"))
    terminal = {key: sum(row.terminal_outcome == key for row in manifest) for key in ("group_1", "group_2", "group_3", "group_4", "ungroupable")}
    terminal_jobs = {f"{key}_eligible_jobs": sum(row.eligible_job_count for row in manifest if row.terminal_outcome == key) for key in terminal}
    rows.append({"stage": "company_terminal_outcomes", "from": "Phase B enriched", "to": "Group 1 / 2 / 3 / 4 / Ungroupable",
                 "companies": len(manifest), "eligible_jobs": jobs, "advanced": sum(terminal.values()), "dropped": 0,
                 "failed": 0, "pending": len(manifest) - sum(terminal.values()), "available": any(row.terminal_outcome for row in manifest), **terminal, **terminal_jobs})
    return pd.DataFrame(rows)


def decision_run_company_funnel_drilldown(session: Session, run_id: str, *, stage_name: str,
                                           source: str | None = None, reason: str | None = None) -> pd.DataFrame:
    """Company subset beside canonical totals, with every eligible job source link."""
    manifest = list(session.execute(select(DecisionRunCompanyFunnelManifest).where(
        DecisionRunCompanyFunnelManifest.run_id == run_id).order_by(DecisionRunCompanyFunnelManifest.company_id)).scalars())
    companies = {item.id: item for item in session.execute(select(Company).where(
        Company.id.in_([row.company_id for row in manifest]))).scalars()}
    run_jobs = list(session.execute(select(DecisionRunJob).where(
        DecisionRunJob.run_id == run_id, DecisionRunJob.outcome == "eligible")).scalars())
    jobs_by_company: dict[int, list[DecisionRunJob]] = {}
    for job in run_jobs: jobs_by_company.setdefault(job.company_id, []).append(job)
    evidence_by_manifest: dict[int, list[DecisionRunCompanyPhaseBEvidence]] = {}
    for evidence in session.execute(select(DecisionRunCompanyPhaseBEvidence).where(
        DecisionRunCompanyPhaseBEvidence.manifest_id.in_([item.id for item in manifest]))).scalars():
        evidence_by_manifest.setdefault(evidence.manifest_id, []).append(evidence)
    rows = []
    for item in manifest:
        if stage_name == StageName.COMPANY_PHASE_A.value: outcome, item_reason = item.phase_a_outcome or "pending", item.phase_a_reason
        elif stage_name == StageName.COMPANY_PHASE_B.value: outcome, item_reason = item.phase_b_outcome or "pending", item.phase_b_reason
        elif stage_name == "company_terminal_outcomes": outcome, item_reason = item.terminal_outcome or "pending", item.terminal_reason
        else: outcome, item_reason = "advanced", None
        for job in jobs_by_company.get(item.company_id, []):
            snapshot = job.snapshot or {}
            item_source = snapshot.get("source")
            if source is not None and item_source != source: continue
            if reason is not None and item_reason != reason: continue
            evidence = evidence_by_manifest.get(item.id, [])
            rows.append({"stage": stage_name, "company_id": item.company_id, "company": companies.get(item.company_id).name if item.company_id in companies else None,
                         "eligible_job_count": item.eligible_job_count, "outcome": outcome, "reason": item_reason,
                         "phase_b_evidence_outcome": item.phase_b_evidence_outcome, "source": item_source,
                         "posting_version_id": job.posting_version_id, "job_url": snapshot.get("job_url"), "apply_url": snapshot.get("apply_url"),
                         "company_evidence": [{"source": e.source, "outcome": e.outcome, "reason": e.reason, "disposition": e.failure_disposition, "url": e.evidence_url, "status": e.source_status} for e in evidence]})
    return pd.DataFrame(rows, columns=["stage", "company_id", "company", "eligible_job_count", "outcome", "reason", "phase_b_evidence_outcome", "source", "posting_version_id", "job_url", "apply_url", "company_evidence"])


def decision_run_contact_funnel(session: Session, run_id: str) -> pd.DataFrame:
    """One frozen approved company/function denominator, never global backlog."""
    rows = list(session.execute(select(DecisionRunContactEnrichmentManifest).where(
        DecisionRunContactEnrichmentManifest.run_id == run_id).order_by(
            DecisionRunContactEnrichmentManifest.company_id,
            DecisionRunContactEnrichmentManifest.search_group)).scalars())
    if not rows:
        return pd.DataFrame(columns=["stage", "from", "to", "input", "enriched", "exhausted", "no_match", "excluded", "failed", "pending", "available"])
    count = lambda outcome: sum(row.outcome == outcome for row in rows)
    return pd.DataFrame([{
        "stage": StageName.CONTACT_ENRICHMENT.value, "from": "Approved company functions",
        "to": "Contact coverage", "input": len(rows), "enriched": count("enriched"),
        "exhausted": count("exhausted"), "no_match": count("no_match"),
        "excluded": count("excluded"), "failed": count("failed"),
        "pending": count(None), "available": True,
    }])


def decision_run_contact_funnel_drilldown(session: Session, run_id: str, *,
                                           outcome: str | None = None,
                                           reason: str | None = None) -> pd.DataFrame:
    rows = list(session.execute(select(DecisionRunContactEnrichmentManifest).where(
        DecisionRunContactEnrichmentManifest.run_id == run_id).order_by(
            DecisionRunContactEnrichmentManifest.company_id,
            DecisionRunContactEnrichmentManifest.search_group)).scalars())
    companies = {item.id: item.name for item in session.execute(select(Company).where(
        Company.id.in_([row.company_id for row in rows]))).scalars()}
    result = []
    for row in rows:
        if outcome is not None and (row.outcome or "pending") != outcome:
            continue
        if reason is not None and row.reason != reason:
            continue
        result.append({"company_id": row.company_id, "company": companies.get(row.company_id),
                       "search_group": row.search_group, "outcome": row.outcome or "pending",
                       "reason": row.reason, "failure_disposition": row.failure_disposition,
                       "reused_contact_ids": row.reused_contact_ids or [], "new_contact_ids": row.new_contact_ids or [],
                       "coverage": row.coverage or {}})
    return pd.DataFrame(result, columns=["company_id", "company", "search_group", "outcome", "reason", "failure_disposition", "reused_contact_ids", "new_contact_ids", "coverage"])

# Friendly display labels for CATEGORY_KEYWORDS families (dashboard-only —
# the family keys themselves stay as defined in app.config.categories, this
# just renders them readably in the filter dropdown / charts).
JOB_CATEGORY_LABELS: dict[str, str] = {
    "data_science": "Data Science",
    "analytics": "DA (Analytics)",
    "credit_risk": "Credit Risk",
    "ai_ml_engineering": "AI/ML Engineering",
    "quant_decision_science": "Quant/Decision Science",
    "data_engineering": "Data Engineering",
}

# The four tiers a contact is actually classified into (see app/contacts/ladder.py).
# exec_fallback is excluded from the per-tier breakdown by request — it's a
# conditional fifth tier, not one of the four regularly-searched-for ones.
CONTACT_TIERS: list[str] = ["head", "hiring_manager", "ic", "talent_acquisition"]

# A single dashboard page render calls `_apply_job_filters` once per chart
# (~20 query functions), and both the category and contact-count filters do a
# Python-side full-table pass rather than a SQL WHERE. Without this cache, a
# render with either filter active repeats that full-table pass ~20 times.
# Plain time-based in-process cache (not st.cache_data) to keep this module
# Streamlit-free, per the module docstring.
_HELPER_CACHE_TTL_SECONDS = 300
_category_ids_cache: dict[str, tuple[list, float]] = {}
_contact_counts_cache: dict[str, tuple] = {"data": None, "ts": 0.0}

# Companies' `industry` is free-text (LLM-inferred per company, see
# CLAUDE.md's enrichment section), so raw values are highly fragmented
# ("Banking / Financial Services" vs "Financial Services / Banking" vs
# "Banking" all mean the same thing). Canonicalize into a small fixed set of
# buckets for display — ordered most-specific-first since e.g. "Fintech /
# Digital Banking" should land in Fintech, not Banking.
_INDUSTRY_BUCKETS = [
    ("Fintech", re.compile(r"fintech", re.I)),
    ("Staffing & Recruiting", re.compile(r"staffing|recruit", re.I)),
    ("Banking & Financial Services", re.compile(r"bank|financial services|nbfc|investment", re.I)),
    ("Insurance", re.compile(r"insurance", re.I)),
    ("Education / EdTech", re.compile(r"edtech|education", re.I)),
    ("Healthcare & Pharma", re.compile(r"health|pharma|biotech", re.I)),
    ("Cybersecurity", re.compile(r"cyber", re.I)),
    ("AI / ML / Data", re.compile(r"\b(ai|ml|llm)\b|data\s|analytics", re.I)),
    ("E-commerce & Marketplace", re.compile(r"e-?commerce|marketplace", re.I)),
    ("Media & Entertainment", re.compile(r"media|entertainment|streaming", re.I)),
    ("Consulting / Professional Services", re.compile(r"consult|professional services", re.I)),
]


def _canonicalize_industry(raw: str) -> str:
    for bucket, pattern in _INDUSTRY_BUCKETS:
        if pattern.search(raw):
            return bucket
    return "Other"


# `salary_raw` is free text straight from each connector — currencies, units,
# and formats vary wildly across sources (see issue #46 discussion): rupee
# ranges ("₹ 3,60,000 - 7,00,000"), "Lacs/LPA" ranges ("20-35 Lacs PA"),
# "INR ...K" ranges ("INR 1500K-2500K"), scraped "salary insights" blurbs
# ("₹10.2 - ₹11.3 L/yr"), plain USD figures, and non-numeric text
# ("Competitive"). Only INR values are parsed — USD/other-currency values are
# excluded rather than guessed at with a made-up exchange rate. Everything
# below resolves to Lakhs-per-annum (LPA) INR, the dominant unit in the data.
_LAKHS_RANGE = re.compile(
    r"₹?\s*(\d+(?:\.\d+)?)\s*(?:-|–|to)\s*₹?\s*(\d+(?:\.\d+)?)\s*"
    r"(?:lacs?|lakhs?|lpa|l/?yr|l\.?p\.?a\.?)",
    re.I,
)
_LAKHS_SINGLE = re.compile(r"₹?\s*(\d+(?:\.\d+)?)\s*(?:lacs?|lakhs?|lpa|l/?yr|l\.?p\.?a\.?)", re.I)
_INR_K_RANGE = re.compile(r"inr\s*(\d+(?:\.\d+)?)\s*k\s*-\s*(\d+(?:\.\d+)?)\s*k", re.I)
_RUPEE_ABS_RANGE = re.compile(r"₹\s*([\d,]{4,})\s*-\s*([\d,]{4,})")


def _parse_salary_lpa(raw: Optional[str]) -> Optional[tuple]:
    """Best-effort parse of an INR salary range from free text, in Lakhs/annum.

    Returns (low, high) in LPA, or None if unparseable / not an INR figure.
    """
    if not raw:
        return None
    m = _LAKHS_RANGE.search(raw)
    if m:
        lo, hi = float(m.group(1)), float(m.group(2))
        return (min(lo, hi), max(lo, hi))
    m = _INR_K_RANGE.search(raw)
    if m:
        lo, hi = float(m.group(1)) / 100, float(m.group(2)) / 100
        return (min(lo, hi), max(lo, hi))
    m = _RUPEE_ABS_RANGE.search(raw)
    if m:
        lo = float(m.group(1).replace(",", "")) / 100_000
        hi = float(m.group(2).replace(",", "")) / 100_000
        return (min(lo, hi), max(lo, hi))
    m = _LAKHS_SINGLE.search(raw)
    if m:
        v = float(m.group(1))
        return (v, v)
    return None


def _salary_excluded_job_ids(session: Session, salary_min: Optional[float], salary_max: Optional[float]):
    """Job ids to drop for a salary_min/salary_max filter — exclude only on certainty.

    A job is excluded only when its parsed salary *range* is entirely outside
    [salary_min, salary_max] (high < salary_min, or low > salary_max) — not merely
    when its midpoint falls outside. Jobs with no salary_raw, or a salary_raw that
    doesn't parse (non-INR, "Competitive", etc.), are never excluded: unknown
    salary is not evidence of being below/above the requested range.
    """
    rows = session.query(Job.id, Job.salary_raw).filter(Job.salary_raw.isnot(None)).all()
    excluded = []
    for job_id, raw in rows:
        parsed = _parse_salary_lpa(raw)
        if not parsed:
            continue
        lo, hi = parsed
        if salary_min is not None and hi < salary_min:
            excluded.append(job_id)
            continue
        if salary_max is not None and lo > salary_max:
            excluded.append(job_id)
    return excluded

def _category_matching_job_ids(session: Session, category: str) -> list:
    """Job ids whose title classifies (app.config.categories.classify_title) into `category`.

    Cached per-category for `_HELPER_CACHE_TTL_SECONDS` — see cache comment above.
    """
    cached = _category_ids_cache.get(category)
    now = time.time()
    if cached is not None and now - cached[1] < _HELPER_CACHE_TTL_SECONDS:
        return cached[0]

    rows = session.query(Job.id, Job.title).filter(Job.title.isnot(None)).all()
    ids = [job_id for job_id, title in rows if classify_title(title) == category]
    _category_ids_cache[category] = (ids, now)
    return ids


def contact_counts_by_company(session: Session) -> pd.DataFrame:
    """Per-company contact counts, one column per CONTACT_TIERS entry plus `total`.

    `total` includes exec_fallback and any other tier value even though only
    CONTACT_TIERS get their own column, so it stays an honest total. Cached
    for `_HELPER_CACHE_TTL_SECONDS` — see cache comment above.
    """
    now = time.time()
    if _contact_counts_cache["data"] is not None and now - _contact_counts_cache["ts"] < _HELPER_CACHE_TTL_SECONDS:
        return _contact_counts_cache["data"].copy()

    query = session.query(
        Contact.company_id, Contact.seniority_tier, func.count(Contact.id).label("count")
    ).group_by(Contact.company_id, Contact.seniority_tier)
    rows = query.all()

    df = pd.DataFrame(rows, columns=["company_id", "seniority_tier", "count"])
    if df.empty:
        cols = ["company_id", "total"] + CONTACT_TIERS
        result = pd.DataFrame(columns=cols)
    else:
        pivot = df.pivot_table(
            index="company_id", columns="seniority_tier", values="count", fill_value=0, aggfunc="sum"
        )
        pivot["total"] = pivot.sum(axis=1)
        for tier in CONTACT_TIERS:
            if tier not in pivot.columns:
                pivot[tier] = 0
        pivot = pivot.reset_index()
        result = pivot[["company_id", "total"] + CONTACT_TIERS]

    _contact_counts_cache["data"] = result
    _contact_counts_cache["ts"] = now
    return result.copy()


def _contact_count_matching_company_ids(
    session: Session, min_total: Optional[int], min_head_hm: Optional[int]
) -> list:
    """Company ids meeting the given min-total and/or min-(head+hiring_manager) contact thresholds."""
    counts = contact_counts_by_company(session)
    if counts.empty:
        return []
    if min_total is not None:
        counts = counts[counts["total"] >= min_total]
    if min_head_hm is not None:
        counts = counts[(counts["head"] + counts["hiring_manager"]) >= min_head_hm]
    return counts["company_id"].tolist()


# Fields checked for the per-source completeness scorecard.
COMPLETENESS_FIELDS = [
    "location_raw",
    "salary_raw",
    "description_raw",
    "apply_url",
    "job_url",
    "employment_type",
    "seniority",
]


@dataclass(frozen=True)
class DashboardFilters:
    """Shared filter state, built once in app.py from sidebar widgets."""
    source: Optional[str] = None
    date_from: Optional[datetime] = None
    date_to: Optional[datetime] = None
    enrichment_status: Optional[str] = None
    company: Optional[str] = None
    skill_search: Optional[str] = None
    seniority: Optional[str] = None
    remote: Optional[bool] = None
    salary_min_lpa: Optional[float] = None
    salary_max_lpa: Optional[float] = None
    posted_from: Optional[datetime] = None
    posted_to: Optional[datetime] = None
    job_category: Optional[str] = None
    min_total_contacts: Optional[int] = None
    min_head_hm_contacts: Optional[int] = None


def _apply_job_filters(query, filters: DashboardFilters):
    """Apply the common Job-level filter set to a query already selecting from Job."""
    if filters.source:
        query = query.filter(Job.source == filters.source)
    if filters.date_from:
        query = query.filter(Job.first_seen_at >= filters.date_from)
    if filters.date_to:
        query = query.filter(Job.first_seen_at <= filters.date_to)
    if filters.enrichment_status:
        query = query.filter(Job.enrichment_status == filters.enrichment_status)
    if filters.company:
        query = query.filter(Job.company_name_raw.ilike(f"%{filters.company}%"))
    if filters.seniority:
        query = query.filter(Job.seniority == filters.seniority)
    if filters.remote is not None:
        query = query.filter(Job.is_remote == filters.remote)
    if filters.skill_search:
        # Exact match against a value chosen from a dropdown of real skills
        # (see app.py's skill selectbox). EXISTS subquery rather than a join,
        # so this composes safely with queries (e.g. top_skills) that
        # already join JobSkill themselves.
        subquery = select(JobSkill.job_id).where(JobSkill.skill == filters.skill_search)
        query = query.filter(Job.id.in_(subquery))
    if filters.salary_min_lpa is not None or filters.salary_max_lpa is not None:
        # Query objects from session.query(...) carry their originating
        # session, so this composes without threading session through every
        # call site of _apply_job_filters.
        excluded_ids = _salary_excluded_job_ids(query.session, filters.salary_min_lpa, filters.salary_max_lpa)
        if excluded_ids:
            query = query.filter(~Job.id.in_(excluded_ids))
    if filters.posted_from:
        query = query.filter(Job.posted_at >= filters.posted_from)
    if filters.posted_to:
        query = query.filter(Job.posted_at <= filters.posted_to)
    if filters.job_category:
        ids = _category_matching_job_ids(query.session, filters.job_category)
        query = query.filter(Job.id.in_(ids))
    if filters.min_total_contacts is not None or filters.min_head_hm_contacts is not None:
        company_ids = _contact_count_matching_company_ids(
            query.session, filters.min_total_contacts, filters.min_head_hm_contacts
        )
        query = query.filter(Job.company_id.in_(company_ids))
    return query


def jobs_per_source(session: Session, filters: DashboardFilters) -> pd.DataFrame:
    """Job count broken down by source."""
    query = session.query(Job.source, func.count(Job.id).label("job_count"))
    query = _apply_job_filters(query, filters)
    query = query.group_by(Job.source).order_by(func.count(Job.id).desc())
    return pd.DataFrame(query.all(), columns=["source", "job_count"])


def jobs_over_time(session: Session, filters: DashboardFilters) -> pd.DataFrame:
    """Jobs ingested per day, by first_seen_at."""
    day = cast(Job.first_seen_at, Date).label("day")
    query = session.query(day, func.count(Job.id).label("job_count"))
    query = _apply_job_filters(query, filters)
    query = query.group_by(day).order_by(day)
    return pd.DataFrame(query.all(), columns=["day", "job_count"])


def enrichment_coverage_overall(session: Session, filters: DashboardFilters) -> pd.DataFrame:
    """Single-row overall enrichment coverage: total, enriched, pct."""
    total_q = _apply_job_filters(session.query(func.count(Job.id)), filters)
    total = total_q.scalar() or 0

    enriched_q = _apply_job_filters(
        session.query(func.count(Job.id)).filter(Job.enrichment_status == "done"), filters
    )
    enriched = enriched_q.scalar() or 0

    pct = (enriched / total * 100) if total else 0.0
    return pd.DataFrame([{"total": total, "enriched": enriched, "pct_enriched": pct}])


def enrichment_coverage_by_source(session: Session, filters: DashboardFilters) -> pd.DataFrame:
    """Per-source enrichment coverage: source, total, enriched, pct."""
    total_query = _apply_job_filters(
        session.query(Job.source, func.count(Job.id).label("total")), filters
    ).group_by(Job.source)
    totals = {row.source: row.total for row in total_query.all()}

    enriched_query = _apply_job_filters(
        session.query(Job.source, func.count(Job.id).label("enriched")).filter(
            Job.enrichment_status == "done"
        ),
        filters,
    ).group_by(Job.source)
    enriched_counts = {row.source: row.enriched for row in enriched_query.all()}

    rows = []
    for source, total in totals.items():
        enriched = enriched_counts.get(source, 0)
        pct = (enriched / total * 100) if total else 0.0
        rows.append({"source": source, "total": total, "enriched": enriched, "pct_enriched": pct})
    rows.sort(key=lambda r: r["total"], reverse=True)
    return pd.DataFrame(rows, columns=["source", "total", "enriched", "pct_enriched"])


def field_completeness_by_source(session: Session, filters: DashboardFilters) -> pd.DataFrame:
    """Per-source, per-field non-null rates for COMPLETENESS_FIELDS."""
    total_query = _apply_job_filters(
        session.query(Job.source, func.count(Job.id).label("total")), filters
    ).group_by(Job.source)
    totals = {row.source: row.total for row in total_query.all()}

    rows = []
    for field in COMPLETENESS_FIELDS:
        col = getattr(Job, field)
        field_query = _apply_job_filters(
            session.query(Job.source, func.count(col).label("non_null")), filters
        ).group_by(Job.source)
        for row in field_query.all():
            total = totals.get(row.source, 0)
            pct = (row.non_null / total * 100) if total else 0.0
            rows.append(
                {
                    "source": row.source,
                    "field": field,
                    "non_null_count": row.non_null,
                    "total": total,
                    "pct_complete": pct,
                }
            )
    return pd.DataFrame(rows, columns=["source", "field", "non_null_count", "total", "pct_complete"])


def company_growth_over_time(session: Session, filters: DashboardFilters) -> pd.DataFrame:
    """Company count over time, by companies.first_seen_at. Not job-filtered (companies table only)."""
    day = cast(Company.first_seen_at, Date).label("day")
    query = session.query(day, func.count(Company.id).label("company_count"))
    query = query.group_by(day).order_by(day)
    df = pd.DataFrame(query.all(), columns=["day", "company_count"])
    if not df.empty:
        df["cumulative_count"] = df["company_count"].cumsum()
    else:
        df["cumulative_count"] = pd.Series(dtype="int64")
    return df


def top_skills(
    session: Session, filters: DashboardFilters, skill_type: Optional[str] = None, limit: int = 25
) -> pd.DataFrame:
    """Top skills by count, optionally restricted to one skill_type ("hard"/"soft").

    Gated to jobs matching filters. `df.attrs["n"]` is jobs with at least one
    recorded skill of the requested type (or any type, if `skill_type` is None).
    """
    query = session.query(
        JobSkill.skill, JobSkill.skill_type, func.count(JobSkill.id).label("count")
    ).join(Job, Job.id == JobSkill.job_id)
    if skill_type:
        query = query.filter(JobSkill.skill_type == skill_type)
    query = _apply_job_filters(query, filters)
    query = query.group_by(JobSkill.skill, JobSkill.skill_type).order_by(
        func.count(JobSkill.id).desc()
    ).limit(limit)
    df = pd.DataFrame(query.all(), columns=["skill", "skill_type", "count"])

    n_query = session.query(func.count(func.distinct(JobSkill.job_id))).join(Job, Job.id == JobSkill.job_id)
    if skill_type:
        n_query = n_query.filter(JobSkill.skill_type == skill_type)
    n_query = _apply_job_filters(n_query, filters)
    df.attrs["n"] = n_query.scalar() or 0
    return df


def seniority_distribution(session: Session, filters: DashboardFilters) -> pd.DataFrame:
    """Seniority distribution, gated to jobs with non-null seniority."""
    query = _apply_job_filters(
        session.query(Job.seniority, func.count(Job.id).label("count")).filter(
            Job.seniority.isnot(None)
        ),
        filters,
    ).group_by(Job.seniority).order_by(func.count(Job.id).desc())
    df = pd.DataFrame(query.all(), columns=["seniority", "count"])
    df.attrs["n"] = int(df["count"].sum()) if not df.empty else 0
    return df


def top_hiring_companies(session: Session, filters: DashboardFilters, limit: int = 25) -> pd.DataFrame:
    """Top companies by job count."""
    query = session.query(
        Job.company_name_raw.label("company_name"), func.count(Job.id).label("job_count")
    ).filter(Job.company_name_raw.isnot(None))
    query = _apply_job_filters(query, filters)
    query = query.group_by(Job.company_name_raw).order_by(func.count(Job.id).desc()).limit(limit)
    return pd.DataFrame(query.all(), columns=["company_name", "job_count"])


def industry_breakdown(session: Session, filters: DashboardFilters) -> pd.DataFrame:
    """Company industry breakdown, gated to companies with non-null industry.

    Joined against Job so job-level filters (source/date/etc.) narrow which
    companies are counted, consistent with the rest of the dashboard. Raw
    `industry` values are free-text and highly fragmented, so they're
    canonicalized into a small fixed set of buckets (see
    `_canonicalize_industry`) before aggregating.
    """
    query = (
        session.query(Company.id, Company.industry)
        .join(Job, Job.company_id == Company.id)
        .filter(Company.industry.isnot(None))
        .distinct()
    )
    query = _apply_job_filters(query, filters)
    rows = query.all()

    df = pd.DataFrame(rows, columns=["company_id", "industry"])
    if not df.empty:
        df["industry"] = df["industry"].apply(_canonicalize_industry)
        df = df.groupby("industry", as_index=False)["company_id"].nunique()
        df = df.rename(columns={"company_id": "company_count"}).sort_values(
            "company_count", ascending=False
        )
    else:
        df["company_count"] = pd.Series(dtype="int64")
        df = df[["industry", "company_count"]]
    df.attrs["n"] = int(df["company_count"].sum()) if not df.empty else 0
    return df


def remote_split(session: Session, filters: DashboardFilters) -> pd.DataFrame:
    """Remote vs on-site split, based on is_remote (null = unknown)."""
    query = _apply_job_filters(
        session.query(Job.is_remote, func.count(Job.id).label("count")), filters
    ).group_by(Job.is_remote)
    df = pd.DataFrame(query.all(), columns=["is_remote", "count"])

    def label(v):
        if v is True:
            return "remote"
        if v is False:
            return "on-site"
        return "unknown"

    if not df.empty:
        df["remote_status"] = df["is_remote"].apply(label)
        df = df.groupby("remote_status", as_index=False)["count"].sum()
    else:
        df["remote_status"] = pd.Series(dtype="object")
    return df[["remote_status", "count"]]


def experience_years_distribution(session: Session, filters: DashboardFilters) -> pd.DataFrame:
    """Job count by minimum required experience (years), gated to non-null values."""
    query = _apply_job_filters(
        session.query(
            Job.experience_min_years, func.count(Job.id).label("count")
        ).filter(Job.experience_min_years.isnot(None)),
        filters,
    ).group_by(Job.experience_min_years).order_by(Job.experience_min_years)
    df = pd.DataFrame(query.all(), columns=["experience_min_years", "count"])
    df.attrs["n"] = int(df["count"].sum()) if not df.empty else 0
    return df


_SALARY_BANDS = [0, 5, 10, 15, 20, 25, 30, 40, 60, float("inf")]
_SALARY_BAND_LABELS = ["0-5", "5-10", "10-15", "15-20", "20-25", "25-30", "30-40", "40-60", "60+"]


def salary_distribution(session: Session, filters: DashboardFilters) -> pd.DataFrame:
    """Job count by salary band (Lakhs/annum, INR), parsed from free-text salary_raw.

    Only INR figures are parsed (see `_parse_salary_lpa`) — non-INR and
    non-numeric values ("Competitive", "USD ...") are excluded rather than
    guessed at. `df.attrs["n"]` is jobs with a parsed salary; `df.attrs["n_with_salary_field"]`
    is jobs with any salary_raw value at all, for comparison.
    """
    query = _apply_job_filters(
        session.query(Job.id, Job.salary_raw).filter(Job.salary_raw.isnot(None)), filters
    )
    rows = query.all()

    parsed = []
    for job_id, raw in rows:
        result = _parse_salary_lpa(raw)
        if result:
            parsed.append((job_id, (result[0] + result[1]) / 2))

    salaries = pd.DataFrame(parsed, columns=["job_id", "salary_lpa"])
    if not salaries.empty:
        salaries["band"] = pd.cut(
            salaries["salary_lpa"], bins=_SALARY_BANDS, labels=_SALARY_BAND_LABELS, right=False
        )
        df = (
            salaries.groupby("band", observed=True)
            .size()
            .reindex(_SALARY_BAND_LABELS, fill_value=0)
            .reset_index(name="count")
        )
    else:
        df = pd.DataFrame({"band": _SALARY_BAND_LABELS, "count": 0})

    df.attrs["n"] = len(salaries)
    df.attrs["n_with_salary_field"] = len(rows)
    return df


def explore_jobs(session: Session, filters: DashboardFilters, limit: Optional[int] = 500) -> pd.DataFrame:
    """Raw filtered job rows, joined with company name and per-tier contact counts, for the Explore tab.

    `limit=None` returns every matching row (used by the "download all" button) —
    the default 500 cap is only for keeping the on-screen table responsive.
    """
    query = session.query(
        Job.id,
        Job.source,
        Job.title,
        Company.id.label("company_id"),
        Company.name.label("company_name"),
        Job.location_raw,
        Job.is_remote,
        Job.seniority,
        Job.employment_type,
        Job.salary_raw,
        Job.experience_min_years,
        Job.experience_max_years,
        Job.enrichment_status,
        Job.posted_at,
        Job.first_seen_at,
        Job.job_url,
        Job.apply_url,
    ).outerjoin(Company, Job.company_id == Company.id)
    query = _apply_job_filters(query, filters)
    query = query.order_by(Job.first_seen_at.desc())
    if limit is not None:
        query = query.limit(limit)
    columns = [
        "id", "source", "title", "company_id", "company_name", "location_raw", "is_remote",
        "seniority", "employment_type", "salary_raw", "experience_min_years",
        "experience_max_years", "enrichment_status", "posted_at", "first_seen_at",
        "job_url", "apply_url",
    ]
    df = pd.DataFrame(query.all(), columns=columns)

    contact_cols = ["total"] + CONTACT_TIERS
    counts = contact_counts_by_company(session)
    if counts.empty:
        for col in contact_cols:
            df[f"contacts_{col}"] = 0
    else:
        counts = counts.rename(columns={c: f"contacts_{c}" for c in contact_cols})
        df = df.merge(counts, on="company_id", how="left")
        for col in contact_cols:
            df[f"contacts_{col}"] = df[f"contacts_{col}"].fillna(0).astype(int)
    return df.drop(columns=["company_id"])


def company_explore_table(session: Session, filters: DashboardFilters, limit: Optional[int] = 500) -> pd.DataFrame:
    """Per-company table for the Explore tab: ranked by job_count desc, with pain_points and a company URL.

    Job-filtered (respects the sidebar filters), same join/order pattern as
    `company_wise_distribution`. `company_url` is derived from
    `Company.canonical_domain` — there's no separate website column on
    Company, and canonical_domain (set by the enrich-company-domains skill,
    MX-verified) is the closest signal to the company's own site.

    `limit=None` returns every matching company (used by the "download all" button).
    """
    query = session.query(
        Company.id.label("company_id"),
        Company.name.label("company_name"),
        Company.canonical_domain,
        Company.overall_rating,
        Company.wlb_rating,
        Company.estimated_salary_lpa,
        Company.pain_points,
        func.count(Job.id).label("job_count"),
    ).join(Job, Job.company_id == Company.id)
    query = _apply_job_filters(query, filters)
    query = query.group_by(
        Company.id, Company.name, Company.canonical_domain,
        Company.overall_rating, Company.wlb_rating,
        Company.estimated_salary_lpa, Company.pain_points
    ).order_by(func.count(Job.id).desc())
    if limit is not None:
        query = query.limit(limit)
    df = pd.DataFrame(
        query.all(),
        columns=[
            "company_id", "company_name", "canonical_domain", "overall_rating",
            "wlb_rating", "estimated_salary_lpa", "pain_points", "job_count",
        ],
    )
    df["company_url"] = df["canonical_domain"].apply(lambda d: f"https://{d}" if d else None)
    return df.drop(columns=["company_id", "canonical_domain"])


def company_wise_distribution(session: Session, filters: DashboardFilters, limit: int = 25) -> pd.DataFrame:
    """Per-company job count and contact counts, for the Job Market Insight tab.

    Job-filtered (respects the sidebar filters); contact counts are the
    company's full count regardless of job filters, since contacts aren't
    tied to a specific job. Ranked by job_count, capped at `limit`. Contact
    counts are split into `head_hiring_manager_contacts` (the two tiers that
    can actually move a candidate forward) and `rest_contacts` (ic +
    talent_acquisition + exec_fallback) rather than one lumped total.
    """
    query = session.query(
        Company.id.label("company_id"),
        Company.name.label("company_name"),
        func.count(Job.id).label("job_count"),
    ).join(Job, Job.company_id == Company.id)
    query = _apply_job_filters(query, filters)
    query = query.group_by(Company.id, Company.name).order_by(func.count(Job.id).desc()).limit(limit)
    df = pd.DataFrame(query.all(), columns=["company_id", "company_name", "job_count"])

    if df.empty:
        df["head_hiring_manager_contacts"] = pd.Series(dtype="int64")
        df["rest_contacts"] = pd.Series(dtype="int64")
        return df.drop(columns=["company_id"])

    counts = contact_counts_by_company(session)
    if counts.empty:
        df["head_hiring_manager_contacts"] = 0
        df["rest_contacts"] = 0
    else:
        counts = counts.copy()
        counts["head_hiring_manager_contacts"] = counts["head"] + counts["hiring_manager"]
        counts["rest_contacts"] = counts["total"] - counts["head_hiring_manager_contacts"]
        counts = counts[["company_id", "head_hiring_manager_contacts", "rest_contacts"]]
        df = df.merge(counts, on="company_id", how="left")
        df["head_hiring_manager_contacts"] = df["head_hiring_manager_contacts"].fillna(0).astype(int)
        df["rest_contacts"] = df["rest_contacts"].fillna(0).astype(int)
    return df.drop(columns=["company_id"])
