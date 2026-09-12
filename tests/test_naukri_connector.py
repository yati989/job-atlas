import base64
from datetime import datetime, timezone

import pytest

from app.collectors.html.naukri import (
    NAUKRI_MAX_RESULTS,
    NAUKRI_PAGE_SIZE,
    NaukriConnector,
    _make_nkparam,
)
from app.config.categories import SEARCH_TERMS
from app.pipeline.registry import ACTIVE_CONNECTORS


def test_naukri_registry_uses_six_terms_by_two_locations():
    instances = [connector for connector in ACTIVE_CONNECTORS if connector.source_name == "naukri"]

    assert len(instances) == 12
    assert sorted((connector.search, connector.location_mode) for connector in instances) == sorted(
        (term, mode) for term in SEARCH_TERMS for mode in ("bengaluru", "remote")
    )


def test_naukri_uses_verified_api_depth_defaults():
    assert NAUKRI_PAGE_SIZE == 100
    assert NAUKRI_MAX_RESULTS == 1000


def test_naukri_generates_frontend_srp_header():
    token = _make_nkparam(now_ms=1_786_546_539_000)

    assert len(token) == 88
    assert len(base64.b64decode(token)) == 64


def test_naukri_params_include_native_date_role_and_location_filters():
    connector = NaukriConnector(
        search="data scientist",
        location_mode="remote",
        date_filter_days=60,
    )

    assert connector._params(3) == {
        "noOfResults": 100,
        "urlType": "search_by_key_loc",
        "searchType": "adv",
        "location": "remote",
        "keyword": "data scientist",
        "pageNo": 3,
        "jobAge": 60,
        "seoKey": "data-scientist-jobs-in-remote",
        "src": "directSearch",
    }


def test_naukri_city_params_use_the_requested_city():
    connector = NaukriConnector(
        search="data scientist",
        location_mode="city",
        location=" Pune ",
    )

    assert connector._params(1)["location"] == "Pune"
    assert connector._params(1)["seoKey"] == "data-scientist-jobs-in-pune"


def test_naukri_india_params_are_india_wide_not_remote():
    connector = NaukriConnector(search="data scientist", location_mode="india")

    assert connector._params(1)["location"] == "india"
    assert connector._params(1)["seoKey"] == "data-scientist-jobs-in-india"


def test_naukri_city_mode_requires_a_city_and_other_modes_reject_one():
    with pytest.raises(ValueError, match="location is required"):
        NaukriConnector(search="data scientist", location_mode="city")
    with pytest.raises(ValueError, match="only when location_mode is 'city'"):
        NaukriConnector(
            search="data scientist", location_mode="india", location="Pune"
        )


def test_naukri_keeps_each_api_page_cookie_free():
    connector = NaukriConnector(search="data scientist", location_mode="bengaluru")

    class FakeCookies:
        cleared = 0

        def clear(self):
            self.cleared += 1

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"jobDetails": []}

    class FakeClient:
        def __init__(self):
            self.cookies = FakeCookies()
            self.calls = []

        def get(self, url, **kwargs):
            self.calls.append((url, kwargs))
            return FakeResponse()

    fake_client = FakeClient()
    connector._client = fake_client

    connector._get_json(2)

    assert fake_client.cookies.cleared == 1
    assert fake_client.calls[0][1]["params"]["pageNo"] == 2
    assert "Cookie" not in fake_client.calls[0][1]["headers"]


def test_naukri_paginates_by_api_pages_dedupes_and_stops_at_raw_cap():
    connector = NaukriConnector(
        search="data scientist",
        location_mode="bengaluru",
        max_results=150,
    )
    requested_pages = []

    def fake_get_json(page_num):
        requested_pages.append(page_num)
        start = 0 if page_num == 1 else 90
        return {
            "noOfJobs": 300,
            "jobDetails": [{"jobId": str(index)} for index in range(start, start + 100)],
        }

    connector._get_json = fake_get_json

    jobs = connector._list_jobs()

    assert requested_pages == [1, 2]
    assert len(jobs) == 150
    assert len({job["jobId"] for job in jobs}) == 150


def test_naukri_stops_on_actual_short_page():
    connector = NaukriConnector(
        search="data scientist",
        location_mode="bengaluru",
        max_results=500,
    )
    requested_pages = []

    def fake_get_json(page_num):
        requested_pages.append(page_num)
        count = 100 if page_num == 1 else 19
        start = (page_num - 1) * 100
        return {
            "noOfJobs": 119,
            "jobDetails": [{"jobId": str(start + index)} for index in range(count)],
        }

    connector._get_json = fake_get_json

    assert len(connector._list_jobs()) == 119
    assert requested_pages == [1, 2]


def test_naukri_normalizes_search_api_fields():
    connector = NaukriConnector(search="data scientist", location_mode="remote")
    connector._list_jobs = lambda: [
        {
            "jobId": "120826505033",
            "title": "Data Scientist",
            "companyName": "Acme",
            "jdURL": "/job-listings-data-scientist-120826505033",
            "applyRedirectUrl": "https://careers.example/jobs/1",
            "jobDescription": "Build models",
            "createdDate": 1_786_539_444_000,
            "placeholders": [
                {"type": "location", "label": "Remote"},
                {"type": "salary", "label": "Not disclosed"},
            ],
        }
    ]

    job = connector.fetch()[0]

    assert job.source == "naukri"
    assert job.external_job_id == "120826505033"
    assert job.company_name_raw == "Acme"
    assert job.location_raw == "Remote"
    assert job.is_remote is True
    assert job.salary_raw is None
    assert job.apply_url == "https://careers.example/jobs/1"
    assert job.job_url == "https://www.naukri.com/job-listings-data-scientist-120826505033"
    assert job.posted_at == datetime.fromtimestamp(1_786_539_444, tz=timezone.utc)
