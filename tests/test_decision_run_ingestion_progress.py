import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.decision_runs.connector_progress import DecisionRunConnectorProgress
from app.decision_runs.ingestion_funnel import report_ingestion_funnel
from app.decision_runs.progress import StageCounts, finish_stage, read_run_progress, start_stage
from app.db import session as db_session
from app.models.orm import Base, DecisionRun
from app.models.schemas import NormalizedJob
from app.pipeline.runner import FetchResult
from app.pipeline.runner import FetchObservation, GateOutcome, IngestionGateFacts
from app.pipeline.progress import load_progress
from app.pipeline.upsert import upsert_job
from scripts import run_full_pipeline


class Connector:
    source_name = "fixture"


def session_factory():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, future=True)
    with maker.begin() as session:
        session.add(DecisionRun(
            id="run-1",
            since_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
            cutoff_at=datetime(2026, 8, 2, tzinfo=timezone.utc),
            input_timezone="UTC",
            state="preparing",
            policy_snapshot={},
            target_count=1,
        ))

    @contextmanager
    def factory():
        with maker.begin() as session:
            yield session

    return maker, factory


def test_connector_projection_updates_durable_fetch_stage(tmp_path):
    maker, factory = session_factory()
    connectors = [Connector(), Connector()]
    progress = DecisionRunConnectorProgress(
        connectors,
        run_id="run-1",
        path=tmp_path / "latest.json",
        session_factory=factory,
        process_id=42,
    )

    progress.instance_started(connectors[0], attempt=1)
    progress.instance_finished(FetchResult(connectors[0], "fixture", jobs=[]))
    with maker() as session:
        stage = read_run_progress(session, "run-1", process_is_alive=lambda _: True).stages[0]
        assert (stage.status, stage.processed_count, stage.expected_count) == ("running", 1, 2)

    progress.instance_started(connectors[1], attempt=1)
    progress.instance_finished(FetchResult(connectors[1], "fixture", jobs=[]))
    progress.complete([])
    with maker() as session:
        stage = read_run_progress(session, "run-1").stages[0]
        assert (stage.status, stage.processed_count, stage.attempt_number) == ("completed", 2, 1)


def test_connector_collection_can_finish_before_durable_fetch_handoff(tmp_path):
    maker, factory = session_factory()
    connector = Connector()
    path = tmp_path / "latest.json"
    progress = DecisionRunConnectorProgress(
        [connector], run_id="run-1", path=path,
        session_factory=factory, process_id=42, heartbeat_interval_s=None,
    )
    progress.instance_started(connector, attempt=1)
    progress.instance_finished(FetchResult(connector, "fixture", jobs=[]))

    progress.finish()

    assert load_progress(path)["status"] == "completed"
    with maker() as session:
        stage = read_run_progress(
            session, "run-1", project_interruptions=False,
        ).stages[0]
        assert (stage.status, stage.processed_count, stage.expected_count) == (
            "running", 1, 1,
        )


def test_connector_failures_reconcile_as_completed_with_errors_and_keep_identifiers(tmp_path):
    maker, factory = session_factory()
    connector = Connector()
    progress = DecisionRunConnectorProgress(
        [connector], run_id="run-1", path=tmp_path / "latest.json",
        session_factory=factory, process_id=42,
    )
    progress.instance_started(connector, attempt=1)
    progress.instance_finished(FetchResult(
        connector, "fixture", jobs=[], error=TimeoutError("slow"),
    ))
    progress.complete([])

    with maker() as session:
        stage = read_run_progress(session, "run-1").stages[0]
        assert stage.status == "completed_with_errors"
        assert stage.counts.failed == 1
        assert stage.reason_counts == {"TimeoutError": 1}
        assert stage.attempts[0].records[0].record_id == "fixture-001"
        assert stage.attempts[0].records[0].reason == "TimeoutError"


