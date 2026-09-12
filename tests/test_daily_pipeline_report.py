import json
from datetime import datetime, timezone

from app.pipeline.runner import ConnectorSummary, FetchAttempt, FetchResult
from scripts.run_daily_pipeline import write_run_artifacts


def test_run_artifacts_expose_truthful_source_instance_and_phase_metrics(tmp_path):
    started = datetime(2026, 8, 14, 10, 0, tzinfo=timezone.utc)
    ended = datetime(2026, 8, 14, 10, 0, 5, tzinfo=timezone.utc)
    attempt = FetchAttempt(
        attempt=1,
        started_at=started,
        ended_at=ended,
        duration_s=5.0,
        status="succeeded",
        fetched_count=4,
    )
    result = FetchResult(
        connector=object(),
        source="instahyre",
        jobs=[],
        elapsed_s=5.0,
        started_at=started,
        ended_at=ended,
        dimensions={"search": "data scientist", "location_mode": "remote"},
        connector_class="InstahyreConnector",
        attempt_details=[attempt],
    )
    source = ConnectorSummary(
        source="instahyre",
        fetched=3,
        kept=2,
        drop_counts={"role": 1, "seniority": 0, "location": 0, "recency": 0},
        fetch_time_s=9.0,
        source_wall_time_s=5.0,
        instances_started=2,
        instances_succeeded=2,
        instances_retried=1,
        raw_count=4,
        unique_count=3,
        duplicate_count=1,
        duplicate_rate_pct=25.0,
        date_quality={
            "known": 0,
            "unknown": 3,
            "within_cutoff": 0,
            "stale": 0,
            "kept_within_cutoff": 0,
            "kept_unknown": 2,
        },
        field_completeness={
            "description": {"count": 2, "pct": 66.7},
            "company": {"count": 3, "pct": 100.0},
            "location": {"count": 3, "pct": 100.0},
            "salary": {"count": 0, "pct": 0.0},
            "employment_type": {"count": 1, "pct": 33.3},
            "seniority": {"count": 0, "pct": 0.0},
            "posted_date": {"count": 0, "pct": 0.0},
            "apply_url": {"count": 3, "pct": 100.0},
        },
        upsert_calls_completed=2,
    )
    metadata = {
        "run_date": "2026-08-14",
        "commit": "abc1234",
        "recency_window_days": 15,
        "cutoff_at": "2026-07-30T10:00:00+00:00",
        "started_at": started.isoformat(),
        "ended_at": ended.isoformat(),
        "active_sources": 21,
        "active_instances": 184,
        "tier_sizes": {"parallel": 160, "browser": 24},
        "options": {"headless_only": False},
    }
    phase_timings = {
        "parallel_fetch_wall_s": 3.0,
        "browser_fetch_wall_s": 2.0,
        "retry_pass_wall_s": 0.5,
        "gate_upsert_wall_s": 0.25,
        "dedup_wall_s": 0.1,
        "report_generation_wall_s": 0.05,
        "total_wall_s": 5.9,
    }

    report_path, metrics_path = write_run_artifacts(
        run_dir=tmp_path,
        metadata=metadata,
        summary=[source],
        results=[result],
        total_upsert_calls_completed=2,
        total_errors=0,
        challenges=[],
        phase_timings=phase_timings,
        dedup_stats={"scanned": 10, "flagged": 1, "review_candidates": 2},
    )

    report = report_path.read_text(encoding="utf-8")
    payload = json.loads(metrics_path.read_text(encoding="utf-8"))

    assert report_path.name == "report.md"
    assert metrics_path.name == "source_metrics.json"
    assert "Configured collection window: 15 days" in report
    assert "Parallel fetch wall: 3.0s" in report
    assert "Source wall 5.0s; cumulative worker 9.0s" in report
    assert "all 3 posting dates are unknown; posting dates are not a relevance gate" in report
    assert "description 2/3 (66.7%)" in report
    assert "`search=data scientist, location_mode=remote`" in report
    assert payload["schema_version"] == 1
    assert payload["run"]["active_instances"] == 184
    assert payload["sources"][0]["raw_count"] == 4
    assert payload["sources"][0]["unique_count"] == 3
    assert payload["sources"][0]["relevance_pct"] == 66.7
    assert payload["sources"][0]["gate_drops"]["role"] == {
        "count": 1,
        "pct": 33.3,
    }
    assert payload["instances"][0]["attempts"][0]["fetched_count"] == 4
