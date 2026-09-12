from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.decision_runs import create_decision_run, prepare_decision_run
from app.models.orm import Base, Job, JobPostingVersion
from app.models.schemas import NormalizedJob
from app.pipeline.upsert import upsert_job
from app.reporting.decision_report import build_decision_workbook


def _session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_anomaly_sheet_has_snapshot_source_and_clickable_job_link(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    session = _session()
    cutoff = datetime(2026, 8, 22, tzinfo=timezone.utc)
    job = upsert_job(
        session,
        NormalizedJob(
            source="wellfound",
            external_job_id="future-1",
            title="Data Scientist",
            company_name_raw="Acme",
            description_raw="work",
            job_url="https://wellfound.com/jobs/future-1",
            posted_at=cutoff + timedelta(days=1),
        ),
    )
    run = create_decision_run(session, since=(cutoff - timedelta(days=1)).isoformat(), cutoff=cutoff)
    run.telemetry_version = None  # This fixture intentionally covers a legacy snapshot.
    prepare_decision_run(session, run.id)

    # Report rendering must not re-read mutable Job / posting-version values.
    session.get(Job, job.id).source = "mutated-live-source"
    version = session.query(JobPostingVersion).filter_by(job_id=job.id).one()
    version.snapshot = {**version.snapshot, "job_url": "https://mutated.example/job"}
    session.flush()

    path = tmp_path / "decision.xlsx"
    build_decision_workbook(session, run.id, path)
    worksheet = openpyxl.load_workbook(path)["Anomalies"]
    headers = [cell.value for cell in worksheet[1]]

    assert "source" in headers
    assert "job_link" in headers
    assert worksheet.cell(2, headers.index("source") + 1).value == "wellfound"
    link_cell = worksheet.cell(2, headers.index("job_link") + 1)
    assert link_cell.value == "https://wellfound.com/jobs/future-1"
    assert link_cell.hyperlink.target == "https://wellfound.com/jobs/future-1"
