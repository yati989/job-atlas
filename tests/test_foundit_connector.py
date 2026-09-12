from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

from app.collectors.html.foundit import (
    FOUNDIT_MAX_RESULTS,
    FOUNDIT_PAGE_SIZE,
    FounditConnector,
)
from app.config.categories import SEARCH_TERMS
from app.config.settings import RECENCY_WINDOW_DAYS
from app.pipeline.relevance import RelevancePolicy
from app.pipeline.registry import ACTIVE_CONNECTORS
from app.workflows.planning import SearchProfile


def test_foundit_registry_uses_bengaluru_and_remote_suffixes_only():
    instances = [c for c in ACTIVE_CONNECTORS if c.source_name == "foundit"]

    assert len(instances) == 12
    assert sorted(c.search for c in instances) == sorted(
        f"{term} {suffix}"
        for term in SEARCH_TERMS
        for suffix in ("bangalore", "remote")
    )


def test_foundit_uses_large_raw_fetch_defaults():
    assert FOUNDIT_PAGE_SIZE == 1000
    assert FOUNDIT_MAX_RESULTS == 500


def test_foundit_pages_by_server_cursor_and_ignores_non_job_rows():
    connector = FounditConnector(
        search="data scientist",
        max_results=5000,
        page_size=1000,
        fetch_descriptions=False,
    )
    requested_starts = []

    def fake_get_json(url, _referer):
        start = int(parse_qs(urlparse(url).query)["start"][0])
        requested_starts.append(start)
        real_counts = {0: 100, 100: 19}
        next_starts = {0: "100", 100: "119"}
        real_jobs = [
            {"id": f"{start + index}", "title": "Data Scientist"}
            for index in range(real_counts[start])
        ]
        return {
            "jobSearchResponse": {
                "data": real_jobs + [{"type": "advertisement"}] * 22,
                "meta": {
                    "paging": {
                        "cursors": {"next": next_starts[start]},
                        "total": 119,
                        "limit": 100,
                    }
                },
            }
        }

    connector._get_json = fake_get_json

    jobs = connector._list_jobs("https://www.foundit.in/search")

    assert len(jobs) == 119
    assert requested_starts == [0, 100]


def test_foundit_requests_configured_native_freshness_filter():
    connector = FounditConnector(search="data scientist", fetch_descriptions=False)
    requested_urls = []

    def fake_get_json(url, _referer):
        requested_urls.append(url)
        return {
            "jobSearchResponse": {
                "data": [],
                "meta": {"paging": {"total": 0, "cursors": {}}},
            }
        }

    connector._get_json = fake_get_json
    connector._list_jobs("https://www.foundit.in/search")

    query = parse_qs(urlparse(requested_urls[0]).query)
    assert query["jobFreshness"] == [str(RECENCY_WINDOW_DAYS)]


def test_foundit_normalizes_listing_epoch_timestamp():
    connector = FounditConnector(search="data scientist", fetch_descriptions=False)
    connector._scrape = lambda: [
        {
            "job_id": "1",
            "jd_url": "/job/1",
            "title": "Data Scientist",
            "company": "Acme",
            "location": "Remote",
            "employment_type": "Full Time",
            "job_types": ["Work From Home"],
            "description": None,
            "salary_raw": None,
            "apply_url": None,
            "posted_epoch_ms": 1_786_471_842_000,
            "raw": {},
        }
    ]

    job = connector.fetch()[0]

    assert job.posted_at == datetime.fromtimestamp(
        1_786_471_842, tz=timezone.utc
    )


def test_foundit_detail_hydration_skips_off_role_cards():
    connector = FounditConnector(search="data scientist", detail_workers=2)
    connector._list_jobs = lambda _referer: [
        {"id": "1", "title": "Data Scientist", "companyName": "Acme"},
        {"id": "2", "title": "Sales Manager", "companyName": "Acme"},
    ]
    requested = []
    connector._fetch_detail = lambda job_id, _referer: requested.append(job_id) or None

    connector._scrape()

    assert requested == ["1"]


def test_foundit_hydrates_a_public_profile_role_for_its_requested_city():
    profile = SearchProfile.model_validate({
        "version": 1,
        "name": "pune-marketing",
        "professions": ["marketing specialist"],
        "search_terms": ["marketing specialist"],
        "relevance_terms": ["marketing specialist"],
        "countries": ["IN"],
        "cities": ["Pune"],
        "arrangements": ["onsite"],
        "seniority": ["individual_contributor"],
        "experience": {"maximum_years": 3},
        "collection_window_days": 30,
        "source_selection": {"mode": "named", "sources": ["foundit"]},
    })
    connector = FounditConnector(
        search="marketing specialist Pune",
        relevance_policy=RelevancePolicy.from_profile(profile),
        detail_workers=1,
    )
    connector._list_jobs = lambda _referer: [{
        "id": "1",
        "title": "Marketing Specialist",
        "companyName": "Acme",
        "locations": [{"city": "Pune, India"}],
    }]
    requested = []
    connector._fetch_detail = lambda job_id, _referer: requested.append(job_id) or None

    connector._scrape()

    assert requested == ["1"]


def test_foundit_hydrates_a_work_from_home_card_for_a_remote_profile():
    profile = SearchProfile.model_validate({
        "version": 1,
        "name": "remote-marketing",
        "professions": ["marketing specialist"],
        "search_terms": ["marketing specialist"],
        "relevance_terms": ["marketing specialist"],
        "countries": ["IN"],
        "cities": [],
        "arrangements": ["remote"],
        "seniority": ["individual_contributor"],
        "collection_window_days": 30,
        "source_selection": {"mode": "named", "sources": ["foundit"]},
    })
    connector = FounditConnector(
        search="marketing specialist remote",
        relevance_policy=RelevancePolicy.from_profile(profile),
        detail_workers=1,
    )
    connector._list_jobs = lambda _referer: [{
        "id": "1",
        "title": "Marketing Specialist",
        "companyName": "Acme",
        "locations": [{"city": "Pune"}],
        "jobTypes": ["Work From Home"],
    }]
    requested = []
    connector._fetch_detail = lambda job_id, _referer: requested.append(job_id) or None

    connector._scrape()

    assert requested == ["1"]


def test_foundit_hydrates_remote_profile_cards_without_location_or_work_mode():
    policy = RelevancePolicy(
        role_terms=("marketing specialist",),
        title_exclusions=(),
        countries=("IN",),
        country_markers=("india",),
        cities=(),
        arrangements=("remote",),
        seniority=("individual_contributor",),
    )
    connector = FounditConnector(
        search="marketing specialist remote", relevance_policy=policy
    )

    assert connector._listing_needs_detail({
        "id": "1", "title": "Marketing Specialist", "companyName": "Acme"
    })
