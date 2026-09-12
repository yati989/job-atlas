"""Decision-run deep module implementation behind four public operations."""
from datetime import datetime, timezone
import secrets

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from app.models.orm import (Company, DecisionRun, DecisionRunApproval, DecisionRunCompany,
    DecisionRunFinding, DecisionRunJob, DecisionRunSelectedJob, Job, JobApplication,
    JobPostingVersion, JobScreeningFact, DecisionRunJobEnrichmentManifest)
from .approval import parse_approval
from .company_funnel import (
    freeze_company_funnel_manifest,
    record_company_terminal_outcomes,
)
from .distributions import salary_distribution
from .enrichment_funnel import report_screening_outcomes, screening_may_start
from .progress import StageName, StageStatus, declare_pending_stage, read_run_progress
from .ranking import RankableJob, build_ranking_plan, classify_role, qualified_wlb
from .screening import evaluate_screening
from .types import ApprovedScope, DecisionPolicy, parse_since
from app.outreach.state import fresh_outreach_eligibility
from .telemetry import is_legacy_telemetry
from .telemetry import CURRENT_TELEMETRY_VERSION
from .lifecycle import (
    approval_artifact_ready,
    approval_input_received,
    approval_input_rejected,
    begin_approval_preparation,
)
from .progress import complete_approval_gate


UNDATED_CURRENT_BATCH_SOURCES = frozenset({"instahyre"})


def create_decision_run(session: Session, *, since: str | datetime, cutoff: datetime | None = None,
                        policy: DecisionPolicy | None = None) -> DecisionRun:
    policy = policy or DecisionPolicy()
    since_at = parse_since(since, timezone_name=policy.input_timezone) if isinstance(since, str) else since
    cutoff = cutoff or datetime.now(timezone.utc)
    if cutoff.tzinfo is None: cutoff = cutoff.replace(tzinfo=timezone.utc)
    if since_at > cutoff: raise ValueError("--since must not be after run cutoff")
    session.execute(update(DecisionRun).where(DecisionRun.state.in_(("preparing", "awaiting_approval"))).values(state="expired"))
    run = DecisionRun(id=f"{cutoff.strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(2)}", since_at=since_at,
                      cutoff_at=cutoff, input_timezone=policy.input_timezone, policy_snapshot=policy.snapshot(),
                      target_count=policy.fresh_company_target, state="preparing",
                      telemetry_version=CURRENT_TELEMETRY_VERSION)
    session.add(run); session.flush(); return run


