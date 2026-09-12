from datetime import datetime, timedelta, timezone

import pytest
import pandas as pd
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.decision_runs.lifecycle import (
    approval_artifact_ready,
    approval_google_spreadsheet,
    approval_input_received,
    await_named_approval,
    begin_approval_preparation,
    reconcile_attended_run,
    record_named_approval,
    replace_approval_artifact_delivery,
)
from app.decision_runs.progress import (
    FailureDisposition,
    FailedRecordDisposition,
    PREDECESSORS,
    StageCounts,
    StageName,
    StageRecord,
    StageTransitionError,
    complete_with_errors_stage,
    fail_stage,
    finish_stage,
    interrupt_stage,
    read_run_progress,
    start_stage,
    skip_stage,
)
from app.models.orm import (
    Base, DecisionRun, DecisionRunCompany, DecisionRunJob, DecisionRunStage,
    JobPostingVersion,
)
from app.dashboard.stage_tracker import render_stage_tracker
from app.dashboard.queries import decision_run_progress
from app.decision_runs import approve_decision_run, create_decision_run
from app.decision_runs import service as decision_service
from app.models.schemas import NormalizedJob
from app.pipeline.upsert import upsert_job


def session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, future=True)
    result = maker()
    result.add(DecisionRun(
        id="run-1", since_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
        cutoff_at=datetime(2026, 8, 2, tzinfo=timezone.utc), input_timezone="UTC",
        state="preparing", policy_snapshot={}, target_count=3,
    ))
    result.flush()
    return result


def complete(s, name, run_id="run-1"):
    stage_name = StageName(name)
    for predecessor in PREDECESSORS.get(stage_name, ()):
        row = s.execute(select(DecisionRunStage).where(
            DecisionRunStage.run_id == run_id,
            DecisionRunStage.name == predecessor.value,
        )).scalar_one_or_none()
        if row is None:
            complete(s, predecessor.value, run_id)
    start_stage(s, run_id, name, expected_count=0, reason=f"starting {name}")
    finish_stage(s, run_id, name)


def await_fixture_approval(s, run, job):
    version = s.execute(select(JobPostingVersion).where(
        JobPostingVersion.job_id == job.id,
    )).scalar_one()
    run_job = DecisionRunJob(
        run_id=run.id, posting_version_id=version.id, job_id=job.id,
        company_id=job.company_id, outcome="eligible", group_number=1,
        snapshot={"title": job.title, "company_name": job.company_name_raw},
    )
    s.add(run_job)
    s.flush()
    s.add(DecisionRunCompany(
        run_id=run.id, company_id=job.company_id, group_number=1, rank=1,
        primary_run_job_id=run_job.id, company_type="employer",
        outreach_success_count=0,
    ))
    complete(s, "screening_ranking_grouping", run.id)
    await_named_approval(s, run.id, available_company_count=1)


EXPECTED_PREDECESSORS = {
    StageName.COLLECTION_DEDUPLICATION: (StageName.FETCH_JOBS,),
    StageName.RELEVANCE_STORAGE: (StageName.COLLECTION_DEDUPLICATION,),
    StageName.JOB_DEDUPLICATION: (StageName.RELEVANCE_STORAGE,),
    StageName.COMPANY_DEDUPLICATION: (StageName.JOB_DEDUPLICATION,),
    StageName.JOB_ENRICHMENT: (StageName.JOB_DEDUPLICATION,),
    StageName.COMPANY_PHASE_A: (StageName.COMPANY_DEDUPLICATION,),
    StageName.COMPANY_PHASE_B: (StageName.COMPANY_DEDUPLICATION,),
    StageName.SCREENING_RANKING_GROUPING: (
        StageName.JOB_ENRICHMENT, StageName.COMPANY_PHASE_B,
    ),
    StageName.APPROVAL_GATE: (StageName.SCREENING_RANKING_GROUPING,),
    StageName.RESUME_TAILORING: (StageName.APPROVAL_GATE,),
    StageName.CONTACT_ENRICHMENT: (
        StageName.APPROVAL_GATE, StageName.COMPANY_PHASE_A,
    ),
    StageName.DRAFT_PREPARATION: (
        StageName.RESUME_TAILORING, StageName.CONTACT_ENRICHMENT,
    ),
    StageName.GMAIL_DRAFT_POSTING: (StageName.DRAFT_PREPARATION,),
    StageName.FINAL_REPORT: (StageName.GMAIL_DRAFT_POSTING,),
}


