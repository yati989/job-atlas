import argparse
import json
from datetime import datetime, timezone

import pytest

from app.models.schemas import NormalizedJob
from app.pipeline import linkedin_location_stage as stage
from app.pipeline.runner import FetchObservation, FetchResult
from app.pipeline.work_location_enrichment import build_input
from scripts import run_full_pipeline


class _Connector:
    source_name = "linkedin"

    def __init__(self):
        self.search = "data scientist"
        self.location_mode = "remote_india"

    def hydrate_relevant_jobs(self, jobs):
        for job in jobs:
            job.description_raw = "Remote-first flexibility lets you work from anywhere."
        return {"eligible": len(jobs), "hydrated": len(jobs), "failed": 0}


def _job():
    return NormalizedJob(
        source="linkedin",
        external_job_id="avahi",
        title="Data Scientist",
        company_name_raw="Avahi",
        location_raw="Pune, India",
        posted_at=datetime(2026, 8, 28, tzinfo=timezone.utc),
        raw_payload={"query_location_mode": "remote_india"},
    )


def test_stage_hydrates_each_unique_candidate_and_propagates_description(monkeypatch):
    connector = _Connector()
    first, duplicate = _job(), _job()
    results = [
        FetchResult(connector=connector, source="linkedin", jobs=[first]),
        FetchResult(connector=connector, source="linkedin", jobs=[duplicate]),
    ]
    monkeypatch.setattr(stage, "_reuse_stored_details", lambda _source, jobs: (jobs, 0))

    candidates, counts = stage.hydrate_candidates(
        results, cutoff_at=datetime(2026, 8, 25, tzinfo=timezone.utc)
    )

    assert len(candidates) == 1
    assert counts == {"eligible": 1, "reused": 0, "hydrated": 1, "failed": 0}
    assert duplicate.description_raw == first.description_raw


def test_frozen_fetch_round_trip_and_agent_results_apply_before_gate(tmp_path):
    connector = _Connector()
    job = _job()
    job.description_raw = "Remote-first flexibility lets you work from anywhere."
    result = FetchResult(
        connector=connector,
        source="linkedin",
        jobs=[job],
        observations=[FetchObservation(job, 1, 1)],
        dimensions={"search": "data scientist", "location_mode": "remote_india"},
    )
    snapshot = tmp_path / "fetch.json"
    input_path = tmp_path / "input.jsonl"
    result_path = tmp_path / "results.jsonl"
    stage.save_fetch_results(snapshot, [result], [connector])
    restored = stage.load_fetch_results(snapshot, [connector])
    packet = build_input(restored[0].jobs[0])
    input_path.write_text(packet.model_dump_json() + "\n", encoding="utf-8")
    result_path.write_text(json.dumps({
        "source": "linkedin",
        "external_job_id": "avahi",
        "description_sha256": packet.description_sha256,
        "decision": "remote_india",
        "evidence": "Remote-first flexibility lets you work from anywhere.",
        "reason": "The role can be performed remotely from India.",
        "confidence": "high",
        "required_location": "India",
    }) + "\n", encoding="utf-8")

    applied = stage.apply_stage_results(
        results=restored, input_path=input_path, result_path=result_path
    )

    assert applied == 1
    assert restored[0].jobs[0].is_remote is True
    assert restored[0].jobs[0].remote_scope == "Remote — India"


def test_frozen_connector_scope_must_match_ingest_scope(tmp_path):
    connector = _Connector()
    snapshot = tmp_path / "fetch.json"
    stage.save_fetch_results(
        snapshot,
        [FetchResult(connector=connector, source="linkedin", jobs=[])],
        [connector],
    )

    with pytest.raises(ValueError, match="scope does not match"):
        stage.load_fetch_results(snapshot, [connector, _Connector()])