def _latest_versions(session: Session, run: DecisionRun):
    manifest = list(session.execute(select(DecisionRunJobEnrichmentManifest).where(
        DecisionRunJobEnrichmentManifest.run_id == run.id,
    ).order_by(DecisionRunJobEnrichmentManifest.id)).scalars())
    if not is_legacy_telemetry(run.telemetry_version):
        advanced_ids = {
            item.posting_version_id for item in manifest if item.outcome == "enriched"
        }
        versions = list(session.execute(select(JobPostingVersion).where(
            JobPostingVersion.id.in_(advanced_ids),
        )).scalars())
        by_id = {version.id: version for version in versions}
        return [by_id[item.posting_version_id] for item in manifest if item.posting_version_id in by_id]
    versions = session.execute(select(JobPostingVersion).join(Job, JobPostingVersion.job_id == Job.id).where(
        Job.status == "active", Job.duplicate_of_job_id.is_(None),
        # Keep post-cutoff rows long enough to persist a future-date anomaly;
        # `prepare_decision_run` excludes them from ranking below.
        or_(JobPostingVersion.posted_at.is_(None), JobPostingVersion.posted_at >= run.since_at),
    ).order_by(JobPostingVersion.job_id, JobPostingVersion.id.desc())).scalars().all()
    seen = set()
    return [v for v in versions if not (v.job_id in seen or seen.add(v.job_id))]


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def screen_decision_run(session: Session, run_id: str, *, policy: DecisionPolicy | None = None) -> dict:
    run = session.get(DecisionRun, run_id)
    if run is None: raise ValueError(f"unknown decision run {run_id}")
    if run.state not in ("preparing", "enriching_companies"): raise ValueError(f"run {run_id} is {run.state}")
    if session.execute(select(DecisionRunJob).where(DecisionRunJob.run_id == run_id)).first():
        return _summary(session, run_id)
    screening_may_start(session, run_id)
    p = policy or DecisionPolicy(**run.policy_snapshot)
    candidates = _latest_versions(session, run)
    is_current_manifest = not is_legacy_telemetry(run.telemetry_version)
    applications = list(session.execute(select(JobApplication)).scalars())
    applied_versions = {application.posting_version_id for application in applications}
    applied_episodes = set()
    for application in applications:
        applied = session.get(JobPostingVersion, application.posting_version_id)
        if applied is not None:
            applied_episodes.add((application.canonical_duplicate_root_id, applied.posted_at, applied.material_content_hash))
    for version in candidates:
        job, company = session.get(Job, version.job_id), session.get(Company, session.get(Job, version.job_id).company_id)
        anomaly = None
        if not job.description_raw: anomaly = "missing_description"
        elif version.posted_at is None and not (
            is_current_manifest and job.source in UNDATED_CURRENT_BATCH_SOURCES
        ):
            anomaly = "missing_posted_at"
        elif version.posted_at is not None and _as_utc(version.posted_at) > _as_utc(run.cutoff_at):
            anomaly = "future_posted_at"
        root = version.canonical_duplicate_root_id
        facts = session.execute(select(JobScreeningFact).where(JobScreeningFact.posting_version_id == version.id).order_by(JobScreeningFact.id.desc())).scalar_one_or_none()
        if version.id in applied_versions or (root, version.posted_at, version.material_content_hash) in applied_episodes:
            outcome, findings = "applied_duplicate", []
        elif anomaly:
            outcome, findings = "anomaly", []
        else:
            findings = evaluate_screening(facts.salary if facts else None, facts.minimum_experience if facts else None, facts.maximum_experience if facts else None)
            outcome = "hard_rejected" if findings else "eligible"
        explicit = (facts.salary or {}).get("guaranteed_max_lpa") if facts else None
        effective, source = (explicit, "explicit") if explicit is not None else (company.ambitionbox_estimated_salary_lpa, "ambitionbox") if company.ambitionbox_estimated_salary_lpa is not None else (None, None)
        role = classify_role(job.title, job.description_raw)
        snapshot = {"title": job.title, "source": job.source,
                    "external_job_id": job.external_job_id,
                    "posted_at": version.posted_at.isoformat() if version.posted_at else None,
                    "description": job.description_raw, "anomaly": anomaly, "company_name": company.name,
                    # The decision report must be reproducible from immutable
                    # snapshots, never mutable live Job rows.
                    "job_url": version.snapshot.get("job_url") or version.snapshot.get("apply_url"),
                    "apply_url": version.snapshot.get("apply_url") or job.apply_url}
        row = DecisionRunJob(run_id=run_id, posting_version_id=version.id, job_id=job.id, company_id=company.id,
            outcome=outcome, snapshot=snapshot, explicit_salary_lpa=explicit,
            ambitionbox_salary_lpa=company.ambitionbox_estimated_salary_lpa, effective_salary_lpa=effective,
            salary_source=source, role_family=role, work_mode="remote" if job.is_remote is True else "other",
            qualified_wlb=qualified_wlb(company.glassdoor_wlb_rating, company.glassdoor_review_count, p.wlb_min_reviews),
            review_count=company.glassdoor_review_count)
        session.add(row); session.flush()
        for finding in findings:
            session.add(DecisionRunFinding(decision_run_job_id=row.id, kind="hard_reject", reason_code=finding.reason_code,
                evidence=finding.evidence, parsed_value=str(finding.parsed_value), threshold=str(finding.threshold), comparison="reject"))
        if anomaly: session.add(DecisionRunFinding(decision_run_job_id=row.id, kind="anomaly", reason_code=anomaly))
    # Screening creates exact eligible jobs first. Company Phase B is an
    # agent-driven boundary and must finish before salary/WLB ranking reads
    # current market evidence. Phase A contact prerequisites wait for approval.
    if is_legacy_telemetry(run.telemetry_version):
        # Genuine pre-instrumentation runs have no exact company evidence
        # manifest. Preserve their historical ranking compatibility rather
        # than falsely claiming they followed the new funnel lifecycle.
        session.flush()
    else:
        freeze_company_funnel_manifest(session, run_id)
        declare_pending_stage(
            session, run_id, StageName.SCREENING_RANKING_GROUPING,
            expected_count=len(candidates),
        )
        run.state = "enriching_companies"
        session.flush()
        return _summary(session, run_id)

    eligible = list(session.execute(select(DecisionRunJob).where(
        DecisionRunJob.run_id == run_id, DecisionRunJob.outcome == "eligible",
    )).scalars())
    _rank_and_persist(session, run, eligible, p)
    run.state, run.prepared_at = "awaiting_approval", datetime.now(timezone.utc)
    session.flush(); return _summary(session, run_id)