def _complete_ancestors(s, stage: StageName) -> None:
    for predecessor in PREDECESSORS.get(stage, ()):
        row = s.execute(select(DecisionRunStage).where(
            DecisionRunStage.run_id == "run-1",
            DecisionRunStage.name == predecessor.value,
        )).scalar_one_or_none()
        if row is None:
            complete(s, predecessor.value)


def _put_predecessor_in_state(s, predecessor: StageName, state: str) -> None:
    _complete_ancestors(s, predecessor)
    if state == "pending":
        return
    if state == "skipped":
        skip_stage(s, "run-1", predecessor, reason="fixture skip")
        return
    expected = 1 if state == "completed_with_errors" else 0
    start_stage(s, "run-1", predecessor, expected_count=expected, reason="fixture work")
    if state == "running":
        return
    if state == "interrupted":
        interrupt_stage(s, "run-1", predecessor, reason="fixture interrupted")
    elif state == "failed":
        fail_stage(s, "run-1", predecessor, reason="fixture failed")
    elif state == "completed":
        finish_stage(s, "run-1", predecessor)
    elif state == "completed_with_errors":
        complete_with_errors_stage(
            s, "run-1", predecessor,
            counts=StageCounts(1, 0, 0, 1, 0),
            reason_counts={"fixture_failure": 1},
            records=(StageRecord("fixture", predecessor.value, "failed", "fixture_failure"),),
            failure_dispositions=(FailedRecordDisposition(
                "fixture", predecessor.value, FailureDisposition.NON_BLOCKING,
            ),),
            reason="fixture non-blocking failure",
        )


EDGES = tuple(
    (successor, predecessor)
    for successor, predecessors in EXPECTED_PREDECESSORS.items()
    for predecessor in predecessors
)


def test_canonical_predecessor_graph_is_complete_and_preserves_parallel_frontiers():
    assert PREDECESSORS == EXPECTED_PREDECESSORS


@pytest.mark.parametrize(("successor", "predecessor"), EDGES)
@pytest.mark.parametrize("state", ("pending", "running", "interrupted", "failed"))
def test_every_canonical_edge_blocks_unfinished_or_failed_predecessor(successor, predecessor, state):
    s = session()
    for other in EXPECTED_PREDECESSORS[successor]:
        if other != predecessor:
            complete(s, other.value)
    _put_predecessor_in_state(s, predecessor, state)
    with pytest.raises(StageTransitionError, match=predecessor.value):
        start_stage(s, "run-1", successor, expected_count=0, reason="fixture successor")


@pytest.mark.parametrize(("successor", "predecessor"), EDGES)
@pytest.mark.parametrize("state", ("completed", "skipped", "completed_with_errors"))
def test_every_canonical_edge_accepts_satisfied_predecessor(successor, predecessor, state):
    s = session()
    for other in EXPECTED_PREDECESSORS[successor]:
        if other != predecessor:
            complete(s, other.value)
    _put_predecessor_in_state(s, predecessor, state)
    start_stage(s, "run-1", successor, expected_count=0, reason="fixture successor")


