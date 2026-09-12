"""Decision Run-scoped telemetry for the two enrich-companies phases.

This is only the durable scope/outcome seam.  The enrich-companies skill owns
the evidence judgement; callers report its reviewed result here.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.orm import (Company, DecisionRun, DecisionRunCompany, DecisionRunCompanyFunnelManifest,
                            DecisionRunCompanyPhaseBEvidence, DecisionRunJob, Job)
from .progress import (FailedRecordDisposition, FailureDisposition, StageCounts,
                       StageName, StageRecord, StageStatus, StageTransitionError,
                       complete_with_errors_stage, declare_pending_stage,
                       finish_stage, heartbeat_stage, read_run_progress,
                       start_stage, update_stage)
from .telemetry import is_legacy_telemetry


class CompanyPhase(str, Enum):
    A = "a"
    B = "b"


class CompanyProcessingOutcome(str, Enum):
    ENRICHED = "enriched"
    FAILED = "failed"


class PhaseBEvidenceOutcome(str, Enum):
    FOUND = "found"
    CONFIRMED_MISSING = "confirmed_missing"
    SOURCE_ERROR = "source_error"


class CompanyTerminalOutcome(str, Enum):
    GROUP_1 = "group_1"
    GROUP_2 = "group_2"
    GROUP_3 = "group_3"
    GROUP_4 = "group_4"
    UNGROUPABLE = "ungroupable"


class CompanyFailureDisposition(str, Enum):
    NON_BLOCKING = "non_blocking"
    EXCLUDED = "excluded"


REQUIRED_PHASE_B_SOURCES = ("glassdoor", "ambitionbox")


def company_terminal_outcome(group_number: int | None) -> CompanyTerminalOutcome:
    if group_number is None:
        return CompanyTerminalOutcome.UNGROUPABLE
    try:
        return {1: CompanyTerminalOutcome.GROUP_1, 2: CompanyTerminalOutcome.GROUP_2,
                3: CompanyTerminalOutcome.GROUP_3, 4: CompanyTerminalOutcome.GROUP_4}[group_number]
    except KeyError as exc:
        raise StageTransitionError("company terminal group must be 1, 2, 3, 4, or None") from exc


def _reused_source_outcome(company: Company, source: str, raw: dict) -> PhaseBEvidenceOutcome:
    """Interpret persisted source status and its actual source-specific fields."""
    status = str(raw.get("status") or raw.get("outcome") or "").lower()
    if raw.get("error") or status in {"error", "failed", "source_error", "deferred"}:
        return PhaseBEvidenceOutcome.SOURCE_ERROR
    terminal_missing = status in {"missing", "not_found", "review_required", "unresolvable", "terminal_missing"}
    if source == "glassdoor":
        found = any(value is not None for value in (company.glassdoor_wlb_rating, company.glassdoor_overall_rating, company.glassdoor_review_count, raw.get("wlb_rating"), raw.get("overall_rating")))
    else:
        found = any(value is not None for value in (company.ambitionbox_estimated_salary_lpa, company.ambitionbox_overall_rating, company.ambitionbox_wlb_rating, raw.get("salary_lpa"), raw.get("observed_salary_range"), raw.get("estimated_salary_lpa")))
    if status in {"ok", "done", "found", "success"} and found:
        return PhaseBEvidenceOutcome.FOUND
    if terminal_missing or status in {"ok", "done", "found", "success"}:
        return PhaseBEvidenceOutcome.CONFIRMED_MISSING
    raise StageTransitionError(f"persisted {source} evidence is not terminal")


@dataclass(frozen=True)
class CompanyEnrichmentResult:
    company_id: int
    outcome: CompanyProcessingOutcome
    reason: str | None = None
    evidence_outcome: PhaseBEvidenceOutcome | None = None
    source: str | None = None
    evidence_url: str | None = None
    source_status: str | None = None
    disposition: CompanyFailureDisposition | None = None


def _rows(session: Session, run_id: str) -> list[DecisionRunCompanyFunnelManifest]:
    return list(session.execute(select(DecisionRunCompanyFunnelManifest).where(
        DecisionRunCompanyFunnelManifest.run_id == run_id,
    ).order_by(DecisionRunCompanyFunnelManifest.company_id)).scalars())


def _phase_a_reusable(company: Company, latest_job_seen_at) -> bool:
    """Mirror enrich-companies' complete/current selection rule conservatively."""
    required_facts = bool(company.industry and company.description and company.pain_points)
    profile_complete = (company.contact_search_groups is not None
                        and company.contact_profile_enriched_at is not None
                        and (latest_job_seen_at is None or company.contact_profile_enriched_at >= latest_job_seen_at))
    domain_terminal = company.domain_resolution_status in {"done", "unresolvable"}
    current = company.enriched_at is not None and (latest_job_seen_at is None or company.enriched_at >= latest_job_seen_at)
    return company.enrichment_status == "done" and required_facts and profile_complete and domain_terminal and current


