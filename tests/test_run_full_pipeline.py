import argparse
import json
from contextlib import nullcontext

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from scripts import run_full_pipeline
from app.models.orm import Base, DecisionRun
from app.decision_runs.progress import StageName, finish_stage, start_stage
from app.decision_runs.types import DecisionPolicy


def _completed_connector_snapshot():
    return {
        "run_id": "run", "status": "completed",
        "command": "scripts.run_full_pipeline ingest",
        "started_at": "2026-08-01T00:00:00+00:00",
        "ended_at": "2026-08-01T00:00:10+00:00",
        "sources": [{
            "source": "linkedin", "status": "completed",
            "jobs_finished": 10, "gate_fetched": 10, "kept": 4,
            "drop_role": 3, "drop_seniority": 1,
            "drop_location": 1, "drop_recency": 1,
            "instances_finished": 1, "instances_total": 1,
            "instances_succeeded": 1, "instances_failed": 0,
            "started_at": "2026-08-01T00:00:00+00:00",
            "ended_at": "2026-08-01T00:00:10+00:00",
        }],
    }


def test_decision_policy_snapshots_durable_collection_scope():
    policy = DecisionPolicy(
        collection_search="data scientist",
        collection_location="remote",
    )

    snapshot = policy.snapshot()

    assert snapshot["collection_search"] == "data scientist"
    assert snapshot["collection_location"] == "remote"
    assert DecisionPolicy(**snapshot) == policy


def test_start_scope_is_normalized_and_ingest_reuses_the_snapshot():
    policy = run_full_pipeline._decision_policy_for_start(
        argparse.Namespace(search="data science", location="remote India")
    )
    args = argparse.Namespace(
        source=None,
        one_per_source=False,
        sample_search="data scientist",
        sample_location=None,
        location_mode=None,
    )

    scope = run_full_pipeline._resolve_collection_scope(args, policy.snapshot())

    assert policy.collection_search == "data scientist"
    assert policy.collection_location == "remote"
    assert scope == {
        "source": None,
        "one_per_source": True,
        "sample_search": "data scientist",
        "sample_location": "remote",
        "location_mode": None,
    }


def test_ingest_rejects_a_cli_override_of_the_durable_scope():
    args = argparse.Namespace(
        source=None,
        one_per_source=True,
        sample_search="ai engineer",
        sample_location="remote",
        location_mode=None,
    )

    with pytest.raises(SystemExit, match="immutable collection scope"):
        run_full_pipeline._resolve_collection_scope(
            args,
            DecisionPolicy(
                collection_search="data scientist",
                collection_location="remote",
            ).snapshot(),
        )


def test_ingest_can_narrow_a_durable_instance_scope_to_one_source():
    args = argparse.Namespace(
        source="linkedin",
        one_per_source=False,
        sample_search="data scientist",
        sample_location=None,
        location_mode=None,
    )

    scope = run_full_pipeline._resolve_collection_scope(
        args,
        DecisionPolicy(
            collection_search="data scientist",
            collection_location="remote",
        ).snapshot(),
    )

    assert scope == {
        "source": "linkedin",
        "one_per_source": True,
        "sample_search": "data scientist",
        "sample_location": "remote",
        "location_mode": None,
    }


def test_connector_completion_report_is_sent_once_and_persists_receipt(
    monkeypatch, tmp_path,
):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    meta = {"run_id": "run", "connector_progress": _completed_connector_snapshot()}
    run_full_pipeline._save_meta(run_dir, meta)
    sent = []
    monkeypatch.setattr("app.outreach.gmail.get_service", lambda: object())
    monkeypatch.setattr(
        "app.outreach.gmail.send_self_report",
        lambda _service, **kwargs: sent.append(kwargs) or {"id": "fetch-mail-1"},
    )

    run_full_pipeline._send_connector_completion_report(run_dir, meta)
    run_full_pipeline._send_connector_completion_report(run_dir, meta)

    assert len(sent) == 1
    assert sent[0].get("attachment_path") is None
    assert "<th>Kept %</th>" in sent[0]["html_body"]
    assert "<th>Drop role</th>" in sent[0]["html_body"]
    assert "<td>linkedin</td>" in sent[0]["html_body"]
    stored = run_full_pipeline._load_meta(run_dir)
    assert stored["connector_completion_email"] == {
        "state": "delivered",
        "subject": "job_agent connector run completed — run",
        "receipt": "fetch-mail-1",
    }


def _terminal_run():
    session = sessionmaker(bind=create_engine("sqlite://", future=True), future=True)()
    Base.metadata.create_all(session.get_bind())
    session.add(DecisionRun(id="run", since_at=run_full_pipeline.datetime(2026, 8, 1),
        cutoff_at=run_full_pipeline.datetime(2026, 8, 2), input_timezone="UTC", state="processing",
        policy_snapshot={}, target_count=1, telemetry_version="decision-run-progress-v1"))
    session.flush()
    from app.decision_runs.progress import PREDECESSORS
    def complete(name):
        for parent in PREDECESSORS.get(StageName(name), ()):
            complete(parent)
        if not any(row.name == StageName(name).value for row in session.query(__import__('app.models.orm', fromlist=['DecisionRunStage']).DecisionRunStage).all()):
            start_stage(session, "run", name, expected_count=0, reason="fixture")
            finish_stage(session, "run", name, reason="fixture")
    complete(StageName.GMAIL_DRAFT_POSTING)
    return session


