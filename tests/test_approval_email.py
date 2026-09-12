from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.decision_runs import create_decision_run, prepare_decision_run
from app.models.orm import Base
from app.models.schemas import NormalizedJob
from app.pipeline.upsert import upsert_job
from app.reporting.approval_email import render_approval_email


def test_approval_email_summarises_each_group_and_run_context():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    now = datetime(2026, 8, 22, tzinfo=timezone.utc)
    upsert_job(
        session,
        NormalizedJob(
            source="x", external_job_id="1", title="ML Engineer", company_name_raw="Acme",
            description_raw="work", posted_at=now, is_remote=True,
        ),
    )
    run = create_decision_run(session, since=(now - timedelta(days=1)).isoformat(), cutoff=now)
    run.telemetry_version = None  # This fixture intentionally covers a legacy snapshot.
    prepare_decision_run(session, run.id)

    email = render_approval_email(session, run.id, "https://docs.google.com/spreadsheets/d/sheet-42/edit")

    assert "Window:" in email.text_body
    assert "State: awaiting_approval" in email.text_body
    assert "Group 1" in email.text_body
    assert "1 company; 1 eligible job; salary known: 0; salary unknown: 1" in email.text_body
    for group in range(1, 5):
        assert f"<h2>Group {group}</h2>" in email.html_body
    assert "All outcomes" in email.html_body
    assert "Eligible role families" in email.html_body
    assert "Open the approval Google Sheet" in email.html_body
    assert "do not need to attach" in email.text_body