def freeze_company_funnel_manifest(session: Session, run_id: str) -> tuple[DecisionRunCompanyFunnelManifest, ...]:
    """Freeze unique companies and eligible-job multiplicity from this run only."""
    existing = _rows(session, run_id)
    if existing:
        return tuple(existing)
    run = session.get(DecisionRun, run_id)
    if run is None or is_legacy_telemetry(run.telemetry_version):
        raise StageTransitionError("company funnel requires current Decision Run telemetry")
    eligible = list(session.execute(select(DecisionRunJob).where(
        DecisionRunJob.run_id == run_id, DecisionRunJob.outcome == "eligible",
    )).scalars())
    by_company: dict[int, int] = {}
    for job in eligible:
        by_company[job.company_id] = by_company.get(job.company_id, 0) + 1
    companies = {company.id: company for company in session.execute(select(Company).where(
        Company.id.in_(by_company)
    )).scalars()}
    latest_seen = {company_id: max((stored.last_seen_at for job in eligible
                                    if job.company_id == company_id
                                    for stored in [session.get(Job, job.job_id)] if stored is not None), default=None)
                   for company_id in by_company}
    for company_id, job_count in by_company.items():
        company = companies[company_id]
        # Completed company work is reusable.  A terminal Phase B ``done``
        # includes legitimate missing evidence; evidence retains the detail.
        session.add(DecisionRunCompanyFunnelManifest(
            run_id=run_id, company_id=company_id, eligible_job_count=job_count,
            phase_a_outcome=CompanyProcessingOutcome.ENRICHED.value if _phase_a_reusable(company, latest_seen[company_id]) else None,
        ))
    session.flush()
    rows = _rows(session, run_id)
    # Existing Phase B evidence is reusable only when every required source
    # has a terminal source record.  Empty/all-missing records remain a valid
    # completed outcome, never an invented ``found`` value.
    for row in rows:
        company = companies[row.company_id]
        evidence = company.market_profile_evidence if isinstance(company.market_profile_evidence, dict) else {}
        sources = evidence.get("sources", {}) if isinstance(evidence, dict) else {}
        if company.market_profile_status != "done" or not isinstance(sources, dict):
            continue
        observations = []
        for source in REQUIRED_PHASE_B_SOURCES:
            raw = sources.get(source)
            if not isinstance(raw, dict):
                break
            status = str(raw.get("status") or raw.get("outcome") or "")
            outcome = _reused_source_outcome(company, source, raw)
            observations.append(DecisionRunCompanyPhaseBEvidence(manifest_id=row.id, source=source, outcome=outcome,
                                                                 reason=str(raw.get("error")) if outcome is PhaseBEvidenceOutcome.SOURCE_ERROR else None,
                                                                 evidence_url=raw.get("url"), source_status=status or None))
        if len(observations) == len(REQUIRED_PHASE_B_SOURCES):
            session.add_all(observations)
    session.flush()
    start_stage(session, run_id, StageName.COMPANY_DEDUPLICATION,
                expected_count=len(rows),
                reason="unique companies frozen from eligible jobs in exact Decision Run")
    records = [StageRecord("company", str(row.company_id), "advanced") for row in rows]
    counts = StageCounts(len(rows), len(rows), 0, 0, 0)
    finish_stage(session, run_id, StageName.COMPANY_DEDUPLICATION, counts=counts,
                 records=records, reason_counts={},
                 reason="company duplicates represented as eligible-job multiplicity, not dropped companies")
    declare_pending_stage(
        session, run_id, StageName.COMPANY_PHASE_B, expected_count=len(rows),
    )
    return tuple(rows)