def test_final_report_pre_delivery_failure_is_failed_and_preserves_prior_stages(monkeypatch, tmp_path):
    session = _terminal_run()
    monkeypatch.setattr("app.db.session.get_session", lambda: nullcontext(session))
    monkeypatch.setattr("app.decision_runs.lifecycle.reconcile_attended_run", lambda *_: 0)
    monkeypatch.setattr("app.reporting.final_run_report.build_final_workbook", lambda *_: (_ for _ in ()).throw(RuntimeError("build failed")))
    with pytest.raises(RuntimeError, match="build failed"):
        run_full_pipeline.cmd_final_report(argparse.Namespace(run_id="run", output=str(tmp_path / "x.xlsx"), send_self_report=True))
    run = session.get(DecisionRun, "run")
    assert run.final_report_delivery_state == "failed"
    assert any(row.name == StageName.GMAIL_DRAFT_POSTING.value and row.status == "completed" for row in session.query(__import__('app.models.orm', fromlist=['DecisionRunStage']).DecisionRunStage))


def test_final_report_send_started_failure_is_indeterminate_and_refuses_auto_resend(monkeypatch, tmp_path):
    session = _terminal_run(); output = tmp_path / "x.xlsx"
    monkeypatch.setattr("app.db.session.get_session", lambda: nullcontext(session))
    monkeypatch.setattr("app.decision_runs.lifecycle.reconcile_attended_run", lambda *_: 0)
    def build(_, __, path):
        from pathlib import Path
        Path(path).write_text("workbook")
    monkeypatch.setattr("app.reporting.final_run_report.build_final_workbook", build)
    monkeypatch.setattr("app.outreach.gmail.get_service", lambda: object())
    monkeypatch.setattr("app.outreach.gmail.send_self_report", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("provider timeout")))
    args = argparse.Namespace(run_id="run", output=str(output), send_self_report=True)
    with pytest.raises(RuntimeError, match="provider timeout"):
        run_full_pipeline.cmd_final_report(args)
    assert session.get(DecisionRun, "run").final_report_delivery_state == "indeterminate"
    with pytest.raises(SystemExit, match="indeterminate"):
        run_full_pipeline.cmd_final_report(args)


def test_operator_final_report_receipt_completes_without_resend(tmp_path):
    session = _terminal_run(); path = tmp_path / "final.xlsx"; path.write_text("x")
    run = session.get(DecisionRun, "run"); run.final_report_path = str(path); run.final_report_delivery_state = "indeterminate"
    from app.decision_runs.outreach_funnel import begin_final_report, reconcile_final_report_delivery
    begin_final_report(session, "run")
    from app.decision_runs.progress import fail_stage
    fail_stage(session, "run", StageName.FINAL_REPORT, reason="provider timeout")
    reconcile_final_report_delivery(session, "run", receipt="gmail-message-1")
    assert (run.final_report_delivery_state, run.final_report_delivery_receipt) == ("delivered", "gmail-message-1")


def test_operator_final_report_confirmed_not_delivered_allows_safe_retry(tmp_path):
    session = _terminal_run(); path = tmp_path / "final.xlsx"; path.write_text("x")
    run = session.get(DecisionRun, "run"); run.final_report_path = str(path); run.final_report_delivery_state = "indeterminate"
    from app.decision_runs.outreach_funnel import begin_final_report, reconcile_final_report_delivery
    begin_final_report(session, "run")
    from app.decision_runs.progress import fail_stage
    fail_stage(session, "run", StageName.FINAL_REPORT, reason="provider timeout")
    reconcile_final_report_delivery(session, "run", confirmed_not_delivered=True)
    assert run.final_report_delivery_state == "failed"


def test_confirmed_not_delivered_real_retry_sends_once_and_completes(monkeypatch, tmp_path):
    session = _terminal_run(); path = tmp_path / "final.xlsx"; path.write_text("x")
    run = session.get(DecisionRun, "run"); run.final_report_path = str(path); run.final_report_delivery_state = "indeterminate"
    from app.decision_runs.outreach_funnel import begin_final_report, reconcile_final_report_delivery
    from app.decision_runs.progress import fail_stage
    begin_final_report(session, "run"); fail_stage(session, "run", StageName.FINAL_REPORT, reason="timeout")
    reconcile_final_report_delivery(session, "run", confirmed_not_delivered=True)
    sent = []
    monkeypatch.setattr("app.db.session.get_session", lambda: nullcontext(session))
    monkeypatch.setattr("app.decision_runs.lifecycle.reconcile_attended_run", lambda *_: 0)
    monkeypatch.setattr("app.reporting.final_run_report.build_final_workbook", lambda *_: path)
    monkeypatch.setattr("app.outreach.gmail.get_service", lambda: object())
    monkeypatch.setattr("app.outreach.gmail.send_self_report", lambda *_a, **_k: sent.append(1) or {"id": "receipt"})
    run_full_pipeline.cmd_final_report(argparse.Namespace(run_id="run", output=str(path), send_self_report=True))
    assert sent == [1] and run.state == "completed" and run.final_report_delivery_state == "delivered"


