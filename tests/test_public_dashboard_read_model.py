from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.dashboard.public_read_model import (
    PublicDashboardFilters,
    combine_public_snapshots,
    dated_search_label,
    filter_kept_jobs,
    friendly_search_label,
    load_public_snapshot,
    outreach_draft_caption,
    profile_link_output_message,
    stage_message,
)
from app.models.orm import (
    Base, Company, Contact, DecisionRun, GuidedSourceRun, Job, JobSkill, LinkedInProfileLink,
    ProfileDiscoveryAuthorization, ProfiledSearchOutcome, PublicSelectionScope,
    OutreachDeliveryAttempt, OutreachMessage, PublicJobPhaseAEvidence,
    PublicSelectionScopeItem,
)
from app.models.schemas import NormalizedJob
from app.workflows.guided_run import collect_guided_run, start_guided_run
from app.workflows.planning import SourceCapability, prepare_run


def test_dashboard_projects_durable_source_and_outcome_progress():
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, future=True)()
    catalog = (SourceCapability(
        name="fixture", runtime="http", countries=frozenset({"IN"}),
        arrangements=frozenset({"remote"}), rationale="test",
    ),)
    preview = prepare_run({
        "version": 1, "name": "ops", "professions": ["operations analyst"],
        "search_terms": ["operations analyst"],
        "relevance_terms": ["operations analyst"], "countries": ["IN"],
        "arrangements": ["remote"], "seniority": ["individual_contributor"],
        "collection_window_days": 30,
        "source_selection": {"mode": "named", "sources": ["fixture"]},
    }, catalog)
    start_guided_run(
        session, preview=preview, profile_fingerprint="fp", confirmed=True,
        now=datetime(2026, 9, 6, tzinfo=timezone.utc), run_id="run-1",
    )
    collect_guided_run(session, run_id="run-1", fetchers={"fixture": lambda _profile: [
        NormalizedJob(
            source="fixture", external_job_id="1", title="Operations Analyst",
            company_name_raw="Example Co", location_raw="Remote - India", is_remote=True,
            job_url="https://jobs.example/1",
        )
    ]})
    company = session.query(Company).filter_by(name="Example Co").one()
    company.glassdoor_overall_rating = 4.3
    company.glassdoor_wlb_rating = 4.1
    company.estimated_salary_lpa = 18.5
    company.industry = "Business services"
    company.description = "Runs distributed operations teams."
    company.pain_points = "Needs reliable reporting."
    company.enrichment_status = "done"
    job = session.query(Job).one()
    job.enrichment_status = "done"
    job.experience_min_years = 1
    job.education_requirement = "Any bachelor's degree"
    session.add(JobSkill(job_id=job.id, skill="Excel", skill_type="hard"))
    outcome = session.query(ProfiledSearchOutcome).one()
    session.add(PublicJobPhaseAEvidence(
        posting_version_id=outcome.posting_version_id, status="done",
        evidence={
            "salary": {"evidence": "40000-45000 MONTHLY", "guaranteed_max_lpa": 5.4},
        },
    ))
    scope = PublicSelectionScope(
        run_id="run-1", revision=1, mode="all_eligible", fingerprint="scope",
        job_count=1, company_count=1,
    )
    session.add(scope)
    session.flush()
    session.add(PublicSelectionScopeItem(
        scope_id=scope.id, posting_version_id=outcome.posting_version_id,
        company_id=company.id,
    ))
    session.flush()

    snapshot = load_public_snapshot(session, "run-1")

    assert snapshot.source_statuses[0]["status"] == "completed"
    assert snapshot.collected_count == 1
    assert snapshot.enriched_job_count == 1
    assert snapshot.run["label"] == "Operations analyst for remote work in India"
    assert {row["name"]: row["status"] for row in snapshot.stage_statuses}["phase_a"] == "awaiting_confirmation"
    assert snapshot.outcome_counts == {"kept": 1}
    assert snapshot.kept_jobs[0]["company"] == "Example Co"
    assert snapshot.kept_jobs[0]["glassdoor_overall_rating"] == 4.3
    assert snapshot.kept_jobs[0]["glassdoor_wlb_rating"] == 4.1
    assert snapshot.kept_jobs[0]["estimated_salary_lpa"] == 18.5
    assert snapshot.kept_jobs[0]["selected"] is True
    assert snapshot.kept_jobs[0]["experience_min_years"] == 1
    assert snapshot.kept_jobs[0]["education"] == "Any bachelor's degree"
    assert snapshot.kept_jobs[0]["hard_skills"] == ("Excel",)
    assert snapshot.kept_jobs[0]["salary_evidence"] == (
        "40000-45000 MONTHLY · up to 5.4 LPA"
    )
    assert snapshot.companies[0]["industry"] == "Business services"
    assert snapshot.companies[0]["pain_points"] == "Needs reliable reporting."
    assert len(filter_kept_jobs(snapshot.kept_jobs, PublicDashboardFilters(
        minimum_glassdoor_overall_rating=4.0,
        minimum_glassdoor_wlb_rating=4.0,
        minimum_estimated_salary_lpa=18.0,
    ))) == 1
    assert filter_kept_jobs(snapshot.kept_jobs, PublicDashboardFilters(
        minimum_glassdoor_wlb_rating=4.2,
    )) == ()
    assert len(filter_kept_jobs(snapshot.kept_jobs, PublicDashboardFilters(
        source="fixture", company="example", skill="Excel", remote=True,
        enrichment_status="done", selected_only=True,
    ))) == 1
    assert filter_kept_jobs(snapshot.kept_jobs, PublicDashboardFilters(
        skill="Python",
    )) == ()

    cumulative = combine_public_snapshots((snapshot, snapshot))
    assert len(cumulative.kept_jobs) == 1
    assert len(cumulative.companies) == 1
    assert cumulative.companies[0]["jobs"] == 1