def _company_enrichment_is_terminal(session: Session, run_id: str) -> bool:
    stages = {stage.name: stage.status for stage in read_run_progress(session, run_id, project_interruptions=False).stages}
    terminal = {StageStatus.COMPLETED.value, StageStatus.COMPLETED_WITH_ERRORS.value}
    return stages.get(StageName.COMPANY_PHASE_B.value) in terminal


def _snapshot_posted_at(run_job: DecisionRunJob) -> datetime | None:
    value = (run_job.snapshot or {}).get("posted_at")
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _rank_and_persist(
    session: Session,
    run: DecisionRun,
    eligible: list[DecisionRunJob],
    policy: DecisionPolicy,
) -> None:
    """Persist one pure ranking plan for every Decision Run lifecycle."""
    plan = build_ranking_plan(
        RankableJob(
            run_job_id=run_job.id,
            job_id=run_job.job_id,
            company_id=run_job.company_id,
            role_family=run_job.role_family or "other",
            effective_salary_lpa=run_job.effective_salary_lpa,
            qualified_wlb=run_job.qualified_wlb,
            posted_at=_snapshot_posted_at(run_job),
            is_remote=run_job.work_mode == "remote",
        )
        for run_job in eligible
    )
    jobs_by_id = {run_job.id: run_job for run_job in eligible}
    for placement in plan.jobs:
        run_job = jobs_by_id[placement.run_job_id]
        run_job.group_number = placement.group_number
        run_job.within_company_rank = placement.within_company_rank
        run_job.is_primary = placement.is_primary
    for placement in plan.companies:
        company = session.get(Company, placement.company_id)
        eligibility = fresh_outreach_eligibility(
            session, company.id, threshold=policy.fresh_outreach_threshold,
        )
        session.add(DecisionRunCompany(
            run_id=run.id,
            company_id=company.id,
            group_number=placement.group_number,
            rank=placement.rank,
            primary_run_job_id=placement.primary_run_job_id,
            company_type=company.company_type or "employer",
            outreach_success_count=eligibility["successful_initial_contacts"],
        ))