def test_approval_wait_and_selection_counts_are_durable_and_separate_from_active_time():
    s = session()
    now = datetime(2026, 8, 25, 10, tzinfo=timezone.utc)
    complete(s, "screening_ranking_grouping")
    await_named_approval(s, "run-1", available_company_count=3, now=now)
    assert s.get(DecisionRun, "run-1").state == "awaiting_approval"

    waiting = next(stage for stage in read_run_progress(
        s, "run-1", now=now + timedelta(minutes=7), project_interruptions=False,
    ).stages if stage.name == "approval_gate")
    assert waiting.status == "waiting_for_approval"
    assert waiting.waiting_started_at == now
    assert waiting.waiting_seconds == 7 * 60
    assert waiting.active_elapsed_seconds == 0

    record_named_approval(
        s, "run-1", available_company_count=3, selected_company_count=2,
        selected_job_count=5, now=now + timedelta(minutes=10),
    )
    assert s.get(DecisionRun, "run-1").state == "approved"
    approved = next(stage for stage in read_run_progress(
        s, "run-1", now=now + timedelta(minutes=12), project_interruptions=False,
    ).stages if stage.name == "approval_gate")
    assert (approved.status, approved.counts) == ("completed", StageCounts(1, 1, 0, 0, 0))
    assert approved.waiting_seconds == 10 * 60
    assert approved.selected_company_count == 2
    assert approved.selected_job_count == 5
    read_model = decision_run_progress(s, "run-1", now=now + timedelta(minutes=12))
    read_model = read_model.loc[read_model["stage"] == "approval_gate"].iloc[0]
    assert (read_model["selected_companies"], read_model["available_companies"], read_model["selected_jobs"]) == (2, 3, 5)
    assert [(attempt.attempt_number, attempt.status) for attempt in approved.attempts] == [
        (1, "waiting_for_approval"), (2, "completed"),
    ]


def test_approval_timing_switches_at_artifact_ready_and_input_received_boundaries():
    s = session()
    now = datetime(2026, 8, 25, 10, tzinfo=timezone.utc)
    complete(s, "screening_ranking_grouping")
    begin_approval_preparation(s, "run-1", now=now)
    approval_artifact_ready(
        s, "run-1", available_company_count=3,
        google_spreadsheet_id="sheet-42",
        google_spreadsheet_url="https://docs.google.com/spreadsheets/d/sheet-42/edit",
        now=now + timedelta(minutes=3),
    )

    waiting = next(stage for stage in read_run_progress(
        s, "run-1", now=now + timedelta(minutes=13), project_interruptions=False,
    ).stages if stage.name == "approval_gate")
    assert waiting.active_elapsed_seconds == 3 * 60
    assert waiting.waiting_seconds == 10 * 60
    assert approval_google_spreadsheet(s, "run-1") == (
        "sheet-42", "https://docs.google.com/spreadsheets/d/sheet-42/edit",
    )
    replace_approval_artifact_delivery(
        s, "run-1", gmail_message_id="message-2", gmail_thread_id="thread-2",
        google_spreadsheet_id="sheet-43",
        google_spreadsheet_url="https://docs.google.com/spreadsheets/d/sheet-43/edit",
    )
    assert approval_google_spreadsheet(s, "run-1") == (
        "sheet-43", "https://docs.google.com/spreadsheets/d/sheet-43/edit",
    )

    approval_input_received(s, "run-1", now=now + timedelta(minutes=13))
    processing = next(stage for stage in read_run_progress(
        s, "run-1", now=now + timedelta(minutes=15), project_interruptions=False,
    ).stages if stage.name == "approval_gate")
    assert processing.status == "running"
    assert processing.active_elapsed_seconds == 5 * 60
    assert processing.waiting_seconds == 10 * 60


def test_typed_predecessor_gates_allow_job_and_company_phases_to_run_independently():
    s = session()
    with pytest.raises(StageTransitionError, match="blocked by predecessor"):
        start_stage(s, "run-1", "job_enrichment", expected_count=0, reason="enriching jobs")

    complete(s, "fetch_jobs")
    complete(s, "relevance_storage")
    complete(s, "job_deduplication")
    complete(s, "company_deduplication")

    start_stage(s, "run-1", "job_enrichment", expected_count=0, reason="enriching jobs")
    start_stage(s, "run-1", "company_phase_a", expected_count=0, reason="enriching companies")
    start_stage(s, "run-1", "company_phase_b", expected_count=0, reason="collecting market evidence")
    states = {stage.name: stage.status for stage in read_run_progress(s, "run-1", project_interruptions=False).stages}
    assert states["job_enrichment"] == states["company_phase_a"] == states["company_phase_b"] == "running"


