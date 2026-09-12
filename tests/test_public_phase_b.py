from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
import pytest
from datetime import datetime, timezone
import threading
from contextlib import contextmanager
import json

from app.models.orm import (
    Base, Company, DecisionRun, Job, JobPostingVersion, PublicPhaseBAuthorization, PublicPhaseBCall,
    PublicPhaseBEvidence, PublicSelectionScope, PublicSelectionScopeItem,
    PublicWorkflowStage,
)
from app.workflows.public_glassdoor_batch import run_reviewed_glassdoor_batch
from app.workflows.public_phase_b import (
    PublicPhaseBBudgetExceeded, authorize_phase_b, complete_phase_b_call,
    execute_phase_b_call, preview_phase_b, reserve_phase_b_call,
)


def _fixture():
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, future=True)()
    session.add(DecisionRun(
        id="run-1", since_at=datetime.now(timezone.utc),
        cutoff_at=datetime.now(timezone.utc), input_timezone="UTC",
        state="optional_stages", policy_snapshot={}, target_count=0,
    ))
    companies = [Company(name=name) for name in ("Selected One", "Selected Two", "Outside")]
    session.add_all(companies); session.flush()
    scope = PublicSelectionScope(
        run_id="run-1", revision=1, mode="manual", fingerprint="scope",
        job_count=2, company_count=2,
    )
    session.add(scope); session.flush()
    for index, company in enumerate(companies[:2], start=1):
        job = Job(source="fixture", external_job_id=str(index), company_id=company.id,
                  company_name_raw=company.name, title="Analyst")
        session.add(job); session.flush()
        version = JobPostingVersion(
            job_id=job.id, canonical_duplicate_root_id=job.id,
            material_content_hash=f"hash-{index}", posting_instance_key=f"fixture:{index}", snapshot={},
        )
        session.add(version); session.flush()
        session.add(PublicSelectionScopeItem(
            scope_id=scope.id, posting_version_id=version.id, company_id=company.id,
        ))
    session.commit()
    return session, scope, companies


def test_phase_b_plan_is_exact_scope_and_reuses_existing_source_evidence():
    session, scope, companies = _fixture()
    companies[0].market_profile_evidence = {"sources": {"glassdoor": {"status": "ok", "url": "https://g"}}}
    session.commit()

    preview = preview_phase_b(session, scope_id=scope.id, sources=["ambitionbox", "glassdoor"])

    assert preview.company_count == 2
    assert preview.source_count == 2
    assert preview.reusable_evidence_count == 1
    assert preview.planned_call_count == 3
    assert {(item.company_id, item.source) for item in preview.items} == {
        (companies[0].id, "glassdoor"), (companies[0].id, "ambitionbox"),
        (companies[1].id, "glassdoor"), (companies[1].id, "ambitionbox"),
    }
    assert all(item.company_id != companies[2].id for item in preview.items)


