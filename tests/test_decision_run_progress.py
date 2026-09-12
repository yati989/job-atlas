from datetime import datetime, timedelta, timezone

import pytest
import pandas as pd
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.orm import Base, DecisionRun, DecisionRunStage
from app.decision_runs.progress import (
    StageName,
    StageCounts,
    StageRecord,
    StageTransitionError,
    complete_with_errors_stage,
    declare_pending_stage,
    finish_stage,
    reconcile_stale_stages,
    read_run_progress,
    skip_stage,
    start_stage,
    update_stage,
    wait_for_approval,
)
from app.dashboard.queries import (
    decision_run_options,
    decision_run_progress,
    default_decision_run_id,
)
from app.dashboard.stage_tracker import render_stage_tracker
from app.decision_runs.progress_table import stage_summary_table
from tests.progress_support import complete_screening


def session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, future=True)
    result = maker()
    result.add(DecisionRun(id="run-1", since_at=datetime(2026, 8, 1, tzinfo=timezone.utc), cutoff_at=datetime(2026, 8, 2, tzinfo=timezone.utc), input_timezone="UTC", state="preparing", policy_snapshot={}, target_count=1))
    result.flush()
    return result


def test_fetch_stage_is_durable_and_reconciles_counts_through_public_interface():
    s = session()
    start_stage(s, "run-1", "fetch_jobs", expected_count=3, process_id=42,
                reason="collecting connector instances")
    update_stage(
        s, "run-1", "fetch_jobs",
        counts=StageCounts(3, 1, 1, 0, 1),
        reason_counts={"duplicate": 1},
        records=(
            StageRecord("connector", "one", "advanced"),
            StageRecord("connector", "two", "dropped", "duplicate"),
        ),
    )
    finish_stage(
        s, "run-1", "fetch_jobs",
        counts=StageCounts(3, 2, 1, 0, 0),
        reason_counts={"duplicate": 1},
        records=(
            StageRecord("connector", "one", "advanced"),
            StageRecord("connector", "two", "dropped", "duplicate"),
            StageRecord("connector", "three", "advanced"),
        ),
    )

    progress = read_run_progress(s, "run-1")
    stage = progress.stages[0]
    assert (stage.name, stage.status, stage.processed_count, stage.expected_count, stage.attempt_number) == ("fetch_jobs", "completed", 3, 3, 1)
    assert stage.counts == StageCounts(3, 2, 1, 0, 0)
    assert stage.reason_counts == {"duplicate": 1}
    assert [record.record_id for record in stage.attempts[0].records] == ["one", "two", "three"]
    assert [transition.to_status for transition in stage.transitions] == ["running", "completed"]


def test_declared_pending_stage_is_durable_before_its_predecessors_complete():
    s = session()

    declare_pending_stage(s, "run-1", "screening_ranking_grouping", expected_count=3)
    declare_pending_stage(s, "run-1", "screening_ranking_grouping", expected_count=3)

    with pytest.raises(StageTransitionError, match="blocked by predecessor"):
        start_stage(
            s, "run-1", "screening_ranking_grouping", expected_count=3,
            reason="ranking screened jobs",
        )

    stage = read_run_progress(s, "run-1", project_interruptions=False).stages[0]
    assert (stage.name, stage.status, stage.counts) == (
        "screening_ranking_grouping", "pending", StageCounts(3, 0, 0, 0, 3),
    )
    assert stage.attempt_number == 0


def test_progress_rejects_invalid_transitions_and_unreconciled_counts():
    s = session()
    start_stage(s, "run-1", "fetch_jobs", expected_count=2, reason="fetching")
    with pytest.raises(StageTransitionError, match="input = advanced"):
        update_stage(s, "run-1", "fetch_jobs", counts=StageCounts(2, 2, 0, 0, 1))
    with pytest.raises(StageTransitionError, match="reason counts"):
        update_stage(s, "run-1", "fetch_jobs", counts=StageCounts(2, 1, 1, 0, 0))
    with pytest.raises(StageTransitionError, match="cannot have failed or pending"):
        finish_stage(s, "run-1", "fetch_jobs", counts=StageCounts(2, 1, 0, 0, 1))
    finish_stage(s, "run-1", "fetch_jobs", counts=StageCounts(2, 2, 0, 0, 0))
    with pytest.raises(StageTransitionError, match="cannot start"):
        start_stage(s, "run-1", "fetch_jobs", expected_count=2, reason="fetching")


def test_status_vocabulary_and_reason_invariants_are_central():
    s = session()
    with pytest.raises(StageTransitionError, match="requires a reason"):
        start_stage(s, "run-1", "fetch_jobs", expected_count=1, reason="")
    with pytest.raises(StageTransitionError, match="unknown Decision Run stage"):
        skip_stage(s, "run-1", "invented_stage", reason="not applicable")

    complete_screening(s, "run-1")
    start_stage(s, "run-1", "approval_gate", expected_count=1, reason="building approval artifact")
    with pytest.raises(StageTransitionError, match="requires a reason"):
        wait_for_approval(s, "run-1", "approval_gate", reason="")
    wait_for_approval(s, "run-1", "approval_gate", reason="awaiting named approval")
    approval = next(stage for stage in read_run_progress(
        s, "run-1", project_interruptions=False,
    ).stages if stage.name == "approval_gate")
    assert approval.status == "waiting_for_approval"


