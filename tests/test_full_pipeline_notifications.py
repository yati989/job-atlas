import argparse
from contextlib import nullcontext
import json
from types import SimpleNamespace

import pytest

from app.pipeline import notifications
from scripts import run_full_pipeline


def _run_dir(tmp_path):
    run_dir = tmp_path / "2026-08-10"
    run_dir.mkdir()
    (run_dir / "full_pipeline_meta.json").write_text(
        json.dumps({"run_date": "2026-08-10", "stage_status": []}),
        encoding="utf-8",
    )
    return run_dir


def test_recording_failed_agent_stage_notifies_and_exits_nonzero(tmp_path, monkeypatch):
    """An enrichment failure must alert even when Codex is no longer watching."""
    run_dir = _run_dir(tmp_path)
    sent = []
    monkeypatch.setattr(run_full_pipeline, "_today_run_dir", lambda: run_dir)
    monkeypatch.setattr(
        run_full_pipeline,
        "send_pipeline_notification",
        lambda **message: sent.append(message) or True,
    )

    args = argparse.Namespace(
        stage="enrich",
        status="failed",
        detail="provider returned no usable response",
    )

    with pytest.raises(SystemExit) as raised:
        run_full_pipeline.cmd_record_stage(args)

    assert raised.value.code == 1
    assert sent == [
        {
            "title": "job_agent pipeline failed: enrich",
            "message": "provider returned no usable response",
            "tags": ["rotating_light"],
            "priority": "high",
        }
    ]
    meta = json.loads((run_dir / "full_pipeline_meta.json").read_text(encoding="utf-8"))
    assert meta["stage_status"][-1] == {
        "stage": "enrich",
        "status": "failed",
        "detail": "provider returned no usable response",
    }
    assert meta["outcome"] == "failed"


def test_successful_final_report_sends_completion_notification(tmp_path, monkeypatch):
    run_dir = _run_dir(tmp_path)
    sent = []
    monkeypatch.setattr(run_full_pipeline, "_today_run_dir", lambda: run_dir)
    monkeypatch.setattr(
        run_full_pipeline,
        "send_pipeline_notification",
        lambda **message: sent.append(message) or True,
    )
    monkeypatch.setattr(run_full_pipeline, "_build_and_send_report", lambda *_args: run_dir / "run_workbook.xlsx")

    run_full_pipeline.cmd_report(argparse.Namespace())

    assert sent[-1]["title"] == "job_agent pipeline completed"
    assert sent[-1]["priority"] == "default"


def test_prepare_decision_self_report_embeds_safe_approval_summary(tmp_path, monkeypatch):
    """The approval workbook email must be reviewable without opening Excel."""
    captured = {}
    order = []
    session = SimpleNamespace(get=lambda _model, _run_id: SimpleNamespace(state="preparing"))
    monkeypatch.setattr("app.db.session.get_session", lambda: nullcontext(session))
    monkeypatch.setattr(
        "app.decision_runs.lifecycle.reconcile_attended_run",
        lambda _session, _run_id: 0,
    )
    monkeypatch.setattr(
        "app.decision_runs.prepare_decision_run",
        lambda _session, _run_id, **_kwargs: order.append("prepare") or {
            "run_id": "run-42", "outcomes": {"eligible": 3},
            "available_company_count": 3,
        },
    )
    monkeypatch.setattr(
        "app.reporting.decision_report.build_decision_workbook",
        lambda _session, _run_id, output: order.append("build") or output,
    )
    monkeypatch.setattr(
        "app.reporting.approval_email.render_approval_email",
        lambda _session, _run_id, _url: type("Email", (), {
            "text_body": "Decision run run-42 is awaiting approval.",
            "html_body": "<p>Group 1 has company and salary details.</p>",
        })(),
    )
    monkeypatch.setattr("app.outreach.gmail.get_service", lambda: object())
    monkeypatch.setattr(
        "app.decision_runs.google_sheet_approval.get_service", lambda: object(),
    )
    monkeypatch.setattr(
        "app.decision_runs.google_sheet_approval.create_approval_spreadsheet",
        lambda *_args, **_kwargs: {
            "spreadsheet_id": "sheet-42",
            "spreadsheet_url": "https://docs.google.com/spreadsheets/d/sheet-42/edit",
        },
    )
    monkeypatch.setattr(
        "app.decision_runs.lifecycle.approval_artifact_ready",
        lambda _session, _run_id, **kwargs: (
            order.append("ready") or captured.update(ready=kwargs)
        ),
    )
    monkeypatch.setattr(
        "app.outreach.gmail.send_self_report",
        lambda service, **kwargs: (
            order.append("deliver") or captured.update(service=service, **kwargs)
            or {"id": "approval-message", "threadId": "approval-thread"}
        ),
    )

    run_full_pipeline.cmd_prepare_decision(
        argparse.Namespace(run_id="run-42", output=str(tmp_path / "approval.xlsx"), send_self_report=True)
    )

    assert captured["attachment_path"] is None
    assert "html_body" in captured
    assert "Group 1" in captured["html_body"]
    assert "company" in captured["html_body"].lower()
    assert "salary" in captured["html_body"].lower()
    assert captured["ready"]["gmail_message_id"] == "approval-message"
    assert captured["ready"]["gmail_thread_id"] == "approval-thread"
    assert captured["ready"]["google_spreadsheet_id"] == "sheet-42"
    assert captured["ready"]["google_spreadsheet_url"].endswith("/sheet-42/edit")
    assert order == ["prepare", "build", "deliver", "ready"]


