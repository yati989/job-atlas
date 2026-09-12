"""Run-scoped, function-level progress seam for the agentic contact skill.

The contact skill supplies judgement and evidence.  This module freezes only
the approved company/function obligation and reconciles reported terminal
outcomes; it never queries a mutable contact backlog.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.orm import (Contact, DecisionRun, DecisionRunContactEnrichmentManifest)
from .manifests import (ContactFunction, ContactObligationKind, NO_CATEGORY_MATCH,
                        NO_CONTACT_SELECTION,
                        approved_contact_obligation, contact_function,
                        reusable_contact_ids)
from .progress import (FailedRecordDisposition, FailureDisposition, StageCounts, StageName,
                       StageRecord, StageTransitionError, complete_with_errors_stage,
                       finish_stage, read_run_progress, start_stage, update_stage)
from .telemetry import is_legacy_telemetry

NO_SELECTION = NO_CONTACT_SELECTION


class ContactOutcome(str, Enum):
    ENRICHED = "enriched"
    EXHAUSTED = "exhausted"
    NO_MATCH = "no_match"
    EXCLUDED = "excluded"
    FAILED = "failed"


@dataclass(frozen=True)
class ContactEnrichmentResult:
    company_id: int
    search_group: str
    outcome: ContactOutcome
    reason: str | None = None
    disposition: FailureDisposition | None = None
    reused_contact_ids: tuple[int, ...] = ()
    new_contact_ids: tuple[int, ...] = ()
    coverage: dict | None = None


def _rows(session: Session, run_id: str) -> list[DecisionRunContactEnrichmentManifest]:
    return list(session.execute(select(DecisionRunContactEnrichmentManifest).where(
        DecisionRunContactEnrichmentManifest.run_id == run_id,
    ).order_by(DecisionRunContactEnrichmentManifest.company_id,
               DecisionRunContactEnrichmentManifest.search_group)).scalars())


def freeze_contact_enrichment_manifest(session: Session, run_id: str) -> tuple[DecisionRunContactEnrichmentManifest, ...]:
    """Freeze approved company/function selections once, including opt-outs."""
    existing = _rows(session, run_id)
    if existing:
        stage = next((item for item in read_run_progress(
            session, run_id, project_interruptions=False,
        ).stages if item.name == StageName.CONTACT_ENRICHMENT.value), None)
        if stage is not None and stage.status in ("interrupted", "failed"):
            start_stage(session, run_id, StageName.CONTACT_ENRICHMENT,
                        expected_count=len(existing), reason="resuming approved contact enrichment")
            _reconcile(session, run_id)
        return tuple(existing)
    run = session.get(DecisionRun, run_id)
    if run is None or is_legacy_telemetry(run.telemetry_version):
        raise StageTransitionError("contact enrichment requires current Decision Run telemetry")
    from .service import load_approved_scope
    scope = load_approved_scope(session, run_id)
    for company_id in scope.company_ids:
        obligation = approved_contact_obligation(session, run_id, company_id)
        if obligation.kind is ContactObligationKind.EXCLUDED:
            session.add(DecisionRunContactEnrichmentManifest(
                run_id=run_id, company_id=company_id, search_group=NO_SELECTION,
                outcome=ContactOutcome.EXCLUDED.value, reason=obligation.reason,
            ))
            continue
        if obligation.kind is ContactObligationKind.NO_MATCH:
            session.add(DecisionRunContactEnrichmentManifest(
                run_id=run_id, company_id=company_id, search_group=NO_CATEGORY_MATCH,
                outcome=ContactOutcome.NO_MATCH.value, reason=obligation.reason,
            ))
            continue
        for group in obligation.search_groups:
            session.add(DecisionRunContactEnrichmentManifest(
                run_id=run_id, company_id=company_id, search_group=group.value,
                reused_contact_ids=[],
            ))
    session.flush()
    rows = _rows(session, run_id)
    start_stage(session, run_id, StageName.CONTACT_ENRICHMENT, expected_count=len(rows),
                reason="approved company and requested contact functions frozen")
    _reconcile(session, run_id)
    return tuple(rows)


def _reconcile(session: Session, run_id: str) -> None:
    rows = _rows(session, run_id)
    records: list[StageRecord] = []
    reasons: dict[str, int] = {}
    advanced = dropped = failed = 0
    for row in rows:
        record_id = f"{row.company_id}:{row.search_group}"
        if row.outcome in (ContactOutcome.ENRICHED.value, ContactOutcome.EXHAUSTED.value,
                           ContactOutcome.NO_MATCH.value):
            # Generic stage reasons are reserved for drops/failures; terminal
            # no-match/exhaustion rationale stays on the manifest drilldown.
            records.append(StageRecord("company_function", record_id, "advanced"))
            advanced += 1
        elif row.outcome == ContactOutcome.EXCLUDED.value:
            records.append(StageRecord("company_function", record_id, "dropped", row.reason))
            dropped += 1; reasons[row.reason or "excluded"] = reasons.get(row.reason or "excluded", 0) + 1
        elif row.outcome == ContactOutcome.FAILED.value:
            records.append(StageRecord("company_function", record_id, "failed", row.reason))
            failed += 1; reasons[row.reason or "failed"] = reasons.get(row.reason or "failed", 0) + 1
    counts = StageCounts(len(rows), advanced, dropped, failed, len(rows) - advanced - dropped - failed)
    if counts.pending:
        update_stage(session, run_id, StageName.CONTACT_ENRICHMENT, counts=counts,
                     reason_counts=reasons, records=records)
        return
    if counts.failed:
        dispositions = tuple(FailedRecordDisposition("company_function", f"{row.company_id}:{row.search_group}",
            FailureDisposition(row.failure_disposition)) for row in rows
            if row.outcome == ContactOutcome.FAILED.value and row.failure_disposition)
        if len(dispositions) != failed:
            raise StageTransitionError("contact failures require non-blocking or excluded disposition")
        complete_with_errors_stage(session, run_id, StageName.CONTACT_ENRICHMENT, counts=counts,
            reason_counts=reasons, records=records, failure_dispositions=dispositions,
            reason="contact enrichment finished with explicitly recorded failures")
    else:
        finish_stage(session, run_id, StageName.CONTACT_ENRICHMENT, counts=counts,
            reason_counts=reasons, records=records, reason="approved contact functions reconciled")


def report_contact_enrichment(session: Session, run_id: str,
                              results: Iterable[ContactEnrichmentResult]) -> None:
    """Report only frozen obligations; standalone work has no ``run_id`` path."""
    rows = _rows(session, run_id)
    if not rows:
        raise StageTransitionError("contact enrichment requires a frozen approved manifest")
    by_key = {(row.company_id, row.search_group): row for row in rows}
    supplied = tuple(results)
    reusable_by_company: dict[int, set[int]] = {}
    seen = set()
    for result in supplied:
        try:
            normalized_function = contact_function(result.search_group)
        except ValueError as exc:
            raise StageTransitionError(
                "contact results must name a real frozen function, not a manifest sentinel"
            ) from exc
        key = (result.company_id, result.search_group)
        if key in seen or key not in by_key:
            raise StageTransitionError("contact result must name one frozen company/function")
        seen.add(key)
        if normalized_function.value != result.search_group:
            raise StageTransitionError("contact result function must use its canonical value")
        if not isinstance(result.outcome, ContactOutcome):
            raise StageTransitionError("contact outcome must be ContactOutcome")
        if result.outcome is ContactOutcome.FAILED and (not result.reason or result.disposition is None):
            raise StageTransitionError("contact failure requires reason and disposition")
        if result.outcome is not ContactOutcome.FAILED and result.disposition is not None:
            raise StageTransitionError("only failed contact results may have a disposition")
        if set(result.reused_contact_ids) & set(result.new_contact_ids):
            raise StageTransitionError("a contact cannot be both reused and new")
        eligible_reuse = reusable_by_company.setdefault(
            result.company_id,
            set(reusable_contact_ids(session, run_id, result.company_id)),
        )
        if not set(result.reused_contact_ids).issubset(eligible_reuse):
            raise StageTransitionError(
                "reused contacts must satisfy the existing reuse policy"
            )
        for kind, contact_ids in (("reused", result.reused_contact_ids),
                                  ("new", result.new_contact_ids)):
            evidence_rows = (result.coverage or {}).get(f"{kind}_contact_evidence", [])
            evidence_by_id = {
                item.get("contact_id"): item for item in evidence_rows
                if isinstance(item, dict)
            }
            for contact_id in contact_ids:
                contact = session.get(Contact, contact_id)
                evidence = evidence_by_id.get(contact_id)
                evidence_text = (evidence or {}).get("evidence") or (evidence or {}).get("judged_text")
                if (contact is None or contact.company_id != result.company_id or not evidence
                        or evidence.get("search_group") != result.search_group
                        or not str(evidence_text or "").strip()):
                    raise StageTransitionError(
                        f"{kind} contacts require durable company/function evidence"
                    )
        row = by_key[key]
        row.outcome = result.outcome.value
        row.reason = result.reason.strip() if result.reason else None
        row.failure_disposition = result.disposition.value if result.disposition else None
        row.reused_contact_ids = list(result.reused_contact_ids)
        row.new_contact_ids = list(result.new_contact_ids)
        row.coverage = dict(result.coverage or {})
    _reconcile(session, run_id)


def frozen_real_contact_functions(session: Session, run_id: str,
                                  company_id: int) -> frozenset[ContactFunction]:
    """Typed production view; manifest sentinels never cross this boundary."""
    result = set()
    for row in _rows(session, run_id):
        if row.company_id != company_id:
            continue
        try:
            result.add(contact_function(row.search_group))
        except ValueError:
            continue
    return frozenset(result)
