from datetime import datetime, timedelta, timezone

import pytest

from app.collectors.html.iimjobs import (
    DETAIL_URL,
    SEARCH_PAGE_SIZE,
    SEARCH_URL,
    TARGET_LOCATION_IDS,
    IIMJobsConnector,
)


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

    def get(self, url, *, params):
        self.calls.append((url, params))
        return _Response(self.payload)


def _row(job_id: int, *, age_days: int = 1) -> dict:
    posted = datetime.now(timezone.utc) - timedelta(days=age_days)
    return {
        "id": job_id,
        "title": "Acme - Data Analyst",
        "companyData": {"companyName": "Acme"},
        "locations": [{"id": 3, "name": "Bangalore"}],
        "createdTimeMs": int(posted.timestamp() * 1000),
        "min": 2,
        "max": 5,
        "tags": [{"id": 1, "name": "SQL"}],
        "workFromHome": 0,
        "jobDetailUrl": f"https://www.iimjobs.com/j/acme-data-analyst-{job_id}",
    }


def test_native_search_uses_verified_query_location_and_page_size():
    connector = IIMJobsConnector(search="data engineer")
    connector._client = _Client({"data": [], "hasMore": False})

    connector._fetch_search_page(2)

    assert connector._client.calls == [
        (
            SEARCH_URL,
            {
                "query": "data engineer",
                "page": 2,
                "size": SEARCH_PAGE_SIZE,
                "loc": TARGET_LOCATION_IDS,
            },
        )
    ]


def test_city_mode_resolves_the_native_city_id_during_fetch_not_construction():
    connector = IIMJobsConnector(search="data engineer", location_mode="city", location=" Pune ")
    connector._client = _Client({
        "data": [{"locations": [{"id": 7, "name": "Pune"}]}],
        "hasMore": False,
    })

    assert connector._client.calls == []
    assert connector._resolve_location_ids() == "7"
    connector._fetch_search_page(1, "7")
    assert connector._client.calls == [
        (
            SEARCH_URL,
            {
                "query": "data engineer",
                "page": 0,
                "size": SEARCH_PAGE_SIZE,
            },
        ),
        (
            SEARCH_URL,
            {
                "query": "data engineer",
                "page": 1,
                "size": SEARCH_PAGE_SIZE,
                "loc": "7",
            },
        ),
    ]


def test_city_india_and_remote_modes_build_the_expected_native_search_scope():
    city = IIMJobsConnector(search="data engineer", location_mode="city", location="Pune")
    india = IIMJobsConnector(search="data engineer", location_mode="india")
    remote = IIMJobsConnector(search="data engineer", location_mode="remote")

    assert city.location_mode == "city"
    assert city.location == "Pune"
    assert india._resolve_location_ids() is None
    assert remote._resolve_location_ids() == "132"


def test_city_mode_requires_a_city_and_other_modes_reject_one():
    with pytest.raises(ValueError, match="location is required"):
        IIMJobsConnector(location_mode="city")
    with pytest.raises(ValueError, match="only when location_mode is 'city'"):
        IIMJobsConnector(location_mode="india", location="Pune")


def test_detail_uses_http_api_and_normalizes_intro_html():
    connector = IIMJobsConnector()
    connector._client = _Client({"data": {"introText": "<p>Hello <b>world</b></p>"}})

    detail = connector._fetch_detail(123)

    assert connector._client.calls == [(DETAIL_URL, {"jobcode": 123})]
    assert detail == {"description_raw": "Hello\nworld"}


def test_fetch_exhausts_pages_deduplicates_filters_recency_and_fetches_details(monkeypatch):
    connector = IIMJobsConnector(search="data analyst", detail_workers=2)
    pages = {
        0: {"data": [_row(1), _row(2, age_days=90)], "hasMore": True},
        1: {"data": [_row(1), _row(3)], "hasMore": False},
    }
    detail_ids = []
    monkeypatch.setattr(
        connector, "_fetch_search_page", lambda page, location_ids=TARGET_LOCATION_IDS: pages[page]
    )

    def detail(job_id):
        detail_ids.append(job_id)
        return {"description_raw": f"Description {job_id}"}

    monkeypatch.setattr(connector, "_fetch_detail", detail)
    jobs = connector.fetch()

    assert [job.external_job_id for job in jobs] == ["1", "3"]
    assert sorted(detail_ids) == [1, 3]
    assert jobs[0].title == "Data Analyst"
    assert jobs[0].company_name_raw == "Acme"
    assert jobs[0].location_raw == "Bangalore"
    assert jobs[0].description_raw == "Description 1"
    assert jobs[0].raw_payload["experience_scraped"] == "2-5 yrs"
    assert jobs[0].raw_payload["skills_scraped"] == ["SQL"]


def test_detail_failure_keeps_card_level_job(monkeypatch):
    connector = IIMJobsConnector(search="data scientist", detail_workers=1)
    monkeypatch.setattr(
        connector,
        "_fetch_search_page",
        lambda page, location_ids=TARGET_LOCATION_IDS: {"data": [_row(7)], "hasMore": False},
    )
    monkeypatch.setattr(connector, "_fetch_detail", lambda job_id: (_ for _ in ()).throw(RuntimeError("boom")))

    jobs = connector.fetch()

    assert len(jobs) == 1
    assert jobs[0].external_job_id == "7"
    assert jobs[0].description_raw is None


def test_fetch_without_descriptions_does_not_call_detail(monkeypatch):
    connector = IIMJobsConnector(search="ai engineer", fetch_descriptions=False)
    monkeypatch.setattr(
        connector,
        "_fetch_search_page",
        lambda page, location_ids=TARGET_LOCATION_IDS: {"data": [_row(9)], "hasMore": False},
    )
    monkeypatch.setattr(connector, "_fetch_detail", lambda job_id: (_ for _ in ()).throw(AssertionError))

    jobs = connector.fetch()

    assert len(jobs) == 1
    assert jobs[0].description_raw is None