def finalize_decision_run(
    session: Session, run_id: str, *, policy: DecisionPolicy | None = None,
    defer_approval_wait: bool = False,
) -> dict:
    """Rank after Phase B market evidence; Phase A is approved-scope work."""
    run = session.get(DecisionRun, run_id)
    if run is None: raise ValueError(f"unknown decision run {run_id}")
    if run.state == "awaiting_approval": return _summary_with_available_companies(session, run_id)
    if run.state != "enriching_companies": raise ValueError(f"run {run_id} is {run.state}")
    stages = {stage.name: stage for stage in read_run_progress(
        session, run_id, project_interruptions=False,
    ).stages}
    screening = stages.get(StageName.SCREENING_RANKING_GROUPING.value)
    approval = stages.get(StageName.APPROVAL_GATE.value)
    if screening is not None and screening.status == StageStatus.COMPLETED.value:
        resumed_preparation = approval is None or approval.status == StageStatus.INTERRUPTED.value
        if resumed_preparation:
            begin_approval_preparation(session, run_id)
        elif approval.status not in {
            StageStatus.RUNNING.value, StageStatus.WAITING_FOR_APPROVAL.value,
            StageStatus.COMPLETED.value,
        }:
            raise ValueError(f"approval preparation cannot resume from {approval.status}")
        summary = _summary_with_available_companies(session, run_id)
        if not defer_approval_wait and (
            resumed_preparation or approval.status == StageStatus.RUNNING.value
        ):
            approval_artifact_ready(
                session, run_id,
                available_company_count=summary["available_company_count"],
            )
        return summary
    if not _company_enrichment_is_terminal(session, run_id):
        raise ValueError("company Phase B must reach terminal completion before ranking")
    manifests = {
        item.company_id: item
        for item in freeze_company_funnel_manifest(session, run_id)
    }
    p = policy or DecisionPolicy(**run.policy_snapshot)
    eligible = list(session.execute(select(DecisionRunJob).where(DecisionRunJob.run_id == run_id, DecisionRunJob.outcome == "eligible")).scalars())
    for run_job in eligible:
        manifest = manifests[run_job.company_id]
        if manifest.phase_b_outcome != "enriched":
            continue
        company = session.get(Company, run_job.company_id)
        explicit = run_job.explicit_salary_lpa
        effective, source = (explicit, "explicit") if explicit is not None else (company.ambitionbox_estimated_salary_lpa, "ambitionbox") if company.ambitionbox_estimated_salary_lpa is not None else (None, None)
        run_job.ambitionbox_salary_lpa, run_job.effective_salary_lpa, run_job.salary_source = company.ambitionbox_estimated_salary_lpa, effective, source
        run_job.qualified_wlb, run_job.review_count = qualified_wlb(company.glassdoor_wlb_rating, company.glassdoor_review_count, p.wlb_min_reviews), company.glassdoor_review_count
    rankable = [run_job for run_job in eligible
                if manifests[run_job.company_id].phase_b_outcome == "enriched"]
    _rank_and_persist(session, run, rankable, p)
    report_screening_outcomes(session, run_id)
    record_company_terminal_outcomes(session, run_id)
    session.flush()
    available_company_count = len(session.execute(select(DecisionRunCompany.id).where(
        DecisionRunCompany.run_id == run_id,
    )).all())
    begin_approval_preparation(session, run_id)
    if not defer_approval_wait:
        approval_artifact_ready(
            session, run_id, available_company_count=available_company_count,
            now=datetime.now(timezone.utc),
        )
    summary = _summary_with_available_companies(session, run_id)
    return summary


def prepare_decision_run(
    session: Session, run_id: str, *, policy: DecisionPolicy | None = None,
    defer_approval_wait: bool = False,
) -> dict:
    """Compatibility command: screen first, finalize only when evidence is terminal."""
    run = session.get(DecisionRun, run_id)
    if run is None: raise ValueError(f"unknown decision run {run_id}")
    if run.state == "preparing": return screen_decision_run(session, run_id, policy=policy)
    if run.state == "enriching_companies":
        return finalize_decision_run(
            session, run_id, policy=policy, defer_approval_wait=defer_approval_wait,
        ) if _company_enrichment_is_terminal(session, run_id) else _summary(session, run_id)
    return _summary(session, run_id)


def _summary(session: Session, run_id: str) -> dict:
    rows = session.execute(select(DecisionRunJob).where(DecisionRunJob.run_id == run_id)).scalars().all()
    return {"run_id": run_id, "outcomes": {state: sum(r.outcome == state for r in rows) for state in {r.outcome for r in rows}},
            "groups": {group: salary_distribution([r.effective_salary_lpa for r in rows if r.group_number == group]) for group in range(1, 5)}}


def _summary_with_available_companies(session: Session, run_id: str) -> dict:
    summary = _summary(session, run_id)
    summary["available_company_count"] = len(session.execute(select(
        DecisionRunCompany.id,
    ).where(DecisionRunCompany.run_id == run_id)).all())
    return summary