def test_dashboard_projects_run_scoped_company_inbox_gmail_drafts_as_unsent():
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, future=True)()
    company = Company(name="Example Co")
    run = DecisionRun(
        id="run-drafts", since_at=datetime(2026, 9, 12, tzinfo=timezone.utc),
        cutoff_at=datetime(2026, 9, 12, tzinfo=timezone.utc), input_timezone="UTC",
        state="optional_stages", policy_snapshot={}, target_count=0,
    )
    session.add_all([company, run])
    session.flush()
    message = OutreachMessage(
        run_id=run.id, company_id=company.id, contact_id=None,
        message_kind="initial", subject="Hello", body="Body", state="pushed",
    )
    session.add(message)
    session.flush()
    session.add(OutreachDeliveryAttempt(
        message_id=message.id, sequence_number=1, to_email="careers@example.com",
        domain="example.co", state="pushed", gmail_draft_id="gmail-draft-1",
        evidence={"source_url": "https://example.co/careers"},
    ))
    session.flush()

    snapshot = load_public_snapshot(session, run.id)

    assert snapshot.outreach_drafts == ({
        "company": "Example Co",
        "contact": None,
        "to_email": "careers@example.com",
        "state": "pushed",
        "sent_at": None,
        "gmail_draft_id": "gmail-draft-1",
        "evidence_source_url": "https://example.co/careers",
    },)
    dashboard_source = (Path(__file__).resolve().parents[1] / "app" / "dashboard" / "public_app.py").read_text()
    assert 'st.subheader("Outreach drafts")' in dashboard_source
    assert "snapshot.outreach_drafts" in dashboard_source
    assert "outreach_draft_caption(snapshot.outreach_drafts)" in dashboard_source
    assert outreach_draft_caption(snapshot.outreach_drafts) == (
        "These drafts were created in Gmail and were not sent."
    )


def test_dashboard_projects_named_retargeted_draft_and_its_original_evidence_url():
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, future=True)()
    company = Company(name="Example Co")
    run = DecisionRun(
        id="run-retargeted", since_at=datetime(2026, 9, 12, tzinfo=timezone.utc),
        cutoff_at=datetime(2026, 9, 12, tzinfo=timezone.utc), input_timezone="UTC",
        state="optional_stages", policy_snapshot={}, target_count=0,
    )
    session.add_all([company, run])
    session.flush()
    contact = Contact(
        company_id=company.id, full_name="Jane Doe", linkedin_url="https://linkedin.com/in/jane-doe",
    )
    session.add(contact)
    session.flush()
    message = OutreachMessage(
        run_id=run.id, company_id=company.id, contact_id=contact.id,
        message_kind="initial", subject="Hello", body="Body", state="held",
    )
    session.add(message)
    session.flush()
    session.add(OutreachDeliveryAttempt(
        message_id=message.id, sequence_number=0, to_email="careers@example.com",
        domain="example.co", state="pushed", gmail_draft_id="older-gmail-draft",
        evidence={"source_url": "https://example.co/careers"},
    ))
    session.add(OutreachDeliveryAttempt(
        message_id=message.id, sequence_number=1, to_email="jane@example.com",
        domain="example.co", state="held", gmail_draft_id="gmail-draft-2",
        evidence={
            "linkedin_url": "https://linkedin.com/in/jane-doe",
            "retargeted_from": {"recipient_evidence": {
                "source_url": "https://example.co/careers",
            }},
        },
    ))
    session.flush()

    snapshot = load_public_snapshot(session, run.id)

    assert snapshot.outreach_drafts == ({
        "company": "Example Co",
        "contact": "Jane Doe",
        "to_email": "jane@example.com",
        "state": "held",
        "sent_at": None,
        "gmail_draft_id": "gmail-draft-2",
        "evidence_source_url": "https://linkedin.com/in/jane-doe",
    },)


