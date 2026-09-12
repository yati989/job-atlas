import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
import time

import httpx
import pytest

import app.collectors.html.instahyre as instahyre
from app.collectors.html.instahyre import InstahyreConnector
from app.pipeline.runner import is_browser_connector


FIXTURES = Path(__file__).parent / "fixtures"


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.request = httpx.Request("GET", "https://www.instahyre.com")

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            response = httpx.Response(self.status_code, request=self.request)
            raise httpx.HTTPStatusError("failed", request=self.request, response=response)


class FakeClient:
    def __init__(self, listing, detail):
        self.listing = listing
        self.detail = detail
        self.requests = []

    def get(self, url, **kwargs):
        self.requests.append((url, kwargs))
        if "/employer_public_jobs/" in url:
            return FakeResponse(self.detail)
        return FakeResponse(self.listing)


def _fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_fetch_normalizes_listing_and_public_detail_api_without_fabricating_missing_fields():
    connector = InstahyreConnector(max_results=1)
    connector._client = FakeClient(
        _fixture("instahyre_listing.json"),
        _fixture("instahyre_detail.json"),
    )

    jobs = connector.fetch()

    assert len(jobs) == 1
    job = jobs[0]
    assert job.external_job_id == "432883"
    assert job.title == "Lead Backend Engineer (Java)"
    assert job.company_name_raw == "Deutsche Telekom Digital Labs"
    assert job.location_raw == "Gurgaon"
    assert job.description_raw == "Build scalable backend applications.\nUse Java and Spring."
    assert job.posted_at is None
    assert job.employment_type is None
    assert job.raw_payload["experience_years_scraped"] == "8-10 Years"


def test_fetch_bounds_parallel_public_detail_requests():
    active = 0
    maximum = 0
    lock = threading.Lock()
    listing = {
        "meta": {"next": None},
        "objects": [
            {
                "id": job_id,
                "title": f"Data Job {job_id}",
                "public_url": f"https://www.instahyre.com/job-{job_id}/",
                "locations": "Bangalore",
                "employer": {"company_name": "Example"},
            }
            for job_id in range(12)
        ],
    }

    class TrackingClient:
        def get(self, url, **kwargs):
            nonlocal active, maximum
            if "/employer_public_jobs/" not in url:
                return FakeResponse(listing)
            with lock:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.02)
            with lock:
                active -= 1
            return FakeResponse({"description": "<p>Detail</p>"})

    connector = InstahyreConnector(max_results=12, detail_workers=4)
    connector._client = TrackingClient()

    jobs = connector.fetch()

    assert len(jobs) == 12
    assert 1 < maximum <= 4


def test_fetch_retries_transient_public_detail_failures(monkeypatch):
    listing = _fixture("instahyre_listing.json")
    responses = [
        FakeResponse({}, status_code=429),
        FakeResponse({}, status_code=503),
        FakeResponse(_fixture("instahyre_detail.json")),
    ]

    class SequenceClient:
        def __init__(self):
            self.detail_requests = 0

        def get(self, url, **kwargs):
            if "/employer_public_jobs/" not in url:
                return FakeResponse(listing)
            self.detail_requests += 1
            return responses.pop(0)

    client = SequenceClient()
    connector = InstahyreConnector(max_results=1)
    connector._client = client
    monkeypatch.setattr(instahyre.time, "sleep", lambda _: None)

    jobs = connector.fetch()

    assert client.detail_requests == 3
    assert jobs[0].description_raw == "Build scalable backend applications.\nUse Java and Spring."


def test_fetch_retries_a_transient_listing_connection_failure(monkeypatch):
    listing = _fixture("instahyre_listing.json")

    class SequenceClient:
        def __init__(self):
            self.requests = 0

        def get(self, url, **kwargs):
            self.requests += 1
            if self.requests == 1:
                raise httpx.ConnectTimeout(
                    "TLS handshake timed out",
                    request=httpx.Request("GET", url),
                )
            return FakeResponse(listing)

    client = SequenceClient()
    connector = InstahyreConnector(max_results=1, fetch_descriptions=False)
    connector._client = client
    monkeypatch.setattr(instahyre.time, "sleep", lambda _: None)

    jobs = connector.fetch()

    assert client.requests == 2
    assert [job.external_job_id for job in jobs] == ["432883"]


def test_registry_searches_every_target_term_across_all_active_location_modes():
    from app.config.categories import SEARCH_TERMS
    from app.pipeline.registry import ACTIVE_CONNECTORS

    instances = [connector for connector in ACTIVE_CONNECTORS if connector.source_name == "instahyre"]

    assert [(connector.search, connector.location_mode) for connector in instances] == [
        (term, mode)
        for term in SEARCH_TERMS
        for mode in ("bangalore", "remote")
    ]
    assert all(connector.max_results == 100 for connector in instances)
    assert all(not is_browser_connector(connector) for connector in instances)