def begin_company_phase_b(session: Session, run_id: str) -> tuple[int, ...]:
    """Make Phase B worker dispatch visible in the durable progress ledger.

    The agent-owned source lanes can take long enough that waiting for their
    first result makes an active run look idle.  This seam is intentionally
    idempotent so a resumed coordinator refreshes liveness without inventing
    another attempt.
    """
    rows = _rows(session, run_id)
    if not rows:
        freeze_company_funnel_manifest(session, run_id)
        rows = _rows(session, run_id)
    stage = next((item for item in read_run_progress(
        session, run_id, project_interruptions=False,
    ).stages if item.name == StageName.COMPANY_PHASE_B.value), None)
    reason = "Glassdoor and AmbitionBox source lanes dispatched"
    if stage is None or stage.status in {
        StageStatus.PENDING.value,
        StageStatus.INTERRUPTED.value,
    }:
        start_stage(
            session, run_id, StageName.COMPANY_PHASE_B,
            expected_count=len(rows), process_untracked=True, reason=reason,
        )
    elif stage.status == StageStatus.RUNNING.value:
        heartbeat_stage(session, run_id, StageName.COMPANY_PHASE_B, reason=reason)
    return tuple(row.company_id for row in rows)


def _report(session: Session, run_id: str, phase: CompanyPhase,
            results: Iterable[CompanyEnrichmentResult]) -> None:
    if not isinstance(phase, CompanyPhase):
        raise StageTransitionError("company enrichment phase must be CompanyPhase")
    stage = {CompanyPhase.A: StageName.COMPANY_PHASE_A, CompanyPhase.B: StageName.COMPANY_PHASE_B}[phase]
    rows = _rows(session, run_id)
    if not rows:
        freeze_company_funnel_manifest(session, run_id)
        rows = _rows(session, run_id)
    fields = ("phase_a_outcome", "phase_a_reason", "phase_a_failure_disposition", None) if stage is StageName.COMPANY_PHASE_A else (
        "phase_b_outcome", "phase_b_reason", "phase_b_failure_disposition", "phase_b_evidence_outcome")
    outcome_field, reason_field, disposition_field, evidence_field = fields
    existing_stage = next((item for item in read_run_progress(session, run_id, project_interruptions=False).stages
                           if item.name == stage.value), None)
    if existing_stage is None or existing_stage.status == StageStatus.PENDING.value:
        start_stage(session, run_id, stage, expected_count=len(rows),
                    reason=f"exact company manifest frozen for {stage.value}")
    supplied = tuple(results)
    for result in supplied:
        if not isinstance(result.outcome, CompanyProcessingOutcome):
            raise StageTransitionError("company outcome must be CompanyProcessingOutcome")
        if result.evidence_outcome is not None and not isinstance(result.evidence_outcome, PhaseBEvidenceOutcome):
            raise StageTransitionError("Phase B evidence outcome must be PhaseBEvidenceOutcome")
        if result.disposition is not None and not isinstance(result.disposition, CompanyFailureDisposition):
            raise StageTransitionError("failure disposition must be CompanyFailureDisposition")
    by_id: dict[int, list[CompanyEnrichmentResult]] = {}
    for item in supplied:
        by_id.setdefault(item.company_id, []).append(item)
    if set(by_id) - {row.company_id for row in rows}:
        raise StageTransitionError("company enrichment results must be inside the exact Decision Run manifest")
    if stage is StageName.COMPANY_PHASE_A and any(len(items) != 1 for items in by_id.values()):
        raise StageTransitionError("Phase A permits one result per company")
    if stage is StageName.COMPANY_PHASE_B and any(len({item.source for item in items}) != len(items) for items in by_id.values()):
        raise StageTransitionError("Phase B permits one result per company/source")
    for row in rows:
        company_results = by_id.get(row.company_id, [])
        if not company_results:
            continue
        for result in company_results:
            outcome = result.outcome
            if outcome is CompanyProcessingOutcome.ENRICHED:
                setattr(row, outcome_field, "enriched")
                setattr(row, reason_field, None)
                setattr(row, disposition_field, None)
                if evidence_field:
                    if result.source not in REQUIRED_PHASE_B_SOURCES:
                        raise StageTransitionError("Phase B results must name glassdoor or ambitionbox")
                    if result.evidence_outcome is None:
                        raise StageTransitionError("Phase B result requires an explicit evidence outcome")
                    evidence = result.evidence_outcome
                    if evidence is PhaseBEvidenceOutcome.SOURCE_ERROR:
                        raise StageTransitionError("source_error is a failed processing result")
                    existing = session.execute(select(DecisionRunCompanyPhaseBEvidence).where(
                        DecisionRunCompanyPhaseBEvidence.manifest_id == row.id,
                        DecisionRunCompanyPhaseBEvidence.source == result.source)).scalar_one_or_none()
                    if existing is None:
                        existing = DecisionRunCompanyPhaseBEvidence(manifest_id=row.id, source=result.source)
                        session.add(existing)
                    existing.outcome, existing.reason, existing.failure_disposition, existing.evidence_url, existing.source_status = evidence.value, None, None, result.evidence_url, result.source_status
            else:
                if not result.reason or not result.reason.strip():
                    raise StageTransitionError("failed company enrichment results require a reason")
                if result.disposition is None:
                    raise StageTransitionError("failed company enrichment requires non_blocking or excluded disposition")
                disposition = result.disposition
                setattr(row, outcome_field, "failed")
                setattr(row, reason_field, result.reason.strip())
                setattr(row, disposition_field, disposition.value)
                if evidence_field:
                    if result.source not in REQUIRED_PHASE_B_SOURCES:
                        raise StageTransitionError("Phase B results must name glassdoor or ambitionbox")
                    existing = session.execute(select(DecisionRunCompanyPhaseBEvidence).where(DecisionRunCompanyPhaseBEvidence.manifest_id == row.id, DecisionRunCompanyPhaseBEvidence.source == result.source)).scalar_one_or_none()
                    if existing is None:
                        existing = DecisionRunCompanyPhaseBEvidence(manifest_id=row.id, source=result.source)
                        session.add(existing)
                    existing.outcome, existing.reason, existing.failure_disposition, existing.evidence_url, existing.source_status = PhaseBEvidenceOutcome.SOURCE_ERROR.value, result.reason.strip(), disposition.value, result.evidence_url, result.source_status
    if stage is StageName.COMPANY_PHASE_B:
        # Production sessions disable autoflush. Make newly inserted/updated
        # source evidence visible to the aggregate query in this transaction.
        session.flush()
        for row in rows:
            evidence = list(session.execute(select(DecisionRunCompanyPhaseBEvidence).where(DecisionRunCompanyPhaseBEvidence.manifest_id == row.id)).scalars())
            by_source = {item.source: item for item in evidence}
            if not all(source in by_source for source in REQUIRED_PHASE_B_SOURCES):
                row.phase_b_outcome = None; row.phase_b_reason = None
                row.phase_b_failure_disposition = None; row.phase_b_evidence_outcome = None
            elif any(item.outcome == PhaseBEvidenceOutcome.SOURCE_ERROR.value for item in evidence):
                failures = sorted((item for item in evidence if item.outcome == PhaseBEvidenceOutcome.SOURCE_ERROR.value), key=lambda item: item.source)
                row.phase_b_outcome = CompanyProcessingOutcome.FAILED.value
                row.phase_b_reason = failures[0].reason
                if any(item.failure_disposition is None for item in failures):
                    row.phase_b_failure_disposition = None
                elif all(item.failure_disposition == CompanyFailureDisposition.EXCLUDED.value for item in failures):
                    row.phase_b_failure_disposition = CompanyFailureDisposition.EXCLUDED.value
                else:
                    row.phase_b_failure_disposition = CompanyFailureDisposition.NON_BLOCKING.value
                row.phase_b_evidence_outcome = PhaseBEvidenceOutcome.SOURCE_ERROR.value
            else:
                row.phase_b_outcome = CompanyProcessingOutcome.ENRICHED.value
                row.phase_b_reason = None
                row.phase_b_failure_disposition = None
                row.phase_b_evidence_outcome = (PhaseBEvidenceOutcome.CONFIRMED_MISSING.value if all(item.outcome == PhaseBEvidenceOutcome.CONFIRMED_MISSING.value for item in evidence) else PhaseBEvidenceOutcome.FOUND.value)
    records, reasons = [], {}
    advanced = dropped = failed = 0
    for row in rows:
        outcome, reason = getattr(row, outcome_field), getattr(row, reason_field)
        if outcome == "enriched":
            records.append(StageRecord("company", str(row.company_id), "advanced")); advanced += 1
        elif outcome == "skipped":
            records.append(StageRecord("company", str(row.company_id), "dropped", reason)); dropped += 1
            reasons[reason] = reasons.get(reason, 0) + 1
        elif outcome == "failed":
            records.append(StageRecord("company", str(row.company_id), "failed", reason)); failed += 1
            reasons[reason] = reasons.get(reason, 0) + 1
    counts = StageCounts(len(rows), advanced, dropped, failed, len(rows) - advanced - dropped - failed)
    if counts.pending:
        checkpoint_reason = None
        if stage is StageName.COMPANY_PHASE_B and supplied:
            sources = ", ".join(sorted({item.source for item in supplied if item.source}))
            checkpoint_reason = f"Phase B evidence checkpoint persisted: {sources}"
        update_stage(session, run_id, stage, counts=counts,
                     reason_counts=reasons, records=records,
                     reason=checkpoint_reason)
        return
    if counts.failed:
        dispositions = company_failure_dispositions(session, run_id, phase)
        complete_with_errors_stage(session, run_id, stage, counts=counts, reason_counts=reasons,
                                   records=records,
                                   failure_dispositions=tuple(
                                       FailedRecordDisposition(
                                           "company", str(company_id),
                                           FailureDisposition(disposition.value),
                                       )
                                       for company_id, disposition in dispositions.items()
                                   ),
                                   reason="company enrichment finished with source errors")
    else:
        finish_stage(session, run_id, stage, counts=counts, reason_counts=reasons, records=records,
                     reason="company enrichment completed for exact manifest")


