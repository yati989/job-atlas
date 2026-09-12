import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

import app.collectors.html.talentcom as talentcom
from app.collectors.html.talentcom import TalentComConnector, _SessionMaterial
from app.config.settings import RECENCY_WINDOW_DAYS
from app.pipeline.registry import ACTIVE_CONNECTORS
from app.pipeline.runner import is_browser_connector


FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def reset_talentcom_caches(monkeypatch):
    monkeypatch.setattr(talentcom, "_SESSION_MATERIAL", None)
    talentcom._DETAIL_CACHE.clear()
    talentcom._DETAIL_IN_FLIGHT.clear()


class FakeResponse:
    def __init__(self, text="", status_code=200):
        self.text = text
        self.status_code = status_code
        self.request = httpx.Request("GET", "https://in.talent.com")

    def raise_for_status(self):
        if self.status_code >= 400:
            response = httpx.Response(self.status_code, request=self.request)
            raise httpx.HTTPStatusError("failed", request=self.request, response=response)


class FakeClient:
    def __init__(self, pages):
        self.pages = pages
        self.urls = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        if "/jobs?" in url:
            page = int(parse_qs(urlparse(url).query)["p"][0])
            return FakeResponse(self.pages.get(page, "<html></html>"))
        return FakeResponse(self.pages.get("detail", "<html></html>"))


