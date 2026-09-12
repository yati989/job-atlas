from datetime import datetime, timedelta, timezone

import httpx

from app.collectors.html.efinancialcareers import (
    BENGALURU,
    EFinancialCareersConnector,
    PAGE_SIZE,
    _parse_posted,
)


PUNE_RESOLVER_HTML = """
<a href="https://job-search-ui.efinancialcareers.com/v3/efc/jobs/search?
locationPrecision=City&amp;latitude=18.5246&amp;longitude=73.87862&amp;
location=Pune%2C%20Maharashtra%2C%20India&amp;countryCode2=IN&amp;
radius=50&amp;radiusUnit=mi">Pune</a>
"""


def _row(job_id: int, title: str = "Data Scientist", **overrides):
    row = {
        "id": f"opaque-{job_id}",
        "jobId": str(job_id),
        "detailsPageUrl": f"/jobs-India-Bangalore-Data_Scientist.id{job_id}",
        "title": title,
        "jobLocation": {
            "displayName": "Bengaluru, India",
            "city": "Bangalore",
            "country": "India",
        },
        "postedDate": datetime.now(timezone.utc).isoformat(),
        "salary": "Competitive",
        "companyName": "Example Bank",
        "employmentType": "Full time",
        "workArrangementType": "Hybrid",
        "description": "Build production models",
    }
    row.update(overrides)
    return row


def test_params_use_verified_bengaluru_and_server_date_filter():
    connector = EFinancialCareersConnector(["data scientist"], freshness_days=60)
    params = connector._params("data scientist")
    assert {key: params[key] for key in BENGALURU} == BENGALURU
    assert params["pageSize"] == PAGE_SIZE
    assert params["includeRemote"] == "false"
    cutoff = datetime.fromisoformat(str(params["startDate"])).date()
    assert cutoff in {
        (datetime.now(timezone.utc) - timedelta(days=60)).date(),
        (datetime.now(timezone.utc) - timedelta(days=61)).date(),
    }


def test_location_modes_resolve_city_at_fetch_time_and_use_india_native_filters():
    requested_urls = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        return httpx.Response(200, text=PUNE_RESOLVER_HTML)

    city = EFinancialCareersConnector(
        ["data scientist"], location_mode="city", location="Pune"
    )
    city._client = httpx.Client(transport=httpx.MockTransport(handler))
    city_params = city._params("data scientist")
    india = EFinancialCareersConnector(["data scientist"], location_mode="india")
    remote = EFinancialCareersConnector(["data scientist"], location_mode="remote")

    assert requested_urls == [
        "https://www.efinancialcareers.com/jobs/data-scientist/in-pune"
    ]
    assert {key: city_params[key] for key in BENGALURU} == {
        "locationPrecision": "City",
        "latitude": 18.5246,
        "longitude": 73.87862,
        "location": "Pune, Maharashtra, India",
        "countryCode2": "IN",
        "radius": 50,
        "radiusUnit": "mi",
    }
    assert india._params("data scientist")["locationPrecision"] == "Country"
    assert remote._params("data scientist")["filters.workArrangementType"] == "REMOTE"


def test_city_mode_requires_location_without_network_access():
    try:
        EFinancialCareersConnector(["data scientist"], location_mode="city")
    except ValueError as exc:
        assert str(exc) == "location is required when location_mode is 'city'"
    else:
        raise AssertionError("city mode must require a location")


def test_city_resolver_rejects_a_different_indian_city():
    connector = EFinancialCareersConnector(
        ["data scientist"], location_mode="city", location="Pune"
    )
    wrong_city = PUNE_RESOLVER_HTML.replace(
        "Pune%2C%20Maharashtra%2C%20India", "Mumbai%2C%20Maharashtra%2C%20India"
    )
    connector._client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, text=wrong_city)
        )
    )

    try:
        connector._params("data scientist")
    except RuntimeError as exc:
        assert str(exc) == (
            "eFinancialCareers resolved city 'Mumbai' does not match requested city 'Pune'"
        )
    else:
        raise AssertionError("different Indian city must not be accepted")


def test_city_scope_resolves_once_before_parallel_term_fetches():
    resolver_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal resolver_calls
        if request.url.host == "www.efinancialcareers.com":
            resolver_calls += 1
            return httpx.Response(200, text=PUNE_RESOLVER_HTML)
        return httpx.Response(200, json={"data": []})

    connector = EFinancialCareersConnector(
        ["data scientist", "data analyst"],
        location_mode="city",
        location="Pune",
        search_workers=2,
    )
    connector._client = httpx.Client(transport=httpx.MockTransport(handler))

    assert connector.fetch() == []
    assert resolver_calls == 1


def test_terms_are_fetched_in_parallel_order_and_deduplicated(monkeypatch):
    connector = EFinancialCareersConnector(
        ["data scientist", "data analyst"], search_workers=2
    )

    def fake_search(term):
        shared = _row(1, title="Data Scientist" if term == "data scientist" else "Data Analyst")
        unique = _row(2 if term == "data scientist" else 3, title=shared["title"])
        return {"data": [shared, unique]}

    monkeypatch.setattr(connector, "_fetch_search", fake_search)
    jobs = connector.fetch()
    assert [job.external_job_id for job in jobs] == ["1", "2", "3"]
    assert jobs[0].raw_payload["matched_search_term"] == "data scientist"


def test_relevance_drift_stops_noisy_native_results(monkeypatch):
    connector = EFinancialCareersConnector(["data scientist"])
    rows = [_row(i) for i in range(1, 11)] + [
        _row(i, title="Office Administrator") for i in range(11, 21)
    ]
    monkeypatch.setattr(connector, "_fetch_search", lambda term: {"data": rows})
    jobs = connector.fetch()
    assert len(jobs) == 16


def test_full_api_row_mapping_preserves_supported_fields(monkeypatch):
    connector = EFinancialCareersConnector(["data scientist"])
    row = _row(7, workArrangementType="Remote", description="Full description")
    monkeypatch.setattr(connector, "_fetch_search", lambda term: {"data": [row]})
    [job] = connector.fetch()
    assert job.external_job_id == "7"
    assert job.location_raw == "Bengaluru, India"
    assert job.is_remote is True
    assert job.remote_scope == "Bengaluru, India"
    assert job.description_raw == "Full description"
    assert job.salary_raw == "Competitive"
    assert job.employment_type == "Full time"
    assert job.job_url.endswith(".id7")


def test_stale_rows_are_filtered_but_unknown_dates_are_kept(monkeypatch):
    connector = EFinancialCareersConnector(["data scientist"], freshness_days=60)
    stale = (datetime.now(timezone.utc) - timedelta(days=61)).isoformat()
    rows = [_row(1, postedDate=stale), _row(2, postedDate="bad")]
    monkeypatch.setattr(connector, "_fetch_search", lambda term: {"data": rows})
    jobs = connector.fetch()
    assert [job.external_job_id for job in jobs] == ["2"]
    assert jobs[0].posted_at is None


def test_parse_posted_normalizes_naive_and_aware_values():
    assert _parse_posted("2026-08-14T10:00:00").tzinfo == timezone.utc
    assert _parse_posted("2026-08-14T10:00:00Z").tzinfo == timezone.utc
    assert _parse_posted("bad") is None