def test_completed_with_errors_requires_failures_and_no_pending_records():
    s = session()
    start_stage(s, "run-1", "fetch_jobs", expected_count=2, reason="fetching")
    with pytest.raises(StageTransitionError, match="requires failed"):
        complete_with_errors_stage(
            s, "run-1", "fetch_jobs", counts=StageCounts(2, 2, 0, 0, 0),
            reason="one connector failed",
        )
    complete_with_errors_stage(
        s, "run-1", "fetch_jobs", counts=StageCounts(2, 1, 0, 1, 0),
        reason_counts={"timeout": 1},
        records=(StageRecord("connector", "one", "advanced"),
                 StageRecord("connector", "two", "failed", "timeout")),
        reason="one connector failed",
    )
    assert read_run_progress(s, "run-1").stages[0].status == "completed_with_errors"


def test_stale_or_dead_running_stage_is_interrupted_without_losing_counts():
    s = session()
    now = datetime(2026, 8, 25, 10, tzinfo=timezone.utc)
    start_stage(s, "run-1", "fetch_jobs", expected_count=4, process_id=999999,
                reason="fetching", now=now - timedelta(minutes=11))
    update_stage(s, "run-1", "fetch_jobs", counts=StageCounts(4, 2, 0, 0, 2),
                 now=now - timedelta(minutes=10))

    progress = read_run_progress(s, "run-1", now=now, stale_after=timedelta(minutes=5), process_is_alive=lambda _: False)
    stage = progress.stages[0]
    assert (stage.status, stage.processed_count, stage.expected_count) == ("interrupted", 2, 4)
    assert stage.reason == "process_missing_or_heartbeat_stale"
    assert stage.projected_interruption is True
    assert s.query(DecisionRunStage).filter_by(run_id="run-1").one().status == "running"

    assert reconcile_stale_stages(
        s, "run-1", now=now, stale_after=timedelta(minutes=5),
        process_is_alive=lambda _: False,
    ) == 1
    assert s.query(DecisionRunStage).filter_by(run_id="run-1").one().status == "interrupted"


def test_process_untracked_stage_uses_heartbeat_not_missing_pid_for_staleness():
    s = session()
    started = datetime(2026, 8, 25, 10, tzinfo=timezone.utc)
    start_stage(
        s, "run-1", "fetch_jobs", expected_count=1,
        process_untracked=True, reason="agent worker dispatched", now=started,
    )

    fresh = read_run_progress(
        s, "run-1", now=started + timedelta(minutes=1),
        process_is_alive=lambda _: False,
    ).stages[0]
    assert (fresh.status, fresh.projected_interruption, fresh.attempts[-1].process_id) == (
        "running", False, None,
    )
    assert reconcile_stale_stages(
        s, "run-1", now=started + timedelta(minutes=1),
        process_is_alive=lambda _: False,
    ) == 0

    stale = read_run_progress(
        s, "run-1", now=started + timedelta(minutes=6),
        process_is_alive=lambda _: True,
    ).stages[0]
    assert (stale.status, stale.projected_interruption) == ("interrupted", True)
    assert reconcile_stale_stages(
        s, "run-1", now=started + timedelta(minutes=6),
        process_is_alive=lambda _: True,
    ) == 1


def test_interrupted_stage_resume_keeps_recorded_counts_and_starts_next_attempt():
    s = session()
    start_stage(s, "run-1", "fetch_jobs", expected_count=4, process_id=41, reason="fetching")
    update_stage(s, "run-1", "fetch_jobs", counts=StageCounts(4, 2, 0, 0, 2))
    reconcile_stale_stages(s, "run-1", process_is_alive=lambda _: False)
    progress = read_run_progress(s, "run-1", project_interruptions=False)
    assert progress.stages[0].processed_count == 2

    start_stage(s, "run-1", "fetch_jobs", expected_count=4, process_id=42, reason="resuming fetch")
    resumed = read_run_progress(s, "run-1", process_is_alive=lambda _: True)
    assert (resumed.stages[0].processed_count, resumed.stages[0].attempt_number) == (2, 2)
    assert [(attempt.attempt_number, attempt.status, attempt.counts.processed) for attempt in resumed.stages[0].attempts] == [
        (1, "interrupted", 2), (2, "running", 2),
    ]