def test_reasoned_skipped_predecessor_is_finished_and_unlocks_its_successor():
    s = session()
    complete(s, "fetch_jobs")
    skip_stage(s, "run-1", "collection_deduplication", reason="no duplicate candidates")
    start_stage(s, "run-1", "relevance_storage", expected_count=0, reason="no fetched jobs to store")
    assert {stage.name: stage.status for stage in read_run_progress(
        s, "run-1", project_interruptions=False,
    ).stages}["relevance_storage"] == "running"


def test_completed_with_errors_needs_explicit_non_blocking_or_excluded_failure_reasons_before_unlocking_successor():
    s = session()
    complete(s, "fetch_jobs")
    complete(s, "relevance_storage")
    complete(s, "job_deduplication")

    start_stage(s, "run-1", "company_deduplication", expected_count=1, reason="deduplicating companies")
    complete_with_errors_stage(
        s, "run-1", "company_deduplication", counts=StageCounts(1, 0, 0, 1, 0),
        reason_counts={"unresolved_duplicate": 1}, reason="one duplicate could not be resolved",
    )
    with pytest.raises(StageTransitionError, match="non-blocking or excluded"):
        start_stage(s, "run-1", "company_phase_a", expected_count=0, reason="enriching companies")

    # A fresh run marks the failed item as intentionally excluded, which safely opens the successor.
    s = session()
    complete(s, "fetch_jobs")
    complete(s, "relevance_storage")
    complete(s, "job_deduplication")
    start_stage(s, "run-1", "company_deduplication", expected_count=1, reason="deduplicating companies")
    complete_with_errors_stage(
        s, "run-1", "company_deduplication", counts=StageCounts(1, 0, 0, 1, 0),
        reason_counts={"unresolved_duplicate": 1},
        failure_dispositions=(FailedRecordDisposition(
            "company", "duplicate-1", FailureDisposition.EXCLUDED,
        ),),
        records=(StageRecord("company", "duplicate-1", "failed", "unresolved_duplicate"),),
        reason="one duplicate was explicitly excluded",
    )
    start_stage(s, "run-1", "company_phase_a", expected_count=0, reason="enriching companies")


def test_completed_with_errors_dispositions_are_per_failed_record_not_per_reason():
    def failed_stage(s):
        complete(s, "fetch_jobs")
        complete(s, "relevance_storage")
        complete(s, "job_deduplication")
        start_stage(s, "run-1", "company_deduplication", expected_count=3, reason="deduplicating")

    records = (
        StageRecord("company", "one", "failed", "same_reason"),
        StageRecord("company", "two", "failed", "same_reason"),
        StageRecord("company", "three", "dropped", "known_duplicate"),
    )
    s = session(); failed_stage(s)
    complete_with_errors_stage(
        s, "run-1", "company_deduplication", counts=StageCounts(3, 0, 1, 2, 0),
        reason_counts={"same_reason": 2, "known_duplicate": 1}, records=records,
        failure_dispositions=(FailedRecordDisposition(
            "company", "one", FailureDisposition.NON_BLOCKING,
        ),), reason="one failed item remains blocking",
    )
    with pytest.raises(StageTransitionError, match="non-blocking or excluded"):
        start_stage(s, "run-1", "company_phase_a", expected_count=0, reason="enriching")

    s = session(); failed_stage(s)
    complete_with_errors_stage(
        s, "run-1", "company_deduplication", counts=StageCounts(3, 0, 1, 2, 0),
        reason_counts={"same_reason": 2, "known_duplicate": 1}, records=records,
        failure_dispositions=(
            FailedRecordDisposition("company", "one", FailureDisposition.NON_BLOCKING),
            FailedRecordDisposition("company", "two", FailureDisposition.EXCLUDED),
        ), reason="both failed items explicitly disposed",
    )
    start_stage(s, "run-1", "company_phase_a", expected_count=0, reason="enriching")
    completed = next(stage for stage in read_run_progress(
        s, "run-1", project_interruptions=False,
    ).stages if stage.name == "company_deduplication")
    assert [(item.record_id, item.disposition) for item in completed.failed_record_dispositions] == [
        ("one", FailureDisposition.NON_BLOCKING),
        ("two", FailureDisposition.EXCLUDED),
    ]