def test_reviewed_glassdoor_batch_deduplicates_aliases_and_checkpoints_snapshot(tmp_path):
    session, scope, companies = _fixture()
    authorization = authorize_phase_b(
        session, scope_id=scope.id, sources=["glassdoor"], maximum_calls=1,
    )
    session.commit()
    artifact = tmp_path / "reviewed.json"
    artifact.write_text(json.dumps({
        "schema_version": 1,
        "resolutions": [{
            "company_id": company.id,
            "company_name": company.name,
            "status": "accepted",
            "employer_id": "123",
            "overview_url": "https://www.glassdoor.co.in/Overview/x-EI_IE123.11,12.htm",
            "observed_name": "Selected",
        } for company in companies[:2]],
    }), encoding="utf-8")

    factory = sessionmaker(bind=session.get_bind(), future=True)

    @contextmanager
    def session_context():
        child = factory()
        try:
            yield child
            child.commit()
        except Exception:
            child.rollback()
            raise
        finally:
            child.close()

    class FakeClient:
        calls = 0

        def collect(self, resolutions, *, snapshot_id=None, snapshot_callback=None):
            self.calls += 1
            assert len(resolutions) == 1
            assert snapshot_id is None
            snapshot_callback("snapshot-1", 1)
            return {}

    client = FakeClient()
    checkpoint = tmp_path / "checkpoint.json"
    result = run_reviewed_glassdoor_batch(
        run_id="run-1", authorization_id=authorization.id,
        reviewed_path=artifact, checkpoint_path=checkpoint,
        session_context=session_context, client=client,
    )

    assert result == {
        "reviewed": 2,
        "accepted_companies": 2,
        "unresolved_companies": 0,
        "unique_bright_data_inputs": 1,
        "completed": 2,
        "failed": 0,
        "checkpoint": str(checkpoint),
    }
    assert client.calls == 1
    assert json.loads(checkpoint.read_text())["active_snapshot_id"] == "snapshot-1"
    check = factory()
    try:
        calls = check.scalars(select(PublicPhaseBCall)).all()
        assert len(calls) == 1
        assert calls[0].status == "missing"
        assert all(
            check.get(Company, company.id).glassdoor_employer_id == "123"
            for company in companies[:2]
        )
        assert check.get(Company, companies[1].id).market_profile_evidence[
            "sources"
        ]["glassdoor"]["reused_from_company_id"] == companies[0].id
        stage = check.scalar(select(PublicWorkflowStage).where(
            PublicWorkflowStage.run_id == "run-1",
            PublicWorkflowStage.name == "phase_b",
        ))
        assert (stage.status, stage.completed_count, stage.expected_count) == (
            "completed", 2, 2,
        )
    finally:
        check.close()

    # Completed checkpoints are idempotent and never trigger a replacement snapshot.
    again = run_reviewed_glassdoor_batch(
        run_id="run-1", authorization_id=authorization.id,
        reviewed_path=artifact, checkpoint_path=checkpoint,
        session_context=session_context, client=client,
    )
    assert again == result
    assert client.calls == 1


def test_authorization_reservation_ceiling_and_terminal_market_values_are_durable():
    session, scope, companies = _fixture()
    authorization = authorize_phase_b(
        session, scope_id=scope.id, sources=["glassdoor", "ambitionbox"], maximum_calls=2,
    )
    first = reserve_phase_b_call(
        session, authorization_id=authorization.id, company_id=companies[0].id, source="glassdoor",
    )
    session.commit()
    completed = complete_phase_b_call(session, call_id=first.call.id, result={
        "company_id": companies[0].id, "source": "glassdoor", "status": "ok",
        "evidence": {"observed_name": "Selected One"}, "evidence_url": "https://glassdoor.example/one",
        "overall_rating": 4.2, "wlb_rating": 4.0, "review_count": 123,
    })
    session.commit()
    second = reserve_phase_b_call(
        session, authorization_id=authorization.id, company_id=companies[0].id, source="ambitionbox",
    )
    session.commit()
    complete_phase_b_call(session, call_id=second.call.id, result={
        "company_id": companies[0].id, "source": "ambitionbox", "status": "ok",
        "evidence": {"selected_role": "data analyst"}, "estimated_salary_lpa": 18.5,
    })
    session.commit()

    company = session.get(Company, companies[0].id)
    assert completed.status == "succeeded"
    assert company.glassdoor_wlb_rating == 4.0
    assert company.glassdoor_review_count == 123
    assert company.ambitionbox_estimated_salary_lpa == 18.5
    assert company.estimated_salary_lpa == 18.5
    assert company.market_profile_evidence["sources"]["glassdoor"]["url"] == "https://glassdoor.example/one"
    assert session.scalar(select(PublicPhaseBEvidence).where(PublicPhaseBEvidence.call_id == first.call.id)).status == "ok"
    assert session.get(PublicPhaseBAuthorization, authorization.id).reserved_call_count == 2
    with pytest.raises(PublicPhaseBBudgetExceeded, match="2/2"):
        reserve_phase_b_call(
            session, authorization_id=authorization.id, company_id=companies[1].id, source="glassdoor",
        )


