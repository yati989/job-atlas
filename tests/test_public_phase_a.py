from datetime import datetime, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.models.orm import (
    Base, Company, DecisionRun, Job, JobPostingVersion, JobScreeningFact,
    JobSkill, ProfiledSearchOutcome,
)
from app.workflows.public_phase_a import apply_phase_a_results, phase_a_context


def _fixture():
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, future=True)()
    session.add(DecisionRun(
        id="run-1", since_at=datetime.now(timezone.utc),
        cutoff_at=datetime.now(timezone.utc), input_timezone="UTC",
        state="enriching", policy_snapshot={
            "version": 1, "name": "design", "professions": ["product design"],
            "search_terms": ["product designer"], "relevance_terms": ["product designer"],
            "countries": ["IN"], "arrangements": ["remote"],
            "seniority": ["individual_contributor"], "collection_window_days": 30,
            "source_selection": {"mode": "all_compatible"},
        }, target_count=0,
    ))
    company = Company(name="DesignCo")
    session.add(company); session.flush()
    job = Job(
        source="fixture", external_job_id="1", company_id=company.id,
        company_name_raw=company.name, title="Product Designer",
        description_raw="Requires Figma and 3 years experience.", salary_raw=None,
    )
    session.add(job); session.flush()
    version = JobPostingVersion(
        job_id=job.id, canonical_duplicate_root_id=job.id,
        material_content_hash="material-1", posting_instance_key="fixture:1",
        snapshot={"title": job.title, "description_raw": job.description_raw},
    )
    session.add(version); session.flush()
    session.add(ProfiledSearchOutcome(
        run_id="run-1", source="fixture", external_job_id="1", outcome="kept",
        profile_fingerprint="fp", evidence={}, job_id=job.id,
        posting_version_id=version.id,
    ))
    session.flush()
    return session, company, job, version


def test_public_phase_a_uses_exact_run_and_persists_reusable_evidence_without_mx():
    session, company, job, version = _fixture()

    context = phase_a_context(session, "run-1")
    assert context["jobs"][0]["posting_version_id"] == version.id
    assert context["companies"][0]["company_id"] == company.id

    counts = apply_phase_a_results(session, "run-1", {
        "context_fingerprint": context["context_fingerprint"],
        "jobs": [{
            "posting_version_id": version.id, "status": "done",
            "experience_min_years": 3, "hard_skills": ["Figma"],
        }],
        "companies": [{
            "company_id": company.id, "industry": "Design software",
            "description": "Builds design collaboration software.",
            "company_type": "employer", "canonical_domain": "design.example",
            "search_groups": ["product_design"],
        }],
    })

    assert counts == {"jobs_recorded": 1, "companies_recorded": 1}
    assert job.enrichment_status == "done"
    assert [(row.skill, row.skill_type) for row in session.scalars(select(JobSkill))] == [("Figma", "hard")]
    assert session.scalar(select(JobScreeningFact)).posting_version_id == version.id
    assert company.enrichment_status == "done"
    assert company.contact_search_groups == ["product_design"]
    assert company.canonical_domain == "design.example"
    assert company.domain_resolution_status is None

    reused = phase_a_context(session, "run-1")
    assert reused["jobs"] == []
    assert reused["reused_jobs"][0]["posting_version_id"] == version.id
    assert reused["companies"] == []
    assert reused["reused_companies"][0]["company_id"] == company.id


def test_public_phase_a_rejects_results_outside_run():
    session, company, _job, _version = _fixture()
    context = phase_a_context(session, "run-1")
    try:
        apply_phase_a_results(session, "run-1", {
            "context_fingerprint": context["context_fingerprint"],
            "jobs": [{"posting_version_id": 999, "status": "done"}],
            "companies": [],
        })
    except ValueError as exc:
        assert "coverage mismatch" in str(exc)
    else:
        raise AssertionError("out-of-run enrichment was accepted")
    assert company.enrichment_status == "pending"


def test_public_phase_a_rejects_partial_duplicate_and_unapproved_function_results():
    session, company, _job, version = _fixture()
    context = phase_a_context(session, "run-1")
    base_job = {"posting_version_id": version.id, "status": "done"}
    base_company = {
        "company_id": company.id, "company_type": "employer",
        "search_groups": ["product_design"],
    }
    bad_payloads = [
        {"context_fingerprint": context["context_fingerprint"], "jobs": [], "companies": [base_company]},
        {"context_fingerprint": context["context_fingerprint"], "jobs": [base_job, base_job], "companies": [base_company]},
        {"context_fingerprint": context["context_fingerprint"], "jobs": [base_job], "companies": [{**base_company, "search_groups": ["crypto_sales"]}]},
    ]
    for payload in bad_payloads:
        try:
            apply_phase_a_results(session, "run-1", payload)
        except ValueError:
            session.rollback()
        else:
            raise AssertionError("invalid Phase A coverage/function was accepted")


def test_phase_a_resolves_experience_unknown_before_selection():
    session, company, job, version = _fixture()
    outcome = session.scalar(select(ProfiledSearchOutcome))
    outcome.outcome = "needs_review"
    outcome.first_failed_axis = "experience"
    outcome.reason = "experience requirement is unknown"
    run = session.get(DecisionRun, "run-1")
    run.policy_snapshot = {
        **run.policy_snapshot,
        "experience": {"minimum_years": 2, "maximum_years": 5},
    }
    session.flush()

    context = phase_a_context(session, "run-1")

    apply_phase_a_results(session, "run-1", {
        "context_fingerprint": context["context_fingerprint"],
        "jobs": [{
            "posting_version_id": version.id, "status": "done",
            "experience_min_years": 3, "experience_max_years": 4,
        }],
        "companies": [{
            "company_id": company.id, "company_type": "employer",
            "search_groups": ["product_design"],
        }],
    })

    assert outcome.outcome == "kept"
    assert outcome.first_failed_axis is None
    assert job.experience_min_years == 3


def test_public_phase_a_rejects_a_stale_context_before_persisting_results():
    session, company, job, version = _fixture()
    context = phase_a_context(session, "run-1")
    company.name = "DesignCo Renamed"
    session.flush()

    try:
        apply_phase_a_results(session, "run-1", {
            "context_fingerprint": context["context_fingerprint"],
            "jobs": [{"posting_version_id": version.id, "status": "done"}],
            "companies": [{
                "company_id": company.id,
                "company_type": "employer",
                "search_groups": ["product_design"],
            }],
        })
    except ValueError as exc:
        assert "stale" in str(exc)
    else:
        raise AssertionError("stale Phase A context was accepted")
    assert job.enrichment_status == "pending"
