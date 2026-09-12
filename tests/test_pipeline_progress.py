from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.models.schemas import NormalizedJob
from app.pipeline.progress import (
    ConnectorProgress,
    default_progress_run_id,
    load_progress,
    load_progress_history,
    record_gate_summary_for_snapshot,
    run_elapsed_seconds,
    source_rows,
    total_source_row,
)
from app.pipeline.runner import FetchResult, fetch_all, retry_failed


class Connector:
    source_name = "example"

    def __init__(self, search: str, jobs: int = 0, fail_once: bool = False):
        self.search = search
        self.jobs = jobs
        self.fail_once = fail_once
        self.calls = 0

    def fetch(self):
        self.calls += 1
        if self.fail_once and self.calls == 1:
            raise RuntimeError("temporary failure")
        return [
            NormalizedJob(
                source=self.source_name,
                external_job_id=f"{self.search}-{index}",
                title="Data Scientist",
                company_name_raw="Acme",
                location_raw="Remote",
                is_remote=True,
            )
            for index in range(self.jobs)
        ]


def test_progress_aggregates_instances_and_jobs_by_source(tmp_path):
    path = tmp_path / "latest.json"
    started = datetime(2026, 8, 22, 8, 0, tzinfo=timezone.utc)
    clock = iter(
        [
            started,
            started,
            started + timedelta(seconds=2),
            started + timedelta(seconds=5),
        ]
    )
    connectors = [Connector("data scientist"), Connector("data analyst")]
    progress = ConnectorProgress(
        connectors,
        path=path,
        run_id="run-1",
        now=lambda: next(clock),
        process_id=123,
    )

    progress.instance_started(connectors[0], attempt=1)
    progress.instance_finished(
        FetchResult(
            connector=connectors[0],
            source="example",
            jobs=[object(), object(), object()],
            error=None,
        )
    )
    snapshot = load_progress(path)
    rows = source_rows(snapshot, now=started + timedelta(seconds=9))

    assert rows == [
        {
            "source": "example",
            "status": "running",
            "elapsed_seconds": 9.0,
            "jobs_finished": 3,
            "fetched": 3,
            "kept": None,
            "kept_pct": None,
            "drop_role": None,
            "drop_seniority": None,
            "drop_location": None,
            "drop_recency": None,
            "instances_finished": 1,
            "instances_total": 2,
            "instances_succeeded": 1,
            "instances_failed": 0,
        }
    ]


def test_retry_completion_does_not_finish_pipeline_progress(tmp_path):
    connectors = [
        Connector("data scientist", jobs=2),
        Connector("data analyst", jobs=1, fail_once=True),
    ]
    progress = ConnectorProgress(
        connectors,
        path=tmp_path / "latest.json",
        run_id="run-2",
    )

    results = fetch_all(connectors, progress=progress)
    retry_failed(results, progress=progress)

    snapshot = load_progress(tmp_path / "latest.json")
    assert snapshot["status"] == "running"


def test_pipeline_completion_includes_post_collection_elapsed_time(tmp_path):
    path = tmp_path / "latest.json"
    started = datetime(2026, 8, 24, 13, 59, 44, tzinfo=timezone.utc)
    current = [started]
    connector = Connector("data scientist", jobs=3)
    progress = ConnectorProgress(
        [connector],
        path=path,
        now=lambda: current[0],
    )
    progress.instance_started(connector, attempt=1)
    current[0] = started + timedelta(minutes=3, seconds=26)
    progress.instance_finished(FetchResult(
        connector=connector,
        source="example",
        jobs=[object(), object(), object()],
        error=None,
    ))

    current[0] = started + timedelta(minutes=6, seconds=30)
    progress.complete([
        SimpleNamespace(
            source="example", fetched=3, kept=2,
            drop_counts={"role": 1, "seniority": 0, "location": 0, "recency": 0},
        )
    ])

    snapshot = load_progress(path)
    assert snapshot["status"] == "completed"
    assert run_elapsed_seconds(snapshot) == 390.0
    row = source_rows(snapshot)[0]
    assert row["status"] == "completed"
    assert row["elapsed_seconds"] == 390.0
    assert row["jobs_finished"] == 3
    assert row["kept"] == 2
    assert row["instances_finished"] == 1
    assert row["instances_succeeded"] == 1
    assert row["instances_failed"] == 0


def test_progress_publishes_post_gate_kept_and_drop_outcomes(tmp_path):
    connector = Connector("data scientist", jobs=3)
    progress = ConnectorProgress([connector], path=tmp_path / "latest.json")
    progress.record_gate_summary([
        SimpleNamespace(
            source="example",
            fetched=3,
            kept=1,
            drop_counts={"role": 1, "seniority": 0, "location": 0, "recency": 1},
        )
    ])

    row = source_rows(load_progress(tmp_path / "latest.json"))[0]
    assert row["fetched"] == 3
    assert row["kept"] == 1
    assert row["kept_pct"] == 33.3
    assert row["drop_role"] == 1
    assert row["drop_seniority"] == 0
    assert row["drop_location"] == 0
    assert row["drop_recency"] == 1


