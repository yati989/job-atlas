from datetime import datetime, timedelta, timezone

from app.collectors.html.timesjobs import (
    LOCATION_VALUES,
    TIMESJOBS_MAX_RESULTS,
    TIMESJOBS_PAGE_SIZE,
    TimesJobsConnector,
)
from app.pipeline.relevance import RelevancePolicy


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload
        self.status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeClient:
    def __init__(self, jobs):
        self.jobs = jobs
        self.post_requests = []

    def post(self, url, **kwargs):
        self.post_requests.append((url, kwargs))
        page = kwargs["json"]["page"]
        return FakeResponse({"jobs": self.jobs if page == "1" else []})


def _job(job_id="1", title="Data Scientist", location="Remote"):
    return {
        "jobId": job_id,
        "title": title,
        "company": "Acme",
        "location": location,
        "jobDetailUrl": f"https://www.timesjobs.com/job/{job_id}",
        "postDate": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
    }


def test_fetch_uses_one_native_location_pass_with_large_page_size():
    connector = TimesJobsConnector(
        search="data scientist",
        location_mode="remote",
        fetch_descriptions=False,
    )
    client = FakeClient([_job()])
    connector._client = client

    jobs = connector.fetch()

    assert len(jobs) == 1
    assert len(client.post_requests) == 2  # page 1, then empty page 2
    first_body = client.post_requests[0][1]["json"]
    assert first_body["keyword"] == "data scientist"
    assert first_body["location"] == "Remote"
    assert first_body["size"] == "1000"


def test_location_modes_are_exactly_bengaluru_and_remote():
    assert LOCATION_VALUES == {
        "bengaluru": "Bengaluru",
        "remote": "Remote",
    }
    assert TIMESJOBS_PAGE_SIZE == 1000
    assert TIMESJOBS_MAX_RESULTS == 500


def test_invalid_location_mode_is_rejected():
    try:
        TimesJobsConnector(search="data scientist", location_mode="invalid")
    except ValueError as exc:
        assert "Unsupported TimesJobs location mode" in str(exc)
    else:
        raise AssertionError("invalid location mode should fail")


def test_city_and_india_reach_native_request():
    for mode, city, expected in [("city", "Pune", "Pune"), ("india", None, "")]:
        connector = TimesJobsConnector("data analyst", mode, location=city, fetch_descriptions=False)
        client = FakeClient([_job(location="Pune")])
        connector._client = client
        connector.fetch()
        assert client.post_requests[0][1]["json"]["location"] == expected


def test_registry_has_six_terms_for_each_of_two_location_modes():
    from app.config.categories import SEARCH_TERMS
    from app.pipeline.registry import ACTIVE_CONNECTORS

    instances = [c for c in ACTIVE_CONNECTORS if c.source_name == "timesjobs"]

    assert len(instances) == 12
    assert sorted(c.location_mode for c in instances) == sorted(
        mode for mode in ("bengaluru", "remote") for _ in SEARCH_TERMS
    )
    assert sorted(c.search for c in instances) == sorted(
        term for term in SEARCH_TERMS for _ in ("bengaluru", "remote")
    )


def test_detail_hydration_skips_cards_the_central_gate_will_reject():
    connector = TimesJobsConnector(
        search="data scientist", location_mode="remote", detail_workers=2
    )
    connector._client = FakeClient([
        _job("1", "Data Scientist"),
        _job("2", "Sales Manager"),
    ])
    requested = []
    connector._fetch_detail = lambda _client, job_id: requested.append(job_id) or {}

    connector.fetch()

    assert requested == ["1"]


def test_profile_policy_hydrates_nondefault_pune_role_even_when_legacy_gate_drops_it():
    policy = RelevancePolicy(
        role_terms=("operations analyst",),
        title_exclusions=(),
        countries=("IN",),
        country_markers=("india",),
        cities=("Pune",),
        arrangements=("onsite",),
        seniority=("individual_contributor",),
    )
    connector = TimesJobsConnector(
        search="operations analyst",
        location_mode="city",
        location="Pune",
        relevance_policy=policy,
    )
    connector._client = FakeClient([_job("1", "Operations Analyst", "Pune")])
    requested = []
    connector._fetch_detail = (
        lambda _client, job_id: requested.append(job_id) or {"description": "Details"}
    )

    jobs = connector.fetch()

    assert requested == ["1"]
    assert jobs[0].description_raw == "Details"