def approve_decision_run(session: Session, run_id: str, approval: str, *, approver: str | None = None,
                         contact_search_selections: dict[int, str] | None = None,
                         input_received: bool = False):
    run = session.get(DecisionRun, run_id)
    if run is None: raise ValueError("approval must name an existing run")
    if run.state in ("approved", "processing", "completed"):
        return load_approved_scope(session, run_id)
    if run.state != "awaiting_approval": raise ValueError("approval must name the active awaiting-approval run")
    legacy = is_legacy_telemetry(run.telemetry_version)
    if not legacy and not input_received:
        approval_input_received(session, run_id)
    try:
        parsed = parse_approval(approval, maximum_target=run.target_count)
        companies = session.execute(select(DecisionRunCompany).where(DecisionRunCompany.run_id == run_id).order_by(DecisionRunCompany.group_number, DecisionRunCompany.rank)).scalars().all()
        _validate_contact_search_selections(contact_search_selections, companies)
    except Exception:
        if not legacy:
            approval_input_rejected(
                session, run_id, reason="approval input rejected during validation",
            )
        raise
    chosen = []
    fresh_chosen = 0
    for company in companies:
        limit = parsed.group_limits[company.group_number]
        used = sum(c.group_number == company.group_number for c in chosen)
        if limit is not None and used >= limit:
            continue
        is_fresh = company.outreach_success_count < run.policy_snapshot["fresh_outreach_threshold"]
        # Application-only rows are still an approved job scope, but their
        # historical outreach state must never consume the fresh quota.
        if not is_fresh or fresh_chosen < parsed.target:
            chosen.append(company)
            if is_fresh:
                fresh_chosen += 1
    normalized_rules = {str(k): v for k, v in parsed.group_limits.items()}
    if contact_search_selections is not None:
        normalized_rules["contact_search_selections"] = {
            str(company_id): selection for company_id, selection in contact_search_selections.items()
        }
    return _commit_approved_scope(
        session, run, companies, chosen, original_text=approval,
        normalized_rules=normalized_rules, target_count=parsed.target,
        approver=approver, legacy=legacy,
    )


def _commit_approved_scope(
    session: Session, run: DecisionRun, companies: list[DecisionRunCompany],
    chosen: list[DecisionRunCompany], *, original_text: str,
    normalized_rules: dict, target_count: int, approver: str | None,
    legacy: bool,
) -> ApprovedScope:
    """Persist one validated exact scope through the shared approval tail."""
    session.add(DecisionRunApproval(
        run_id=run.id, original_text=original_text,
        normalized_rules=normalized_rules, target_count=target_count,
        approver=approver,
    ))
    for order, company in enumerate(chosen, 1):
        is_fresh = company.outreach_success_count < run.policy_snapshot["fresh_outreach_threshold"]
        company.mode = "fresh_outreach_selected" if is_fresh else "application_only_selected"
        run_jobs = session.execute(select(DecisionRunJob).where(
            DecisionRunJob.run_id == run.id,
            DecisionRunJob.company_id == company.company_id,
            DecisionRunJob.outcome == "eligible",
        )).scalars()
        for run_job in run_jobs:
            session.add(DecisionRunSelectedJob(
                run_id=run.id, company_id=company.company_id,
                posting_version_id=run_job.posting_version_id,
                company_order=order,
                selection_kind="fresh_outreach" if is_fresh else "application_only",
            ))
    session.flush()
    if legacy:
        run.state, run.approved_at = "approved", datetime.now(timezone.utc)
    else:
        complete_approval_gate(
            session, run.id, available_company_count=len(companies),
            selected_company_count=len(chosen),
            selected_job_count=len(session.execute(select(DecisionRunSelectedJob).where(
                DecisionRunSelectedJob.run_id == run.id,
            )).scalars().all()),
            now=datetime.now(timezone.utc),
        )
    return load_approved_scope(session, run.id)