def test_final_self_report_contains_only_decision_run_progress_table(monkeypatch, tmp_path):
    session = _terminal_run(); path = tmp_path / "final.xlsx"; path.write_text("x")
    captured = {}
    monkeypatch.setattr("app.db.session.get_session", lambda: nullcontext(session))
    monkeypatch.setattr("app.decision_runs.lifecycle.reconcile_attended_run", lambda *_: 0)
    monkeypatch.setattr("app.reporting.final_run_report.build_final_workbook", lambda *_: path)
    monkeypatch.setattr("app.outreach.gmail.get_service", lambda: object())
    monkeypatch.setattr(
        "app.outreach.gmail.send_self_report",
        lambda _service, **kwargs: captured.update(kwargs) or {"id": "receipt"},
    )

    run_full_pipeline.cmd_final_report(
        argparse.Namespace(run_id="run", output=str(path), send_self_report=True)
    )

    assert captured["subject"] == "job_agent Decision Run completed — run"
    assert captured["attachment_path"] == str(path)
    assert "<table" in captured["html_body"]
    assert "<th>Stage</th>" in captured["html_body"]
    assert "<td>Final Report</td>" in captured["html_body"]
    assert "<td>Completed</td>" in captured["html_body"]
    assert "<th>Drop role</th>" not in captured["html_body"]
    assert "Connector run" not in captured["html_body"]


def test_crash_left_in_progress_receipt_reconciliation_completes_run(tmp_path):
    session = _terminal_run(); path = tmp_path / "final.xlsx"; path.write_text("x")
    run = session.get(DecisionRun, "run"); run.final_report_path = str(path); run.final_report_delivery_state = "in_progress"
    from app.decision_runs.outreach_funnel import begin_final_report, reconcile_final_report_delivery
    from app.decision_runs.progress import fail_stage
    begin_final_report(session, "run"); fail_stage(session, "run", StageName.FINAL_REPORT, reason="crash after intent")
    reconcile_final_report_delivery(session, "run", receipt="found-after-crash")
    assert run.state == "completed" and run.final_report_delivery_state == "delivered"


def test_crash_left_in_progress_not_delivered_can_real_retry_once(monkeypatch, tmp_path):
    session = _terminal_run(); path = tmp_path / "final.xlsx"; path.write_text("x")
    run = session.get(DecisionRun, "run"); run.final_report_path = str(path); run.final_report_delivery_state = "in_progress"
    from app.decision_runs.outreach_funnel import begin_final_report, reconcile_final_report_delivery
    begin_final_report(session, "run")
    reconcile_final_report_delivery(session, "run", confirmed_not_delivered=True)
    sent=[]; monkeypatch.setattr("app.db.session.get_session", lambda: nullcontext(session)); monkeypatch.setattr("app.decision_runs.lifecycle.reconcile_attended_run", lambda *_: 0)
    monkeypatch.setattr("app.reporting.final_run_report.build_final_workbook", lambda *_: path); monkeypatch.setattr("app.outreach.gmail.get_service", lambda: object())
    monkeypatch.setattr("app.outreach.gmail.send_self_report", lambda *_a, **_k: sent.append(1) or {"id":"receipt"})
    run_full_pipeline.cmd_final_report(argparse.Namespace(run_id="run", output=str(path), send_self_report=True))
    assert sent == [1] and run.state == "completed"


def test_record_stage_can_resume_an_explicit_prior_run_date(tmp_path, monkeypatch):
    monkeypatch.setattr(run_full_pipeline, "LOG_ROOT", tmp_path)
    run_dir = tmp_path / "2026-08-09"
    run_dir.mkdir()
    meta_path = run_dir / "full_pipeline_meta.json"
    meta_path.write_text(
        json.dumps({"run_date": "2026-08-09", "since": "2026-08-08T00:00:00+00:00", "stage_status": []}),
        encoding="utf-8",
    )

    run_full_pipeline.cmd_record_stage(
        argparse.Namespace(
            run_date="2026-08-09",
            stage="enrich",
            status="ok",
            detail="2400 jobs enriched, 73 no_description",
        )
    )

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta["stage_status"] == [
        {
            "stage": "enrich",
            "status": "ok",
            "detail": "2400 jobs enriched, 73 no_description",
        }
    ]
    assert not (tmp_path / run_full_pipeline.date_cls.today().isoformat()).exists()