def test_file_sqlite_concurrent_distinct_phase_b_reservations_cannot_exceed_ceiling(tmp_path):
    """SQLite ignores FOR UPDATE, so the authorization row is the semaphore."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'phase-b-budget.sqlite3'}", future=True,
        connect_args={"timeout": 5},
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, future=True)
    seed = sessions()
    # Reuse the normal fixture shape, but persist it in this real file database.
    seed.add(DecisionRun(
        id="run-1", since_at=datetime.now(timezone.utc),
        cutoff_at=datetime.now(timezone.utc), input_timezone="UTC",
        state="optional_stages", policy_snapshot={}, target_count=0,
    ))
    companies = [Company(name=name) for name in ("Selected One", "Selected Two")]
    seed.add_all(companies); seed.flush()
    scope = PublicSelectionScope(
        run_id="run-1", revision=1, mode="manual", fingerprint="scope",
        job_count=2, company_count=2,
    )
    seed.add(scope); seed.flush()
    for index, company in enumerate(companies, start=1):
        job = Job(source="fixture", external_job_id=str(index), company_id=company.id,
                  company_name_raw=company.name, title="Analyst")
        seed.add(job); seed.flush()
        version = JobPostingVersion(
            job_id=job.id, canonical_duplicate_root_id=job.id,
            material_content_hash=f"hash-{index}", posting_instance_key=f"fixture:{index}", snapshot={},
        )
        seed.add(version); seed.flush()
        seed.add(PublicSelectionScopeItem(
            scope_id=scope.id, posting_version_id=version.id, company_id=company.id,
        ))
    authorization = authorize_phase_b(
        seed, scope_id=scope.id, sources=["glassdoor"], maximum_calls=1,
    )
    authorization_id = authorization.id
    company_ids = [company.id for company in companies]
    seed.commit()
    seed.close()

    barrier = threading.Barrier(2)
    outcomes: list[str] = []
    outcome_lock = threading.Lock()

    def reserve(company_id):
        session = sessions()
        try:
            barrier.wait()
            reserve_phase_b_call(
                session, authorization_id=authorization_id, company_id=company_id, source="glassdoor",
            )
            session.commit()
            outcome = "reserved"
        except PublicPhaseBBudgetExceeded:
            session.rollback()
            outcome = "exhausted"
        finally:
            session.close()
        with outcome_lock:
            outcomes.append(outcome)

    threads = [threading.Thread(target=reserve, args=(company_id,)) for company_id in company_ids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    check = sessions()
    try:
        assert sorted(outcomes) == ["exhausted", "reserved"]
        assert check.query(PublicPhaseBCall).count() == 1
        assert check.get(PublicPhaseBAuthorization, authorization_id).reserved_call_count == 1
    finally:
        check.close()


def test_execution_commits_reservation_before_provider_and_resume_is_repeat_safe():
    session, scope, companies = _fixture()
    authorization = authorize_phase_b(
        session, scope_id=scope.id, sources=["glassdoor"], maximum_calls=1,
    )
    observed = []

    def provider(company_id, source):
        call = session.scalar(select(PublicPhaseBCall).where(
            PublicPhaseBCall.authorization_id == authorization.id,
            PublicPhaseBCall.company_id == company_id,
            PublicPhaseBCall.source == source,
        ))
        observed.append(call.status)
        return {
            "company_id": company_id, "source": source, "status": "missing",
            "evidence": {"reason": "not found"},
        }

    first = execute_phase_b_call(
        session, authorization_id=authorization.id, company_id=companies[0].id,
        source="glassdoor", provider=provider,
    )
    repeated = execute_phase_b_call(
        session, authorization_id=authorization.id, company_id=companies[0].id,
        source="glassdoor", provider=provider,
    )

    assert observed == ["reserved"]
    assert first.id == repeated.id
    assert repeated.status == "missing"
    assert session.query(PublicPhaseBCall).count() == 1
    assert session.query(PublicPhaseBEvidence).count() == 1


def test_phase_b_rejects_outside_plan_and_mismatched_terminal_evidence():
    session, scope, companies = _fixture()
    authorization = authorize_phase_b(
        session, scope_id=scope.id, sources=["glassdoor"], maximum_calls=1,
    )
    with pytest.raises(ValueError, match="outside the approved"):
        reserve_phase_b_call(
            session, authorization_id=authorization.id, company_id=companies[2].id, source="glassdoor",
        )
    reservation = reserve_phase_b_call(
        session, authorization_id=authorization.id, company_id=companies[0].id, source="glassdoor",
    )
    with pytest.raises(ValueError, match="does not match"):
        complete_phase_b_call(session, call_id=reservation.call.id, result={
            "company_id": companies[1].id, "source": "glassdoor", "status": "missing",
        })
