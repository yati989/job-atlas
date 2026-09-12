from datetime import datetime, timezone

import pytest

from app.collectors.html.glassdoor import (
    GLASSDOOR_MAX_RESULTS,
    GLASSDOOR_PAGE_SIZE,
    GlassdoorConnector,
    _USER_AGENT,
)
from app.config.categories import SEARCH_TERMS
from app.config.settings import RECENCY_WINDOW_DAYS
from app.pipeline.registry import ACTIVE_CONNECTORS
from scripts.run_headed_sources import CONNECTORS as HEADED_CONNECTORS


def _listing(job_id: int, *, title: str = "Data Scientist") -> dict:
    return {
        "jobview": {
            "header": {
                "ageInDays": 2,
                "employerNameFromSearch": "Acme",
                "jobTitleText": title,
                "locationName": "Bengaluru",
                "payCurrency": "INR",
                "payPeriod": "ANNUAL",
                "payPeriodAdjustedPay": {"p10": 1_000_000, "p90": 2_000_000},
                "seoJobLink": f"https://www.glassdoor.co.in/job-listing/{job_id}",
            },
            "job": {
                "descriptionFragmentsText": "Build forecasting models",
                "jobTitleText": title,
                "listingId": job_id,
            },
            "overview": {"shortName": "Acme"},
        }
    }


def test_glassdoor_moved_to_headless_registry_with_twelve_instances():
    active = [connector for connector in ACTIVE_CONNECTORS if connector.source_name == "glassdoor"]
    headed = [connector for connector in HEADED_CONNECTORS if connector.source_name == "glassdoor"]

    assert len(active) == 12
    assert not headed
    assert sorted((connector.search, connector.location_mode) for connector in active) == sorted(
        (term, mode) for term in SEARCH_TERMS for mode in ("bengaluru", "remote_india")
    )


def test_glassdoor_uses_verified_api_depth_and_date_defaults():
    assert GLASSDOOR_PAGE_SIZE == 100
    assert GLASSDOOR_MAX_RESULTS == 500


def test_glassdoor_passes_global_window_without_assuming_ui_preset_is_a_ceiling():
    bengaluru = GlassdoorConnector(search="data scientist", location_mode="bengaluru")
    remote = GlassdoorConnector(search="data scientist", location_mode="remote_india")

    bengaluru_url, bengaluru_payload = bengaluru._search_context()
    remote_url, remote_payload = remote._search_context()

    assert "IC2940587" in bengaluru_url
    assert bengaluru_payload["locationId"] == 2940587
    assert bengaluru_payload["locationType"] == "CITY"
    assert bengaluru_payload["filterParams"] == [
        {"filterKey": "fromAge", "values": str(RECENCY_WINDOW_DAYS)}
    ]
    assert "IN115" in remote_url
    assert remote_payload["locationId"] == 115
    assert remote_payload["locationType"] == "COUNTRY"
    assert remote_payload["filterParams"] == [
        {"filterKey": "remoteWorkType", "values": "1"},
        {"filterKey": "fromAge", "values": str(RECENCY_WINDOW_DAYS)},
    ]


def test_glassdoor_uses_exact_native_filter_when_global_window_fits():
    connector = GlassdoorConnector(
        search="data scientist",
        location_mode="bengaluru",
        date_filter_days=30,
    )

    url, payload = connector._search_context()

    assert "fromAge=30" in url
    assert payload["filterParams"] == [
        {"filterKey": "fromAge", "values": "30"}
    ]


def test_glassdoor_city_resolves_a_native_city_id_only_when_fetching():
    connector = GlassdoorConnector(
        search="data scientist", location_mode="city", location=" Pune "
    )

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return [
                {
                    "locationId": 2856202,
                    "locationType": "C",
                    "label": "Pune (India)",
                    "countryName": "India",
                }
            ]

    class FakeSession:
        def __init__(self):
            self.get_calls = []

        def get(self, url, **kwargs):
            self.get_calls.append((url, kwargs))
            return FakeResponse()

    fake_session = FakeSession()
    connector._session = fake_session

    url, payload = connector._search_context()

    assert fake_session.get_calls == [
        (
            "https://www.glassdoor.co.in/findPopularLocationAjax.htm",
            {
                "params": {"maxLocationsToReturn": 10, "term": "Pune"},
                "headers": {"Accept": "application/json", "User-Agent": _USER_AGENT},
                "timeout": 45,
            },
        )
    ]
    assert "IC2856202" in url
    assert payload["locationId"] == 2856202
    assert payload["locationType"] == "CITY"
    assert payload["filterParams"] == [
        {"filterKey": "fromAge", "values": str(RECENCY_WINDOW_DAYS)}
    ]


