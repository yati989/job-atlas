from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.orm import Base, Company, Job, JobSkill
from app.workflows.mock_interview import find_interview_jobs, load_interview_context


def _session():
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True)()


def test_public_mock_interview_context_uses_stored_job_company_and_skills():
    session = _session()
    company = Company(
        name="Example Robotics", industry="Robotics",
        pain_points="Improve fleet reliability", employee_count_range="51-200",
    )
    session.add(company)
    session.flush()
    job = Job(
        source="example", external_job_id="role-1", company_id=company.id,
        company_name_raw=company.name, title="Platform Engineer",
        description_raw="Own reliable services.", seniority="senior",
        employment_type="full-time", experience_min_years=4,
        education_requirement="Bachelor's degree or equivalent experience",
    )
    session.add(job)
    session.flush()
    session.add_all([
        JobSkill(job_id=job.id, skill="Python", skill_type="hard"),
        JobSkill(job_id=job.id, skill="Communication", skill_type="soft"),
    ])
    session.flush()

    matches = find_interview_jobs(session, "robotics")
    context = load_interview_context(session, job.id)

    assert matches == [{
        "job_id": job.id, "title": "Platform Engineer",
        "company_name": "Example Robotics", "source": "example",
        "posted_at": None,
    }]
    assert context["company"]["pain_points"] == "Improve fleet reliability"
    assert context["job"]["description"] == "Own reliable services."
    assert context["skills"] == {
        "hard": ["Python"], "soft": ["Communication"],
    }


def test_missing_job_is_reported_instead_of_guessed():
    session = _session()
    try:
        load_interview_context(session, 999)
    except ValueError as exc:
        assert str(exc) == "unknown job id: 999"
    else:
        raise AssertionError("missing job should fail")
