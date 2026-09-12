import asyncio
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import httpx
from app.collectors.html.linkedin import (
    LINKEDIN_INITIAL_RESULT_COUNT,
    LINKEDIN_MAX_RESULTS,
    LINKEDIN_POSITION_CEILING,
    LinkedInConnector,
    _parse_detail,
    _parse_listings,
)
from app.config.categories import SEARCH_TERMS
from app.pipeline.registry import ACTIVE_CONNECTORS
from app.pipeline.runner import is_browser_connector
from app.pipeline.window import apply_sync_window


LISTING_HTML = """
<div class="base-search-card" data-entity-urn="urn:li:jobPosting:4432563093">
  <a class="base-card__full-link" href="https://in.linkedin.com/jobs/view/4432563093"></a>
  <h3 class="base-search-card__title"> Data Scientist </h3>
  <h4 class="base-search-card__subtitle"> Acme India </h4>
  <span class="job-search-card__location"> Bengaluru, Karnataka, India </span>
  <time datetime="2026-08-11">2 days ago</time>
</div>
"""


def _listing_html(start: int, count: int) -> str:
    return "".join(
        f"""
        <div class="base-search-card" data-entity-urn="urn:li:jobPosting:{4400000000 + index}">
          <a class="base-card__full-link" href="https://in.linkedin.com/jobs/view/{4400000000 + index}"></a>
          <h3 class="base-search-card__title">ML Engineer {index}</h3>
          <h4 class="base-search-card__subtitle">Acme India</h4>
          <span class="job-search-card__location">Bengaluru, Karnataka, India</span>
          <time datetime="2026-08-11">2 days ago</time>
        </div>
        """
        for index in range(start, start + count)
    )

DETAIL_HTML = """
<div class="show-more-less-html__markup"><p>Build machine learning models.</p></div>
<li class="description__job-criteria-item">
  <h3 class="description__job-criteria-subheader">Seniority level</h3>
  <span class="description__job-criteria-text">Mid-Senior level</span>
</li>
<li class="description__job-criteria-item">
  <h3 class="description__job-criteria-subheader">Employment type</h3>
  <span class="description__job-criteria-text">Full-time</span>
</li>
"""


class SlowDetailTransport(httpx.BaseTransport, httpx.AsyncBaseTransport):
    """Same delayed response for the connector's sync and async clients."""

    def __init__(self, delay_seconds: float):
        self.delay_seconds = delay_seconds

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        time.sleep(self.delay_seconds)
        return httpx.Response(200, text=DETAIL_HTML)

    async def handle_async_request(
        self, request: httpx.Request
    ) -> httpx.Response:
        await asyncio.sleep(self.delay_seconds)
        return httpx.Response(200, text=DETAIL_HTML)


def test_linkedin_has_twelve_logged_out_india_instances():
    instances = [item for item in ACTIVE_CONNECTORS if item.source_name == "linkedin"]
    assert len(instances) == 12
    assert all(type(item) is LinkedInConnector for item in instances)
    assert all(not is_browser_connector(item) for item in instances)
    assert sorted((item.search, item.location_mode) for item in instances) == sorted(
        (term, mode) for term in SEARCH_TERMS for mode in ("remote_india", "bengaluru")
    )


def test_linkedin_uses_initial_sixty_cards_and_two_hundred_result_cap():
    assert LINKEDIN_INITIAL_RESULT_COUNT == 60
    assert LINKEDIN_MAX_RESULTS == 200
    assert LINKEDIN_POSITION_CEILING == 200
    assert LinkedInConnector("x", "bengaluru", max_results=5000).max_results == 200


def test_linkedin_supports_shared_connector_cleanup():
    connector = LinkedInConnector("veterinary", "remote_india")

    connector.close_client()