def test_missing_agent_result_blocks_relevance_handoff(tmp_path):
    job = _job()
    job.description_raw = "No workplace arrangement is stated."
    input_path = tmp_path / "input.jsonl"
    result_path = tmp_path / "results.jsonl"
    input_path.write_text(build_input(job).model_dump_json() + "\n", encoding="utf-8")

    with pytest.raises(FileNotFoundError, match="required before relevance"):
        stage.apply_stage_results(
            results=[FetchResult(connector=_Connector(), source="linkedin", jobs=[job])],
            input_path=input_path,
            result_path=result_path,
        )


def test_location_input_publishes_connector_and_decision_run_progress(
    monkeypatch, tmp_path,
):
    connector = _Connector()
    captured = {}
    since = datetime(2026, 8, 25, tzinfo=timezone.utc)
    cutoff = datetime(2026, 8, 29, tzinfo=timezone.utc)

    class Progress:
        def finish(self):
            captured["finished"] = True

        def fail(self, reason):
            pytest.fail(reason)

    progress = Progress()

    monkeypatch.setattr(run_full_pipeline, "LOG_ROOT", tmp_path)
    monkeypatch.setattr(run_full_pipeline, "ACTIVE_CONNECTORS", [connector])
    monkeypatch.setattr(run_full_pipeline, "_reconcile_attended_progress", lambda _run_id: None)
    monkeypatch.setattr(run_full_pipeline, "_decision_run_window", lambda _run_id: (since, cutoff))
    monkeypatch.setattr(run_full_pipeline, "_setup_logging", lambda _run_dir: None)
    monkeypatch.setattr(
        run_full_pipeline,
        "ConnectorProgress",
        lambda connectors, **kwargs: captured.update(
            connectors=connectors, progress_kwargs=kwargs,
        ) or progress,
    )
    monkeypatch.setattr(
        run_full_pipeline,
        "apply_sync_window",
        lambda connectors, since_at, cutoff_at: type(
            "Window", (), {"since_at": since_at},
        )(),
    )
    monkeypatch.setattr(run_full_pipeline, "enforce_workload_bounds", lambda _connectors: None)

    def fetch(connectors, *, progress=None):
        assert progress is captured["progress"]
        return [FetchResult(connector=connectors[0], source="linkedin", jobs=[])]

    def retry(results, *, progress=None):
        assert progress is captured["progress"]

    captured["progress"] = progress
    monkeypatch.setattr(run_full_pipeline, "fetch_all", fetch)
    monkeypatch.setattr(run_full_pipeline, "retry_failed", retry)
    monkeypatch.setattr(
        run_full_pipeline,
        "write_linkedin_location_stage",
        lambda **kwargs: captured.update(stage_kwargs=kwargs) or {
            "eligible": 0, "reused": 0, "hydrated": 0, "failed": 0,
        },
    )
    monkeypatch.setattr(run_full_pipeline, "_emit_agent_result", lambda *_args, **_kwargs: None)

    run_full_pipeline.cmd_linkedin_location_input(argparse.Namespace(
        run_id="run-1",
        one_instance=False,
        sample_search="data scientist",
        sample_location="remote_india",
    ))

    assert captured["connectors"] == [connector]
    assert captured["progress_kwargs"] == {
        "run_id": "run-1",
        "command": "scripts.run_full_pipeline linkedin-location-input",
    }
    assert captured["stage_kwargs"]["result_path"].name == "linkedin-location-results.jsonl"
    assert captured["stage_kwargs"]["full_job_enrichment_requested"] is False
    assert captured["finished"] is True


def test_stage_input_is_always_description_only(monkeypatch, tmp_path):
    connector = _Connector()
    job = _job()
    job.description_raw = "The role supports remote work from India."
    result = FetchResult(
        connector=connector,
        source="linkedin",
        jobs=[job],
        observations=[FetchObservation(job, 1, 1)],
    )
    monkeypatch.setattr(stage, "_reuse_stored_details", lambda _source, jobs: ([], len(jobs)))

    input_path = tmp_path / "input.jsonl"
    stage.write_stage(
        snapshot_path=tmp_path / "fetch.json",
        input_path=input_path,
        results=[result],
        connectors=[connector],
        cutoff_at=datetime(2026, 8, 25, tzinfo=timezone.utc),
        full_job_enrichment_requested=True,
    )

    packet = json.loads(input_path.read_text(encoding="utf-8"))
    assert packet["description"] == job.description_raw
    assert packet["description_sha256"]