def report_company_phase_a(session: Session, run_id: str, results: Iterable[CompanyEnrichmentResult]) -> None:
    _report(session, run_id, CompanyPhase.A, results)


def scope_company_phase_a_to_approval(session: Session, run_id: str) -> tuple[int, ...]:
    """Mark unapproved companies skipped before approved-only Phase A work."""
    from app.models.orm import DecisionRun, DecisionRunSelectedJob

    run = session.get(DecisionRun, run_id)
    if run is None or run.state not in {"approved", "processing", "completed"}:
        raise StageTransitionError("company Phase A scoping requires named approval")

    rows = _rows(session, run_id)
    selected = {
        row.company_id for row in session.execute(select(DecisionRunSelectedJob).where(
            DecisionRunSelectedJob.run_id == run_id,
        )).scalars()
    }
    if not selected:
        raise StageTransitionError("company Phase A requires a non-empty approved scope")
    if selected - {row.company_id for row in rows}:
        raise StageTransitionError("approved company is outside the frozen company manifest")
    existing = next((item for item in read_run_progress(
        session, run_id, project_interruptions=False,
    ).stages if item.name == StageName.COMPANY_PHASE_A.value), None)
    if existing is not None and existing.status in {
        StageStatus.COMPLETED.value, StageStatus.COMPLETED_WITH_ERRORS.value,
    }:
        return tuple(sorted(selected))
    if existing is None:
        start_stage(
            session, run_id, StageName.COMPANY_PHASE_A, expected_count=len(rows),
            reason="approved company scope frozen for contact/profile enrichment",
        )
    for row in rows:
        if row.company_id not in selected:
            row.phase_a_outcome = "skipped"
            row.phase_a_reason = "not_approved"
            row.phase_a_failure_disposition = None
    _report(session, run_id, CompanyPhase.A, ())
    return tuple(sorted(selected))