def test_linkedin_supports_city_and_india_wide_searches():
    pune = LinkedInConnector("data scientist", "city", location="Pune")
    india = LinkedInConnector("data scientist", "india")

    assert parse_qs(urlparse(pune._build_search_url()).query) == {
        "keywords": ["data scientist"],
        "location": ["Pune"],
        "f_TPR": [f"r{pune.date_filter_days * 86400}"],
    }
    assert parse_qs(urlparse(india._build_search_url()).query) == {
        "keywords": ["data scientist"],
        "location": ["India"],
        "f_TPR": [f"r{india.date_filter_days * 86400}"],
    }


def test_linkedin_city_search_requires_a_location():
    try:
        LinkedInConnector("data scientist", "city")
    except ValueError as exc:
        assert str(exc) == "location is required when location_mode is 'city'"
    else:
        raise AssertionError("city mode must require a location")


def test_linkedin_continues_at_ten_job_offsets_without_geo_id():
    requested_starts = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/jobs/search":
            return httpx.Response(200, text=_listing_html(0, 60))
        if "seeMoreJobPostings" in request.url.path:
            start = int(request.url.params["start"])
            requested_starts.append(start)
            assert "geoId" not in request.url.params
            return httpx.Response(200, text=_listing_html(start, 10))
        return httpx.Response(404)

    connector = LinkedInConnector(
        "ml engineer",
        "bengaluru",
        max_results=80,
        fetch_descriptions=False,
        listing_workers=2,
        listing_interval_seconds=0,
        transport=httpx.MockTransport(handler),
    )

    jobs = connector.fetch()

    assert len(jobs) == 80
    assert sorted(requested_starts) == [60, 70]
    assert len({job.external_job_id for job in jobs}) == 80


def test_linkedin_never_requests_rejected_start_200():
    requested_starts = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/jobs/search":
            return httpx.Response(200, text=_listing_html(0, 60))
        start = int(request.url.params["start"])
        requested_starts.append(start)
        assert start < LINKEDIN_MAX_RESULTS
        return httpx.Response(200, text=_listing_html(start, 10))

    connector = LinkedInConnector(
        "ml engineer",
        "bengaluru",
        max_results=1000,
        fetch_descriptions=False,
        listing_workers=4,
        listing_interval_seconds=0,
        transport=httpx.MockTransport(handler),
    )

    jobs = connector.fetch()

    assert len(jobs) == 200
    assert min(requested_starts) == 60
    assert max(requested_starts) == 190
    assert 200 not in requested_starts


def test_linkedin_parses_listing_and_detail_fields():
    row = _parse_listings(LISTING_HTML, 60)[0]
    detail = _parse_detail(DETAIL_HTML)

    assert row == {
        "job_id": "4432563093",
        "title": "Data Scientist",
        "href": "https://in.linkedin.com/jobs/view/4432563093",
        "company": "Acme India",
        "location": "Bengaluru, Karnataka, India",
        "posted_at_text": "2026-08-11",
    }
    assert detail["description_raw"] == "Build machine learning models."
    assert detail["seniority"] == "Mid-Senior level"
    assert detail["employment_type"] == "Full-time"


def test_linkedin_fetch_defers_detail_until_post_gate_hydration():
    detail_requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/jobs/search":
            return httpx.Response(200, text=LISTING_HTML)
        if request.url.path.endswith("/4432563093"):
            detail_requests.append(request.url.path)
            return httpx.Response(200, text=DETAIL_HTML)
        return httpx.Response(404)

    connector = LinkedInConnector(
        "data scientist",
        "bengaluru",
        max_results=1,
        listing_interval_seconds=0,
        detail_interval_seconds=0,
        final_detail_retry_delay_seconds=0,
        transport=httpx.MockTransport(handler),
    )
    connector._detail_cache.clear()
    job = connector.fetch()[0]

    assert job.external_job_id == "4432563093"
    assert job.company_name_raw == "Acme India"
    assert job.description_raw is None
    assert detail_requests == []

    hydration = connector.hydrate_relevant_jobs([job])

    assert hydration == {"eligible": 1, "hydrated": 1, "failed": 0}
    assert detail_requests == [
        "/jobs-guest/jobs/api/jobPosting/4432563093"
    ]
    assert job.description_raw == "Build machine learning models."
    assert job.posted_at.isoformat() == "2026-08-11T00:00:00+00:00"
    assert job.employment_type == "Full-time"
    assert job.seniority == "Mid-Senior level"