def test_attended_reconciliation_durably_interrupts_then_resumes_same_run_with_attempt_history():
    s = session()
    now = datetime(2026, 8, 25, 10, tzinfo=timezone.utc)
    start_stage(s, "run-1", "fetch_jobs", expected_count=2, process_id=9001,
                reason="collecting", now=now - timedelta(minutes=8))
    assert reconcile_attended_run(
        s, "run-1", now=now, stale_after=timedelta(minutes=5), process_is_alive=lambda _: False,
    ) == 1
    start_stage(s, "run-1", "fetch_jobs", expected_count=2, process_id=9002,
                reason="resuming", now=now + timedelta(minutes=1))
    stage = read_run_progress(s, "run-1", project_interruptions=False).stages[0]
    assert [(attempt.attempt_number, attempt.status) for attempt in stage.attempts] == [
        (1, "interrupted"), (2, "running"),
    ]


def test_stage_tracker_presents_attempt_history_read_only_in_expanders():
    calls = []
    class Expander:
        def __enter__(self): return self
        def __exit__(self, *_): return False
    class Renderer:
        def dataframe(self, table, **kwargs): calls.append(("dataframe", table, kwargs))
        def expander(self, label): calls.append(("expander", label)); return Expander()
    render_stage_tracker(Renderer(), pd.DataFrame([{
        "stage": "approval_gate", "status": "completed", "processed": 2, "expected": 3,
        "advanced": 2, "dropped": 1, "failed": 0, "pending": 0, "attempt": 2,
        "reason": "named approval recorded", "active_elapsed_seconds": 3,
        "waiting_seconds": 600, "selected_companies": 2, "available_companies": 3,
        "selected_jobs": 5, "updated_at": datetime(2026, 8, 25, 10, tzinfo=timezone.utc),
        "attempt_history": [{"attempt": 1, "status": "waiting_for_approval"},
                            {"attempt": 2, "status": "completed"}],
    }]))
    assert calls[0][0] == "dataframe"
    summary = calls[0][1]
    assert summary.iloc[0]["Selected companies"] == "2 / 3"
    assert summary.iloc[0]["Selected jobs"] == 5
    assert summary.iloc[0]["Last update"] == "2026-08-25T10:00:00+00:00"
    assert calls[1] == ("expander", "Approval Gate — 2 attempt(s)")
    assert calls[2][0] == "dataframe"


def test_tracker_keeps_failed_interrupted_and_skipped_reasons_visible():
    calls = []
    class Expander:
        def __enter__(self): return self
        def __exit__(self, *_): return False
    class Renderer:
        def dataframe(self, table, **kwargs): calls.append(table)
        def expander(self, _label): return Expander()
    rows = []
    for status, reason in (
        ("failed", "source unavailable"),
        ("interrupted", "process_missing_or_heartbeat_stale"),
        ("skipped", "not applicable"),
    ):
        rows.append({
            "stage": status, "status": status, "processed": 0, "expected": 0,
            "advanced": 0, "dropped": 0, "failed": 0, "pending": 0, "attempt": 1,
            "reason": reason, "active_elapsed_seconds": 0, "waiting_seconds": 0,
            "updated_at": datetime(2026, 8, 25, 10, tzinfo=timezone.utc),
            "selected_companies": None, "available_companies": None, "selected_jobs": None,
            "attempt_history": [],
        })
    render_stage_tracker(Renderer(), pd.DataFrame(rows))
    assert calls[0]["Result"].tolist() == [
        "source unavailable", "process_missing_or_heartbeat_stale", "not applicable",
    ]