def report_company_phase_b(session: Session, run_id: str, results: Iterable[CompanyEnrichmentResult]) -> None:
    _report(session, run_id, CompanyPhase.B, results)


def company_failure_dispositions(session: Session, run_id: str, phase: CompanyPhase) -> dict[int, CompanyFailureDisposition]:
    """Typed deterministic map consumed by shared progress integration."""
    if not isinstance(phase, CompanyPhase):
        raise StageTransitionError("company enrichment phase must be CompanyPhase")
    field = "phase_a_failure_disposition" if phase is CompanyPhase.A else "phase_b_failure_disposition"
    result: dict[int, CompanyFailureDisposition] = {}
    for row in _rows(session, run_id):
        value = getattr(row, field)
        if value is not None:
            result[row.company_id] = CompanyFailureDisposition(value)
    return result


def record_company_terminal_outcomes(session: Session, run_id: str) -> None:
    """Place every entered company exactly once after ranking.

    Current ranking stores grouped companies in DecisionRunCompany.  Anything
    not stored there is deliberately surfaced as Ungroupable with a durable
    blocker, rather than silently disappearing.
    """
    rows = _rows(session, run_id)
    if not rows:
        return
    ranked = {row.company_id: row for row in session.execute(select(DecisionRunCompany).where(
        DecisionRunCompany.run_id == run_id,
    )).scalars()}
    for row in rows:
        if row.phase_b_outcome != "enriched":
            # A Phase B source error is not a ranking decision.  Preserve its
            # actual blocker instead of showing a falsely confident group.
            row.terminal_outcome = CompanyTerminalOutcome.UNGROUPABLE.value
            row.terminal_reason = row.phase_b_reason or "company_market_evidence_incomplete"
            continue
        decision = ranked.get(row.company_id)
        if decision and decision.group_number is not None:
            terminal = company_terminal_outcome(decision.group_number)
            row.terminal_outcome, row.terminal_reason = terminal.value, None
        else:
            blocker = row.phase_b_reason or "ranked_company_missing"
            row.terminal_outcome, row.terminal_reason = CompanyTerminalOutcome.UNGROUPABLE.value, blocker