def test_linkedin_detail_prefilter_does_not_reject_received_job_by_date():
    run_start = datetime(2026, 8, 22, 16, 0, tzinfo=timezone.utc)
    exact_cutoff = run_start - timedelta(hours=24)
    connector = LinkedInConnector("data scientist", "remote_india")
    apply_sync_window(
        [connector], since_at=exact_cutoff, cutoff_at=run_start
    )
    row = {
        "job_id": "too-old-for-exact-window",
        "href": "https://in.linkedin.com/jobs/view/too-old-for-exact-window",
        "title": "Data Scientist",
        "company": "Acme India",
        "location": "Remote - India",
        "posted_at_text": (exact_cutoff - timedelta(hours=1)).isoformat(),
    }

    assert connector.date_filter_days == 1
    assert connector._should_fetch_detail(row) is True


def test_linkedin_listing_and_detail_requests_share_one_start_coordinator():
    calls = []

    class TrackingLinkedInConnector(LinkedInConnector):
        @classmethod
        def _wait_for_slot(
            cls, rate_lock, last_started_name, cooldown_name, interval_seconds
        ):
            calls.append(
                (rate_lock, last_started_name, cooldown_name, interval_seconds)
            )

    def handler(request: httpx.Request) -> httpx.Response:
        if "seeMoreJobPostings" in request.url.path:
            return httpx.Response(200, text="")
        if "/jobPosting/" in request.url.path:
            return httpx.Response(200, text=DETAIL_HTML)
        return httpx.Response(404)

    connector = TrackingLinkedInConnector(
        "data scientist",
        "remote_india",
        transport=httpx.MockTransport(handler),
    )
    with connector._listing_client() as client:
        connector._fetch_continuation(client, 60)
        connector._request_detail(client, "4432563093")

    assert len(calls) == 2
    assert calls[0] == calls[1]
    assert calls[0][3] == 1.2


def test_linkedin_detail_response_has_absolute_wall_clock_deadline():
    connector = LinkedInConnector(
        "data scientist",
        "bengaluru",
        detail_interval_seconds=0,
        final_detail_retry_delay_seconds=0,
        detail_response_deadline_seconds=0.03,
        detail_batch_no_progress_seconds=0.5,
        transport=SlowDetailTransport(0.5),
    )
    connector._detail_cache.clear()
    job = connector._normalize({
        "job_id": "absolute-deadline",
        "href": "https://in.linkedin.com/jobs/view/absolute-deadline",
        "title": "Data Scientist",
        "company": "Acme India",
        "location": "Bengaluru, Karnataka, India",
    })

    started = time.monotonic()
    hydration = connector.hydrate_relevant_jobs([job])
    elapsed = time.monotonic() - started

    assert elapsed < 0.25
    assert hydration == {"eligible": 1, "hydrated": 0, "failed": 1}


def test_linkedin_detail_batch_stops_after_no_progress_deadline():
    connector = LinkedInConnector(
        "data scientist",
        "bengaluru",
        detail_interval_seconds=0,
        final_detail_retry_delay_seconds=0,
        detail_response_deadline_seconds=1.0,
        detail_batch_no_progress_seconds=0.03,
        transport=SlowDetailTransport(0.5),
    )
    connector._detail_cache.clear()
    jobs = [
        connector._normalize({
            "job_id": f"no-progress-{index}",
            "href": f"https://in.linkedin.com/jobs/view/no-progress-{index}",
            "title": "Data Scientist",
            "company": "Acme India",
            "location": "Bengaluru, Karnataka, India",
        })
        for index in range(2)
    ]

    started = time.monotonic()
    hydration = connector.hydrate_relevant_jobs(jobs)
    elapsed = time.monotonic() - started

    assert elapsed < 0.25
    assert hydration == {"eligible": 2, "hydrated": 0, "failed": 2}
