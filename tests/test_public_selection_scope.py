from datetime import datetime, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.models.orm import (
    Base,
    Company,
    DecisionRun,
    Job,
    JobPostingVersion,
    ProfiledSearchOutcome,
    PublicJobPhaseAEvidence,
    PublicSelectionScope,
    PublicSelectionScopeItem,
)
from app.workflows.selection import (
    SelectionFilterConfig,
    freeze_filtered_selection_scope,
    freeze_selection_scope,
    preview_filtered_selection,
)


def _session():
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True)()


def _run(session):
    session.add(DecisionRun(
        id="run-1",
        since_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        cutoff_at=datetime(2026, 9, 2, tzinfo=timezone.utc),
        input_timezone="UTC",
        state="selecting",
        policy_snapshot={},
        target_count=0,
    ))
    session.flush()
    session.add_all([
        Company(
            id=10, name="One", enrichment_status="done",
            contact_search_groups=["data"],
            enriched_at=datetime(2026, 9, 10, tzinfo=timezone.utc),
        ),
        Company(
            id=20, name="Two", enrichment_status="done",
            contact_search_groups=["data"],
            enriched_at=datetime(2026, 9, 10, tzinfo=timezone.utc),
        ),
    ])
    session.flush()
    for posting_id, company_id in ((1, 10), (2, 10), (3, 20)):
        job = Job(
            id=posting_id, source="fixture", external_job_id=str(posting_id),
            company_id=company_id, company_name_raw=str(company_id),
            title=f"Role {posting_id}",
            location_raw="Remote, India" if posting_id != 2 else "Bengaluru, India",
            is_remote=posting_id != 2,
            seniority="individual_contributor",
            experience_min_years=posting_id + 1,
            posted_at=datetime(2026, 9, posting_id, tzinfo=timezone.utc),
            last_seen_at=datetime(2026, 9, posting_id, tzinfo=timezone.utc),
        )
        session.add(job)
        session.flush()
        session.add(JobPostingVersion(
            id=posting_id, job_id=job.id, canonical_duplicate_root_id=job.id,
            material_content_hash=f"hash-{posting_id}",
            posting_instance_key=f"fixture:{posting_id}", snapshot={},
        ))
        session.flush()
        session.add(PublicJobPhaseAEvidence(
            posting_version_id=posting_id,
            status="done",
            evidence={"skills": [], "experience": {}},
        ))
        session.add(ProfiledSearchOutcome(
            run_id="run-1", source="fixture", external_job_id=str(posting_id),
            outcome="kept", profile_fingerprint="fp", evidence={},
            job_id=job.id, posting_version_id=posting_id,
        ))
    session.flush()


def test_scope_is_repeat_safe_and_a_changed_selection_creates_a_revision():
    session = _session()
    _run(session)

    first = freeze_selection_scope(
        session, run_id="run-1", mode="all_eligible",
        posting_version_ids=[3, 1, 2], company_ids={1: 10, 2: 10, 3: 20},
    )
    repeat = freeze_selection_scope(
        session, run_id="run-1", mode="all_eligible",
        posting_version_ids=[1, 2, 3], company_ids={1: 10, 2: 10, 3: 20},
    )
    revised = freeze_selection_scope(
        session, run_id="run-1", mode="manual",
        posting_version_ids=[2], company_ids={2: 10},
    )

    assert repeat.id == first.id
    assert revised.revision == 2
    assert first.revision == 1
    assert first.job_count == 3
    assert first.company_count == 2
    assert list(session.scalars(select(PublicSelectionScope))) == [first, revised]
    assert [row.posting_version_id for row in session.scalars(
        select(PublicSelectionScopeItem)
        .where(PublicSelectionScopeItem.scope_id == first.id)
        .order_by(PublicSelectionScopeItem.posting_version_id)
    )] == [1, 2, 3]