def test_prepare_without_workbook_still_enters_approval_waiting(monkeypatch):
    order = []
    session = SimpleNamespace(get=lambda _model, _run_id: SimpleNamespace(state="enriching_companies"))
    monkeypatch.setattr("app.db.session.get_session", lambda: nullcontext(session))
    monkeypatch.setattr("app.decision_runs.lifecycle.reconcile_attended_run", lambda *_args: 0)
    monkeypatch.setattr(
        "app.decision_runs.prepare_decision_run",
        lambda *_args, **_kwargs: {"run_id": "run-42", "available_company_count": 3},
    )
    monkeypatch.setattr(
        "app.decision_runs.lifecycle.approval_artifact_ready",
        lambda *_args, **_kwargs: order.append("ready"),
    )

    run_full_pipeline.cmd_prepare_decision(argparse.Namespace(
        run_id="run-42", output=None, send_self_report=False,
    ))

    assert order == ["ready"]


def test_email_approval_waiter_ignores_invalid_reply_and_accepts_correction(
    tmp_path, monkeypatch,
):
    from app.decision_runs.workbook_approval import WorkbookApproval

    replies = iter([
        SimpleNamespace(
            gmail_message_id="bad",
            gmail_thread_id="thread-42", internal_date=2,
            approver="candidate@example.com",
        ),
        SimpleNamespace(
            gmail_message_id="good",
            gmail_thread_id="thread-42", internal_date=3,
            approver="candidate@example.com",
        ),
    ])
    seen = []
    monkeypatch.setattr("app.outreach.gmail.get_service", lambda: object())
    monkeypatch.setattr("app.decision_runs.email_approval.find_latest_approval_reply", lambda *_args, **_kwargs: next(replies))
    monkeypatch.setattr("app.decision_runs.google_sheet_approval.get_service", lambda: object())
    exported = []
    monkeypatch.setattr(
        "app.decision_runs.google_sheet_approval.export_approval_spreadsheet",
        lambda _service, _id, path: exported.append((_id, path)) or path,
    )

    def read(path, _run_id):
        if "bad-google-sheet.xlsx" in path.name:
            raise ValueError("missing company row")
        return WorkbookApproval({7: True}, {7: "auto"})

    monkeypatch.setattr(
        "app.decision_runs.workbook_approval.read_workbook_approval", read,
    )
    monkeypatch.setattr("app.db.session.get_session", lambda: nullcontext(object()))
    monkeypatch.setattr(
        "app.decision_runs.lifecycle.approval_email_thread_id",
        lambda *_args: "thread-42",
    )
    monkeypatch.setattr(
        "app.decision_runs.lifecycle.approval_google_spreadsheet",
        lambda *_args: ("sheet-42", "https://docs.google.com/spreadsheets/d/sheet-42/edit"),
    )
    monkeypatch.setattr(
        "app.decision_runs.lifecycle.reconcile_attended_run", lambda *_args: 0,
    )
    monkeypatch.setattr(
        "app.decision_runs.lifecycle.approval_input_received",
        lambda *_args: seen.append("received"),
    )
    monkeypatch.setattr(
        "app.decision_runs.lifecycle.approval_input_rejected",
        lambda *_args, **_kwargs: seen.append("rejected"),
    )
    monkeypatch.setattr(
        "app.decision_runs.approve_decision_run_from_workbook",
        lambda *_args, **_kwargs: SimpleNamespace(
            run_id="run-42", company_ids=(7,), job_ids=(11,),
        ),
    )

    run_full_pipeline.cmd_approve_email_reply(argparse.Namespace(
        run_id="run-42", output_dir=str(tmp_path), wait=True,
        poll_seconds=5, timeout_seconds=0,
    ))

    assert seen == ["received", "rejected"]
    assert [item[0] for item in exported] == ["sheet-42", "sheet-42"]