def test_total_source_row_reconciles_gate_outcomes_only_after_the_gate():
    rows = [
        {
            "source": "first", "jobs_finished": 8, "fetched": 6, "kept": 2,
            "drop_role": 1, "drop_seniority": 1, "drop_location": 1, "drop_recency": 1,
            "instances_finished": 2, "instances_total": 2,
            "instances_succeeded": 2, "instances_failed": 0,
        },
        {
            "source": "second", "jobs_finished": 7, "fetched": 4, "kept": 1,
            "drop_role": 2, "drop_seniority": 0, "drop_location": 1, "drop_recency": 0,
            "instances_finished": 1, "instances_total": 2,
            "instances_succeeded": 1, "instances_failed": 0,
        },
    ]

    total = total_source_row(rows, status="running", elapsed_seconds=12.4)

    assert total["source"] == "Total"
    assert total["fetched"] == 10
    assert total["kept"] == 3
    assert total["kept_pct"] == 30.0
    assert total["drop_role"] == 3
    assert total["instances_finished"] == 3
    assert total["instances_total"] == 4

    rows[1]["kept"] = None
    assert total_source_row(rows, status="running", elapsed_seconds=12.4)["kept"] is None


def test_interrupted_progress_uses_its_last_update_as_the_elapsed_endpoint():
    started = datetime(2026, 8, 22, 22, 58, 17, tzinfo=timezone.utc)
    updated = started + timedelta(minutes=9, seconds=35)
    much_later = started + timedelta(hours=12, minutes=2, seconds=44)
    snapshot = {
        "status": "running",
        "started_at": started.isoformat(),
        "updated_at": updated.isoformat(),
        "ended_at": None,
        "sources": [
            {
                "source": "linkedin", "status": "running", "jobs_finished": 886,
                "instances_finished": 3, "instances_total": 12,
                "instances_succeeded": 3, "instances_failed": 0,
                "started_at": started.isoformat(), "ended_at": None,
            }
        ],
    }

    assert run_elapsed_seconds(snapshot, now=much_later, interrupted=True) == 575.0
    assert source_rows(snapshot, now=much_later, interrupted=True)[0]["elapsed_seconds"] == 575.0


def test_completed_snapshot_can_be_backfilled_without_recollecting(tmp_path):
    path = tmp_path / "latest.json"
    ConnectorProgress([Connector("data scientist", jobs=3)], path=path)
    snapshot = record_gate_summary_for_snapshot([
        SimpleNamespace(
            source="example", fetched=3, kept=2,
            drop_counts={"role": 1, "seniority": 0, "location": 0, "recency": 0},
        )
    ], path=path)

    assert snapshot is not None
    assert source_rows(snapshot)[0]["kept"] == 2


def test_progress_history_preserves_previous_runs_and_updates_current_run(tmp_path):
    latest_path = tmp_path / "latest.json"
    history_dir = tmp_path / "runs"
    first = ConnectorProgress(
        [Connector("first", jobs=1)],
        path=latest_path,
        history_dir=history_dir,
        run_id="run/one",
        command="first command",
    )
    first.finish()
    second = ConnectorProgress(
        [Connector("second", jobs=2)],
        path=latest_path,
        history_dir=history_dir,
        run_id="run-two",
        command="second command",
    )

    snapshots = load_progress_history(latest_path, history_dir=history_dir)

    assert [snapshot["run_id"] for snapshot in snapshots] == ["run-two", "run/one"]
    assert snapshots[0]["status"] == "running"
    assert snapshots[1]["status"] == "completed"
    assert len(list(history_dir.glob("*.json"))) == 2
    assert default_progress_run_id(snapshots) == "run-two"


def test_progress_history_ignores_corrupt_files_and_includes_legacy_latest(tmp_path):
    latest_path = tmp_path / "latest.json"
    history_dir = tmp_path / "runs"
    ConnectorProgress(
        [],
        path=latest_path,
        run_id="legacy-latest",
    ).finish()
    history_dir.mkdir()
    (history_dir / "broken.json").write_text("{", encoding="utf-8")

    snapshots = load_progress_history(latest_path, history_dir=history_dir)

    assert [snapshot["run_id"] for snapshot in snapshots] == ["legacy-latest"]


def test_starting_first_historical_run_archives_preexisting_legacy_latest(tmp_path):
    latest_path = tmp_path / "latest.json"
    history_dir = tmp_path / "runs"
    ConnectorProgress([], path=latest_path, run_id="legacy-run").finish()

    ConnectorProgress(
        [],
        path=latest_path,
        history_dir=history_dir,
        run_id="new-run",
    )

    assert {
        snapshot["run_id"]
        for snapshot in load_progress_history(latest_path, history_dir=history_dir)
    } == {"legacy-run", "new-run"}


def test_gate_summary_backfill_updates_historical_snapshot(tmp_path):
    latest_path = tmp_path / "latest.json"
    history_dir = tmp_path / "runs"
    connector = Connector("data scientist", jobs=3)
    ConnectorProgress(
        [connector],
        path=latest_path,
        history_dir=history_dir,
        run_id="run-with-gate",
    )

    record_gate_summary_for_snapshot([
        SimpleNamespace(
            source="example", fetched=3, kept=2,
            drop_counts={"role": 1, "seniority": 0, "location": 0, "recency": 0},
        )
    ], path=latest_path, history_dir=history_dir)

    snapshot = load_progress_history(latest_path, history_dir=history_dir)[0]
    assert source_rows(snapshot)[0]["kept"] == 2


def test_default_progress_run_prefers_newest_snapshot_over_stale_running_run():
    snapshots = [
        {"run_id": "newest-complete", "status": "completed"},
        {"run_id": "older-running", "status": "running"},
    ]

    assert default_progress_run_id(snapshots) == "newest-complete"
    assert default_progress_run_id([]) is None