def test_phase_b_research_scope_is_distinct_from_final_selection():
    session = _session()
    _run(session)

    research = freeze_selection_scope(
        session, run_id="run-1", mode="all_eligible",
        posting_version_ids=[1, 2, 3], company_ids={1: 10, 2: 10, 3: 20},
        purpose="phase_b_research",
    )
    final = freeze_selection_scope(
        session, run_id="run-1", mode="all_eligible",
        posting_version_ids=[1, 2, 3], company_ids={1: 10, 2: 10, 3: 20},
    )

    assert research.purpose == "phase_b_research"
    assert final.purpose == "selection"
    assert final.revision == research.revision + 1


def test_scope_rejects_missing_company_identity():
    session = _session()
    _run(session)
    try:
        freeze_selection_scope(
            session, run_id="run-1", mode="manual",
            posting_version_ids=[1], company_ids={},
        )
    except ValueError as exc:
        assert "company" in str(exc)
    else:
        raise AssertionError("missing company identity was accepted")


def test_scope_rejects_selection_until_job_and_company_phase_a_are_complete():
    session = _session()
    _run(session)
    evidence = session.scalar(select(PublicJobPhaseAEvidence).where(
        PublicJobPhaseAEvidence.posting_version_id == 1,
    ))
    session.delete(evidence)
    session.flush()

    try:
        freeze_selection_scope(
            session, run_id="run-1", mode="manual",
            posting_version_ids=[1], company_ids={1: 10},
        )
    except ValueError as exc:
        assert "Phase A job evidence" in str(exc)
    else:
        raise AssertionError("selection without Phase A job evidence was accepted")

    session.add(PublicJobPhaseAEvidence(
        posting_version_id=1,
        status="done",
        evidence={"skills": [], "experience": {}},
    ))
    session.get(Company, 10).contact_search_groups = None
    session.flush()

    try:
        freeze_selection_scope(
            session, run_id="run-1", mode="manual",
            posting_version_ids=[1], company_ids={1: 10},
        )
    except ValueError as exc:
        assert "Phase A company classification" in str(exc)
    else:
        raise AssertionError("selection without company classification was accepted")


def test_scope_rejects_company_phase_a_older_than_its_newest_selected_posting():
    session = _session()
    _run(session)
    session.get(Company, 10).enriched_at = datetime(2026, 8, 31, tzinfo=timezone.utc)
    session.flush()

    try:
        freeze_selection_scope(
            session, run_id="run-1", mode="manual",
            posting_version_ids=[1, 2], company_ids={1: 10, 2: 10},
        )
    except ValueError as exc:
        assert "Phase A company classification" in str(exc)
    else:
        raise AssertionError("stale company Phase A classification was accepted")


def test_filter_groups_are_or_of_and_exclusions_win_and_unknown_thresholds_are_visible():
    session = _session()
    _run(session)
    session.get(Company, 10).wlb_rating = 4.5
    session.get(Company, 10).glassdoor_review_count = 20
    session.get(Company, 20).wlb_rating = 4.5
    session.get(Company, 20).glassdoor_review_count = 2
    config = SelectionFilterConfig.model_validate({
        "version": 1,
        "groups": [
            {
                "name": "good-remote-company",
                "arrangements": ["remote"],
                "maximum_experience_required_years": 5,
                "minimum_company_wlb_rating": 4.0,
                "minimum_company_wlb_review_count": 10,
            },
            {
                "name": "onsite-explicit-role",
                "arrangements": ["onsite"],
                "title_terms": ["role 2"],
            },
        ],
        "exclusions": {"company_ids": [10]},
    })

    preview = preview_filtered_selection(session, run_id="run-1", configuration=config)

    assert [(item.posting_version_id, item.outcome, item.reasons) for item in preview.items] == [
        (1, "excluded", ("company",)),
        (2, "excluded", ("company",)),
        (3, "unresolved", ("good-remote-company:minimum_company_wlb_review_count",)),
    ]
    assert preview.retained_job_count == 0
    assert preview.excluded_job_count == 2
    assert preview.unresolved_job_count == 1