def test_fetch_completion_rolls_back_when_its_durable_handoff_fails(tmp_path):
    maker, factory = session_factory()
    connector = Connector()
    progress = DecisionRunConnectorProgress(
        [connector], run_id="run-1", path=tmp_path / "latest.json",
        session_factory=factory, process_id=42, heartbeat_interval_s=None,
    )
    progress.instance_started(connector, attempt=1)
    progress.instance_finished(FetchResult(connector, "fixture", jobs=[]))

    def fail_handoff(_session):
        raise RuntimeError("funnel persistence failed")

    with pytest.raises(RuntimeError, match="funnel persistence failed"):
        progress.complete([], after_finish=fail_handoff)

    with maker() as session:
        stage = read_run_progress(
            session, "run-1", project_interruptions=False,
        ).stages[0]
        assert stage.status == "running"
        assert [transition.to_status for transition in stage.transitions] == [
            "running",
        ]
        assert [(attempt.attempt_number, attempt.status) for attempt in stage.attempts] == [
            (1, "running"),
        ]


def test_ingest_uses_named_decision_run_window_and_progress(monkeypatch, tmp_path):
    since = datetime(2026, 8, 1, tzinfo=timezone.utc)
    cutoff = datetime(2026, 8, 2, tzinfo=timezone.utc)
    captured = {"order": []}
    maker, durable_session_factory = session_factory()
    with maker.begin() as session:
        start_stage(
            session, "run-1", "fetch_jobs", expected_count=0, process_id=999999,
            reason="interrupted collection", now=datetime.now(timezone.utc) - timedelta(minutes=10),
        )

    class ResumingProgress:
        snapshot = {}
        def __init__(self, connectors, **kwargs):
            captured.update(kwargs)
            with durable_session_factory() as session:
                start_stage(session, kwargs["run_id"], "fetch_jobs", expected_count=len(connectors), reason="resuming fetch")
        def complete(self, _summary, *, after_finish=None):
            captured["order"].append("fetch_completed")
            with durable_session_factory() as session:
                finish_stage(session, captured["run_id"], "fetch_jobs")
                if after_finish is not None:
                    after_finish(session)
        def fail(self, _reason):
            raise AssertionError("the fixture ingest must not fail")

    monkeypatch.setattr(run_full_pipeline, "LOG_ROOT", tmp_path)
    monkeypatch.setattr(run_full_pipeline, "ACTIVE_CONNECTORS", [])
    monkeypatch.setattr(run_full_pipeline, "_decision_run_window", lambda run_id: (since, cutoff))
    monkeypatch.setattr(db_session, "get_session", durable_session_factory)
    monkeypatch.setattr(run_full_pipeline, "DecisionRunConnectorProgress", ResumingProgress)
    monkeypatch.setattr(run_full_pipeline, "apply_sync_window", lambda connectors, since_at, cutoff_at: type("Window", (), {"since_at": since_at, "native_days": 1})())
    monkeypatch.setattr(run_full_pipeline, "enforce_workload_bounds", lambda connectors: None)
    monkeypatch.setattr(run_full_pipeline, "IncrementalRunPersister", lambda cutoff_at: type("Persister", (), {"persist": lambda self, result: None, "persist_remaining": lambda self, results: None})())
    monkeypatch.setattr(run_full_pipeline, "fetch_all", lambda *args, **kwargs: [])
    monkeypatch.setattr(run_full_pipeline, "retry_failed", lambda *args, **kwargs: None)
    def gate(*args, **kwargs):
        kwargs["outcome_sink"](object())
        return [], 0, 0
    monkeypatch.setattr(run_full_pipeline, "gate_and_upsert", gate)
    def report_funnel(*args, **kwargs):
        captured["order"].append("funnel_started")
        return ()
    monkeypatch.setattr(run_full_pipeline, "report_ingestion_funnel", report_funnel)
    monkeypatch.setattr(run_full_pipeline, "report_canonical_jobs", lambda *args, **kwargs: None)
    monkeypatch.setattr(run_full_pipeline, "get_session", durable_session_factory)
    monkeypatch.setattr(run_full_pipeline, "log_summary", lambda summary: None)
    monkeypatch.setattr(
        run_full_pipeline, "_send_connector_completion_report",
        lambda *_args: captured["order"].append("connector_email"),
    )
    monkeypatch.setattr(
        run_full_pipeline, "run_job_dedup",
        lambda dry_run: captured["order"].append("job_dedup") or {"scanned": 0, "flagged": 0},
    )
    monkeypatch.setattr(run_full_pipeline, "run_company_dedup", lambda apply: {"tier1_merged": 0, "tier2_groups": 0})

    run_full_pipeline.cmd_ingest(argparse.Namespace(run_id="run-1", headless_only=True))

    assert captured["run_id"] == "run-1"
    assert captured["order"] == [
        "fetch_completed", "funnel_started", "connector_email", "job_dedup",
    ]
    with maker() as session:
        stage = read_run_progress(session, "run-1", project_interruptions=False).stages[0]
        assert [(attempt.attempt_number, attempt.status) for attempt in stage.attempts] == [
            (1, "interrupted"), (2, "completed"),
        ]
    meta = run_full_pipeline._load_meta(run_full_pipeline._canonical_run_dir(cutoff.date().isoformat(), "run-1"))
    assert meta["run_id"] == "run-1"
    assert meta["since"] == since.isoformat()
    assert not (tmp_path / "full_pipeline_state.json").exists()


