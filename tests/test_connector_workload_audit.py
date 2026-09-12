import pytest

from app.pipeline.workload import WorkloadLimitError, enforce_workload_bounds
from app.pipeline.runner import FetchResult, retry_failed
from app.pipeline.registry import ACTIVE_CONNECTORS


class Connector:
    source_name = "dangerous"
    fetch_descriptions = True

    def __init__(self, *, max_results=300, detail_workers=None, interval=0.0):
        self.max_results = max_results
        self.detail_interval_seconds = interval
        if detail_workers is not None:
            self.detail_workers = detail_workers


def test_rejects_timesjobs_style_large_serial_hydration_before_fetching():
    with pytest.raises(WorkloadLimitError, match="serial detail hydration"):
        enforce_workload_bounds([Connector(max_results=15_000)])


def test_preserves_source_wide_pacing_when_details_are_parallel():
    connectors = [
        Connector(max_results=1_000, detail_workers=4, interval=1.2)
        for _ in range(12)
    ]
    enforce_workload_bounds(connectors)


def test_preserves_large_static_depth_when_details_are_parallel():
    connectors = [
        Connector(max_results=1_100, detail_workers=4)
        for _ in range(12)
    ]
    enforce_workload_bounds(connectors)


def test_accepts_bounded_parallel_hydration():
    enforce_workload_bounds(
        [Connector(max_results=300, detail_workers=4) for _ in range(12)]
    )


def test_slow_failed_attempt_is_not_doubled_by_runner_retry():
    connector = Connector()
    connector.fetch = lambda: (_ for _ in ()).throw(
        AssertionError("slow connector should not be called again")
    )
    result = FetchResult(
        connector=connector,
        source=connector.source_name,
        error=RuntimeError("timed out"),
        elapsed_s=301.0,
    )

    retry_failed([result])

    assert result.attempts == 1
    assert result.retried is False


def test_only_live_verified_newest_first_active_sources_scale_with_recency():
    scalable = {
        connector.source_name
        for connector in ACTIVE_CONNECTORS
        if getattr(connector, "verified_newest_first", False)
    }

    assert scalable == {"hn_hiring", "workingnomads"}