def test_outreach_draft_caption_does_not_claim_sent_attempts_are_unsent():
    assert outreach_draft_caption(({"sent_at": None}, {"sent_at": datetime.now(timezone.utc)})) == (
        "Some delivery attempts have been sent; review each row's state and sent time."
    )


def test_dashboard_counts_pending_review_rows_and_only_completed_enrichment():
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, future=True)()
    catalog = (SourceCapability(
        name="fixture", runtime="http", countries=frozenset({"IN"}),
        arrangements=frozenset({"remote"}), rationale="test",
    ),)
    preview = prepare_run({
        "version": 1, "name": "ops", "professions": ["operations analyst"],
        "search_terms": ["operations analyst"],
        "relevance_terms": ["operations analyst"], "countries": ["IN"],
        "arrangements": ["remote"], "seniority": ["individual_contributor"],
        "collection_window_days": 14,
        "source_selection": {"mode": "named", "sources": ["fixture"]},
    }, catalog)
    start_guided_run(
        session, preview=preview, profile_fingerprint="fp", confirmed=True,
        now=datetime(2026, 9, 12, tzinfo=timezone.utc), run_id="run-pending",
    )
    collect_guided_run(session, run_id="run-pending", fetchers={"fixture": lambda _profile: [
        NormalizedJob(
            source="fixture", external_job_id="1", title="Operations Analyst",
            company_name_raw="Example Co", location_raw="Remote - India", is_remote=True,
            job_url="https://jobs.example/1",
        )
    ]})
    source_run = session.query(GuidedSourceRun).one()
    source_run.outcome_counts = {
        "rejected": 1,
        "awaiting_role_review": 4,
        "kept": 0,
        "needs_review": 0,
    }
    session.query(Job).one().enrichment_status = "no_description"
    session.flush()

    snapshot = load_public_snapshot(session, "run-pending")

    assert snapshot.collected_count == 5
    assert snapshot.enriched_job_count == 0


def test_partial_stage_message_surfaces_stored_blocker():
    assert stage_message({
        "name": "tailoring",
        "status": "partial",
        "expected_count": 3,
        "completed_count": 0,
        "failed_count": 3,
        "detail": {"blocker": "resume master missing"},
    }) == "Partly completed: resume master missing."


def test_profile_link_output_message_preserves_partial_zero_result_progress():
    assert profile_link_output_message(({
        "name": "profile_links",
        "status": "partial",
        "expected_count": 16,
        "completed_count": 8,
        "failed_count": 0,
        "detail": {
            "maximum_calls": 8,
            "blocker": "No usable profile results were returned within the approved 8-call limit",
        },
    },)) == (
        "Partly completed: No usable profile results were returned within the approved "
        "8-call limit. 8 of 16 planned profile searches completed (approved limit: 8); "
        "no profile links were found."
    )


def test_friendly_search_label_hides_slug_and_run_id():
    assert friendly_search_label({
        "name": "chemistry-teaching-chennai-remote-class-11-12",
        "professions": ["chemistry teacher", "chemistry tutor"],
        "cities": ["Chennai"],
        "countries": ["IN"],
        "arrangements": ["remote", "onsite"],
    }, fallback="raw-run-id") == "Chemistry teacher or chemistry tutor in Chennai or remote"
    assert dated_search_label(
        {
            "professions": ["chemistry teacher", "chemistry tutor"],
            "cities": ["Chennai"],
            "arrangements": ["remote", "onsite"],
        },
        datetime(2026, 9, 11, 17, 23, tzinfo=timezone.utc),
        "Asia/Kolkata",
    ) == (
        "11 Sep 2026 · 10:53 PM · Chemistry teacher / chemistry tutor · "
        "Chennai / Remote"
    )


def test_public_market_filters_keep_unknowns_when_a_threshold_is_enabled():
    jobs = ({
        "title": "Unknown market evidence",
        "glassdoor_overall_rating": None,
        "glassdoor_wlb_rating": None,
        "estimated_salary_lpa": None,
    },)

    assert filter_kept_jobs(jobs, PublicDashboardFilters()) == jobs
    assert filter_kept_jobs(jobs, PublicDashboardFilters(
        minimum_estimated_salary_lpa=1.0,
    )) == jobs
    assert filter_kept_jobs(jobs, PublicDashboardFilters(
        minimum_estimated_salary_lpa=1.0,
        include_blank_estimated_salary_lpa=False,
    )) == ()


