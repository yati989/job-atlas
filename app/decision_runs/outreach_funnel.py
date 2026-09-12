"""Immutable, run-scoped telemetry for draft preparation, Gmail Draft posting,
and final-report delivery.  Copy judgment stays in /draft-outreach."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.orm import (Contact, DecisionRun, DecisionRunContactEnrichmentManifest,
    DecisionRunDraftPreparationManifest, DecisionRunGmailDraftManifest,
    DecisionRunResumeTailoringManifest, JobPostingVersion, OutreachDraft, OutreachMessage, OutreachDeliveryAttempt)
from app.outreach.pairing import PairingResult
from app.outreach.pairing import REASON_NO_MATCHING_JOB
from .manifests import pair_for_approved_scope
from .progress import (FailedRecordDisposition, FailureDisposition, StageCounts, StageName,
    StageRecord, StageTransitionError, complete_with_errors_stage, finish_stage,
    read_run_progress, start_stage, update_stage, fail_stage)
from .telemetry import is_legacy_telemetry


class DraftOutcome(str, Enum): PREPARED = "prepared"; DROPPED = "dropped"; FAILED = "failed"
class GmailOutcome(str, Enum): POSTED = "posted"; DROPPED = "dropped"; FAILED = "failed"; INDETERMINATE = "indeterminate"

@dataclass(frozen=True)
class DraftResult:
    contact_id: int; outcome: DraftOutcome | str; reason: str | None = None
    disposition: FailureDisposition | str | None = None; outreach_draft_id: int | None = None
@dataclass(frozen=True)
class ChosenPairing:
    contact_id: int; job_id: int | None; posting_version_id: int | None

@dataclass(frozen=True)
class GmailResult:
    delivery_attempt_id: int; outcome: GmailOutcome | str; reason: str | None = None
    disposition: FailureDisposition | str | None = None

def _draft_rows(s, run_id):
    return list(s.execute(select(DecisionRunDraftPreparationManifest).where(
        DecisionRunDraftPreparationManifest.run_id == run_id).order_by(DecisionRunDraftPreparationManifest.id)).scalars())
def _gmail_rows(s, run_id):
    return list(s.execute(select(DecisionRunGmailDraftManifest).where(
        DecisionRunGmailDraftManifest.run_id == run_id).order_by(DecisionRunGmailDraftManifest.id)).scalars())

def freeze_draft_preparation_manifest(s: Session, run_id: str, *, chosen_pairings: Iterable[ChosenPairing] | None = None):
    existing = _draft_rows(s, run_id)
    stage = next((x for x in read_run_progress(s, run_id, project_interruptions=False).stages
                  if x.name == StageName.DRAFT_PREPARATION.value), None)
    if stage is not None: return tuple(existing)
    run = s.get(DecisionRun, run_id)
    if run is None or is_legacy_telemetry(run.telemetry_version):
        raise StageTransitionError("draft preparation requires current Decision Run telemetry")
    # Both completed predecessor manifests are the only source of scope.
    # A tailored pairing can only name a posting version whose exact resume
    # manifest reached TAILORED; global current resume rows are irrelevant.
    tailored_versions = {version.id: version.job_id for version in s.execute(select(JobPostingVersion).join(
        DecisionRunResumeTailoringManifest,
        DecisionRunResumeTailoringManifest.posting_version_id == JobPostingVersion.id,
    ).where(DecisionRunResumeTailoringManifest.run_id == run_id,
            DecisionRunResumeTailoringManifest.outcome == "tailored")).scalars()}
    tailored_job_ids = set(tailored_versions.values())
    # Contact outcomes are the only source of recipients. Deduplicate a
    # contact occurring in more than one approved function obligation.
    contacts: dict[int, int] = {}
    rows = s.execute(select(DecisionRunContactEnrichmentManifest).where(
        DecisionRunContactEnrichmentManifest.run_id == run_id,
        DecisionRunContactEnrichmentManifest.outcome.in_(("enriched", "exhausted")))).scalars()
    for row in rows:
        for contact_id in [*(row.reused_contact_ids or []), *(row.new_contact_ids or [])]:
            contact = s.get(Contact, contact_id)
            if contact is not None and contact.company_id == row.company_id:
                contacts.setdefault(contact_id, row.company_id)
    # This is an attended decision: the drafting agent must explicitly choose
    # *every* recipient's exact tailored version or explicit master path.
    # In particular, never silently select the first candidate as a fallback.
    if contacts and chosen_pairings is None:
        raise StageTransitionError("draft preparation requires explicit chosen pairings for every frozen contact")
    pairing_items = tuple(chosen_pairings or ())
    chosen = {item.contact_id: item for item in pairing_items}
    if len(chosen) != len(pairing_items):
        raise StageTransitionError("chosen pairings must name each contact once")
    if chosen and set(chosen) != set(contacts):
        raise StageTransitionError("chosen pairings must cover exactly the frozen contacts")
    # The live-draft key is normalized recipient email, so resolve collisions
    # before any persistence. Lowest contact id is deterministic; the other
    # approved pairing is retained as an explicit dropped ledger record.
    email_winners: dict[str, int] = {}
    for contact_id in sorted(contacts):
        contact = s.get(Contact, contact_id)
        email = (contact.email_guess or "").strip().lower() if contact else ""
        if email and email in email_winners:
            pair = pair_for_approved_scope(s, run_id, contact_id)
            s.add(DecisionRunDraftPreparationManifest(run_id=run_id, company_id=contacts[contact_id], contact_id=contact_id,
                candidate_job_ids=list(pair.candidate_job_ids), resume_kind=pair.resume_kind,
                pairing_reason=pair.reason, outcome=DraftOutcome.DROPPED.value,
                reason=f"recipient_email_collision_with_contact:{email_winners[email]}"))
        else:
            if email: email_winners[email] = contact_id
    for contact_id, company_id in contacts.items():
        contact = s.get(Contact, contact_id)
        email = (contact.email_guess or "").strip().lower() if contact else ""
        if email and email_winners.get(email) != contact_id:
            continue
        pair: PairingResult = pair_for_approved_scope(s, run_id, contact_id)
        # A contact whose policy candidates all failed tailoring may take the
        # explicit master route. This changes eligibility, never chooses a
        # version for the agent.
        if pair.resume_kind == "tailored":
            candidates = [job_id for job_id in pair.candidate_job_ids if job_id in tailored_job_ids]
            pair = (PairingResult("tailored", pair.reason, search_group=pair.search_group,
                                  candidate_job_ids=candidates) if candidates else
                    PairingResult("master", REASON_NO_MATCHING_JOB, search_group=pair.search_group))
        frozen_version_id = None
        explicit = chosen.get(contact_id)
        assert explicit is not None  # coverage checked above
        if explicit.job_id is None or explicit.posting_version_id is None:
            if explicit.job_id is not None or explicit.posting_version_id is not None:
                raise StageTransitionError("explicit master pairing requires both job_id and posting_version_id to be null")
            if pair.resume_kind != "master":
                raise StageTransitionError("explicit master pairing is not permitted when approved tailored evidence exists")
            pair = PairingResult("master", pair.reason, search_group=pair.search_group)
        else:
            if (explicit.job_id not in pair.candidate_job_ids or explicit.posting_version_id not in tailored_versions
                    or tailored_versions[explicit.posting_version_id] != explicit.job_id):
                raise StageTransitionError("chosen pairing is not an approved tailored candidate")
            frozen_version_id = explicit.posting_version_id
            pair = PairingResult("tailored", pair.reason, search_group=pair.search_group,
                                 candidate_job_ids=[explicit.job_id])
        if not contact.email_guess:
            s.add(DecisionRunDraftPreparationManifest(run_id=run_id, company_id=company_id, contact_id=contact_id,
                posting_version_id=frozen_version_id, job_id=tailored_versions.get(frozen_version_id),
                candidate_job_ids=list(pair.candidate_job_ids), resume_kind=pair.resume_kind,
                pairing_reason=pair.reason, outcome=DraftOutcome.DROPPED.value, reason="no_usable_email"))
        else:
            s.add(DecisionRunDraftPreparationManifest(run_id=run_id, company_id=company_id, contact_id=contact_id,
                posting_version_id=frozen_version_id, job_id=tailored_versions.get(frozen_version_id),
                candidate_job_ids=list(pair.candidate_job_ids), resume_kind=pair.resume_kind, pairing_reason=pair.reason))
    s.flush(); rows = _draft_rows(s, run_id)
    start_stage(s, run_id, StageName.DRAFT_PREPARATION, expected_count=len(rows),
                reason="exact approved contact-to-job pairings frozen for draft preparation")
    _publish_drafts(s, run_id); return tuple(rows)

def _validate_result(outcome, reason, disposition):
    try: outcome = DraftOutcome(outcome)
    except ValueError as exc: raise StageTransitionError("unknown draft outcome") from exc
    if outcome is DraftOutcome.FAILED:
        if not reason or disposition is None: raise StageTransitionError("failed draft requires reason and disposition")
        try: disposition = FailureDisposition(disposition).value
        except ValueError as exc: raise StageTransitionError("draft failure disposition is invalid") from exc
    elif disposition is not None: raise StageTransitionError("only failed drafts may have a disposition")
    elif outcome is DraftOutcome.DROPPED and not reason: raise StageTransitionError("dropped draft requires a reason")
    return outcome.value, reason.strip() if reason else None, disposition

def report_draft_preparation(s: Session, run_id: str, results: Iterable[DraftResult]):
    rows = {r.contact_id: r for r in _draft_rows(s, run_id)}
    if not rows: raise StageTransitionError("draft preparation requires a frozen manifest")
    stage = next((x for x in read_run_progress(s, run_id, project_interruptions=False).stages
                  if x.name == StageName.DRAFT_PREPARATION.value), None)
    if stage is not None and stage.status in ("completed", "completed_with_errors"):
        return
    seen = set()
    for result in results:
        if result.contact_id in seen or result.contact_id not in rows: raise StageTransitionError("draft result must name one frozen contact")
        seen.add(result.contact_id); row = rows[result.contact_id]
        outcome, reason, disposition = _validate_result(result.outcome, result.reason, result.disposition)
        if outcome == DraftOutcome.PREPARED.value:
            draft = s.get(OutreachDraft, result.outreach_draft_id) if result.outreach_draft_id else None
            if not draft or draft.contact_id != row.contact_id or draft.company_id != row.company_id:
                raise StageTransitionError("prepared draft must identify its frozen recipient")
            if draft.resume_kind != row.resume_kind or draft.pairing_reason != row.pairing_reason:
                raise StageTransitionError("prepared draft disagrees with frozen pairing")
            if row.candidate_job_ids and draft.matched_job_id not in row.candidate_job_ids:
                raise StageTransitionError("prepared draft uses a job outside the frozen pairing candidates")
            if row.job_id is not None and draft.matched_job_id != row.job_id:
                raise StageTransitionError("prepared draft does not use the frozen tailored job")
            message = s.execute(select(OutreachMessage).where(OutreachMessage.run_id == run_id,
                OutreachMessage.contact_id == row.contact_id)).scalar_one_or_none()
            if message is None or message.posting_version_id != row.posting_version_id:
                raise StageTransitionError("prepared draft ledger does not prove the frozen posting version")
            row.outreach_draft_id = draft.id
        row.outcome, row.reason, row.failure_disposition = outcome, reason, disposition
    _publish_drafts(s, run_id)

def report_saved_draft(s: Session, *, run_id: str | None, draft: OutreachDraft) -> None:
    if run_id is None: return
    report_draft_preparation(s, run_id, [DraftResult(draft.contact_id, DraftOutcome.PREPARED, outreach_draft_id=draft.id)])

def _publish_outcome_stage(s, run_id, *, stage_name, rows, record_type, record_id,
                           advanced_outcomes, dropped_outcomes, failure_outcomes, reason):
    """Shared typed publisher for immutable funnel manifests.

    Each caller supplies only its domain mapping; count arithmetic, durable
    records, reason aggregation and terminal transitions stay identical.
    """
    records=[]; reasons={}; a=d=f=0
    for row in rows:
        identifier = str(record_id(row))
        if row.outcome in advanced_outcomes: records.append(StageRecord(record_type, identifier, "advanced")); a += 1
        elif row.outcome in dropped_outcomes: records.append(StageRecord(record_type, identifier, "dropped", row.reason)); d += 1; reasons[row.reason] = reasons.get(row.reason, 0)+1
        elif row.outcome in failure_outcomes: records.append(StageRecord(record_type, identifier, "failed", row.reason)); f += 1; reasons[row.reason] = reasons.get(row.reason, 0)+1
    counts=StageCounts(len(rows),a,d,f,len(rows)-a-d-f)
    if counts.pending:
        update_stage(s,run_id,stage_name,counts=counts,reason_counts=reasons,records=records)
        return
    kwargs=dict(counts=counts,reason_counts=reasons,records=records,reason=reason)
    if f:
        if stage_name == StageName.GMAIL_DRAFT_POSTING: fail_stage(s,run_id,stage_name,**kwargs)
        else: complete_with_errors_stage(s,run_id,stage_name,failure_dispositions=tuple(FailedRecordDisposition(record_type,str(record_id(r)),FailureDisposition(r.failure_disposition)) for r in rows if r.outcome in failure_outcomes),**kwargs)
    else: finish_stage(s,run_id,stage_name,**kwargs)


def _publish_drafts(s, run_id):
    _publish_outcome_stage(s, run_id, stage_name=StageName.DRAFT_PREPARATION, rows=_draft_rows(s, run_id),
        record_type="contact", record_id=lambda row: row.contact_id, advanced_outcomes=("prepared",),
        dropped_outcomes=("dropped",), failure_outcomes=("failed",), reason="approved draft preparation reconciled")

def freeze_gmail_draft_manifest(s: Session, run_id: str):
    existing=_gmail_rows(s,run_id)
    gmail_stage = next((x for x in read_run_progress(s, run_id, project_interruptions=False).stages
                        if x.name == StageName.GMAIL_DRAFT_POSTING.value), None)
    if gmail_stage is not None: return tuple(existing)
    prepared=[r for r in _draft_rows(s,run_id) if r.outcome=="prepared"]
    draft_stage = next((x for x in read_run_progress(s, run_id, project_interruptions=False).stages
                        if x.name == StageName.DRAFT_PREPARATION.value), None)
    if draft_stage is None or draft_stage.status not in ("completed", "completed_with_errors", "skipped"):
        raise StageTransitionError("Gmail posting requires terminal draft preparation")
    for row in prepared:
        message = s.execute(select(OutreachMessage).where(OutreachMessage.run_id == run_id,
            OutreachMessage.contact_id == row.contact_id, OutreachMessage.posting_version_id == row.posting_version_id)).scalar_one_or_none()
        attempt = s.execute(select(OutreachDeliveryAttempt).where(
            OutreachDeliveryAttempt.message_id == message.id).order_by(OutreachDeliveryAttempt.sequence_number.desc())).scalars().first() if message else None
        if attempt is None: raise StageTransitionError("prepared run draft requires its immutable delivery attempt")
        s.add(DecisionRunGmailDraftManifest(run_id=run_id,draft_preparation_manifest_id=row.id,
            outreach_draft_id=row.outreach_draft_id, delivery_attempt_id=attempt.id))
    s.flush(); rows=_gmail_rows(s,run_id); start_stage(s,run_id,StageName.GMAIL_DRAFT_POSTING,expected_count=len(rows),reason="exact prepared draft manifest frozen for Gmail Draft posting"); _publish_gmail(s,run_id); return tuple(rows)

def report_gmail_posting(s: Session, run_id: str, results: Iterable[GmailResult]):
    manifest_rows = _gmail_rows(s, run_id)
    rows={r.delivery_attempt_id:r for r in manifest_rows}
    if not rows and not any(x.name==StageName.GMAIL_DRAFT_POSTING.value for x in read_run_progress(s,run_id).stages): raise StageTransitionError("Gmail posting requires a frozen manifest")
    seen=set()
    for result in results:
        attempt_for_result = s.get(OutreachDeliveryAttempt, result.delivery_attempt_id)
        row = rows.get(result.delivery_attempt_id)
        if row is None and attempt_for_result is not None:
            # Later calibration/release attempts belong to the recipient's
            # original manifest row, never a newly-numbered funnel item.
            row = next((item for item in manifest_rows
                        if (original := s.get(OutreachDeliveryAttempt, item.delivery_attempt_id)) is not None
                        and original.message_id == attempt_for_result.message_id), None)
        if result.delivery_attempt_id in seen or row is None: raise StageTransitionError("Gmail result must name one frozen delivery attempt")
        seen.add(result.delivery_attempt_id)
        try: outcome=GmailOutcome(result.outcome)
        except ValueError as exc: raise StageTransitionError("unknown Gmail outcome") from exc
        if outcome in (GmailOutcome.FAILED, GmailOutcome.INDETERMINATE):
            if not result.reason or result.disposition is None: raise StageTransitionError("failed Gmail posting requires reason and disposition")
            disposition=FailureDisposition(result.disposition).value
        elif result.disposition is not None: raise StageTransitionError("only failed Gmail posting may have a disposition")
        else: disposition=None
        attempt = s.get(OutreachDeliveryAttempt, result.delivery_attempt_id)
        canonical = s.get(OutreachDeliveryAttempt, row.delivery_attempt_id)
        if attempt is None or canonical is None or attempt.message_id != canonical.message_id:
            raise StageTransitionError("Gmail result attempt is outside the prepared recipient ledger")
        if outcome is GmailOutcome.POSTED and not (attempt and attempt.gmail_draft_id): raise StageTransitionError("posted means a Gmail Draft exists")
        terminal_stage = next((x for x in read_run_progress(s, run_id, project_interruptions=False).stages
                               if x.name == StageName.GMAIL_DRAFT_POSTING.value), None)
        if terminal_stage is not None and terminal_stage.status in ("completed", "completed_with_errors"):
            if row.outcome != outcome.value or row.delivery_attempt_id != result.delivery_attempt_id:
                raise StageTransitionError("terminal Gmail posting cannot be mutated by a later attempt")
            continue
        evidence = dict(row.attempt_evidence or {})
        evidence.setdefault("attempts", [])
        if not any(item["attempt_id"] == result.delivery_attempt_id for item in evidence["attempts"]):
            evidence["attempts"].append({"attempt_id": result.delivery_attempt_id, "outcome": outcome.value,
                "state": attempt.state, "provider_draft_id": attempt.gmail_draft_id, "reason": result.reason})
        row.delivery_attempt_id = result.delivery_attempt_id
        row.attempt_evidence = evidence
        row.outcome,row.reason,row.failure_disposition=outcome.value,result.reason.strip() if result.reason else None,disposition
    stage = next((x for x in read_run_progress(s, run_id, project_interruptions=False).stages
                  if x.name == StageName.GMAIL_DRAFT_POSTING.value), None)
    # The funnel records one prepared recipient, not one sequence number.
    # A reconciled calibration retry updates the same durable row/evidence
    # after the initial posting stage has already become terminal.
    if stage is None or stage.status not in ("completed", "completed_with_errors"):
        _publish_gmail(s,run_id)

def report_gmail_draft_posted(s: Session, *, run_id: str | None, delivery_attempt_id: int) -> None:
    if run_id is not None: report_gmail_posting(s,run_id,[GmailResult(delivery_attempt_id,GmailOutcome.POSTED)])

def _publish_gmail(s,run_id):
    _publish_outcome_stage(s, run_id, stage_name=StageName.GMAIL_DRAFT_POSTING, rows=_gmail_rows(s, run_id),
        record_type="outreach_draft", record_id=lambda row: row.outreach_draft_id, advanced_outcomes=("posted",),
        dropped_outcomes=("dropped",), failure_outcomes=("failed", "indeterminate"), reason="Gmail Draft posting reconciled")


def prepare_gmail_retry(s: Session, run_id: str) -> None:
    """Legally restart only a failed posting attempt; never auto-retry it."""
    stage = next((x for x in read_run_progress(s, run_id, project_interruptions=False).stages
                  if x.name == StageName.GMAIL_DRAFT_POSTING.value), None)
    if stage is not None and stage.status == "failed":
        start_stage(s, run_id, StageName.GMAIL_DRAFT_POSTING, expected_count=stage.counts.input,
                    reason="operator-authorized Gmail posting recovery")


def reconcile_indeterminate_gmail(s: Session, run_id: str, *, delivery_attempt_id: int,
                                   provider_draft_id: str | None = None,
                                   provider_thread_id: str | None = None,
                                   confirmed_not_created: bool = False) -> None:
    """Operator-only resolution for an ambiguous provider call. No resend."""
    if bool(provider_draft_id) == bool(confirmed_not_created):
        raise StageTransitionError("provide a provider draft id or confirmed-not-created, exclusively")
    attempt = s.get(OutreachDeliveryAttempt, delivery_attempt_id)
    if attempt is None or attempt.state not in ("indeterminate", "posting_intent"):
        raise StageTransitionError("only an indeterminate or committed posting intent can be reconciled")
    prepare_gmail_retry(s, run_id)
    if provider_draft_id:
        attempt.gmail_draft_id, attempt.gmail_thread_id, attempt.state = provider_draft_id, provider_thread_id, "pushed"
        report_gmail_posting(s, run_id, [GmailResult(attempt.id, GmailOutcome.POSTED)])
    else:
        attempt.state = "held"; message = s.get(OutreachMessage, attempt.message_id); message.state = "held"
        for row in _gmail_rows(s, run_id):
            if row.delivery_attempt_id == attempt.id:
                row.outcome = row.reason = row.failure_disposition = None
                row.attempt_evidence = {**(row.attempt_evidence or {}), "confirmed_not_created": True}
                break

def begin_final_report(s: Session, run_id: str): start_stage(s,run_id,StageName.FINAL_REPORT,expected_count=1,reason="building final workbook and requested self report")
def report_final_report(s: Session, run_id: str, *, output: str, self_report_requested: bool, self_report_delivered: bool):
    if not Path(output).is_file(): raise StageTransitionError("final report cannot complete until its workbook exists")
    if self_report_requested and not self_report_delivered: raise StageTransitionError("final report cannot complete until requested self report is delivered")
    run = s.get(DecisionRun, run_id)
    run.final_report_path = output
    run.final_report_delivery_requested = self_report_requested
    if self_report_delivered and not run.final_report_delivery_receipt:
        run.final_report_delivery_receipt = "self_report_delivered"
    reason = ("final workbook created and requested self report delivered" if self_report_requested
              else "final workbook created; self delivery not requested")
    finish_stage(s,run_id,StageName.FINAL_REPORT,counts=StageCounts(1,1,0,0,0),records=(StageRecord("workbook",output,"advanced"),),reason=reason)


def reconcile_final_report_delivery(s: Session, run_id: str, *, receipt: str | None = None,
                                    confirmed_not_delivered: bool = False) -> None:
    """Operator-only final self-report reconciliation; it never sends mail."""
    run = s.get(DecisionRun, run_id)
    if run is None or run.final_report_delivery_state not in ("indeterminate", "in_progress"):
        raise StageTransitionError("final report is not awaiting delivery reconciliation")
    if bool(receipt) == bool(confirmed_not_delivered):
        raise StageTransitionError("provide a receipt or confirmed-not-delivered, exclusively")
    if receipt:
        stage = next(x for x in read_run_progress(s, run_id, project_interruptions=False).stages if x.name == StageName.FINAL_REPORT.value)
        if stage.status == "failed":
            start_stage(s, run_id, StageName.FINAL_REPORT, expected_count=1, reason="operator final-report delivery reconciliation")
        run.final_report_delivery_receipt, run.final_report_delivery_state = receipt, "delivered"
        report_final_report(s, run_id, output=run.final_report_path, self_report_requested=True, self_report_delivered=True)
        from app.models.orm import utcnow
        run.state, run.completed_at = "completed", utcnow()
    else:
        stage = next(x for x in read_run_progress(s, run_id, project_interruptions=False).stages if x.name == StageName.FINAL_REPORT.value)
        if stage.status == "running":
            fail_stage(s, run_id, StageName.FINAL_REPORT,
                       reason="operator_confirmed_final_report_not_delivered")
        run.final_report_delivery_state = "failed"
