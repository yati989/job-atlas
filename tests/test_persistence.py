"""
Persistence seam (ATS #3 / issue #41): the tailored_resumes table records one
current tailored resume per job, and a re-run updates rather than duplicates.

Runs against SQLite in-memory — no live Postgres needed — since we're asserting
the ORM/upsert behavior (write-one, re-run-updates), not Postgres specifics.
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.orm import Base, Job, TailoredResume
from app.resume.persistence import upsert_tailored_resume


def _session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)  # create_all builds tailored_resumes too
    return sessionmaker(bind=engine)()


def _make_job(session, ext_id="ext-1"):
    job = Job(source="test", external_job_id=ext_id, title="Data Scientist")
    session.add(job)
    session.flush()
    return job


def test_create_all_builds_tailored_resumes():
    assert "tailored_resumes" in Base.metadata.tables


def test_writes_one_row_per_job():
    session = _session()
    job = _make_job(session)
    upsert_tailored_resume(
        session, job_id=job.id, score=82.0,
        gap_report={"covered": ["python"], "real_gaps": ["pyspark"]},
        tailored_yaml={"contact": {"name": "Alex"}}, artifact_dir="/out/1",
    )
    session.commit()
    rows = session.query(TailoredResume).all()
    assert len(rows) == 1
    assert rows[0].job_id == job.id
    assert rows[0].score == 82.0


def test_rerun_updates_not_duplicates():
    session = _session()
    job = _make_job(session)
    first = upsert_tailored_resume(session, job_id=job.id, score=70.0, artifact_dir="/out/a")
    session.commit()
    first_id = first.id

    second = upsert_tailored_resume(session, job_id=job.id, score=91.0, artifact_dir="/out/b")
    session.commit()

    assert session.query(TailoredResume).count() == 1  # updated, not duplicated
    assert second.id == first_id
    assert second.score == 91.0
    assert second.artifact_dir == "/out/b"


def test_distinct_jobs_get_distinct_rows():
    session = _session()
    j1 = _make_job(session, "ext-1")
    j2 = _make_job(session, "ext-2")
    upsert_tailored_resume(session, job_id=j1.id, score=60.0)
    upsert_tailored_resume(session, job_id=j2.id, score=65.0)
    session.commit()
    assert session.query(TailoredResume).count() == 2
