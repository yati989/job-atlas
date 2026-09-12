import asyncio
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import pytest

from app.collectors.browser.zip_recruiter import (
    ZIPRECRUITER_MAX_PAGES,
    ZIPRECRUITER_MAX_RESULTS,
    ZIPRECRUITER_PAGE_SIZE,
    ZipRecruiterConnector,
    _job_id,
    _parse_posted_at,
)
from app.config.categories import SEARCH_TERMS
from app.pipeline.registry import ACTIVE_CONNECTORS


def _row(index: int) -> dict:
    return {
        "job_id": str(index),
        "href": f"/jobs/{index}-data-scientist-at-acme",
        "title": "Data Scientist",
        "company": "Acme",
        "location": "Bengaluru, Karnataka, India",
        "description": "Build forecasting models",
        "date_text": "10 Aug",
    }


def test_ziprecruiter_uses_twelve_india_specific_instances():
    instances = [
        connector for connector in ACTIVE_CONNECTORS if connector.source_name == "ziprecruiter"
    ]

    assert len(instances) == 12
    assert sorted((connector.search, connector.location_mode) for connector in instances) == sorted(
        (term, mode) for term in SEARCH_TERMS for mode in ("bengaluru", "remote_india")
    )


def test_ziprecruiter_uses_five_verified_thousand_row_pages():
    assert ZIPRECRUITER_PAGE_SIZE == 1000
    assert ZIPRECRUITER_MAX_RESULTS == 5000
    assert ZIPRECRUITER_MAX_PAGES == 5


def test_ziprecruiter_builds_india_bengaluru_and_remote_urls():
    bengaluru = ZipRecruiterConnector(search="data scientist", location_mode="bengaluru")
    remote = ZipRecruiterConnector(search="data scientist", location_mode="remote_india")

    bengaluru_url = bengaluru._build_url(3)
    remote_url = remote._build_url(4)
    bengaluru_query = parse_qs(urlparse(bengaluru_url).query)
    remote_query = parse_qs(urlparse(remote_url).query)

    assert urlparse(bengaluru_url).netloc == "www.ziprecruiter.in"
    assert bengaluru_query == {
        "q": ["data scientist"],
        "l": ["bengaluru"],
        "page": ["3"],
        "per_page": ["1000"],
    }
    assert remote_query == {
        "q": ["data scientist"],
        "l": ["India"],
        "page": ["4"],
        "per_page": ["1000"],
        "remote": ["full"],
    }


def test_ziprecruiter_builds_requested_city_and_india_wide_urls():
    city = ZipRecruiterConnector(
        search="data scientist", location_mode="city", location=" Pune "
    )
    india = ZipRecruiterConnector(search="data scientist", location_mode="india")

    city_query = parse_qs(urlparse(city._build_url(1)).query)
    india_query = parse_qs(urlparse(india._build_url(1)).query)

    assert city_query["l"] == ["Pune"]
    assert india_query["l"] == ["India"]
    assert "remote" not in city_query
    assert "remote" not in india_query


def test_ziprecruiter_city_mode_requires_a_city_and_other_modes_reject_one():
    with pytest.raises(ValueError, match="location is required"):
        ZipRecruiterConnector(search="data scientist", location_mode="city")
    with pytest.raises(ValueError, match="only when location_mode is 'city'"):
        ZipRecruiterConnector(
            search="data scientist", location_mode="india", location="Pune"
        )


def test_ziprecruiter_extracts_numeric_id_from_india_job_url():
    assert _job_id("/jobs/561041538-sr-ai-ml-engineer-at-zeta-global") == "561041538"
    assert _job_id("/not-a-job") is None


def test_ziprecruiter_parses_day_month_with_new_year_rollover():
    now = datetime(2026, 1, 5, tzinfo=timezone.utc)

    assert _parse_posted_at("02 Jan", now) == datetime(2026, 1, 2, tzinfo=timezone.utc)
    assert _parse_posted_at("30 Dec", now) == datetime(2025, 12, 30, tzinfo=timezone.utc)


def test_ziprecruiter_fetches_five_pages_concurrently_and_merges_in_page_order():
    connector = ZipRecruiterConnector(
        search="data scientist",
        location_mode="bengaluru",
        max_results=1000,
    )
    requested_pages = []
    active = 0
    max_active = 0

    async def fake_fetch_page(_browser, page_number):
        nonlocal active, max_active
        requested_pages.append(page_number)
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return [_row(page_number), _row(99)]

    connector._fetch_page = fake_fetch_page
    pages = asyncio.run(connector._fetch_pages(object()))
    jobs = connector._merge_pages(pages)

    assert sorted(requested_pages) == [1, 2, 3, 4, 5]
    assert max_active == 5
    assert [job["job_id"] for job in jobs] == ["1", "99", "2", "3", "4", "5"]


def test_ziprecruiter_normalizes_india_html_fields():
    connector = ZipRecruiterConnector(search="data scientist", location_mode="bengaluru")
    connector._scrape = lambda: [_row(561041538)]

    job = connector.fetch()[0]

    assert job.external_job_id == "561041538"
    assert job.company_name_raw == "Acme"
    assert job.location_raw == "Bengaluru, Karnataka, India"
    assert job.is_remote is False
    assert job.description_raw == "Build forecasting models"
    assert job.salary_raw is None
    assert job.job_url == "https://www.ziprecruiter.in/jobs/561041538-data-scientist-at-acme"
    assert job.posted_at is not None
