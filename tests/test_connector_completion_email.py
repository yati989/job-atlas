from datetime import datetime, timezone

from app.pipeline.completion_email import render_completion_email, send_completion_email


def test_completion_email_renders_dashboard_source_rows_as_html_table():
    snapshot = {
        "run_id": "run-1",
        "command": "app.pipeline.run_all",
        "status": "partial",
        "started_at": "2026-08-22T10:00:00+00:00",
        "ended_at": "2026-08-22T10:01:05+00:00",
        "sources": [
            {
                "source": "test_source",
                "status": "failed",
                "jobs_finished": 0,
                "instances_finished": 12,
                "instances_total": 12,
                "instances_succeeded": 0,
                "instances_failed": 12,
                "started_at": "2026-08-22T10:00:00+00:00",
                "ended_at": "2026-08-22T10:00:52+00:00",
                "gate_fetched": 20,
                "kept": 4,
                "drop_role": 10,
                "drop_seniority": 2,
                "drop_location": 1,
                "drop_recency": 3,
            }
        ],
    }

    email = render_completion_email(
        snapshot, now=datetime(2026, 8, 22, 10, 1, 5, tzinfo=timezone.utc)
    )

    assert email.subject == "job_agent connector run partial — run-1"
    assert "test_source" in email.plain_text
    assert "12/12" in email.plain_text
    assert "20" in email.plain_text
    assert "4" in email.plain_text
    assert "20.0%" in email.plain_text
    assert "Drop recency" in email.html
    assert "<table" in email.html
    assert "<td>failed</td>" in email.html
    assert "12/12" in email.html
    assert "Total" in email.plain_text
    assert "<tfoot>" in email.html


def test_completion_email_delivers_only_rendered_self_report(monkeypatch):
    sent = {}
    from app.outreach import gmail

    monkeypatch.setattr(gmail, "get_service", lambda: "gmail-service")
    monkeypatch.setattr(gmail, "send_self_report", lambda service, **kwargs: sent.update(service=service, **kwargs))

    assert send_completion_email({
        "run_id": "run-2", "status": "completed",
        "started_at": "2026-08-22T10:00:00+00:00",
        "ended_at": "2026-08-22T10:00:01+00:00", "sources": [],
    })
    assert sent["service"] == "gmail-service"
    assert sent["subject"] == "job_agent connector run completed — run-2"
    assert "<table" in sent["html_body"]