def test_read_model_keeps_failed_interrupted_and_skipped_reasons_visible():
    s = session()
    complete(s, "approval_gate")
    start_stage(s, "run-1", "resume_tailoring", expected_count=0, reason="tailoring")
    interrupt_stage(s, "run-1", "resume_tailoring", reason="operator stopped process")
    complete(s, "company_phase_a")
    start_stage(s, "run-1", "contact_enrichment", expected_count=0, reason="finding contacts")
    fail_stage(s, "run-1", "contact_enrichment", reason="contact source unavailable")
    skip_stage(s, "run-1", "final_report", reason="no approved scope")

    rows = decision_run_progress(s, "run-1").set_index("status")
    assert rows.loc["interrupted", "reason"] == "operator stopped process"
    assert rows.loc["failed", "reason"] == "contact source unavailable"
    assert rows.loc["skipped", "reason"] == "no approved scope"


def test_decision_preparation_and_named_approval_drive_the_approval_lifecycle():
    s = session()
    posted_at = datetime(2026, 8, 2, tzinfo=timezone.utc)
    job = upsert_job(s, NormalizedJob(
        source="fixture", external_job_id="job-1", title="Data Engineer", company_name_raw="Acme",
        description_raw="work", posted_at=posted_at, is_remote=True,
    ))
    run = create_decision_run(s, since=(posted_at - timedelta(days=1)).isoformat(), cutoff=posted_at)
    await_fixture_approval(s, run, job)
    approval = next(stage for stage in read_run_progress(
        s, run.id, project_interruptions=False,
    ).stages if stage.name == "approval_gate")
    assert approval.status == "waiting_for_approval"

    scope = approve_decision_run(s, run.id, "target 1; G1 all; G2 0; G3 0; G4 0")
    stage = next(stage for stage in read_run_progress(
        s, run.id, project_interruptions=False,
    ).stages if stage.name == "approval_gate")
    assert (stage.status, stage.selected_company_count, stage.selected_job_count) == (
        "completed", len(scope.company_ids), len(scope.job_ids),
    )


def test_approval_service_stops_waiting_before_parsing_and_completes_after_persistence(monkeypatch):
    s = session()
    posted_at = datetime(2026, 8, 2, tzinfo=timezone.utc)
    job = upsert_job(s, NormalizedJob(
        source="fixture", external_job_id="job-order", title="Data Engineer",
        company_name_raw="Ordering Co", description_raw="work",
        posted_at=posted_at, is_remote=True,
    ))
    run = create_decision_run(s, since=(posted_at - timedelta(days=1)).isoformat(), cutoff=posted_at)
    await_fixture_approval(s, run, job)
    order = []
    real_received = decision_service.approval_input_received
    real_parse = decision_service.parse_approval
    real_complete = decision_service.complete_approval_gate
    monkeypatch.setattr(decision_service, "approval_input_received", lambda *args, **kwargs: order.append("input_received") or real_received(*args, **kwargs))
    monkeypatch.setattr(decision_service, "parse_approval", lambda *args, **kwargs: order.append("parse") or real_parse(*args, **kwargs))
    monkeypatch.setattr(decision_service, "complete_approval_gate", lambda *args, **kwargs: order.append("persist_complete") or real_complete(*args, **kwargs))

    approve_decision_run(s, run.id, "target 1; G1 all; G2 0; G3 0; G4 0")
    assert order == ["input_received", "parse", "persist_complete"]


def test_rejected_approval_input_returns_to_waiting_with_a_reason():
    s = session()
    now = datetime(2026, 8, 2, tzinfo=timezone.utc)
    job = upsert_job(s, NormalizedJob(
        source="fixture", external_job_id="bad-approval", title="Data Engineer",
        company_name_raw="Retry Co", description_raw="work", posted_at=now, is_remote=True,
    ))
    run = create_decision_run(s, since=(now - timedelta(days=1)).isoformat(), cutoff=now)
    await_fixture_approval(s, run, job)
    with pytest.raises(ValueError, match="approval must begin"):
        approve_decision_run(s, run.id, "not an approval")
    stage = next(stage for stage in read_run_progress(
        s, run.id, project_interruptions=False,
    ).stages if stage.name == "approval_gate")
    assert stage.status == "waiting_for_approval"
    assert stage.reason == "approval input rejected during validation"
