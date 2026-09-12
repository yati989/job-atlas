from datetime import datetime, timezone
import threading

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.contacts import ladder as L
from app.contacts.agentic_batch import CompanyContext
from app.contacts.profile_query_budget import (
    ProfileBudgetExceeded,
    authorize_profile_queries,
    complete_profile_query,
    preview_profile_queries,
    reserve_profile_query,
)
from app.models.orm import (
    Base, Company, DecisionRun, ProfileDiscoveryAuthorization, ProfileDiscoveryCall,
    PublicSelectionScope, PublicSelectionScopeItem, PublicWorkflowStage,
)


def _session():
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True)()


def _scope_and_context(session):
    company = Company(
        name="Example Co", company_type="employer",
        contact_search_groups=[L.DATA_AI],
    )
    run = DecisionRun(
        id="run-1", since_at=datetime.now(timezone.utc),
        cutoff_at=datetime.now(timezone.utc), input_timezone="UTC",
        state="optional_stages", policy_snapshot={}, target_count=0,
    )
    session.add_all([company, run])
    session.flush()
    scope = PublicSelectionScope(
        run_id=run.id, revision=1, mode="all_eligible", fingerprint="scope",
        job_count=1, company_count=1,
    )
    session.add(scope)
    session.flush()
    session.add(PublicSelectionScopeItem(
        scope_id=scope.id, posting_version_id=999, company_id=company.id,
    ))
    context = CompanyContext(
        company_id=company.id, name=company.name, company_type="employer",
        canonical_domain=None, industry=None, hq_location=None,
        search_groups=[L.DATA_AI],
        ladder={L.DATA_AI: L.ladder_for("employer", L.DATA_AI)},
        quotas=L.quotas_for("employer"),
    )
    return scope, context


def test_preview_and_authorization_reuse_existing_contact_search_plan():
    session = _session()
    scope, context = _scope_and_context(session)
    preview = preview_profile_queries([context])
    authorization = authorize_profile_queries(
        session, run_id="run-1", scope_id=scope.id,
        contexts=[context], maximum_calls=6,
    )

    assert preview.company_count == 1
    assert preview.initial_query_count == 4
    assert preview.retry_allowance == 4
    assert preview.planned_query_count == 8
    assert authorization.query_plan == [item.as_dict() for item in preview.queries]
    assert authorization.maximum_calls == 6


def test_profile_authorization_rejects_phase_b_research_scope():
    session = _session()
    scope, context = _scope_and_context(session)
    scope.purpose = "phase_b_research"

    with pytest.raises(ValueError, match="final selection scope"):
        authorize_profile_queries(
            session, run_id="run-1", scope_id=scope.id,
            contexts=[context], maximum_calls=0,
        )


def test_exhausted_zero_result_search_records_a_plain_language_blocker():
    session = _session()
    scope, context = _scope_and_context(session)
    preview = preview_profile_queries([context])
    authorization = authorize_profile_queries(
        session, run_id="run-1", scope_id=scope.id,
        contexts=[context], maximum_calls=1,
    )
    reservation = reserve_profile_query(session, authorization.id, preview.queries[0])

    complete_profile_query(session, reservation.call.id, result_count=0)

    stage = session.scalar(select(PublicWorkflowStage).where(
        PublicWorkflowStage.run_id == "run-1",
        PublicWorkflowStage.name == "profile_links",
    ))
    assert stage.status == "partial"
    assert stage.detail["blocker"] == (
        "No usable profile results were returned within the approved 1-call limit"
    )