def test_ingest_fetches_linkedin_with_live_sources_then_joins_before_gate(
    monkeypatch, tmp_path,
):
    """LinkedIn's deterministic location work must not serialize collection."""
    since = datetime(2026, 8, 1, tzinfo=timezone.utc)
    cutoff = datetime(2026, 8, 2, tzinfo=timezone.utc)
    maker, durable_session_factory = session_factory()
    linkedin = type("LinkedIn", (), {"source_name": "linkedin"})()
    fixture = Connector()
    linkedin_result = FetchResult(linkedin, "linkedin", jobs=[])
    fixture_result = FetchResult(fixture, "fixture", jobs=[])
    captured = {"events": [], "fetch_calls": []}

    class Progress:
        snapshot = {"status": "completed"}

        def __init__(self, connectors, **kwargs):
            captured["progress_connectors"] = connectors
            captured["progress_kwargs"] = kwargs

        def complete(self, _summary, *, after_finish=None):
            captured["events"].append("progress_complete")
            with durable_session_factory() as session:
                if after_finish is not None:
                    after_finish(session)

        def fail(self, reason):
            pytest.fail(reason)

    class Persister:
        def __init__(self, **_kwargs):
            pass

        def persist(self, result):
            captured["events"].append(("persist", result.source))

        def persist_remaining(self, results):
            captured["events"].append(("persist_remaining", tuple(
                result.source for result in results
            )))

    monkeypatch.setattr(run_full_pipeline, "LOG_ROOT", tmp_path)
    monkeypatch.setattr(run_full_pipeline, "ACTIVE_CONNECTORS", [linkedin, fixture])
    monkeypatch.setattr(run_full_pipeline, "_decision_run_window", lambda _run_id: (since, cutoff))
    monkeypatch.setattr(db_session, "get_session", durable_session_factory)
    monkeypatch.setattr(run_full_pipeline, "get_session", durable_session_factory)
    monkeypatch.setattr(run_full_pipeline, "DecisionRunConnectorProgress", Progress)
    monkeypatch.setattr(
        run_full_pipeline, "apply_sync_window",
        lambda connectors, since_at, cutoff_at: type(
            "Window", (), {"since_at": since_at, "native_days": 1},
        )(),
    )
    monkeypatch.setattr(run_full_pipeline, "enforce_workload_bounds", lambda _connectors: None)
    monkeypatch.setattr(run_full_pipeline, "IncrementalRunPersister", Persister)

    def fetch(connectors, **kwargs):
        captured["fetch_calls"].append(tuple(connectors))
        captured["events"].append("fetch")
        assert kwargs["progress"] is not None
        kwargs["on_result"](linkedin_result)
        kwargs["on_result"](fixture_result)
        return [linkedin_result, fixture_result]

    monkeypatch.setattr(run_full_pipeline, "fetch_all", fetch)
    monkeypatch.setattr(
        run_full_pipeline, "retry_failed",
        lambda results, **_kwargs: captured["events"].append((
            "retry", tuple(result.source for result in results),
        )),
    )
    monkeypatch.setattr(
        run_full_pipeline, "write_linkedin_location_stage",
        lambda **kwargs: captured.update(location_stage=kwargs)
        or captured["events"].append("location_written")
        or {"eligible": 0, "reused": 0, "hydrated": 0, "failed": 0},
    )
    monkeypatch.setattr(
        run_full_pipeline, "apply_stage_results",
        lambda **kwargs: captured["events"].append("location_applied") or 0,
    )

    def gate(results, **kwargs):
        captured["events"].append("gate")
        assert captured["events"].index("location_written") < captured["events"].index("gate")
        assert captured["events"].index("location_applied") < captured["events"].index("gate")
        assert results == [linkedin_result, fixture_result]
        kwargs["outcome_sink"](object())
        return [], 0, 0

    monkeypatch.setattr(run_full_pipeline, "gate_and_upsert", gate)
    monkeypatch.setattr(run_full_pipeline, "report_ingestion_funnel", lambda *_args: ())
    monkeypatch.setattr(run_full_pipeline, "report_canonical_jobs", lambda *_args: None)
    monkeypatch.setattr(run_full_pipeline, "log_summary", lambda _summary: None)
    monkeypatch.setattr(run_full_pipeline, "_send_connector_completion_report", lambda *_args: None)
    monkeypatch.setattr(run_full_pipeline, "run_job_dedup", lambda dry_run: {"scanned": 0, "flagged": 0})
    monkeypatch.setattr(run_full_pipeline, "run_company_dedup", lambda apply: {"tier1_merged": 0, "tier2_groups": 0})

    run_full_pipeline.cmd_ingest(argparse.Namespace(run_id="run-1", headless_only=True))

    assert captured["fetch_calls"] == [(linkedin, fixture)]
    assert captured["progress_connectors"] == [linkedin, fixture]
    assert captured["location_stage"]["results"] == [linkedin_result]
    assert captured["location_stage"]["connectors"] == [linkedin]