def test_dashboard_read_model_selects_historical_runs_and_labels_legacy_telemetry():
    s = session()
    older = DecisionRun(id="old-run", since_at=datetime(2026, 7, 1, tzinfo=timezone.utc), cutoff_at=datetime(2026, 7, 2, tzinfo=timezone.utc), input_timezone="UTC", state="completed", policy_snapshot={}, target_count=1)
    s.add(older); s.flush()
    start_stage(s, "run-1", "fetch_jobs", expected_count=1, reason="fetching")
    update_stage(s, "run-1", "fetch_jobs", counts=StageCounts(1, 1, 0, 0, 0))
    finish_stage(s, "run-1", "fetch_jobs")

    assert set(decision_run_options(s)["run_id"]) == {"run-1", "old-run"}
    legacy_option = decision_run_options(s).set_index("run_id").loc["old-run"]
    assert legacy_option["since_at"] == older.since_at
    assert legacy_option["cutoff_at"] == older.cutoff_at
    assert legacy_option["started_at"] == older.created_at
    assert decision_run_progress(s, "run-1").iloc[0]["telemetry"] == "complete"
    assert decision_run_progress(s, "run-1").iloc[0]["elapsed_seconds"] is not None
    assert decision_run_progress(s, "old-run").iloc[0]["telemetry"] == "incomplete legacy telemetry"


def test_dashboard_projects_every_planned_stage_before_work_reaches_it():
    s = session()
    start_stage(s, "run-1", StageName.FETCH_JOBS, expected_count=2, reason="fetching")

    table = decision_run_progress(s, "run-1").set_index("stage")

    assert list(table.index) == [stage.value for stage in StageName]
    assert table.loc[StageName.FETCH_JOBS.value, "status"] == "running"
    assert table.loc[StageName.JOB_ENRICHMENT.value, "status"] == "pending"
    assert table.loc[StageName.COMPANY_PHASE_B.value, "status"] == "pending"
    assert table.loc[StageName.FINAL_REPORT.value, "status"] == "pending"
    assert pd.isna(table.loc[StageName.FINAL_REPORT.value, "processed"])


def test_stage_summary_projects_completed_fetch_during_atomic_funnel_handoff():
    """Collection completion is visible while its recovery-safe handoff commits."""
    table = pd.DataFrame([{
        "stage": StageName.FETCH_JOBS.value,
        "status": "running",
        "processed": 160,
        "expected": 160,
        "advanced": 160,
        "dropped": 0,
        "failed": 0,
        "pending": 0,
        "active_elapsed_seconds": 12.0,
        "waiting_seconds": None,
        "updated_at": datetime(2026, 8, 30, tzinfo=timezone.utc),
        "selected_companies": None,
        "available_companies": None,
        "selected_jobs": None,
        "attempt": 1,
        "reason": "jobs_fetched=16829",
    }, {
        "stage": StageName.COLLECTION_DEDUPLICATION.value,
        "status": "pending",
        "processed": None,
        "expected": None,
        "advanced": None,
        "dropped": None,
        "failed": None,
        "pending": None,
        "active_elapsed_seconds": None,
        "waiting_seconds": None,
        "updated_at": None,
        "selected_companies": None,
        "available_companies": None,
        "selected_jobs": None,
        "attempt": 0,
        "reason": "waiting for preceding stage",
    }])

    summary = stage_summary_table(table).set_index("Stage")

    fetch = summary.loc["Fetch Jobs"]
    assert fetch["Status"] == "Completed"
    assert fetch["Result"] == (
        "connector collection completed; durable funnel handoff in progress"
    )
    assert summary.loc["Collection Deduplication", "Status"] == "Pending"
    assert summary.loc["Collection Deduplication", "Result"] == (
        "waiting for preceding stage"
    )


def test_dashboard_selector_defaults_to_latest_active_run_then_latest_run():
    s = session()
    s.query(DecisionRun).filter_by(id="run-1").update(
        {"state": "expired", "created_at": datetime(2026, 8, 2, tzinfo=timezone.utc)}
    )
    s.add_all([
        DecisionRun(id="completed-run", since_at=datetime(2026, 8, 2, tzinfo=timezone.utc), cutoff_at=datetime(2026, 8, 3, tzinfo=timezone.utc), input_timezone="UTC", state="completed", policy_snapshot={}, target_count=1, created_at=datetime(2026, 8, 3, tzinfo=timezone.utc)),
        DecisionRun(id="active-run", since_at=datetime(2026, 8, 1, tzinfo=timezone.utc), cutoff_at=datetime(2026, 8, 2, tzinfo=timezone.utc), input_timezone="UTC", state="preparing", policy_snapshot={}, target_count=1, created_at=datetime(2026, 8, 2, 12, tzinfo=timezone.utc)),
    ])
    s.flush()
    options = decision_run_options(s)
    assert default_decision_run_id(options) == "active-run"

    s.query(DecisionRun).filter_by(id="active-run").update({"state": "expired"})
    assert default_decision_run_id(decision_run_options(s)) == "completed-run"


def test_stage_tracker_isolated_rendering_seam():
    calls = []
    class Renderer:
        def dataframe(self, table, **kwargs): calls.append((table, kwargs))
    render_stage_tracker(Renderer(), decision_run_options(session()))
    assert calls[0][1] == {"width": "stretch", "hide_index": True}