def approve_decision_run_from_workbook(
    session: Session, run_id: str, company_approvals: dict[int, bool],
    contact_search_selections: dict[int, str], *,
    approver: str | None = None, input_received: bool = False,
):
    """Approve the exact companies marked Approved in a returned workbook."""
    run = session.get(DecisionRun, run_id)
    if run is None:
        raise ValueError("approval must name an existing run")
    if run.state in ("approved", "processing", "completed"):
        return load_approved_scope(session, run_id)
    if run.state != "awaiting_approval":
        raise ValueError("approval must name the active awaiting-approval run")
    legacy = is_legacy_telemetry(run.telemetry_version)
    if not legacy and not input_received:
        approval_input_received(session, run_id)
    try:
        companies = session.execute(select(DecisionRunCompany).where(
            DecisionRunCompany.run_id == run_id,
        ).order_by(DecisionRunCompany.group_number, DecisionRunCompany.rank)).scalars().all()
        _validate_contact_search_selections(contact_search_selections, companies)
        _validate_company_approvals(company_approvals, companies)
        chosen = [company for company in companies
                  if company_approvals[company.company_id]]
        if not chosen:
            raise ValueError("approval workbook must mark at least one company Approved")
        fresh_chosen = sum(
            company.outreach_success_count < run.policy_snapshot["fresh_outreach_threshold"]
            for company in chosen
        )
        if fresh_chosen > run.target_count:
            raise ValueError(
                f"approval workbook selects {fresh_chosen} fresh companies; maximum is {run.target_count}"
            )
    except Exception:
        if not legacy:
            approval_input_rejected(
                session, run_id, reason="email workbook approval rejected during validation",
            )
        raise
    normalized_rules = {
        "selection_mode": "workbook_exact",
        "selected_company_ids": [company.company_id for company in chosen],
        "company_approvals": {
            str(company_id): approved
            for company_id, approved in company_approvals.items()
        },
        "contact_search_selections": {
            str(company_id): selection
            for company_id, selection in contact_search_selections.items()
        },
    }
    return _commit_approved_scope(
        session, run, companies, chosen,
        original_text=f"email workbook exact approval: {len(chosen)} companies",
        normalized_rules=normalized_rules, target_count=fresh_chosen,
        approver=approver, legacy=legacy,
    )


def _validate_company_approvals(
    approvals: dict[int, bool], companies: list[DecisionRunCompany],
) -> None:
    allowed = {company.company_id for company in companies}
    unknown = set(approvals) - allowed
    if unknown:
        raise ValueError(f"company approval names companies outside this run: {sorted(unknown)}")
    missing = allowed - set(approvals)
    if missing:
        raise ValueError(f"company approval is missing companies from this run: {sorted(missing)}")
    if any(not isinstance(approved, bool) for approved in approvals.values()):
        raise ValueError("company approval decisions must be booleans")


def _validate_contact_search_selections(
    selections: dict[int, str] | None, companies: list[DecisionRunCompany],
) -> None:
    if selections is None:
        return
    from app.decision_runs.workbook_approval import groups_for_selection
    allowed = {company.company_id for company in companies}
    unknown = set(selections) - allowed
    if unknown:
        raise ValueError(f"contact search selection names companies outside this run: {sorted(unknown)}")
    missing = allowed - set(selections)
    if missing:
        raise ValueError(f"contact search selection is missing companies from this run: {sorted(missing)}")
    for selection in selections.values():
        groups_for_selection(selection)


def load_approved_scope(session: Session, run_id: str) -> ApprovedScope:
    rows = session.execute(select(DecisionRunSelectedJob, JobPostingVersion).join(JobPostingVersion, DecisionRunSelectedJob.posting_version_id == JobPostingVersion.id).where(DecisionRunSelectedJob.run_id == run_id).order_by(DecisionRunSelectedJob.company_order)).all()
    return ApprovedScope(run_id, tuple(dict.fromkeys(r[0].company_id for r in rows)), tuple(r[1].job_id for r in rows), tuple(r[0].posting_version_id for r in rows))