def test_glassdoor_india_query_is_not_a_remote_query():
    connector = GlassdoorConnector(search="data scientist", location_mode="india")

    _url, payload = connector._search_context()

    assert payload["locationId"] == 115
    assert payload["locationType"] == "COUNTRY"
    assert payload["filterParams"] == [
        {"filterKey": "fromAge", "values": str(RECENCY_WINDOW_DAYS)}
    ]


def test_glassdoor_city_mode_requires_a_city_and_other_modes_reject_one():
    with pytest.raises(ValueError, match="location is required"):
        GlassdoorConnector(search="data scientist", location_mode="city")
    with pytest.raises(ValueError, match="only when location_mode is 'city'"):
        GlassdoorConnector(
            search="data scientist", location_mode="india", location="Pune"
        )


def test_glassdoor_retries_city_resolution_after_a_bff_session_primer():
    connector = GlassdoorConnector(
        search="data scientist", location_mode="city", location="Pune"
    )

    class ResolverResponse:
        def __init__(self, status_code, body):
            self.status_code = status_code
            self.body = body

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError("blocked")

        def json(self):
            return self.body

    class FakeSession:
        def __init__(self):
            self.get_calls = 0
            self.post_payloads = []

        def get(self, *_args, **_kwargs):
            self.get_calls += 1
            if self.get_calls == 1:
                return ResolverResponse(403, None)
            return ResolverResponse(
                200,
                [
                    {
                        "locationId": 2856202,
                        "locationType": "C",
                        "locationName": "Pune, India",
                    }
                ],
            )

        def post(self, _url, **kwargs):
            self.post_payloads.append(kwargs["json"])
            return ResolverResponse(200, {})

    fake_session = FakeSession()
    connector._session = fake_session

    _url, payload = connector._search_context()

    assert fake_session.get_calls == 2
    assert fake_session.post_payloads[0]["locationId"] == 115
    assert fake_session.post_payloads[0]["numJobsToShow"] == 1
    assert payload["locationId"] == 2856202


def test_glassdoor_city_rejects_an_unresolved_or_non_indian_match():
    connector = GlassdoorConnector(
        search="data scientist", location_mode="city", location="Pune"
    )

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return [
                {
                    "locationId": 123,
                    "locationType": "C",
                    "locationName": "Pune, Australia",
                }
            ]

    class FakeSession:
        def get(self, *_args, **_kwargs):
            return FakeResponse()

    connector._session = FakeSession()

    with pytest.raises(ValueError, match="could not resolve Indian city 'Pune'"):
        connector._search_context()


def test_glassdoor_follows_server_cursors_dedupes_and_stops_on_short_page():
    connector = GlassdoorConnector(
        search="data scientist",
        location_mode="bengaluru",
        max_results=1000,
    )
    calls = []

    def fake_post_json(payload, _referer):
        calls.append((payload["pageNumber"], payload["pageCursor"]))
        if payload["pageNumber"] == 1:
            jobs = [_listing(index) for index in range(1, 101)]
            cursors = [{"pageNumber": 2, "cursor": "next-2"}]
        else:
            jobs = [_listing(index) for index in range(100, 119)]
            cursors = []
        return {
            "data": {
                "jobListings": {
                    "jobListings": jobs,
                    "paginationCursors": cursors,
                    "totalJobsCount": 500,
                }
            }
        }

    connector._post_json = fake_post_json

    jobs = connector._list_jobs()

    assert calls == [(1, None), (2, "next-2")]
    assert len(jobs) == 118


def test_glassdoor_normalizes_bff_fields():
    connector = GlassdoorConnector(search="data scientist", location_mode="remote_india")
    connector._list_jobs = lambda: [_listing(101)]

    before = datetime.now(timezone.utc)
    job = connector.fetch()[0]
    after = datetime.now(timezone.utc)

    assert job.source == "glassdoor"
    assert job.external_job_id == "101"
    assert job.title == "Data Scientist"
    assert job.company_name_raw == "Acme"
    assert job.location_raw == "Bengaluru"
    assert job.is_remote is True
    assert job.description_raw == "Build forecasting models"
    assert job.salary_raw == "INR 1,000,000-2,000,000 ANNUAL"
    assert job.job_url == "https://www.glassdoor.co.in/job-listing/101"
    assert before.timestamp() - 2 * 86400 <= job.posted_at.timestamp() <= after.timestamp() - 2 * 86400