def test_ceiling_counts_ambiguous_calls_and_reservations_are_repeat_safe():
    session = _session()
    scope, context = _scope_and_context(session)
    preview = preview_profile_queries([context])
    authorization = authorize_profile_queries(
        session, run_id="run-1", scope_id=scope.id,
        contexts=[context], maximum_calls=2,
    )
    session.commit()

    first = reserve_profile_query(session, authorization.id, preview.queries[0])
    session.commit()
    complete_profile_query(session, first.call.id, error="provider outcome unknown")
    session.commit()
    repeated = reserve_profile_query(session, authorization.id, preview.queries[0])
    second = reserve_profile_query(session, authorization.id, preview.queries[1])
    session.commit()

    assert first.created is True
    assert repeated.created is False
    assert repeated.call.id == first.call.id
    assert repeated.call.status == "ambiguous"
    assert second.call.status == "started"
    assert session.scalar(select(func.count(ProfileDiscoveryCall.id))) == 2
    assert session.get(ProfileDiscoveryAuthorization, authorization.id).reserved_call_count == 2
    with pytest.raises(ProfileBudgetExceeded, match="2/2"):
        reserve_profile_query(session, authorization.id, preview.queries[2])


def test_file_sqlite_concurrent_distinct_profile_reservations_cannot_exceed_ceiling(tmp_path):
    """The durable conditional increment, not FOR UPDATE, owns the ceiling."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'profile-budget.sqlite3'}", future=True,
        connect_args={"timeout": 5},
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, future=True)
    seed = sessions()
    scope, context = _scope_and_context(seed)
    preview = preview_profile_queries([context])
    authorization = authorize_profile_queries(
        seed, run_id="run-1", scope_id=scope.id, contexts=[context], maximum_calls=1,
    )
    authorization_id = authorization.id
    seed.commit()
    seed.close()

    barrier = threading.Barrier(2)
    outcomes: list[str] = []
    outcome_lock = threading.Lock()

    def reserve(query):
        session = sessions()
        try:
            barrier.wait()
            reserve_profile_query(session, authorization_id, query)
            session.commit()
            outcome = "reserved"
        except ProfileBudgetExceeded:
            session.rollback()
            outcome = "exhausted"
        finally:
            session.close()
        with outcome_lock:
            outcomes.append(outcome)

    threads = [threading.Thread(target=reserve, args=(query,)) for query in preview.queries[:2]]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    check = sessions()
    try:
        assert sorted(outcomes) == ["exhausted", "reserved"]
        assert check.scalar(select(func.count(ProfileDiscoveryCall.id))) == 1
        assert check.get(ProfileDiscoveryAuthorization, authorization_id).reserved_call_count == 1
    finally:
        check.close()


def test_query_outside_frozen_plan_is_rejected():
    session = _session()
    scope, context = _scope_and_context(session)
    preview = preview_profile_queries([context])
    authorization = authorize_profile_queries(
        session, run_id="run-1", scope_id=scope.id,
        contexts=[context], maximum_calls=1,
    )

    with pytest.raises(ValueError, match="outside the approved query plan"):
        reserve_profile_query(
            session, authorization.id,
            preview.queries[0].with_query("invented query"),
        )


def test_new_scope_reuses_successful_exact_queries_from_same_run():
    session = _session()
    first_scope, context = _scope_and_context(session)
    first_preview = preview_profile_queries([context])
    first = authorize_profile_queries(
        session, run_id="run-1", scope_id=first_scope.id,
        contexts=[context], maximum_calls=1,
    )
    reservation = reserve_profile_query(session, first.id, first_preview.queries[0])
    complete_profile_query(session, reservation.call.id, result_count=2)
    second_scope = PublicSelectionScope(
        run_id="run-1", revision=2, mode="manual", fingerprint="scope-2",
        job_count=1, company_count=1,
    )
    session.add(second_scope); session.flush()
    session.add(PublicSelectionScopeItem(
        scope_id=second_scope.id, posting_version_id=1000,
        company_id=context.company_id,
    ))
    session.flush()

    preview = preview_profile_queries([context], session=session, run_id="run-1")
    second = authorize_profile_queries(
        session, run_id="run-1", scope_id=second_scope.id,
        contexts=[context], maximum_calls=1,
    )

    assert preview.reused_query_count == 1
    assert preview.planned_query_count == first_preview.planned_query_count - 1
    assert first_preview.queries[0].as_dict() not in second.query_plan