def test_stage_writes_deterministic_location_results(monkeypatch, tmp_path):
    connector = _Connector()
    job = _job()
    job.description_raw = "We have been and will remain a remote first employer."
    result = FetchResult(connector=connector, source="linkedin", jobs=[job])
    monkeypatch.setattr(stage, "_reuse_stored_details", lambda _source, jobs: ([], len(jobs)))
    result_path = tmp_path / "results.jsonl"

    stage.write_stage(
        snapshot_path=tmp_path / "fetch.json",
        input_path=tmp_path / "input.jsonl",
        result_path=result_path,
        results=[result],
        connectors=[connector],
        cutoff_at=datetime(2026, 8, 25, tzinfo=timezone.utc),
    )

    judgment = json.loads(result_path.read_text(encoding="utf-8"))
    assert judgment["decision"] == "remote_unspecified"
    assert judgment["reason"] == "deterministic_rule:explicit_remote"


def test_location_input_replays_frozen_fetch_into_progress_without_refetch(
    monkeypatch, tmp_path,
):
    connector = _Connector()
    since = datetime(2026, 8, 25, tzinfo=timezone.utc)
    cutoff = datetime(2026, 8, 29, tzinfo=timezone.utc)
    frozen = FetchResult(
        connector=connector, source="linkedin", jobs=[], attempts=2,
    )
    events = []

    class Progress:
        def __init__(self, connectors, **kwargs):
            events.append(("created", connectors, kwargs))

        def instance_started(self, replayed_connector, *, attempt):
            events.append(("started", replayed_connector, attempt))

        def instance_finished(self, result):
            events.append(("finished", result))

        def finish(self):
            events.append(("projection_finished",))

    monkeypatch.setattr(run_full_pipeline, "LOG_ROOT", tmp_path)
    monkeypatch.setattr(run_full_pipeline, "ACTIVE_CONNECTORS", [connector])
    monkeypatch.setattr(run_full_pipeline, "_reconcile_attended_progress", lambda _run_id: None)
    monkeypatch.setattr(run_full_pipeline, "_decision_run_window", lambda _run_id: (since, cutoff))
    monkeypatch.setattr(run_full_pipeline, "_setup_logging", lambda _run_dir: None)
    monkeypatch.setattr(run_full_pipeline, "ConnectorProgress", Progress)
    monkeypatch.setattr(
        run_full_pipeline, "load_fetch_results",
        lambda snapshot_path, connectors: [frozen],
    )
    monkeypatch.setattr(
        run_full_pipeline, "fetch_all",
        lambda *_args, **_kwargs: pytest.fail("frozen fetch must not be rerun"),
    )
    monkeypatch.setattr(run_full_pipeline, "_emit_agent_result", lambda *_args, **_kwargs: None)

    run_dir = run_full_pipeline._canonical_run_dir(cutoff.date().isoformat(), "run-1")
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "linkedin-location-fetch.json").write_text("{}", encoding="utf-8")
    packet = build_input(_job())
    (run_dir / "linkedin-location-input.jsonl").write_text(
        packet.model_dump_json() + "\n", encoding="utf-8"
    )

    run_full_pipeline.cmd_linkedin_location_input(argparse.Namespace(
        run_id="run-1",
        one_instance=False,
        sample_search="data scientist",
        sample_location="remote_india",
    ))

    assert events[1:] == [
        ("started", connector, 2),
        ("finished", frozen),
        ("projection_finished",),
    ]
    deterministic = json.loads(
        (run_dir / "linkedin-location-results.jsonl").read_text(encoding="utf-8")
    )
    assert deterministic["reason"].startswith("deterministic_rule:")