def _fixture(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_registered_connector_is_in_parallel_family():
    assert not is_browser_connector(TalentComConnector(search="data scientist"))


def test_registry_uses_native_remote_and_bengaluru_modes():
    modes = {
        connector.location_mode
        for connector in ACTIVE_CONNECTORS
        if isinstance(connector, TalentComConnector)
    }

    assert modes == {"remote", "bengaluru"}


def test_search_url_encodes_native_remote_india_filter():
    remote = TalentComConnector(search="machine learning", location_mode="remote")
    city = TalentComConnector(search="data scientist", location_mode="bengaluru")

    remote_params = parse_qs(urlparse(remote._search_url(2)).query)
    city_params = parse_qs(urlparse(city._search_url(1)).query)

    assert remote_params == {
        "k": ["machine learning"],
        "date": [str(RECENCY_WINDOW_DAYS)],
        "l": ["india"],
        "p": ["2"],
        "workplace": ["remote"],
        "showSignInModal": ["true"],
    }
    assert city_params["l"] == ["Bengaluru"]
    assert "workplace" not in city_params


def test_search_url_encodes_requested_city_and_india_wide_modes():
    city = TalentComConnector(
        search="data scientist", location_mode="city", location=" Pune "
    )
    india = TalentComConnector(search="data scientist", location_mode="india")

    city_params = parse_qs(urlparse(city._search_url(1)).query)
    india_params = parse_qs(urlparse(india._search_url(1)).query)

    assert city_params["l"] == ["Pune"]
    assert india_params["l"] == ["India"]
    assert "workplace" not in city_params
    assert "workplace" not in india_params


def test_city_location_mode_requires_a_city_and_other_modes_reject_one():
    with pytest.raises(ValueError, match="location is required"):
        TalentComConnector(search="data scientist", location_mode="city")
    with pytest.raises(ValueError, match="only when location_mode is 'city'"):
        TalentComConnector(
            search="data scientist", location_mode="india", location="Pune"
        )


def test_location_keyword_keeps_the_existing_positional_constructor_contract():
    connector = TalentComConnector("data scientist", 1, "remote", 30, False, 2)

    assert connector.date_filter_days == 30
    assert connector.fetch_descriptions is False
    assert connector.detail_workers == 2


def test_native_remote_filter_marks_city_labelled_results_remote():
    connector = TalentComConnector(
        search="data scientist", location_mode="remote", max_results=1,
        fetch_descriptions=False,
    )
    connector._client = FakeClient({1: _fixture("talentcom_listing.html")})

    job = connector.fetch()[0]

    assert job.location_raw == "Bengaluru, India"
    assert job.is_remote is True


def test_listing_parser_maps_required_card_fields():
    items = TalentComConnector._parse_listing_page(_fixture("talentcom_listing.html"))

    assert items == [
        {"job_id": "111", "title": "Data Scientist", "company": "Example Analytics", "location": "Bengaluru, India"},
        {"job_id": "222", "title": "Machine Learning Engineer", "company": "Second Company", "location": "Remote, India"},
    ]


def test_pagination_dedup_and_fetch_descriptions_false():
    listing = _fixture("talentcom_listing.html")
    duplicate_page = listing.replace('data-new-id="222"', 'data-new-id="111"')
    connector = TalentComConnector(search="data scientist", max_results=10, fetch_descriptions=False)
    client = FakeClient({1: listing, 2: duplicate_page, 3: "<html></html>"})
    connector._client = client

    jobs = connector.fetch()

    assert [job.external_job_id for job in jobs] == ["111", "222"]
    assert len(client.urls) == 2
    assert all("/view?" not in url for url in client.urls)


def test_jobposting_jsonld_maps_description_date_type_location_and_apply_url():
    detail = TalentComConnector._parse_detail(_fixture("talentcom_detail.html"))

    assert "Build machine-learning models" in detail["description_raw"]
    assert detail["posted_at"].isoformat() == "2026-08-10T00:00:00+00:00"
    assert detail["employment_type"] == "FULL_TIME"
    assert detail["company"] == "Example Analytics"
    assert detail["location"] == "Bengaluru, Karnataka, in"
    assert detail["apply_url"] == "https://in.talent.com/redirect?id=111"
    assert "description" not in detail["jobposting"]


def test_html_fallback_extracts_description_and_relative_date():
    html = """
    <html><body><div><span>Job description</span>
    <div><p>Build credit risk strategies.</p><p>Use Python and SQL.</p></div></div>
    <span>2 days ago</span><a href="/redirect?id=333">Apply</a></body></html>
    """
    detail = TalentComConnector._parse_detail(html)

    assert "Build credit risk strategies" in detail["description_raw"]
    assert detail["posted_at"] is not None
    assert detail["apply_url"] == "https://in.talent.com/redirect?id=333"


def test_full_mapping_preserves_external_id_and_uses_detail_fields():
    connector = TalentComConnector(search="data scientist", max_results=1)
    connector._client = FakeClient({1: _fixture("talentcom_listing.html"), "detail": _fixture("talentcom_detail.html")})

    jobs = connector.fetch()

    assert len(jobs) == 1
    job = jobs[0]
    assert job.external_job_id == "111"
    assert job.description_raw
    assert job.posted_at is not None
    assert job.employment_type == "FULL_TIME"
    assert job.apply_url.endswith("/redirect?id=111")


def test_session_bootstrap_is_shared_once_across_threads(monkeypatch):
    calls = 0
    calls_lock = threading.Lock()

    def bootstrap():
        nonlocal calls
        with calls_lock:
            calls += 1
        time.sleep(0.02)
        return _SessionMaterial(user_agent="test", cookies={"session": "value"})

    monkeypatch.setattr(talentcom, "_SESSION_MATERIAL", None)
    monkeypatch.setattr(talentcom, "_bootstrap_session", bootstrap)
    with ThreadPoolExecutor(max_workers=12) as pool:
        sessions = list(pool.map(lambda _: talentcom._get_session_material(), range(12)))

    assert calls == 1
    assert len({id(session) for session in sessions}) == 1


def test_detail_workers_are_bounded(monkeypatch):
    active = 0
    maximum = 0
    lock = threading.Lock()

    def detail(self, job_id):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.02)
        with lock:
            active -= 1
        return {}

    connector = TalentComConnector(search="data scientist", detail_workers=4)
    listings = [{"job_id": str(i), "title": "Data Scientist", "company": "C", "location": "India"} for i in range(12)]
    monkeypatch.setattr(TalentComConnector, "_fetch_detail", detail)

    assert len(connector._enrich_details(listings)) == 12
    assert 1 < maximum <= 4


def test_detail_cache_deduplicates_concurrent_cross_instance_requests(monkeypatch):
    calls = 0
    lock = threading.Lock()

    def detail(self, job_id):
        nonlocal calls
        with lock:
            calls += 1
        time.sleep(0.02)
        return {"description_raw": "same"}

    first = TalentComConnector(search="data scientist")
    second = TalentComConnector(search="data analyst")
    monkeypatch.setattr(TalentComConnector, "_fetch_detail", detail)
    with ThreadPoolExecutor(max_workers=2) as pool:
        values = list(pool.map(lambda connector: connector._fetch_detail_cached("111"), [first, second]))

    assert calls == 1
    assert values[0] == values[1]


def test_missing_required_listing_fields_fail_loudly():
    malformed = '<article class="JobCard_card__abc"><h2 class="JobCard_title__abc">Data Scientist</h2></article>'
    with pytest.raises(RuntimeError, match="schema changed"):
        TalentComConnector._parse_listing_page(malformed)


@pytest.mark.parametrize("workers", [0, -1])
def test_detail_workers_must_be_positive(workers):
    with pytest.raises(ValueError, match="detail_workers"):
        TalentComConnector(search="data scientist", detail_workers=workers)