def test_filter_freeze_saves_canonical_contract_and_exact_kept_manifest():
    session = _session()
    _run(session)
    # A kept outcome for a different run points at a genuine posting version
    # but must never leak into this run's selection.
    session.add(ProfiledSearchOutcome(
        run_id="other-run", source="fixture", external_job_id="3",
        outcome="kept", profile_fingerprint="other", evidence={},
        job_id=3, posting_version_id=3,
    ))
    config = {
        "version": 1,
        "groups": [{"name": "remote", "arrangements": ["remote"]}],
        "explicit_posting_version_ids": [2],
        "exclusions": {"posting_version_ids": [3]},
    }

    scope = freeze_filtered_selection_scope(session, run_id="run-1", configuration=config)
    repeat = freeze_filtered_selection_scope(session, run_id="run-1", configuration=config)

    assert scope.id == repeat.id
    assert scope.mode == "filtered"
    assert scope.filter_snapshot == SelectionFilterConfig.model_validate(config).model_dump(mode="json")
    assert scope.job_count == 2
    assert scope.company_count == 1
    assert [row.posting_version_id for row in session.scalars(
        select(PublicSelectionScopeItem)
        .where(PublicSelectionScopeItem.scope_id == scope.id)
        .order_by(PublicSelectionScopeItem.posting_version_id)
    )] == [1, 2]


def test_filter_contract_rejects_empty_arbitrary_and_invalid_date_rules():
    for bad in (
        {"version": 1},
        {"version": 1, "groups": [{"name": "bad", "arrangements": ["teleport"]}]},
        {"version": 1, "groups": [{
            "name": "bad-dates", "posted_on_or_after": "2026-09-02",
            "posted_on_or_before": "2026-09-01",
        }]},
    ):
        try:
            SelectionFilterConfig.model_validate(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"invalid filter contract was accepted: {bad}")


def test_filter_rejects_an_explicit_pick_outside_the_exact_kept_run():
    session = _session()
    _run(session)

    try:
        preview_filtered_selection(session, run_id="run-1", configuration={
            "version": 1,
            "explicit_posting_version_ids": [999],
        })
    except ValueError as exc:
        assert "not kept results" in str(exc)
    else:
        raise AssertionError("an out-of-run explicit selection was accepted")


def test_missing_seniority_and_unqualified_wlb_are_unresolved_not_rejected():
    session = _session()
    _run(session)
    session.get(Job, 2).seniority = None
    session.get(Company, 10).wlb_rating = 4.5
    session.get(Company, 10).glassdoor_review_count = 20
    session.get(Company, 20).wlb_rating = 4.5
    session.get(Company, 20).glassdoor_review_count = 2

    preview = preview_filtered_selection(session, run_id="run-1", configuration={
        "version": 1,
        "groups": [
            {
                "name": "qualified-role",
                "seniority": ["individual_contributor"],
                "minimum_company_wlb_rating": 4.0,
                "minimum_company_wlb_review_count": 10,
            },
        ],
    })

    assert [(item.posting_version_id, item.outcome, item.reasons) for item in preview.items] == [
        (1, "retained", ("qualified-role",)),
        (2, "unresolved", ("qualified-role:seniority",)),
        (3, "unresolved", ("qualified-role:minimum_company_wlb_review_count",)),
    ]


def test_canonical_duplicate_is_absent_from_filters_and_cannot_be_frozen():
    session = _session()
    _run(session)
    session.get(Job, 3).duplicate_of_job_id = 1

    preview = preview_filtered_selection(session, run_id="run-1", configuration={
        "version": 1,
        "groups": [{"name": "all-remote", "arrangements": ["remote"]}],
    })

    assert [item.posting_version_id for item in preview.items] == [1, 2]
    try:
        freeze_selection_scope(
            session, run_id="run-1", mode="manual",
            posting_version_ids=[3], company_ids={3: 20},
        )
    except ValueError as exc:
        assert "canonical eligible" in str(exc)
    else:
        raise AssertionError("a duplicate job was frozen into a scope")
