from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest

from app.collectors.html.builtin import BuiltInConnector, _parse_relative_age


def _card(
    job_id: str,
    title: str,
    location: str,
    mode: str = "Hybrid",
    salary: str | None = "2M-3M Annually",
) -> str:
    salary_row = (
        f'<div><div><i class="fa-sack-dollar"></i></div><span>{salary}</span></div>'
        if salary
        else ""
    )
    return f"""
    <div data-id="job-card">
      <a data-id="company-title">Acme</a>
      <a data-id="job-card-title" data-builtin-track-job-id="{job_id}"
         href="/job/{title.lower().replace(' ', '-')}/{job_id}">{title}</a>
      <span><i class="fa-clock"></i>2 Days Ago</span>
      <div><div><i class="fa-house-building"></i></div><span>{mode}</span></div>
      <div><div><i class="fa-location-dot"></i></div><span>{location}</span></div>
      {salary_row}
      <div><div><i class="fa-trophy"></i></div><span>Senior level</span></div>
      <div class="fs-sm fw-regular mb-md text-gray-04">Card description</div>
    </div>
    """


def _page(cards: str, last_page: int = 1) -> str:
    pagination = "".join(
        f'<a href="/jobs?search=data&page={page}">{page}</a>'
        for page in range(2, last_page + 1)
    )
    return f"<main>{cards}</main><nav>{pagination}</nav>"


def _detail() -> str:
    return """
    <script type="application/ld+json">
    {
      "@context": "https://schema.org",
      "@type": "JobPosting",
      "datePosted": "2026-08-12",
      "description": "<p>Full detail description</p>",
      "employmentType": "FULL_TIME",
      "jobLocationType": "TELECOMMUTE",
      "applicantLocationRequirements": [
        {"@type": "Country", "name": "USA"},
        {"@type": "Country", "name": "IND"}
      ],
      "jobLocation": [
        {"address": {"addressLocality": "New York", "addressCountry": "USA"}},
        {"address": {"addressLocality": "Bengaluru", "addressRegion": "Karnataka", "addressCountry": "IND"}}
      ],
      "hiringOrganization": {"name": "Detail Co"}
    }
    </script>
    """


def test_search_params_support_profile_city_and_country():
    city = BuiltInConnector(search="software engineer", location_mode="city", location="Pune")
    params = city._search_params(1)
    assert params["city"] == "Pune"
    assert params["country"] == "IND"
    assert "state" not in params
    india = BuiltInConnector(search="software engineer", location_mode="india")
    assert "city" not in india._search_params(1)
    assert india._search_url() == city._search_url()
    assert "/remote" not in india._search_url()


def test_search_params_use_verified_bengaluru_and_remote_surfaces():
    bengaluru = BuiltInConnector("data scientist", location_mode="bengaluru")
    remote = BuiltInConnector("data scientist", location_mode="remote_india")

    assert bengaluru._search_url() == "https://builtin.com/jobs"
    assert bengaluru._search_params(3) == {
        "search": "data scientist",
        "page": 3,
        "country": "IND",
        "daysSinceUpdated": bengaluru.days_since_updated,
        "city": "Bengaluru",
        "state": "Karnataka",
    }
    assert remote._search_url() == "https://builtin.com/jobs/remote"
    assert remote._search_params(1)["allLocations"] == "true"

    with pytest.raises(ValueError, match="location_mode"):
        BuiltInConnector("data scientist", location_mode="invalid")


def test_fetch_exhausts_advertised_pages_and_keeps_card_fallbacks(monkeypatch):
    connector = BuiltInConnector(
        "data scientist",
        location_mode="bengaluru",
        fetch_descriptions=False,
    )
    pages = {
        1: _page(_card("1", "Data Scientist", "Bengaluru, Karnataka, IND"), 2),
        2: _page(_card("2", "Senior Data Scientist", "Bangalore, Karnataka, IND"), 2),
    }
    calls = []

    def fetch_page(page: int) -> str:
        calls.append(page)
        return pages[page]

    monkeypatch.setattr(connector, "_fetch_page", fetch_page)
    jobs = connector.fetch()

    assert sorted(calls) == [1, 2]
    assert [job.external_job_id for job in jobs] == ["1", "2"]
    assert all(job.description_raw == "Card description" for job in jobs)
    assert all(job.salary_raw == "2M-3M Annually" for job in jobs)
    assert all(job.posted_at is not None for job in jobs)


def test_remote_detail_parses_multi_location_and_applicant_scope(monkeypatch):
    connector = BuiltInConnector("data scientist", location_mode="remote_india")
    monkeypatch.setattr(
        connector,
        "_fetch_page",
        lambda _page_number: _page(_card("42", "Data Scientist", "2 Locations", "Remote")),
    )
    monkeypatch.setattr(connector, "_fetch_detail_cached", lambda _url: _detail())

    job = connector.fetch()[0]

    assert job.is_remote is True
    assert job.location_raw == "Bengaluru, Karnataka, IND"
    assert job.remote_scope.startswith("IND")
    assert job.employment_type == "FULL_TIME"
    assert job.description_raw == "Full detail description"
    assert job.posted_at == datetime(2026, 8, 12, tzinfo=timezone.utc)


def test_detail_compensation_fills_missing_card_salary(monkeypatch):
    connector = BuiltInConnector("data scientist", location_mode="bengaluru")
    monkeypatch.setattr(
        connector,
        "_fetch_page",
        lambda _page_number: _page(
            _card("43", "Data Scientist", "Bengaluru, Karnataka, IND", salary=None)
        ),
    )
    detail = """
    <script type="application/ld+json">
    {
      "@context": "https://schema.org",
      "@type": "JobPosting",
      "description": "<p>Role overview</p><p>Salary Range: INR 30-40 LPA</p>"
    }
    </script>
    """
    monkeypatch.setattr(connector, "_fetch_detail_cached", lambda _url: detail)

    job = connector.fetch()[0]

    assert job.salary_raw == "Salary Range: INR 30-40 LPA"


def test_detail_cache_is_shared_across_instances(monkeypatch):
    url = "https://builtin.com/job/cache-test/99"
    first = BuiltInConnector("data scientist")
    second = BuiltInConnector("ai engineer", location_mode="remote_india")
    calls = []

    with BuiltInConnector._detail_lock:
        BuiltInConnector._detail_cache.pop(url, None)
        BuiltInConnector._detail_inflight.pop(url, None)

    def fake_fetch(_url: str) -> str:
        calls.append(_url)
        return _detail()

    monkeypatch.setattr(first, "_fetch_detail", fake_fetch)
    monkeypatch.setattr(second, "_fetch_detail", fake_fetch)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda connector: connector._fetch_detail_cached(url),
                (first, second),
            )
        )

    assert results == [_detail(), _detail()]
    assert calls == [url]

    with BuiltInConnector._detail_lock:
        BuiltInConnector._detail_cache.pop(url, None)


def test_relative_card_age_variants_are_preserved():
    assert _parse_relative_age("Reposted Yesterday") is not None
    assert _parse_relative_age("An Hour Ago") is not None
    assert _parse_relative_age("Reposted 15 Days Ago") is not None
    assert _parse_relative_age("No date") is None
