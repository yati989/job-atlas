from datetime import datetime, timedelta, timezone

import pytest

from app.collectors.api.workingnomads import (
    ELIGIBLE_LOCATIONS,
    SEARCH_SIZE,
    SOURCE_FIELDS,
    WORKINGNOMADS_API_URL,
    WorkingNomadsConnector,
)
from app.pipeline.window import apply_sync_window


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class _Client:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def post(self, url, *, json):
        self.calls.append((url, json))
        return _Response(self.payload)


def _hit(job_id: int, *, locations=None, age_days: int = 1, expired: bool = False):
    posted = datetime.now(timezone.utc) - timedelta(days=age_days)
    return {
        "_id": str(job_id),
        "_source": {
            "id": job_id,
            "slug": f"data-engineer-{job_id}",
            "title": "Data Engineer",
            "company": "Acme",
            "locations": ["Anywhere"] if locations is None else locations,
            "description": "Build data systems.",
            "pub_date": posted.isoformat(),
            "apply_url": f"https://apply.example/{job_id}",
            "salary_range": "$100k-$150k",
            "position_type": "Full-time",
            "experience_level": "Mid-level",
            "expired": expired,
        },
    }


def _payload(hits, *, total=None):
    return {
        "hits": {
            "total": {"value": len(hits) if total is None else total, "relation": "eq"},
            "hits": hits,
        }
    }


def test_query_matches_captured_role_location_sort_and_single_request_shape():
    cutoff = datetime(2026, 6, 1, tzinfo=timezone.utc)
    connector = WorkingNomadsConnector(search="machine learning engineer")

    query = connector._build_query(cutoff)

    assert query["track_total_hits"] is True
    assert query["from"] == 0
    assert query["size"] == SEARCH_SIZE == 1000
    assert query["_source"] == list(SOURCE_FIELDS)
    assert query["sort"] == [
        {"pub_date": {"order": "desc"}},
        {"premium": {"order": "desc"}},
        {"_score": {"order": "desc"}},
    ]
    assert query["query"]["bool"]["must"] == {
        "query_string": {
            "query": '"machine learning engineer"',
            "fields": ["title"],
        }
    }
    assert query["query"]["bool"]["filter"] == [
        {"terms": {"locations": list(ELIGIBLE_LOCATIONS)}},
        {"range": {"pub_date": {"gte": cutoff.isoformat()}}},
    ]
    assert query["min_score"] == 2


def test_fetch_normalizes_eligible_scope_deduplicates_and_filters_expired():
    connector = WorkingNomadsConnector(search="data engineer")
    hits = [
        _hit(1),
        _hit(1),
        _hit(2, locations=["India", "USA"]),
        _hit(3, locations=["APAC"]),
        _hit(4, expired=True),
    ]
    connector._client = _Client(_payload(hits))

    jobs = connector.fetch()

    assert connector._client.calls[0][0] == WORKINGNOMADS_API_URL
    assert [job.external_job_id for job in jobs] == ["1", "2", "3"]
    assert [job.remote_scope for job in jobs] == ["Worldwide", "India", "APAC"]
    assert all(job.description_raw == "Build data systems." for job in jobs)
    assert all(isinstance(job.posted_at, datetime) for job in jobs)


def test_fetch_fails_loudly_if_one_request_does_not_exhaust_tracked_total():
    connector = WorkingNomadsConnector(search="ai engineer", size=100)
    connector._client = _Client(_payload([_hit(1)], total=280))

    with pytest.raises(RuntimeError, match="returned 1 of 280 tracked hits"):
        connector.fetch()


def test_fetch_allows_intentional_newest_first_window_cap():
    connector = WorkingNomadsConnector(search="ai engineer")
    run_start = datetime(2026, 8, 22, 12, tzinfo=timezone.utc)
    apply_sync_window(
        [connector],
        since_at=run_start - timedelta(days=15),
        cutoff_at=run_start,
    )
    connector._client = _Client(_payload([_hit(1)], total=280))

    jobs = connector.fetch()

    assert [job.external_job_id for job in jobs] == ["1"]


def test_query_omits_native_date_filter_when_recency_is_disabled():
    connector = WorkingNomadsConnector(search="credit risk", max_age_days=None)

    filters = connector._build_query(None)["query"]["bool"]["filter"]

    assert filters == [{"terms": {"locations": list(ELIGIBLE_LOCATIONS)}}]
