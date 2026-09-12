from datetime import datetime, timedelta, timezone

import pandas as pd
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.dashboard.job_funnel import render_job_funnel
from app.dashboard import queries
from app.dashboard.queries import decision_run_job_funnel, decision_run_job_funnel_drilldown
from app.decision_runs.ingestion_funnel import report_canonical_jobs, report_ingestion_funnel
from app.decision_runs.progress import StageRecord, finish_stage, start_stage
from app.models.orm import (
    Base, DecisionRun, DecisionRunIngestionObservation, DecisionRunStageRecord, Job,
)
from app.models.schemas import NormalizedJob
from app.pipeline import runner
from app.pipeline.runner import FetchObservation, FetchResult, gate_and_upsert


NOW = datetime(2026, 8, 2, tzinfo=timezone.utc)


def _session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, future=True)()
    session.add(DecisionRun(
        id="run-1", since_at=NOW - timedelta(days=1), cutoff_at=NOW,
        input_timezone="UTC", state="preparing", policy_snapshot={}, target_count=1,
    ))
    session.flush()
    start_stage(
        session, "run-1", "fetch_jobs", expected_count=0,
        reason="fixture collection",
    )
    finish_stage(session, "run-1", "fetch_jobs")
    return session


def _job(external_id: str, *, source: str = "board-a", title: str = "Data Engineer",
         location: str = "Bengaluru", posted_at: datetime = NOW) -> NormalizedJob:
    return NormalizedJob(
        source=source, external_job_id=external_id, title=title,
        company_name_raw="Acme", location_raw=location, is_remote=False,
        posted_at=posted_at, job_url=f"https://jobs.test/{source}/{external_id}",
        apply_url=f"https://apply.test/{source}/{external_id}",
    )


def _canonical_facts(monkeypatch, results):
    captured = []
    monkeypatch.setattr(runner, "_upsert_job_batch", lambda jobs, **kwargs: (len(jobs), 0))
    gate_and_upsert(results, cutoff_at=NOW - timedelta(days=180), outcome_sink=captured.append)
    return captured[0]


def test_retry_pass_preserves_repeat_observations_before_merging_jobs(monkeypatch):
    connector = type("Connector", (), {"source_name": "board-a", "max_attempts": 2})()
    first_job, retry_job = _job("same"), _job("same")
    result = FetchResult(
        connector=connector, source="board-a", jobs=[first_job], error=RuntimeError("retry"),
        elapsed_s=0.1, observations=[FetchObservation(first_job, 1, 1)],
    )
    retry = FetchResult(
        connector=connector, source="board-a", jobs=[retry_job], elapsed_s=0.1,
        observations=[FetchObservation(retry_job, 2, 1)],
    )
    monkeypatch.setattr(runner, "_fetch_with_progress", lambda *args, **kwargs: retry)
    runner.retry_failed([result])
    assert len(result.jobs) == 1
    assert [(item.attempt, item.ordinal) for item in result.observations] == [(1, 1), (2, 1)]

    facts = _canonical_facts(monkeypatch, [result])
    assert (len(facts.raw), len(facts.collection)) == (2, 2)
    assert [fact.reason for fact in facts.collection] == [None, "collection_duplicate"]


def test_raw_retry_sightings_and_all_gate_reasons_come_from_canonical_gate(monkeypatch):
    good = _job("good")
    repeat = _job("good")
    role = _job("role", title="Software Engineer")
    seniority = _job("senior", title="Director of Data Engineering")
    location = _job("location", location="New York")
    recency = _job("old", posted_at=NOW - timedelta(days=181))
    result = FetchResult(
        connector=object(), source="board-a", jobs=[good, role, seniority, location, recency],
        dimensions={"search": "Data Engineer", "location_mode": "india"},
        observations=[
            FetchObservation(good, 1, 1), FetchObservation(role, 1, 2),
            FetchObservation(seniority, 1, 3), FetchObservation(location, 1, 4),
            FetchObservation(recency, 1, 5), FetchObservation(repeat, 2, 1),
        ],
    )
    facts = _canonical_facts(monkeypatch, [result])
    assert len(facts.raw) == 6
    assert [fact.reason for fact in facts.collection if fact.outcome == "dropped"] == ["collection_duplicate"]
    assert {fact.reason for fact in facts.relevance if fact.outcome == "dropped"} == {
        "role", "seniority", "location",
    }

    session = _session()
    manifest = report_ingestion_funnel(session, "run-1", facts)
    assert len(manifest) == 2
    collection = decision_run_job_funnel_drilldown(
        session, "run-1", stage_name="collection_deduplication",
    )
    repeated = collection[collection["reason"] == "collection_duplicate"].iloc[0]
    assert (repeated["fetch_attempt"], repeated["occurrence_ordinal"]) == (2, 1)
    assert repeated["connector_dimensions"] == {"search": "Data Engineer", "location_mode": "india"}
    assert repeated["job_url"] == "https://jobs.test/board-a/good"
    assert repeated["apply_url"] == "https://apply.test/board-a/good"


def test_job_and_ingestion_urls_are_not_length_capped():
    for model in (Job, DecisionRunIngestionObservation):
        assert model.__table__.c.job_url.type.length is None
        assert model.__table__.c.apply_url.type.length is None


def test_dashboard_drilldown_chunks_large_observation_identity_sets(monkeypatch):
    monkeypatch.setattr(queries, "_DRILLDOWN_IDENTITY_CHUNK_SIZE", 2)
    assert list(queries._chunks([("board", str(index)) for index in range(5)])) == [
        [("board", "0"), ("board", "1")],
        [("board", "2"), ("board", "3")],
        [("board", "4")],
    ]