def test_ingest_reuses_complete_linkedin_checkpoint_without_second_fetch(
    monkeypatch, tmp_path,
):
    since = datetime(2026, 8, 1, tzinfo=timezone.utc)
    cutoff = datetime(2026, 8, 2, tzinfo=timezone.utc)
    _maker, durable_session_factory = session_factory()
    linkedin = type("LinkedIn", (), {"source_name": "linkedin"})()
    fixture = Connector()
    frozen = FetchResult(linkedin, "linkedin", jobs=[])
    live = FetchResult(fixture, "fixture", jobs=[])
    captured = {"fetch_calls": []}

    class Progress:
        snapshot = {"status": "completed"}

        def __init__(self, _connectors, **_kwargs):
            pass

        def instance_started(self, connector, *, attempt):
            captured["replayed"] = (connector, attempt)

        def instance_finished(self, result):
            captured["replayed_result"] = result

        def complete(self, _summary, *, after_finish=None):
            with durable_session_factory() as session:
                if after_finish is not None:
                    after_finish(session)

        def fail(self, reason):
            pytest.fail(reason)

    class Persister:
        def __init__(self, **_kwargs):
            pass

        def persist(self, _result):
            pass

        def persist_remaining(self, _results):
            pass

    monkeypatch.setattr(run_full_pipeline, "LOG_ROOT", tmp_path)
    monkeypatch.setattr(run_full_pipeline, "ACTIVE_CONNECTORS", [linkedin, fixture])
    monkeypatch.setattr(run_full_pipeline, "_decision_run_window", lambda _run_id: (since, cutoff))
    monkeypatch.setattr(db_session, "get_session", durable_session_factory)
    monkeypatch.setattr(run_full_pipeline, "get_session", durable_session_factory)
    monkeypatch.setattr(run_full_pipeline, "DecisionRunConnectorProgress", Progress)
    monkeypatch.setattr(
        run_full_pipeline, "apply_sync_window",
        lambda _connectors, since_at, cutoff_at: type(
            "Window", (), {"since_at": since_at, "native_days": 1},
        )(),
    )
    monkeypatch.setattr(run_full_pipeline, "enforce_workload_bounds", lambda _connectors: None)
    monkeypatch.setattr(run_full_pipeline, "IncrementalRunPersister", Persister)
    monkeypatch.setattr(run_full_pipeline, "load_fetch_results", lambda *_args: [frozen])
    monkeypatch.setattr(
        run_full_pipeline, "apply_stage_results",
        lambda **kwargs: captured.update(applied=kwargs["results"]) or 0,
    )
    monkeypatch.setattr(
        run_full_pipeline, "write_linkedin_location_stage",
        lambda **_kwargs: pytest.fail("complete checkpoint must not be rewritten"),
    )

    def fetch(connectors, **_kwargs):
        captured["fetch_calls"].append(tuple(connectors))
        return [live]

    monkeypatch.setattr(run_full_pipeline, "fetch_all", fetch)
    monkeypatch.setattr(run_full_pipeline, "retry_failed", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        run_full_pipeline, "gate_and_upsert",
        lambda results, **kwargs: (
            kwargs["outcome_sink"](object()) or ([], 0, 0)
            if results == [frozen, live]
            else pytest.fail("gate must receive frozen LinkedIn plus live results")
        ),
    )
    monkeypatch.setattr(run_full_pipeline, "report_ingestion_funnel", lambda *_args: ())
    monkeypatch.setattr(run_full_pipeline, "report_canonical_jobs", lambda *_args: None)
    monkeypatch.setattr(run_full_pipeline, "log_summary", lambda _summary: None)
    monkeypatch.setattr(run_full_pipeline, "_send_connector_completion_report", lambda *_args: None)
    monkeypatch.setattr(run_full_pipeline, "run_job_dedup", lambda dry_run: {"scanned": 0, "flagged": 0})
    monkeypatch.setattr(run_full_pipeline, "run_company_dedup", lambda apply: {"tier1_merged": 0, "tier2_groups": 0})

    run_dir = run_full_pipeline._canonical_run_dir(cutoff.date().isoformat(), "run-1")
    for filename in (
        "linkedin-location-fetch.json",
        "linkedin-location-input.jsonl",
        "linkedin-location-results.jsonl",
    ):
        (run_dir / filename).write_text("{}", encoding="utf-8")

    run_full_pipeline.cmd_ingest(argparse.Namespace(run_id="run-1", headless_only=True))

    assert captured["fetch_calls"] == [(fixture,)]
    assert captured["applied"] == [frozen]
    assert captured["replayed"] == (linkedin, 1)
    assert captured["replayed_result"] is frozen