def test_email_approval_waiter_rejects_malformed_sheet_and_keeps_polling(
    tmp_path, monkeypatch,
):
    from app.decision_runs.workbook_approval import WorkbookApproval

    good = SimpleNamespace(
        gmail_message_id="good",
        gmail_thread_id="thread-42", internal_date=3,
        approver="candidate@example.com",
    )
    bad = SimpleNamespace(
        gmail_message_id="bad", gmail_thread_id="thread-42",
        internal_date=2, approver="candidate@example.com",
    )
    events = iter([bad, good])
    calls = []

    def download(*_args, **kwargs):
        calls.append(kwargs)
        return next(events)

    monkeypatch.setattr("app.outreach.gmail.get_service", lambda: object())
    monkeypatch.setattr(
        "app.decision_runs.email_approval.find_latest_approval_reply", download,
    )
    monkeypatch.setattr("app.decision_runs.google_sheet_approval.get_service", lambda: object())
    monkeypatch.setattr(
        "app.decision_runs.google_sheet_approval.export_approval_spreadsheet",
        lambda _service, _id, path: path,
    )
    monkeypatch.setattr(
        "app.decision_runs.workbook_approval.read_workbook_approval",
        lambda path, *_args: (
            (_ for _ in ()).throw(ValueError("invalid sheet"))
            if "bad-google-sheet.xlsx" in path.name
            else WorkbookApproval({7: True}, {7: "auto"})
        ),
    )
    monkeypatch.setattr("app.db.session.get_session", lambda: nullcontext(object()))
    monkeypatch.setattr(
        "app.decision_runs.lifecycle.approval_email_thread_id",
        lambda *_args: "thread-42",
    )
    monkeypatch.setattr(
        "app.decision_runs.lifecycle.approval_google_spreadsheet",
        lambda *_args: ("sheet-42", "https://docs.google.com/spreadsheets/d/sheet-42/edit"),
    )
    monkeypatch.setattr(
        "app.decision_runs.lifecycle.reconcile_attended_run", lambda *_args: 0,
    )
    recorded = []
    monkeypatch.setattr(
        "app.decision_runs.lifecycle.approval_input_received",
        lambda *_args: recorded.append("received"),
    )
    monkeypatch.setattr(
        "app.decision_runs.lifecycle.approval_input_rejected",
        lambda *_args, **_kwargs: recorded.append("rejected"),
    )
    monkeypatch.setattr(
        "app.decision_runs.approve_decision_run_from_workbook",
        lambda *_args, **_kwargs: SimpleNamespace(
            run_id="run-42", company_ids=(7,), job_ids=(11,),
        ),
    )
    monkeypatch.setattr("time.sleep", lambda _seconds: None)

    run_full_pipeline.cmd_approve_email_reply(argparse.Namespace(
        run_id="run-42", output_dir=str(tmp_path), wait=True,
        poll_seconds=5, timeout_seconds=0,
    ))

    assert recorded == ["received", "rejected"]
    assert calls[1]["after_internal_date"] == 2
    assert calls[1]["excluded_message_ids"] == {"bad"}


def test_ntfy_uses_utf8_json_body_not_non_ascii_headers(monkeypatch):
    captured = {}

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def fake_urlopen(request, timeout):
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["headers"] = dict(request.header_items())
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr(notifications, "FULL_PIPELINE_NTFY_TOPIC", "private-test-topic")
    monkeypatch.setattr(notifications.urllib.request, "urlopen", fake_urlopen)

    assert notifications.send_pipeline_notification(
        title="Pipeline failed — enrichment",
        message="Couldn’t enrich résumé…",
        tags=["rotating_light"],
        priority="high",
    )
    assert captured["body"]["title"] == "Pipeline failed — enrichment"
    assert captured["body"]["message"] == "Couldn’t enrich résumé…"
    assert captured["body"]["priority"] == 4
    assert all(value.isascii() for value in captured["headers"].values())