def test_dashboard_hides_profile_links_for_companies_excluded_from_latest_scope():
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, future=True)()
    session.add_all([
        Company(id=1, name="Included"), Company(id=2, name="Excluded"),
    ])
    session.add(DecisionRun(
        id="run-1", since_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        cutoff_at=datetime(2026, 9, 2, tzinfo=timezone.utc), input_timezone="UTC",
        state="selecting", policy_snapshot={}, target_count=0,
    ))
    session.flush()
    first = PublicSelectionScope(
        run_id="run-1", revision=1, mode="manual", fingerprint="first", job_count=2, company_count=2,
    )
    latest = PublicSelectionScope(
        run_id="run-1", revision=2, mode="manual", fingerprint="latest", job_count=1, company_count=1,
    )
    research = PublicSelectionScope(
        run_id="run-1", revision=3, mode="all_eligible", purpose="phase_b_research",
        fingerprint="research", job_count=2, company_count=2,
    )
    session.add_all([first, latest, research])
    session.flush()
    session.add_all([
        PublicSelectionScopeItem(scope_id=first.id, posting_version_id=101, company_id=1),
        PublicSelectionScopeItem(scope_id=first.id, posting_version_id=102, company_id=2),
        PublicSelectionScopeItem(scope_id=latest.id, posting_version_id=103, company_id=1),
    ])
    authorization = ProfileDiscoveryAuthorization(
        run_id="run-1", scope_id=first.id, plan_fingerprint="first-plan", query_plan=[],
        maximum_calls=0, retry_allowance=0, planned_query_count=0,
    )
    session.add(authorization)
    session.flush()
    session.add_all([
        LinkedInProfileLink(
            authorization_id=authorization.id, company_id=1,
            linkedin_url="https://linkedin.com/in/included", full_name="Included", headline=None,
            search_group="data", evidence={},
        ),
        LinkedInProfileLink(
            authorization_id=authorization.id, company_id=2,
            linkedin_url="https://linkedin.com/in/excluded", full_name="Excluded", headline=None,
            search_group="data", evidence={},
        ),
    ])
    session.commit()

    snapshot = load_public_snapshot(session, "run-1")

    assert [link["name"] for link in snapshot.profile_links] == ["Included"]
    assert snapshot.latest_scope == {
        "revision": 2, "mode": "manual", "purpose": "selection",
        "job_count": 1, "company_count": 1,
    }


def test_dashboard_profile_links_include_their_selected_company():
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, future=True)()
    session.add_all([Company(id=1, name="Alpha"), Company(id=2, name="Beta")])
    session.add(DecisionRun(
        id="run-1", since_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        cutoff_at=datetime(2026, 9, 2, tzinfo=timezone.utc), input_timezone="UTC",
        state="selecting", policy_snapshot={}, target_count=0,
    ))
    session.flush()
    scope = PublicSelectionScope(
        run_id="run-1", revision=1, mode="manual", fingerprint="scope", job_count=2, company_count=2,
    )
    session.add(scope)
    session.flush()
    session.add_all([
        PublicSelectionScopeItem(scope_id=scope.id, posting_version_id=101, company_id=1),
        PublicSelectionScopeItem(scope_id=scope.id, posting_version_id=102, company_id=2),
    ])
    authorization = ProfileDiscoveryAuthorization(
        run_id="run-1", scope_id=scope.id, plan_fingerprint="plan", query_plan=[],
        maximum_calls=0, retry_allowance=0, planned_query_count=0,
    )
    session.add(authorization)
    session.flush()
    session.add_all([
        LinkedInProfileLink(
            authorization_id=authorization.id, company_id=1,
            linkedin_url="https://linkedin.com/in/alpha", full_name="Alpha Person", headline=None,
            search_group="data", evidence={"source": "search"},
        ),
        LinkedInProfileLink(
            authorization_id=authorization.id, company_id=2,
            linkedin_url="https://linkedin.com/in/beta", full_name="Beta Person", headline=None,
            search_group="data", evidence={"source": "search"},
        ),
    ])
    session.commit()

    snapshot = load_public_snapshot(session, "run-1")

    assert [
        (link["name"], link["company"], link["linkedin_url"], link["evidence"])
        for link in snapshot.profile_links
    ] == [
        ("Alpha Person", "Alpha", "https://linkedin.com/in/alpha", {"source": "search"}),
        ("Beta Person", "Beta", "https://linkedin.com/in/beta", {"source": "search"}),
    ]
