from datetime import datetime, timedelta, timezone

from app.models.schemas import NormalizedJob
from app.collectors.api.workingnomads import WorkingNomadsConnector
from app.collectors.feed.hn_hiring import HNHiringConnector
from app.pipeline.relevance import filter_relevant
from app.pipeline.window import apply_sync_window


def _job(posted_at: datetime) -> NormalizedJob:
    return NormalizedJob(
        source="example",
        external_job_id="1",
        title="Data Scientist",
        company_name_raw="Acme",
        location_raw="Remote India",
        is_remote=True,
        posted_at=posted_at,
    )


def test_explicit_sync_cutoff_does_not_drop_a_received_job():
    now = datetime(2026, 8, 22, 12, tzinfo=timezone.utc)
    cutoff = now - timedelta(days=5)

    kept, drops, _details = filter_relevant(
        [_job(now - timedelta(days=6))], cutoff_at=cutoff
    )

    assert len(kept) == 1
    assert drops["recency"] == 0


def test_sync_window_updates_each_supported_native_recency_parameter():
    class Connector:
        source_name = "example"
        date_filter_days = 15
        max_age_days = 15
        freshness_days = 15
        recency_window_days = 15
        days_since_posted = 15

    connector = Connector()
    cutoff = datetime(2026, 8, 21, 6, 30, tzinfo=timezone.utc)
    run_end = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)

    window = apply_sync_window([connector], since_at=cutoff, cutoff_at=run_end)

    # Native APIs accept whole-day windows. Round upward so their prefilter
    # never excludes a row that the exact central timestamp gate would keep.
    assert window.native_days == 2
    assert connector.date_filter_days == 2
    assert connector.max_age_days == 2
    assert connector.freshness_days == 2
    assert connector.recency_window_days == 2
    assert connector.days_since_posted == 2


def test_sync_window_scales_only_a_verified_newest_first_connector_cap():
    class DateSortedConnector:
        source_name = "date_sorted"
        verified_newest_first = True
        recency_scaled_cap_attribute = "max_results"
        max_results = 1_000

    class UnsortedConnector:
        source_name = "unsorted"
        max_results = 1_000

    sorted_connector = DateSortedConnector()
    unsorted_connector = UnsortedConnector()
    run_start = datetime(2026, 8, 22, 12, tzinfo=timezone.utc)

    apply_sync_window(
        [sorted_connector, unsorted_connector],
        since_at=run_start - timedelta(days=15),
        cutoff_at=run_start,
    )

    assert sorted_connector.max_results == 250
    assert unsorted_connector.max_results == 1_000


def test_recency_scaled_cap_rounds_up_and_does_not_compound_between_runs():
    class Connector:
        source_name = "date_sorted"
        verified_newest_first = True
        recency_scaled_cap_attribute = "max_pages"
        max_pages = 60

    connector = Connector()
    run_start = datetime(2026, 8, 22, 12, tzinfo=timezone.utc)

    apply_sync_window(
        [connector],
        since_at=run_start - timedelta(days=1),
        cutoff_at=run_start,
    )
    assert connector.max_pages == 1

    apply_sync_window(
        [connector],
        since_at=run_start - timedelta(days=30),
        cutoff_at=run_start,
    )
    assert connector.max_pages == 30


def test_active_date_sorted_connectors_scale_their_declared_primary_cap():
    working_nomads = WorkingNomadsConnector(search="data scientist")
    hn_hiring = HNHiringConnector()
    run_start = datetime(2026, 8, 22, 12, tzinfo=timezone.utc)

    apply_sync_window(
        [working_nomads, hn_hiring],
        since_at=run_start - timedelta(days=15),
        cutoff_at=run_start,
    )

    assert working_nomads.size == 250  # ceil(1000 / 60 * 15)
    assert hn_hiring.max_pages == 5  # ceil(20 / 60 * 15)