def test_fetch_applies_the_remote_location_mode_to_the_listing_api():
    client = FakeClient(_fixture("instahyre_listing.json"), {})
    connector = InstahyreConnector(
        search="data scientist",
        max_results=1,
        location_mode="remote",
        fetch_descriptions=False,
    )
    connector._client = client

    connector.fetch()

    listing_url, kwargs = client.requests[0]
    assert listing_url.endswith("/api/v1/job_search")
    assert kwargs["params"]["skills"] == "data scientist"
    assert kwargs["params"]["jobLocations"] == "Work From Home"
    assert len(client.requests) == 1


def test_fetch_applies_a_requested_city_to_the_listing_api():
    client = FakeClient(_fixture("instahyre_listing.json"), {})
    connector = InstahyreConnector(
        search="data scientist",
        max_results=1,
        location_mode="city",
        location="Pune",
        fetch_descriptions=False,
    )
    connector._client = client

    connector.fetch()

    _, kwargs = client.requests[0]
    assert kwargs["params"]["jobLocations"] == "Pune"


def test_fetch_leaves_the_native_location_filter_unset_for_india_wide_searches():
    client = FakeClient(_fixture("instahyre_listing.json"), {})
    connector = InstahyreConnector(
        search="data scientist",
        max_results=1,
        location_mode="india",
        fetch_descriptions=False,
    )
    connector._client = client

    connector.fetch()

    _, kwargs = client.requests[0]
    assert "jobLocations" not in kwargs["params"]


def test_city_location_mode_requires_a_city():
    with pytest.raises(ValueError, match="requires a location"):
        InstahyreConnector(location_mode="city")


def test_location_keyword_keeps_the_existing_positional_constructor_contract():
    connector = InstahyreConnector("data scientist", 1, "remote", 30, False, 2)

    assert connector.date_filter_days == 30
    assert connector.fetch_descriptions is False
    assert connector.detail_workers == 2


def test_fetch_keeps_collected_listings_at_the_known_429_depth_wall():
    listing = _fixture("instahyre_listing.json")
    listing["meta"]["next"] = "/api/v1/job_search?offset=1"

    class DepthWallClient:
        def __init__(self):
            self.requests = 0

        def get(self, url, **kwargs):
            self.requests += 1
            if self.requests == 1:
                return FakeResponse(listing)
            return FakeResponse({}, status_code=429)

    client = DepthWallClient()
    connector = InstahyreConnector(max_results=2, fetch_descriptions=False)
    connector._client = client

    jobs = connector.fetch()

    assert [job.external_job_id for job in jobs] == ["432883"]
    assert client.requests == 2


def test_fetch_keeps_listing_when_public_detail_is_unavailable(monkeypatch):
    client = FakeClient(_fixture("instahyre_listing.json"), {})
    connector = InstahyreConnector(max_results=1)
    connector._client = client
    monkeypatch.setattr(instahyre.time, "sleep", lambda _: None)

    original_get = client.get

    def unavailable_detail(url, **kwargs):
        if "/employer_public_jobs/" in url:
            return FakeResponse({}, status_code=503)
        return original_get(url, **kwargs)

    client.get = unavailable_detail

    jobs = connector.fetch()

    assert [job.external_job_id for job in jobs] == ["432883"]
    assert jobs[0].description_raw is None


def test_parallel_instances_serialize_listing_requests_at_the_known_rate_wall():
    active = 0
    maximum = 0
    lock = threading.Lock()
    listing = _fixture("instahyre_listing.json")

    class TrackingClient:
        def get(self, url, **kwargs):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.02)
            with lock:
                active -= 1
            return FakeResponse(listing)

    connectors = [
        InstahyreConnector(max_results=1, location_mode=mode, fetch_descriptions=False)
        for mode in (None, "bangalore", "remote")
    ]
    for connector in connectors:
        connector._client = TrackingClient()

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda connector: connector.fetch(), connectors))

    assert [len(jobs) for jobs in results] == [1, 1, 1]
    assert maximum == 1


def test_parallel_instances_share_a_source_wide_detail_request_ceiling():
    active = 0
    maximum = 0
    lock = threading.Lock()
    listing = {
        "meta": {"next": None},
        "objects": [
            {
                "id": job_id,
                "title": f"Data Scientist {job_id}",
                "public_url": f"https://www.instahyre.com/job-{job_id}/",
                "locations": "Bangalore",
                "employer": {"company_name": "Example"},
            }
            for job_id in range(4)
        ],
    }

    class TrackingClient:
        def get(self, url, **kwargs):
            nonlocal active, maximum
            if "/employer_public_jobs/" not in url:
                return FakeResponse(listing)
            with lock:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.1)
            with lock:
                active -= 1
            return FakeResponse({"description": "<p>Detail</p>"})

    connectors = [InstahyreConnector(max_results=4, detail_workers=4) for _ in range(8)]
    for connector in connectors:
        connector._client = TrackingClient()

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda connector: connector.fetch(), connectors))

    assert all(len(jobs) == 4 for jobs in results)
    assert 1 < maximum <= 24