def test_source_drilldown_is_strict_and_does_not_mutate_canonical_totals(monkeypatch):
    results = [
        FetchResult(connector=object(), source="board-a", jobs=[_job("a")], dimensions={"search": "data"}),
        FetchResult(connector=object(), source="board-b", jobs=[_job("b", source="board-b")], dimensions={"search": "analytics"}),
    ]
    facts = _canonical_facts(monkeypatch, results)
    session = _session()
    manifest = report_ingestion_funnel(session, "run-1", facts)
    observations = session.execute(select(DecisionRunIngestionObservation).where(
        DecisionRunIngestionObservation.id.in_([ref.observation_id for ref in manifest])
    )).scalars()
    for observation in observations:
        session.add(Job(
            source=observation.source, external_job_id=observation.external_job_id,
            title="Data Engineer", company_name_raw="Acme",
            job_url=f"https://jobs.test/{observation.source}/{observation.external_job_id}",
        ))
    session.flush()
    report_canonical_jobs(session, "run-1", manifest)
    before = decision_run_job_funnel(session, "run-1").copy(deep=True)
    subset = decision_run_job_funnel_drilldown(
        session, "run-1", stage_name="relevance_storage", source="board-b",
    )
    after = decision_run_job_funnel(session, "run-1")
    assert subset["source"].tolist() == ["board-b"]
    assert subset["external_job_id"].tolist() == ["b"]
    pd.testing.assert_frame_equal(before, after)


def test_canonical_duplicate_and_missing_upsert_are_separate_reconciled_outcomes(monkeypatch):
    facts = _canonical_facts(monkeypatch, [FetchResult(
        connector=object(), source="board-a", jobs=[_job("canonical"), _job("missing")],
        dimensions={"search": "Data Engineer", "location_mode": "india"},
    )])
    session = _session()
    manifest = report_ingestion_funnel(session, "run-1", facts)
    session.add(Job(
        source="board-a", external_job_id="canonical", title="Data Engineer",
        company_name_raw="Acme", duplicate_of_job_id=999,
    ))
    session.flush()
    report_canonical_jobs(session, "run-1", manifest)
    row = decision_run_job_funnel(session, "run-1").set_index("stage").loc["job_deduplication"]
    assert row["input"] == row["advanced"] + row["dropped"] + row["failed"] + row["pending"]
    assert (row["dropped"], row["failed"], row["pending"]) == (1, 1, 0)
    missing = decision_run_job_funnel_drilldown(
        session, "run-1", stage_name="job_deduplication", reason="upsert_missing",
    ).iloc[0]
    assert missing["external_job_id"] == "missing"
    assert missing["job_url"] == "https://jobs.test/board-a/missing"
    assert missing["apply_url"] == "https://apply.test/board-a/missing"
    assert missing["connector_instance"] == 1
    assert missing["connector_dimensions"] == {
        "search": "Data Engineer", "location_mode": "india",
    }
    assert (missing["fetch_attempt"], missing["occurrence_ordinal"]) == (1, 2)


def test_generic_progress_record_remains_identifier_only_and_domain_neutral():
    assert set(StageRecord.__dataclass_fields__) == {
        "record_type", "record_id", "outcome", "reason",
    }
    assert {
        "source", "external_job_id", "connector_instance", "connector_dimensions",
        "fetch_attempt", "occurrence_ordinal", "job_url", "apply_url",
    }.isdisjoint(DecisionRunStageRecord.__table__.columns.keys())


def test_unavailable_telemetry_is_not_a_completed_zero():
    funnel = decision_run_job_funnel(_session(), "run-1")
    assert funnel["available"].tolist() == [False, False, False, False, False]
    assert funnel["input"].isna().all()


def test_flow_chart_includes_every_reconciliation_outcome():
    class Renderer:
        def __init__(self): self.calls = []
        def plotly_chart(self, chart, **kwargs): self.calls.append(("chart", chart))
        def dataframe(self, table, **kwargs): self.calls.append(("table", table))
        def info(self, message): self.calls.append(("info", message))
    renderer = Renderer()
    table = pd.DataFrame([{
        "stage": "collection_deduplication", "from": "Raw fetched", "to": "Collection-unique",
        "input": 4, "advanced": 1, "dropped": 1, "failed": 1, "pending": 1,
    }])
    render_job_funnel(renderer, table)
    chart = renderer.calls[0][1]
    assert set(chart.data[0]["link"]["value"]) == {1}
    assert len(chart.data[0]["link"]["value"]) == 4
    assert [call[0] for call in renderer.calls] == ["chart", "table"]


def test_flow_chart_treats_unavailable_display_markers_as_missing():
    class Renderer:
        def __init__(self): self.calls = []
        def plotly_chart(self, chart, **kwargs): self.calls.append(("chart", chart))
        def dataframe(self, table, **kwargs): self.calls.append(("table", table))
        def info(self, message): self.calls.append(("info", message))

    renderer = Renderer()
    table = pd.DataFrame([{
        "stage": "collection_deduplication", "from": "Raw fetched",
        "to": "Collection-unique", "input": "—", "advanced": "—",
        "dropped": "—", "failed": "—", "pending": "—",
    }])

    render_job_funnel(renderer, table)

    assert renderer.calls == [(
        "info", "Job funnel telemetry is unavailable for this run."
    )]