def test_ingest_resumes_after_funnel_without_reopening_completed_stages(monkeypatch, tmp_path):
    """A crash before job dedup resumes from the durable canonical manifest."""
    since = datetime(2026, 8, 1, tzinfo=timezone.utc)
    cutoff = datetime(2026, 8, 2, tzinfo=timezone.utc)
    maker, durable_session_factory = session_factory()
    normalized = NormalizedJob(
        source="fixture", external_job_id="job-1", title="Data Engineer",
        company_name_raw="Acme", description_raw="work", posted_at=cutoff,
        is_remote=True, job_url="https://jobs.test/job-1",
    )
    with maker.begin() as session:
        upsert_job(session, normalized)
        start_stage(
            session, "run-1", "fetch_jobs", expected_count=1,
            reason="attended connector collection",
        )
        finish_stage(
            session, "run-1", "fetch_jobs",
            counts=StageCounts(1, 1, 0, 0, 0),
        )
        observation = FetchObservation(normalized, 1, 1)
        advanced = GateOutcome(observation, 1, "advanced")
        manifest = report_ingestion_funnel(
            session, "run-1",
            IngestionGateFacts((advanced,), (advanced,), (advanced,)),
        )
        expected_observation_ids = tuple(ref.observation_id for ref in manifest)

    forbidden = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("completed fetch/funnel work must not be rerun")
    )
    dedup_calls = []
    canonical_ids = []
    real_report_canonical_jobs = run_full_pipeline.report_canonical_jobs

    def capture_canonical_jobs(session, run_id, recovered_manifest):
        recovered_manifest = tuple(recovered_manifest)
        canonical_ids.extend(ref.observation_id for ref in recovered_manifest)
        real_report_canonical_jobs(session, run_id, recovered_manifest)

    monkeypatch.setattr(run_full_pipeline, "LOG_ROOT", tmp_path)
    monkeypatch.setattr(run_full_pipeline, "ACTIVE_CONNECTORS", [])
    monkeypatch.setattr(run_full_pipeline, "_decision_run_window", lambda run_id: (since, cutoff))
    monkeypatch.setattr(db_session, "get_session", durable_session_factory)
    monkeypatch.setattr(run_full_pipeline, "get_session", durable_session_factory)
    monkeypatch.setattr(run_full_pipeline, "DecisionRunConnectorProgress", forbidden)
    monkeypatch.setattr(run_full_pipeline, "fetch_all", forbidden)
    monkeypatch.setattr(run_full_pipeline, "gate_and_upsert", forbidden)
    monkeypatch.setattr(
        run_full_pipeline, "run_job_dedup",
        lambda dry_run: dedup_calls.append(dry_run) or {"scanned": 1, "flagged": 0},
    )
    monkeypatch.setattr(run_full_pipeline, "report_canonical_jobs", capture_canonical_jobs)
    monkeypatch.setattr(
        run_full_pipeline, "_send_connector_completion_report", lambda *_args: None,
    )
    monkeypatch.setattr(
        run_full_pipeline, "run_company_dedup",
        lambda apply: {"tier1_merged": 0, "tier2_groups": 0},
    )

    run_full_pipeline.cmd_ingest(argparse.Namespace(run_id="run-1", headless_only=True))

    assert dedup_calls == [False]
    assert tuple(canonical_ids) == expected_observation_ids
    with maker() as session:
        stages = {stage.name: stage for stage in read_run_progress(
            session, "run-1", project_interruptions=False,
        ).stages}
        for name in ("fetch_jobs", "collection_deduplication", "relevance_storage"):
            assert stages[name].status == "completed"
            assert len(stages[name].attempts) == 1
            assert stages[name].counts == StageCounts(1, 1, 0, 0, 0)
        assert stages["job_deduplication"].status == "completed"

    monkeypatch.setattr(run_full_pipeline, "run_job_dedup", forbidden)
    monkeypatch.setattr(run_full_pipeline, "report_canonical_jobs", forbidden)
    run_full_pipeline.cmd_ingest(
        argparse.Namespace(run_id="run-1", headless_only=True),
    )

    with maker() as session:
        stages = {stage.name: stage for stage in read_run_progress(
            session, "run-1", project_interruptions=False,
        ).stages}
        assert len(stages["job_deduplication"].attempts) == 1
        assert stages["job_deduplication"].counts == StageCounts(1, 1, 0, 0, 0)
